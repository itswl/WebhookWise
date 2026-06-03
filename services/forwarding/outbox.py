"""Transactional forwarding outbox.

The webhook pipeline writes forwarding intents before any HTTP side effect.
Workers consume those intents asynchronously, giving the system an auditable,
recoverable at-least-once delivery path.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from sqlalchemy import select
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
from services.forwarding import outbox_queries
from services.forwarding import rules as forwarding_rules
from services.forwarding.outbox_delivery import _is_forward_success, deliver_outbox_record
from services.forwarding.outbox_state import (
    _claim_outbox,
    _finalize_outbox_failure,
    _finalize_outbox_success,
)
from services.forwarding.policies import ForwardDeliveryPolicy
from services.operations import taskiq_retry_scheduler
from services.operations import tasks as operation_tasks
from services.webhooks.decisioning import ForwardDecision, ForwardRuleSnapshot, select_forward_rules
from services.webhooks.types import (
    AnalysisResult,
    ForwardOutboxStatus,
    ForwardResult,
)

logger = get_logger("forward_outbox")

_mask_url_for_display = outbox_queries._mask_url_for_display
_DELIVERY_RUNTIME_ERRORS = (OSError, RuntimeError, ValueError)
_SCHEDULING_ERRORS = (OSError, RuntimeError, TimeoutError)


async def _create_outbox_records(
    session: AsyncSession,
    matched_rules: list[ForwardRuleSnapshot],
    *,
    webhook_id: int | None,
    orig_id: int | None,
    forward_data: dict[str, Any] | None,
    analysis_result: AnalysisResult | None,
    formatted_payload: dict[str, Any] | None,
    event_type: str,
    is_periodic_reminder: bool,
    idempotency_extra: str = "",
    policy: ForwardDeliveryPolicy,
    log_tag: str,
) -> list[int]:
    """Create outbox records for matched rules within an existing session."""
    now = utcnow()
    outbox_ids: list[int] = []
    for rule in matched_rules:
        target_type = str(rule.target_type or "webhook")
        target_url = str(rule.target_url or "")
        if target_type != "openclaw" and not target_url:
            logger.warning("[%s] 规则 '%s' target_url 为空，跳过", log_tag, rule.name or rule.id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_empty_target").inc()
            continue

        rule_id = rule.id
        key = _idempotency_key(
            webhook_id=webhook_id or 0,
            rule_id=rule_id,
            target_type=target_type,
            target_url=target_url,
            is_periodic_reminder=is_periodic_reminder,
            extra=idempotency_extra,
        )
        existing = (
            await session.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("[%s] 幂等命中 key=%s id=%s", log_tag, key, existing)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
            outbox_ids.append(int(existing))
            continue

        record = ForwardOutbox(
            idempotency_key=key,
            webhook_event_id=webhook_id,
            original_event_id=orig_id,
            forward_rule_id=rule_id,
            rule_name=str(rule.name or rule.id or "default"),
            target_type=target_type,
            target_url=target_url,
            target_name=str(rule.target_name or ""),
            is_periodic_reminder=is_periodic_reminder,
            channel_name=target_type,
            event_type=event_type,
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            forward_data=forward_data,
            analysis_result=analysis_result,
            formatted_payload=formatted_payload,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        await session.flush()
        outbox_ids.append(int(record.id))
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "created").inc()
        logger.info(
            "[%s] 已创建转发意图 id=%s event_id=%s event_type=%s rule=%s target=%s",
            log_tag,
            record.id,
            webhook_id,
            event_type,
            rule.name,
            target_type,
        )

    return outbox_ids


def _outbox_result(outbox_ids: list[int]) -> ForwardResult:
    if not outbox_ids:
        return {"status": "skipped", "reason": "所有匹配规则均已存在或无效", "outbox_ids": []}
    return {"status": "queued", "outbox_ids": outbox_ids, "outbox_id": outbox_ids[0]}


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

    if target_url:
        matched = [
            ForwardRuleSnapshot(
                id=None,
                name="manual_forward",
                match_event_type="",
                match_importance="",
                match_source="",
                match_duplicate="",
                match_payload="",
                target_type="webhook",
                target_url=target_url,
                stop_on_match=True,
                target_name="",
            )
        ]
    else:
        rules = await forwarding_rules.list_enabled_forward_rules()
        matched = select_forward_rules(
            rules,
            event_type=event_type,
            importance=importance,
            source=source,
            is_duplicate=is_duplicate,
            parsed_data=parsed_data,
        )
    if not matched:
        reason = "未匹配转发规则" if not target_url else "目标 URL 为空"
        logger.info("[ForwardNotify] 无匹配规则 event_type=%s source=%s", event_type, source)
        return {"status": "skipped", "reason": reason, "outbox_ids": []}

    async with session_scope() as sess:
        outbox_ids = await _create_outbox_records(
            sess,
            matched,
            webhook_id=webhook_id,
            orig_id=None,
            forward_data=forward_data,
            analysis_result=analysis_result,
            formatted_payload=formatted_payload,
            event_type=event_type,
            is_periodic_reminder=False,
            idempotency_extra=idempotency_extra,
            policy=policy,
            log_tag="ForwardNotify",
        )

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


def _idempotency_key(
    *,
    webhook_id: int,
    rule_id: int | None,
    target_type: str,
    target_url: str,
    is_periodic_reminder: bool,
    extra: str = "",
) -> str:
    raw = f"{webhook_id}|{rule_id or 'default'}|{target_type}|{target_url}|{int(is_periodic_reminder)}|{extra}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"forward:{webhook_id}:{digest[:32]}"


async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    """Dispatch immediately; the scheduled scanner picks up missed records."""
    if not outbox_ids:
        return

    for outbox_id in outbox_ids:
        try:
            await operation_tasks.process_forward_outbox_task.kiq(outbox_id=outbox_id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scheduled").inc()
        except _SCHEDULING_ERRORS as e:  # noqa: PERF203
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "schedule_failed").inc()
            logger.warning("[ForwardOutbox] 即时调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    try:
        await taskiq_retry_scheduler.schedule_forward_outbox(outbox_id, delay_seconds)
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_scheduled").inc()
    except _SCHEDULING_ERRORS as e:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_schedule_failed").inc()
        logger.warning("[ForwardOutbox] 延迟调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


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
