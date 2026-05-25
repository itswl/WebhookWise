"""Transactional forwarding outbox.

The webhook pipeline writes forwarding intents before any HTTP side effect.
Workers consume those intents asynchronously, giving the system an auditable,
recoverable at-least-once delivery path.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.logger import get_logger
from core.observability.attributes import FORWARD_STATUS, FORWARD_TARGET_TYPE, WEBHOOK_EVENT_ID
from core.observability.metrics import (
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS,
    FORWARD_OUTBOX_RECORDS_TOTAL,
)
from core.observability.tracing import otel_span, set_span_error
from db.session import session_scope
from models import ForwardOutbox, WebhookEvent
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.decisioning import ForwardDecision, ForwardRuleSnapshot
from services.webhooks.types import (
    AnalysisResult,
    DeepAnalysisStatus,
    ForwardOutboxStatus,
    ForwardResult,
    WebhookData,
)

logger = get_logger("forward_outbox")


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
    importance: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> ForwardResult:
    """独立路径：匹配规则 → 创建 outbox → 调度投递（或同步送达如 wait=True）。

    当 target_url 非空时跳过规则匹配，直接投递到该 URL。
    """
    from services.webhooks.decisioning import ForwardRuleSnapshot, select_forward_rules
    from services.webhooks.repository import list_enabled_forward_rules

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
        rules = await list_enabled_forward_rules()
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
        from services.analysis.openclaw import forward_to_openclaw

        forward_data = cast(WebhookData, dict(record.forward_data or {}))
        analysis = cast(AnalysisResult, dict(record.analysis_result or {}))
        return await forward_to_openclaw(forward_data, analysis)

    target_url = str(record.target_url or "")
    from services.notifications.feishu import build_feishu_card, is_feishu_url

    payload = record.formatted_payload
    if not isinstance(payload, dict):
        payload = None
    if payload is None and isinstance(record.forward_data, dict) and isinstance(record.analysis_result, dict):
        wd = cast(WebhookData, dict(record.forward_data))
        ar = cast(AnalysisResult, dict(record.analysis_result))
        is_reminder = bool(record.is_periodic_reminder)
        if is_feishu_url(target_url):
            payload = build_feishu_card(wd, ar, is_periodic_reminder=is_reminder)
        else:
            payload = {"webhook": wd, "analysis": ar, "is_periodic_reminder": is_reminder}
    if payload is None:
        payload = {}

    from services.forwarding.circuit_breakers import build_remote_forward_dependencies
    from services.forwarding.remote import post_json_to_remote

    if is_feishu_url(target_url):
        from services.notifications.feishu import send_to_feishu

        return await send_to_feishu(target_url, payload)

    deps = build_remote_forward_dependencies(target_url)
    return await post_json_to_remote(
        target_url,
        payload,
        dependencies=deps,
        target_type_label=channel_name or "webhook",
    )


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

    from services.operations.tasks import process_forward_outbox_task

    for outbox_id in outbox_ids:
        try:
            await process_forward_outbox_task.kiq(outbox_id=outbox_id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scheduled").inc()
        except Exception as e:  # noqa: PERF203
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "schedule_failed").inc()
            logger.warning("[ForwardOutbox] 即时调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    from services.operations.taskiq_retry_scheduler import schedule_forward_outbox

    try:
        await schedule_forward_outbox(outbox_id, delay_seconds)
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_scheduled").inc()
    except Exception as e:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_schedule_failed").inc()
        logger.warning("[ForwardOutbox] 延迟调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


async def _expire_outbox_if_old(
    session: AsyncSession,
    outbox_id: int,
    *,
    now: datetime,
    policy: ForwardDeliveryPolicy,
) -> bool:
    if policy.max_delivery_age_seconds <= 0:
        return False
    cutoff = now - timedelta(seconds=policy.max_delivery_age_seconds)
    stmt = (
        update(ForwardOutbox)
        .where(ForwardOutbox.id == outbox_id)
        .where(ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]))
        .where(ForwardOutbox.created_at < cutoff)
        .values(
            status=ForwardOutboxStatus.EXPIRED,
            next_attempt_at=None,
            updated_at=now,
            last_error=f"forward delivery expired after {policy.max_delivery_age_seconds}s",
        )
        .returning(ForwardOutbox)
    )
    expired = (await session.execute(stmt)).scalar_one_or_none()
    if not expired:
        return False
    FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(expired.target_type or "unknown"), "expired").inc()
    logger.warning(
        "[OutboxScanner] 转发意图已过期 id=%s event_id=%s age_limit=%ss",
        expired.id,
        expired.webhook_event_id,
        policy.max_delivery_age_seconds,
    )
    return True


async def _claim_outbox(outbox_id: int, *, policy: ForwardDeliveryPolicy | None = None) -> ForwardOutbox | None:
    now = utcnow()
    policy = policy or ForwardDeliveryPolicy.from_config()
    async with session_scope() as session:
        if await _expire_outbox_if_old(session, outbox_id, now=now, policy=policy):
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


_TERMINAL_OUTBOX_STATUSES = {ForwardOutboxStatus.SENT, ForwardOutboxStatus.EXPIRED, ForwardOutboxStatus.EXHAUSTED}


def _is_outbox_terminal(status: ForwardOutboxStatus | str | None) -> bool:
    return status in _TERMINAL_OUTBOX_STATUSES


def _related_webhook_event_ids(record: ForwardOutbox) -> list[int]:
    ids = [record.webhook_event_id, record.original_event_id]
    return list(dict.fromkeys(int(i) for i in ids if i))


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
            WEBHOOK_EVENT_ID: record.webhook_event_id,
            "forward.outbox.id": record.id,
            FORWARD_TARGET_TYPE: target_type,
            FORWARD_STATUS: str(record.status or "unknown"),
        },
    ) as outbox_span:
        try:
            result = await deliver_outbox_record(record)
        except Exception as e:
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


async def _finalize_outbox_success(record: ForwardOutbox, result: ForwardResult) -> None:
    now = utcnow()
    openclaw_analysis_id: int | None = None
    async with session_scope() as session:
        current = await session.get(ForwardOutbox, record.id)
        if not current or _is_outbox_terminal(current.status):
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

        notified_event_ids = _related_webhook_event_ids(current)
        if notified_event_ids:
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id.in_(notified_event_ids))
                .values(last_notified_at=now, forward_status="sent")
            )

        logger.info(
            "[ForwardOutbox] 转发成功 id=%s event_id=%s target_type=%s",
            current.id,
            current.webhook_event_id,
            current.target_type,
        )
    if openclaw_analysis_id is not None:
        from services.operations.taskiq_retry_scheduler import schedule_openclaw_poll_best_effort

        await schedule_openclaw_poll_best_effort(openclaw_analysis_id)


async def _finalize_outbox_failure(
    outbox_id: int, error_msg: str, *, policy: ForwardDeliveryPolicy | None = None
) -> None:
    now = utcnow()
    retry_outbox_id: int | None = None
    retry_delay: int | None = None
    exhausted_record: ForwardOutbox | None = None
    policy = policy or ForwardDeliveryPolicy.from_config()
    async with session_scope() as session:
        record = await session.get(ForwardOutbox, outbox_id)
        if not record or _is_outbox_terminal(record.status):
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
            # 更新关联告警的转发状态
            evt_ids = _related_webhook_event_ids(record)
            if evt_ids:
                await session.execute(
                    update(WebhookEvent).where(WebhookEvent.id.in_(evt_ids)).values(forward_status="failed")
                )
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
            from services.notifications.feishu import build_delivery_exhausted_card

            exhausted_event_type = str(getattr(exhausted_record, "event_type", "") or "")
            if exhausted_event_type not in {"outbox_exhausted"}:
                await forward_notification(
                    event_type="outbox_exhausted",
                    formatted_payload=build_delivery_exhausted_card(exhausted_record),
                    webhook_id=exhausted_record.webhook_event_id,
                )
        except Exception as e:
            logger.warning("[ForwardOutbox] EXHAUSTED 通知入队失败 id=%s error=%s", outbox_id, e)
    if retry_outbox_id is not None and retry_delay is not None:
        await schedule_forward_outbox_retry(retry_outbox_id, retry_delay)


# ── Outbox Queries ──────────────────────────────────────────────────────────


async def list_outbox_records(
    *,
    page: int = 1,
    page_size: int = 20,
    status: str = "",
    event_type: str = "",
) -> dict[str, Any]:
    """分页查询转发队列记录。"""
    from sqlalchemy import func

    page = max(1, min(page, 100))
    page_size = max(1, min(page_size, 200))

    filters = []
    if status:
        filters.append(ForwardOutbox.status == status)
    if event_type:
        filters.append(ForwardOutbox.event_type == event_type)

    async with session_scope() as session:
        count_q = select(func.count()).select_from(ForwardOutbox)
        for f in filters:
            count_q = count_q.where(f)
        total = (await session.execute(count_q)).scalar() or 0

        query = select(ForwardOutbox).order_by(ForwardOutbox.id.desc())
        for f in filters:
            query = query.where(f)
        query = query.offset((page - 1) * page_size).limit(page_size)
        rows = (await session.execute(query)).scalars().all()

        items = [
            {
                "id": r.id,
                "webhook_event_id": r.webhook_event_id,
                "original_event_id": r.original_event_id,
                "rule_name": r.rule_name,
                "target_type": r.target_type,
                "target_url": _mask_url_for_display(r.target_url or ""),
                "target_name": r.target_name,
                "event_type": r.event_type,
                "status": r.status,
                "attempts": r.attempts,
                "max_attempts": r.max_attempts,
                "next_attempt_at": utc_isoformat(r.next_attempt_at),
                "last_attempt_at": utc_isoformat(r.last_attempt_at),
                "sent_at": utc_isoformat(r.sent_at),
                "last_error": (r.last_error or "")[:200],
                "is_periodic_reminder": r.is_periodic_reminder,
                "created_at": utc_isoformat(r.created_at),
            }
            for r in rows
        ]

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size) if total else 1,
    }


def _mask_url_for_display(url: str) -> str:
    if not url:
        return ""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.hostname:
            path = parsed.path or ""
            qs = ("?" + parsed.query) if parsed.query else ""
            if len(path) > 40:
                path = path[:40] + "…"
            return f"{parsed.scheme}://{parsed.hostname}{path}{qs}"
    except ValueError as e:
        logger.debug("[ForwardOutbox] 展示 URL 解析失败: %s", e)
    return url[:80] + ("…" if len(url) > 80 else "")
