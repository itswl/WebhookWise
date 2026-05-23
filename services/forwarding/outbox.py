"""Transactional forwarding outbox.

The webhook pipeline writes forwarding intents before any HTTP side effect.
Workers consume those intents asynchronously, giving the system an auditable,
recoverable at-least-once delivery path.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from core.observability.metrics import (
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS,
    FORWARD_OUTBOX_RECORDS_TOTAL,
)
from core.observability.tracing import set_span_error
from core.observability.tracing import span as otel_span
from db.session import session_scope
from models import ForwardOutbox, WebhookEvent
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.decisioning import ForwardDecision, ForwardRuleTarget
from services.webhooks.types import (
    AnalysisResult,
    DeepAnalysisStatus,
    ForwardOutboxStatus,
    ForwardResult,
    WebhookData,
)

logger = get_logger("forward_outbox")

ForwardOutboxEnqueuer = Callable[[int], Awaitable[None]]
ForwardOutboxRetryScheduler = Callable[[int, int], Awaitable[None]]

_forward_outbox_enqueuer: ForwardOutboxEnqueuer | None = None
_forward_outbox_retry_scheduler: ForwardOutboxRetryScheduler | None = None


def configure_forward_outbox_schedulers(
    *,
    enqueue_outbox: ForwardOutboxEnqueuer | None = None,
    schedule_retry: ForwardOutboxRetryScheduler | None = None,
) -> None:
    """Register operations-layer schedulers without importing task definitions here."""
    global _forward_outbox_enqueuer, _forward_outbox_retry_scheduler
    _forward_outbox_enqueuer = enqueue_outbox
    _forward_outbox_retry_scheduler = schedule_retry


async def resolve_and_forward(
    *,
    event_type: str = "webhook_forward",
    source: str = "",
    importance: str = "low",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
    formatted_payload: dict[str, Any] | None = None,
    forward_data: dict[str, Any] | None = None,
    analysis_result: AnalysisResult | None = None,
    webhook_id: int | None = None,
    orig_id: int | None = None,
    wait: bool = False,
    session: AsyncSession | None = None,
    decision: ForwardDecision | None = None,
    is_periodic_reminder: bool = False,
    policy: ForwardDeliveryPolicy | None = None,
) -> ForwardResult:
    """统一转发入口 — 所有外发消息都通过规则匹配决定目标。

    支持两种调用模式：
    - Pipeline 路径：传入 session + decision（同事务创建 outbox，调用方负责提交和调度）
    - 独立路径：不传 session，内部完成匹配+创建+调度全流程
    """
    policy = policy or ForwardDeliveryPolicy.from_config()
    now = datetime.now()

    matched: list[ForwardRuleTarget]
    periodic: bool
    if decision is not None:
        matched = list(decision.matched_rules)
        periodic = decision.is_periodic_reminder
    else:
        from services.webhooks.decisioning import select_forward_rules
        from services.webhooks.repository import list_enabled_forward_rules

        rules = await list_enabled_forward_rules(session=session)
        matched = select_forward_rules(
            rules,
            event_type=event_type,
            importance=importance,
            source=source,
            is_duplicate=is_duplicate,
            parsed_data=parsed_data,
        )
        periodic = is_periodic_reminder

    if not matched:
        logger.info(
            "[ResolveForward] 无匹配规则 event_type=%s source=%s importance=%s",
            event_type,
            source,
            importance,
        )
        return {"status": "skipped", "reason": "未匹配转发规则", "outbox_ids": []}

    outbox_ids: list[int] = []
    async with _resolve_session(session) as sess:
        for rule in matched:
            target_type = str(rule.get("target_type", "webhook") or "webhook")
            target_url = str(rule.get("target_url", "") or "")
            if target_type != "openclaw" and not target_url:
                logger.warning("[ResolveForward] 规则 '%s' target_url 为空，跳过", rule.get("name", rule.get("id")))
                FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_empty_target").inc()
                continue

            rule_id = _rule_id(rule)
            key = _idempotency_key(
                webhook_id=webhook_id or 0,
                rule_id=rule_id,
                target_type=target_type,
                target_url=target_url,
                is_periodic_reminder=periodic,
            )
            existing = (
                await sess.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
            ).scalar_one_or_none()
            if existing is not None:
                logger.info("[ResolveForward] 幂等命中 key=%s id=%s", key, existing)
                FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
                outbox_ids.append(int(existing))
                continue

            record = ForwardOutbox(
                idempotency_key=key,
                webhook_event_id=webhook_id,
                original_event_id=orig_id,
                forward_rule_id=rule_id,
                rule_name=str(rule.get("name") or rule.get("id") or "default"),
                target_type=target_type,
                target_url=target_url,
                target_name=str(rule.get("target_name", "") or ""),
                is_periodic_reminder=periodic,
                channel_name=target_type,
                event_type=event_type,
                status=ForwardOutboxStatus.PENDING,
                attempts=0,
                max_attempts=policy.max_attempts,
                next_attempt_at=now,
                forward_data=forward_data,
                analysis_result=analysis_result,
                formatted_payload=formatted_payload if decision is None else None,
                created_at=now,
                updated_at=now,
            )
            sess.add(record)
            await sess.flush()
            outbox_ids.append(int(record.id))
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "created").inc()
            logger.info(
                "[ResolveForward] 已创建转发意图 id=%s event_id=%s event_type=%s rule=%s target=%s",
                record.id,
                webhook_id,
                event_type,
                rule.get("name"),
                target_type,
            )

    if not outbox_ids:
        return {"status": "skipped", "reason": "所有匹配规则均已存在或无效", "outbox_ids": []}

    # Pipeline 路径：由调用方提交事务后自行调度
    if session is not None:
        return {"status": "queued", "outbox_ids": outbox_ids, "outbox_id": outbox_ids[0]}

    if wait:
        results: list[ForwardResult] = []
        for oid in outbox_ids:
            result = await _deliver_one(oid, policy=policy)
            results.append(result)
        return results[0] if results else {"status": "skipped"}
    else:
        await schedule_forward_outbox_many(outbox_ids)
        return {"status": "queued", "outbox_ids": outbox_ids, "outbox_id": outbox_ids[0]}


@contextlib.asynccontextmanager
async def _resolve_session(existing: AsyncSession | None) -> Any:
    if existing is not None:
        yield existing
    else:
        async with session_scope() as sess:
            yield sess


async def _deliver_one(outbox_id: int, *, policy: ForwardDeliveryPolicy) -> ForwardResult:
    """同步送达一条 outbox 记录并更新状态。"""
    record = await _claim_outbox(outbox_id, policy=policy)
    if record is None:
        return {"status": "not_claimed", "outbox_id": outbox_id}
    try:
        result = await deliver_outbox_record(record)
    except Exception as e:
        await _finalize_outbox_failure(outbox_id, str(e), policy=policy)
        return {"status": "failed", "message": str(e), "outbox_id": outbox_id}

    if _is_forward_success(result):
        await _finalize_outbox_success(record, result)
    else:
        await _finalize_outbox_failure(
            outbox_id, f"status={result.get('status')}: {result.get('message', '')}", policy=policy
        )
    return {**result, "outbox_id": outbox_id}


async def deliver_outbox_record(record: ForwardOutbox) -> ForwardResult:
    channel_name = str(record.channel_name or record.target_type or "")
    if channel_name == "openclaw":
        from services.forwarding.openclaw import forward_to_openclaw

        forward_data = cast(WebhookData, dict(record.forward_data or {}))
        analysis = cast(AnalysisResult, dict(record.analysis_result or {}))
        return await forward_to_openclaw(forward_data, analysis)

    from services.channels.base import FormatContext, resolve_channel

    channel = resolve_channel(channel_name, str(record.target_url or ""))
    if channel is None:
        return {"status": "failed", "message": f"unknown_channel:{channel_name}"}
    payload = record.formatted_payload
    if not isinstance(payload, dict):
        payload = None
    if payload is None and isinstance(record.forward_data, dict) and isinstance(record.analysis_result, dict):
        payload = channel.format(
            FormatContext(
                webhook_data=cast(WebhookData, dict(record.forward_data)),
                analysis_result=cast(AnalysisResult, dict(record.analysis_result)),
                is_periodic_reminder=bool(record.is_periodic_reminder),
            )
        )
    if payload is None:
        payload = {}
    return cast(ForwardResult, await channel.send(str(record.target_url or ""), cast(dict[str, Any], payload)))


def _rule_id(rule: ForwardRuleTarget) -> int | None:
    raw = rule.get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return int(raw)
    return None


def _idempotency_key(
    *,
    webhook_id: int,
    rule_id: int | None,
    target_type: str,
    target_url: str,
    is_periodic_reminder: bool,
) -> str:
    raw = f"{webhook_id}|{rule_id or 'default'}|{target_type}|{target_url}|{int(is_periodic_reminder)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"forward:{webhook_id}:{digest[:32]}"


async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    """Dispatch immediately; the scheduled scanner picks up missed records."""
    if not outbox_ids:
        return

    if _forward_outbox_enqueuer is None:
        logger.warning("[ForwardOutbox] 未注册即时调度器，ids=%s 将由扫描任务补扫", outbox_ids)
        return

    for outbox_id in outbox_ids:
        try:
            await _forward_outbox_enqueuer(outbox_id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scheduled").inc()
        except Exception as e:  # noqa: PERF203
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "schedule_failed").inc()
            logger.warning("[ForwardOutbox] 即时调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    if _forward_outbox_retry_scheduler is None:
        logger.warning("[ForwardOutbox] 未注册延迟调度器 id=%s，将由扫描任务补扫", outbox_id)
        return
    try:
        await _forward_outbox_retry_scheduler(outbox_id, delay_seconds)
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_scheduled").inc()
    except Exception as e:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_schedule_failed").inc()
        logger.warning("[ForwardOutbox] 延迟调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


async def _claim_outbox(outbox_id: int, *, policy: ForwardDeliveryPolicy | None = None) -> ForwardOutbox | None:
    from services.forwarding.outbox_scanner import expire_outbox_if_old

    now = datetime.now()
    policy = policy or ForwardDeliveryPolicy.from_config()
    async with session_scope() as session:
        if await expire_outbox_if_old(session, outbox_id, now=now, policy=policy):
            return None
        stmt = (
            update(ForwardOutbox)
            .where(ForwardOutbox.id == outbox_id)
            .where(ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]))
            .where((ForwardOutbox.next_attempt_at.is_(None)) | (ForwardOutbox.next_attempt_at <= now))
            .values(
                status=ForwardOutboxStatus.PROCESSING,
                attempts=ForwardOutbox.attempts + 1,
                last_attempt_at=now,
                updated_at=now,
            )
            .returning(ForwardOutbox)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()


def _is_forward_success(result: ForwardResult) -> bool:
    return result.get("status") == "success" or bool(result.get("_pending"))


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
            "event_id": record.webhook_event_id,
            "forward.outbox.id": record.id,
            "forward.target_type": target_type,
            "forward.status": str(record.status or "unknown"),
        },
    ) as outbox_span:
        try:
            result = await deliver_outbox_record(record)
        except Exception as e:
            status = "failed"
            set_span_error(outbox_span, e)
            await _finalize_outbox_failure(record.id, str(e))
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, status).inc()
            FORWARD_OUTBOX_PROCESS_DURATION_SECONDS.labels(target_type, status).observe(time.perf_counter() - started)
            return

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
    now = datetime.now()
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


async def _finalize_outbox_success(record: ForwardOutbox, result: ForwardResult) -> None:
    now = datetime.now()
    openclaw_analysis_id: int | None = None
    async with session_scope() as session:
        current = await session.get(ForwardOutbox, record.id)
        if not current or current.status in (
            ForwardOutboxStatus.SENT,
            ForwardOutboxStatus.EXPIRED,
            ForwardOutboxStatus.EXHAUSTED,
        ):
            return
        current.status = ForwardOutboxStatus.SENT
        current.sent_at = now
        current.updated_at = now
        current.last_error = None
        current.response_data = dict(result)

        if current.target_type == "openclaw" and result.get("_pending"):
            from models import DeepAnalysis
            from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

            target_event_id = current.webhook_event_id
            initial_poll_delay = compute_openclaw_poll_delay(0)
            analysis_record = DeepAnalysis(
                webhook_event_id=target_event_id,
                engine="openclaw",
                openclaw_run_id=str(result.get("_openclaw_run_id", "")),
                openclaw_session_key=str(result.get("_openclaw_session_key", "")),
                status=DeepAnalysisStatus.PENDING,
                poll_attempts=0,
                next_poll_at=now + timedelta(seconds=initial_poll_delay),
            )
            session.add(analysis_record)
            await session.flush()
            openclaw_analysis_id = analysis_record.id

        notified_event_id = current.original_event_id or current.webhook_event_id
        if notified_event_id:
            await session.execute(
                update(WebhookEvent).where(WebhookEvent.id == notified_event_id).values(last_notified_at=now)
            )

        logger.info(
            "[ForwardOutbox] 转发成功 id=%s event_id=%s target_type=%s",
            current.id,
            current.webhook_event_id,
            current.target_type,
        )
    if openclaw_analysis_id is not None:
        await _schedule_openclaw_poll_best_effort(openclaw_analysis_id)


async def _schedule_openclaw_poll_best_effort(analysis_id: int) -> None:
    try:
        from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay, schedule_openclaw_poll

        await schedule_openclaw_poll(analysis_id, compute_openclaw_poll_delay(0))
    except Exception as e:
        logger.warning("[ForwardOutbox] OpenClaw poll 调度失败 analysis_id=%s error=%s", analysis_id, e)


async def _finalize_outbox_failure(
    outbox_id: int, error_msg: str, *, policy: ForwardDeliveryPolicy | None = None
) -> None:
    now = datetime.now()
    retry_outbox_id: int | None = None
    retry_delay: int | None = None
    exhausted_record: ForwardOutbox | None = None
    policy = policy or ForwardDeliveryPolicy.from_config()
    async with session_scope() as session:
        record = await session.get(ForwardOutbox, outbox_id)
        if not record or record.status in (
            ForwardOutboxStatus.SENT,
            ForwardOutboxStatus.EXPIRED,
            ForwardOutboxStatus.EXHAUSTED,
        ):
            return
        record.last_error = error_msg[:2000]
        record.updated_at = now
        if record.attempts >= record.max_attempts:
            record.status = ForwardOutboxStatus.EXHAUSTED
            record.next_attempt_at = None
            logger.warning(
                "[ForwardOutbox] 转发耗尽 id=%s attempts=%s/%s error=%s",
                record.id,
                record.attempts,
                record.max_attempts,
                error_msg,
            )
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "exhausted").inc()
            exhausted_record = record
        else:
            delay = policy.delay_for_attempt(record.attempts)
            record.status = ForwardOutboxStatus.RETRYING
            record.next_attempt_at = now + timedelta(seconds=delay)
            retry_outbox_id = record.id
            retry_delay = delay
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "retrying").inc()
            logger.info("[ForwardOutbox] 转发失败 id=%s delay=%ss error=%s", record.id, delay, error_msg)
    if exhausted_record is not None:
        try:
            from services.channels.feishu import build_delivery_exhausted_card

            exhausted_event_type = str(getattr(exhausted_record, "event_type", "") or "")
            if exhausted_event_type not in {"outbox_exhausted"}:
                await resolve_and_forward(
                    event_type="outbox_exhausted",
                    formatted_payload=build_delivery_exhausted_card(exhausted_record),
                    webhook_id=exhausted_record.webhook_event_id,
                    wait=False,
                )
        except Exception as e:
            logger.warning("[ForwardOutbox] EXHAUSTED 通知入队失败 id=%s error=%s", outbox_id, e)
    if retry_outbox_id is not None and retry_delay is not None:
        await schedule_forward_outbox_retry(retry_outbox_id, retry_delay)


