"""Transactional forwarding outbox.

The webhook pipeline writes forwarding intents before any HTTP side effect.
Workers consume those intents asynchronously, giving the system an auditable,
recoverable at-least-once delivery path.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.attributes import FORWARD_STATUS, FORWARD_TARGET_TYPE, WEBHOOK_EVENT_ID
from core.observability.metrics import (
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS,
    FORWARD_OUTBOX_RECORDS_TOTAL,
)
from core.observability.tracing import otel_span, set_span_error
from db.session import session_scope
from models import ForwardOutbox
from services.forwarding import outbox_queries, outbox_records, outbox_scheduling
from services.forwarding.outbox_delivery import _is_forward_success, deliver_outbox_record
from services.forwarding.outbox_notifications import create_forward_notification_outbox_records
from services.forwarding.outbox_state import (
    _claim_outbox,
    _finalize_outbox_failure,
    _finalize_outbox_success,
)
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.decisioning import ForwardDecision
from services.webhooks.types import (
    AnalysisResult,
    ForwardOutboxStatus,
    ForwardResult,
)

logger = get_logger("forward_outbox")

_mask_url_for_display = outbox_queries._mask_url_for_display
_create_outbox_records = outbox_records.create_outbox_records
_idempotency_key = outbox_records.idempotency_key
_outbox_result = outbox_records.outbox_result
_DELIVERY_RUNTIME_ERRORS = (OSError, RuntimeError, ValueError)


async def resolve_and_forward(
    *,
    session: AsyncSession,
    decision: ForwardDecision,
    forward_data: dict[str, Any] | None = None,
    analysis_result: AnalysisResult | None = None,
    webhook_id: int | None = None,
    orig_id: int | None = None,
    policy: ForwardDeliveryPolicy | None = None,
) -> ForwardResult:
    """Pipeline 路径：在已有事务中创建 outbox 记录，调用方负责提交和调度。"""
    matched = list(decision.matched_rules)
    if not matched:
        return {"status": "skipped", "reason": "未匹配转发规则", "outbox_ids": []}

    outbox_ids = await _create_outbox_records(
        session,
        matched,
        webhook_id=webhook_id,
        orig_id=orig_id,
        forward_data=forward_data,
        analysis_result=analysis_result,
        formatted_payload=None,
        event_type="webhook_forward",
        is_periodic_reminder=decision.is_periodic_reminder,
        policy=policy or ForwardDeliveryPolicy.from_config(),
        log_tag="ResolveForward",
    )
    return _outbox_result(outbox_ids)


async def forward_notification(
    *,
    event_type: str,
    source: str = "",
    formatted_payload: dict[str, Any] | None = None,
    forward_data: dict[str, Any] | None = None,
    analysis_result: AnalysisResult | None = None,
    webhook_id: int | None = None,
    wait: bool = False,
    policy: ForwardDeliveryPolicy | None = None,
    target_url: str = "",
    idempotency_extra: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> ForwardResult:
    """独立路径：匹配规则 → 创建 outbox → 调度投递（或同步送达如 wait=True）。

    当 target_url 非空时跳过规则匹配，直接投递到该 URL。
    """
    policy = policy or ForwardDeliveryPolicy.from_config()

    outbox_ids, skip_reason = await create_forward_notification_outbox_records(
        event_type=event_type,
        source=source,
        formatted_payload=formatted_payload,
        forward_data=forward_data,
        analysis_result=analysis_result,
        webhook_id=webhook_id,
        policy=policy,
        target_url=target_url,
        idempotency_extra=idempotency_extra,
        importance=importance,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
    )
    if skip_reason:
        return {"status": "skipped", "reason": skip_reason, "outbox_ids": []}

    if not outbox_ids:
        return _outbox_result(outbox_ids)

    if wait:
        results: list[ForwardResult] = []
        for oid in outbox_ids:
            result = await _deliver_one(oid, policy=policy)
            results.append(result)
        return results[0] if results else {"status": "skipped"}

    await schedule_forward_outbox_many(outbox_ids)
    return _outbox_result(outbox_ids)


async def _deliver_one(outbox_id: int, *, policy: ForwardDeliveryPolicy) -> ForwardResult:
    """同步送达一条 outbox 记录并更新状态。"""
    record = await _claim_outbox(outbox_id, policy=policy)
    if record is None:
        return {"status": "not_claimed", "outbox_id": outbox_id}
    try:
        result = await deliver_outbox_record(record)
    except _DELIVERY_RUNTIME_ERRORS as e:
        await _finalize_outbox_failure(outbox_id, str(e), policy=policy)
        return {"status": "failed", "message": str(e), "outbox_id": outbox_id}

    if _is_forward_success(result):
        await _finalize_outbox_success(record, result)
    else:
        await _finalize_outbox_failure(
            outbox_id, f"status={result.get('status')}: {result.get('message', '')}", policy=policy
        )
    return {**result, "outbox_id": outbox_id}

async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    await outbox_scheduling.schedule_forward_outbox_many(outbox_ids)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    await outbox_scheduling.schedule_forward_outbox_retry(outbox_id, delay_seconds)


async def process_forward_outbox_by_id(outbox_id: int) -> None:
    started = time.perf_counter()
    target_type = "unknown"
    status = "not_claimed"
    record = await _claim_outbox(outbox_id)
    if not record:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, status).inc()
        FORWARD_OUTBOX_PROCESS_DURATION_SECONDS.labels(target_type, status).observe(time.perf_counter() - started)
        return
    target_type = str(record.target_type or "unknown")

    with otel_span(
        "forward.outbox.process",
        {
            WEBHOOK_EVENT_ID: record.webhook_event_id,
            "forward.outbox.id": record.id,
            FORWARD_TARGET_TYPE: target_type,
            FORWARD_STATUS: str(record.status or "unknown"),
        },
    ) as outbox_span:
        try:
            result = await deliver_outbox_record(record)
        except _DELIVERY_RUNTIME_ERRORS as e:
            status = "failed"
            set_span_error(outbox_span, e)
            await _finalize_outbox_failure(record.id, str(e))
        else:
            if _is_forward_success(result):
                status = "sent"
                await _finalize_outbox_success(record, result)
            else:
                status = "failed"
                await _finalize_outbox_failure(
                    record.id, f"forward status={result.get('status')}: {result.get('message', '')}"
                )
            if outbox_span is not None:
                outbox_span.set_attribute("forward.status", status)
    FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, status).inc()
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS.labels(target_type, status).observe(time.perf_counter() - started)


async def requeue_forward_outbox(outbox_id: int) -> bool:
    now = utcnow()
    updated = False
    async with session_scope() as session:
        record = await session.get(ForwardOutbox, outbox_id)
        if record is None:
            return False
        status_value = (
            record.status.value if isinstance(record.status, ForwardOutboxStatus) else str(record.status or "")
        )
        if status_value not in {
            ForwardOutboxStatus.EXHAUSTED.value,
            ForwardOutboxStatus.EXPIRED.value,
            ForwardOutboxStatus.RETRYING.value,
            ForwardOutboxStatus.PENDING.value,
        }:
            return False
        record.status = ForwardOutboxStatus.RETRYING
        record.next_attempt_at = now
        record.updated_at = now
        record.attempts = 0
        record.last_error = "manual_retry"
        updated = True
    if updated:
        await schedule_forward_outbox_many([outbox_id])
    return updated

async def list_outbox_records(
    *,
    page: int = 1,
    page_size: int = 20,
    cursor: int | None = None,
    status: str = "",
    event_type: str = "",
) -> dict[str, Any]:
    return await outbox_queries.list_outbox_records(
        page=page,
        page_size=page_size,
        cursor=cursor,
        status=status,
        event_type=event_type,
        session_scope_factory=session_scope,
    )
