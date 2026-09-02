"""Transactional forwarding outbox.

The webhook pipeline writes forwarding intents before any HTTP side effect.
Workers consume those intents asynchronously, giving the system an auditable,
recoverable at-least-once delivery path.

This module owns the outbox lifecycle end to end — previously split across
outbox_delivery / outbox_notifications / outbox_state, which was over-fragmented
(one was a 17-line delegate). It reads top-to-bottom as the lifecycle: enqueue
(notification/pipeline) -> schedule -> claim -> deliver -> finalize
(success/failure) -> expire/requeue -> list.

Persistence/query helpers stay in outbox_records / outbox_queries; channel
dispatch in channels.py. TaskIQ dispatch stays in outbox_scheduling.py, kept
separate on purpose: the scheduled scanner (outbox_scanner.py) enqueues task IDs
without importing this whole facade (channels -> feishu/deep analysis, decisioning,
rules).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
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
from models import DeepAnalysis, ForwardOutbox, ForwardRule, WebhookEvent
from services.analysis.ai_prompt import DEEP_ANALYSIS_PROMPT_KIND, get_prompt_version
from services.forwarding import outbox_queries, outbox_records, outbox_scheduling
from services.forwarding import rules as forwarding_rules
from services.forwarding.channels import DigestChannel, resolve_channel
from services.forwarding.outbox_records import create_outbox_records, outbox_result
from services.forwarding.policies import ForwardDeliveryPolicy
from services.forwarding.types import ForwardRuleSnapshot
from services.notifications import feishu
from services.operations import taskiq_retry_scheduler
from services.webhooks.decisioning import ForwardDecision, select_forward_rules
from services.webhooks.types import (
    AnalysisResult,
    DeepAnalysisStatus,
    ForwardOutboxStatus,
    ForwardResult,
    gateway_run_id,
    gateway_session_key,
    is_pending_result,
)

logger = get_logger("forward_outbox")
# The state-transition helpers historically logged under this distinct name;
# preserved so existing log filters/queries keep matching.
_state_logger = get_logger("forward_outbox_state")

_DELIVERY_RUNTIME_ERRORS = (OSError, RuntimeError, ValueError)
_OUTBOX_NOTIFICATION_ERRORS = (OSError, RuntimeError, SQLAlchemyError, ValueError)

_TERMINAL_OUTBOX_STATUSES = {
    ForwardOutboxStatus.SENT,
    ForwardOutboxStatus.EXPIRED,
    ForwardOutboxStatus.EXHAUSTED,
}

# A digest group is at most this many rows per send: the card is read by a
# person, and the sibling claim is bounded by it. Rows past the cap stay due
# and form the next group when the scan re-kicks them.
DIGEST_GROUP_MAX_RECORDS = 200


# ── Enqueue: turn a forward decision / notification into outbox records ───────


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
    """Pipeline path: create outbox records within an existing transaction; the caller is responsible for commit and scheduling."""
    matched = list(decision.matched_rules)
    if not matched:
        return {"status": "skipped", "reason": "no matching forward rule", "outbox_ids": []}

    outbox_ids = await outbox_records.create_outbox_records(
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
    return outbox_records.outbox_result(outbox_ids)


async def create_forward_notification_outbox_records(
    *,
    event_type: str,
    source: str = "",
    formatted_payload: dict[str, Any] | None = None,
    forward_data: dict[str, Any] | None = None,
    analysis_result: AnalysisResult | None = None,
    webhook_id: int | None = None,
    policy: ForwardDeliveryPolicy,
    target_url: str = "",
    idempotency_extra: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> tuple[list[int], str]:
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
                target_gateway="",
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
        logger.info("[ForwardNotify] No matching rule event_type=%s source=%s", event_type, source)
        return [], "no matching forward rule" if not target_url else "target URL is empty"

    async with session_scope() as sess:
        outbox_ids = await create_outbox_records(
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
    return outbox_ids, ""


async def enqueue_forward_notification(
    *,
    event_type: str,
    source: str = "",
    formatted_payload: dict[str, Any] | None = None,
    forward_data: dict[str, Any] | None = None,
    analysis_result: AnalysisResult | None = None,
    webhook_id: int | None = None,
    policy: ForwardDeliveryPolicy | None = None,
    target_url: str = "",
    idempotency_extra: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> ForwardResult:
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
    if outbox_ids:
        await schedule_forward_outbox_many(outbox_ids)
    return outbox_result(outbox_ids)


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
    """Standalone path: match rules -> create outbox -> schedule delivery (or deliver synchronously if wait=True).

    When target_url is non-empty, rule matching is skipped and delivery goes directly to that URL.
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
        return outbox_records.outbox_result(outbox_ids)

    if wait:
        results: list[ForwardResult] = []
        for oid in outbox_ids:
            result = await _deliver_one(oid, policy=policy)
            results.append(result)
        return results[0] if results else {"status": "skipped"}

    await schedule_forward_outbox_many(outbox_ids)
    return outbox_records.outbox_result(outbox_ids)


# ── Schedule: dispatch to TaskIQ (via the outbox_scheduling leaf) ─────────────
# Thin re-exports so callers of this facade don't need to know about the leaf,
# while the scheduled scanner keeps importing outbox_scheduling directly.


async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    await outbox_scheduling.schedule_forward_outbox_many(outbox_ids)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    await outbox_scheduling.schedule_forward_outbox_retry(outbox_id, delay_seconds)


# ── Deliver: execute one record via its channel ──────────────────────────────


def _is_forward_success(result: ForwardResult) -> bool:
    return result.get("status") == "success" or is_pending_result(result)


async def deliver_outbox_record(record: ForwardOutbox) -> ForwardResult:
    # Channel-specific dispatch (deep analysis / feishu / generic webhook) lives in
    # the ForwardChannel registry; this just resolves and delegates.
    return await resolve_channel(record).deliver(record)


async def _deliver_one(outbox_id: int, *, policy: ForwardDeliveryPolicy) -> ForwardResult:
    """Synchronously deliver a single outbox record and update its status."""
    record = await _claim_outbox(outbox_id, policy=policy)
    if record is None:
        return {"status": "not_claimed", "outbox_id": outbox_id}
    if record.digest_key:
        _status, digest_result = await _deliver_digest_group(record, policy=policy)
        return {**digest_result, "outbox_id": outbox_id}
    try:
        result = await deliver_outbox_record(record)
    except _DELIVERY_RUNTIME_ERRORS as e:
        await _finalize_outbox_failure(outbox_id, str(e), policy=policy)
        return {"status": "failed", "message": str(e), "outbox_id": outbox_id}

    if _is_forward_success(result):
        await _finalize_outbox_success(record, result)
    else:
        await _finalize_outbox_failure(
            outbox_id,
            f"status={result.get('status')}: {result.get('message', '')}",
            policy=policy,
            permanent=result.get("retryable") is False,
            quarantine_rule=result.get("disable_rule") is True,
            failure_data=result,
        )
    return {**result, "outbox_id": outbox_id}


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
        if record.digest_key:
            status, _result = await _deliver_digest_group(record)
        else:
            status = await _deliver_single(record, outbox_span)
        if outbox_span is not None:
            outbox_span.set_attribute("forward.status", status)
    FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, status).inc()
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS.labels(target_type, status).observe(time.perf_counter() - started)


async def _deliver_single(record: ForwardOutbox, outbox_span: Any | None) -> str:
    """Deliver one claimed record and finalize it; returns the status label for metrics."""
    try:
        result = await deliver_outbox_record(record)
    except _DELIVERY_RUNTIME_ERRORS as e:
        set_span_error(outbox_span, e)
        await _finalize_outbox_failure(record.id, str(e))
        return "failed"
    if _is_forward_success(result):
        await _finalize_outbox_success(record, result)
        return "sent"
    await _finalize_outbox_failure(
        record.id,
        f"forward status={result.get('status')}: {result.get('message', '')}",
        permanent=result.get("retryable") is False,
        quarantine_rule=result.get("disable_rule") is True,
        failure_data=result,
    )
    return "failed"


# ── Deliver: a digest group — one message for every due row of a window ──────


async def _claim_digest_siblings(leader: ForwardOutbox, *, now: datetime) -> list[ForwardOutbox]:
    """Claim every due, unclaimed row of the leader's digest group in one conditional UPDATE.

    Same shape as _claim_outbox, for the same reason: two workers kicked for
    two rows of one group cannot both take a sibling — whichever UPDATE runs
    first owns it, the other sees zero rows and delivers only what it holds.
    """
    key = str(leader.digest_key or "")
    if not key:
        return []
    due = (
        select(ForwardOutbox.id)
        .where(ForwardOutbox.digest_key == key)
        .where(ForwardOutbox.id != leader.id)
        .where(ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]))
        .where((ForwardOutbox.next_attempt_at.is_(None)) | (ForwardOutbox.next_attempt_at <= now))
        .order_by(ForwardOutbox.id.asc())
        .limit(DIGEST_GROUP_MAX_RECORDS - 1)
    )
    async with session_scope() as session:
        claimed = (
            (
                await session.execute(
                    update(ForwardOutbox)
                    .where(ForwardOutbox.id.in_(due))
                    .values(
                        status=ForwardOutboxStatus.PROCESSING,
                        attempts=ForwardOutbox.attempts + 1,
                        last_attempt_at=now,
                        updated_at=now,
                    )
                    .returning(ForwardOutbox)
                )
            )
            .scalars()
            .all()
        )
    return sorted(claimed, key=lambda row: int(row.id))


async def _deliver_digest_group(
    leader: ForwardOutbox, *, policy: ForwardDeliveryPolicy | None = None
) -> tuple[str, ForwardResult]:
    """Deliver ONE message for the leader and every due sibling of its digest group.

    Success finalizes every row exactly as a single delivery would — the same
    bookkeeping and the same per-record metrics, so "how many notifications
    went out" still counts alerts. Failure applies the normal retry policy to
    the leader and parks the siblings on the leader's next attempt, so the
    group coalesces again instead of fanning out into per-alert retries.
    """
    channel = resolve_channel(leader)
    now = utcnow()
    can_digest = isinstance(channel, DigestChannel)
    siblings = await _claim_digest_siblings(leader, now=now) if can_digest else []
    group: list[ForwardOutbox] = [leader, *siblings]
    window_end = leader.digest_window_end or now
    window_start = outbox_records.digest_window_start_from_key(leader.digest_key) or window_end
    try:
        if isinstance(channel, DigestChannel):
            result = await channel.deliver_digest(leader, group, window_start=window_start, window_end=window_end)
        else:
            # A digest row whose channel cannot batch should not exist (the
            # enqueue path marks chat targets only); deliver it alone rather
            # than leave it waiting forever.
            result = await channel.deliver(leader)
    except _DELIVERY_RUNTIME_ERRORS as e:
        await _finalize_outbox_failure(leader.id, str(e), policy=policy)
        await _park_digest_siblings(leader.id, siblings, error=str(e))
        return "failed", {"status": "failed", "message": str(e)}

    if _is_forward_success(result):
        for record in group:
            await _finalize_outbox_success(record, result)
        if siblings:
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(leader.target_type or "unknown"), "sent").inc(len(siblings))
        _state_logger.info(
            "[ForwardOutbox] Digest delivered key=%s records=%s leader_id=%s",
            leader.digest_key,
            len(group),
            leader.id,
        )
        return "sent", result

    error = f"forward status={result.get('status')}: {result.get('message', '')}"
    await _finalize_outbox_failure(
        leader.id,
        error,
        policy=policy,
        permanent=result.get("retryable") is False,
        quarantine_rule=result.get("disable_rule") is True,
        failure_data=result,
    )
    await _park_digest_siblings(leader.id, siblings, error=error)
    return "failed", result


async def _park_digest_siblings(leader_id: int, siblings: Sequence[ForwardOutbox], *, error: str) -> None:
    """Return the claimed siblings to the queue on the leader's terms.

    Leader retrying → siblings go back to PENDING, due at the leader's next
    attempt, so the same kick claims the whole group again. Leader exhausted →
    the siblings are exhausted with it: they failed the same send to the same
    target, and letting each take a turn as leader would raise the exhausted
    notice once per alert for one dead webhook.
    """
    ids = [int(row.id) for row in siblings]
    if not ids:
        return
    now = utcnow()
    async with session_scope() as session:
        leader = await session.get(ForwardOutbox, leader_id)
        retry_at = (
            leader.next_attempt_at
            if leader is not None and leader.status == ForwardOutboxStatus.RETRYING and leader.next_attempt_at
            else None
        )
        if retry_at is not None:
            await session.execute(
                update(ForwardOutbox)
                .where(ForwardOutbox.id.in_(ids))
                .where(ForwardOutbox.status == ForwardOutboxStatus.PROCESSING)
                .values(
                    status=ForwardOutboxStatus.PENDING,
                    next_attempt_at=retry_at,
                    updated_at=now,
                    last_error=error[:2000],
                )
            )
            _state_logger.info(
                "[ForwardOutbox] Digest siblings parked count=%s until=%s leader_id=%s",
                len(ids),
                retry_at,
                leader_id,
            )
            return
        await session.execute(
            update(ForwardOutbox)
            .where(ForwardOutbox.id.in_(ids))
            .where(ForwardOutbox.status == ForwardOutboxStatus.PROCESSING)
            .values(
                status=ForwardOutboxStatus.EXHAUSTED,
                next_attempt_at=None,
                updated_at=now,
                last_error=error[:2000],
            )
        )
        event_ids = sorted({event_id for row in siblings for event_id in _related_webhook_event_ids(row)})
        if event_ids:
            await session.execute(
                update(WebhookEvent).where(WebhookEvent.id.in_(event_ids)).values(forward_status="failed")
            )
    for row in siblings:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(row.target_type or "unknown"), "exhausted").inc()
    _state_logger.warning(
        "[ForwardOutbox] Digest siblings exhausted with their leader count=%s leader_id=%s error=%s",
        len(ids),
        leader_id,
        error,
    )


# ── State transitions: claim / expire / finalize success|failure / requeue ───


def _is_outbox_terminal(status: ForwardOutboxStatus | str | None) -> bool:
    return status in _TERMINAL_OUTBOX_STATUSES


def _related_webhook_event_ids(record: ForwardOutbox) -> list[int]:
    ids = [record.webhook_event_id, record.original_event_id]
    return list(dict.fromkeys(int(i) for i in ids if i))


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
    _state_logger.warning(
        "[OutboxScanner] Forward intent expired id=%s event_id=%s age_limit=%ss",
        expired.id,
        expired.webhook_event_id,
        policy.max_delivery_age_seconds,
    )
    return True


async def _claim_outbox(outbox_id: int, *, policy: ForwardDeliveryPolicy | None = None) -> ForwardOutbox | None:
    if not isinstance(policy, ForwardDeliveryPolicy):
        policy = ForwardDeliveryPolicy.from_config()

    now = utcnow()
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


def _resolved_gateway(gateway_name: str) -> tuple[str, str]:
    """(platform, gateway name) for the instance this delivery went to.

    Falls back to the configured default platform under an unknown name only to
    label the row: the delivery itself already failed loudly in that case, so
    this is describing a record, not choosing a destination.
    """
    from services.analysis.deep_analysis_gateways import UnknownGatewayError, resolve_gateway
    from services.analysis.deep_analysis_platforms import configured_deep_analysis_platform

    try:
        gateway = resolve_gateway(gateway_name)
    except UnknownGatewayError:
        return configured_deep_analysis_platform(), gateway_name
    return gateway.platform, gateway.name


async def _finalize_outbox_success(record: ForwardOutbox, result: ForwardResult) -> None:
    now = utcnow()
    deep_analysis_id: int | None = None
    async with session_scope() as session:
        # Atomically claim the SENT transition: only a row that is not already
        # terminal flips to SENT, and exactly one finalizer wins. A stale-scan
        # requeue can cause a slow delivery to be re-claimed and delivered
        # twice; without this conditional UPDATE both finalizers could pass the
        # read-then-write check and each insert a duplicate DeepAnalysis row.
        claim = await session.execute(
            update(ForwardOutbox)
            .where(ForwardOutbox.id == record.id)
            .where(ForwardOutbox.status.notin_(_TERMINAL_OUTBOX_STATUSES))
            .values(status=ForwardOutboxStatus.SENT, sent_at=now, updated_at=now, last_error=None)
            # RETURNING the full row folds the follow-up session.get() into the
            # claim itself — one round trip per successful delivery, not two.
            .returning(ForwardOutbox)
        )
        current = claim.scalar_one_or_none()
        if current is None:
            # Another finalizer already terminalized this record.
            return
        response_snapshot = dict(result)
        # The pseudonym map's home is the DeepAnalysis row (the poller needs it);
        # duplicating real identifiers into response_data buys nothing.
        pseudonym_map = response_snapshot.pop("pseudonym_map", None)
        current.response_data = response_snapshot

        # Ask the channel whether a successful delivery needs a post-commit
        # follow-up record (the gateway spawns a DeepAnalysis poll). The state
        # machine owns the session/transaction, so the gateway-specific row is
        # built here, but the decision is delegated to the channel strategy
        # instead of a hardcoded target_type check.
        if resolve_channel(current).needs_followup_on_success(current, result):
            target_event_id = current.webhook_event_id
            initial_poll_delay = taskiq_retry_scheduler.compute_deep_analysis_poll_delay(0)
            gateway = _resolved_gateway(str(getattr(current, "target_gateway", "") or ""))
            analysis_record = DeepAnalysis(
                webhook_event_id=target_event_id,
                # The platform that answered, and WHICH instance of it — the
                # poller needs the instance to ask the right one back.
                engine=gateway[0],
                gateway_name=gateway[1],
                gateway_run_id=gateway_run_id(result),
                gateway_session_key=gateway_session_key(result),
                # The prompt AS ASKED. The report lands minutes from now and the
                # prompt is editable at runtime, so recording it at poll time
                # would name whatever it says then, not what produced this.
                prompt_version=get_prompt_version(DEEP_ANALYSIS_PROMPT_KIND),
                pseudonym_map=pseudonym_map if isinstance(pseudonym_map, dict) else None,
                status=DeepAnalysisStatus.PENDING,
                poll_attempts=0,
                next_poll_at=now + timedelta(seconds=initial_poll_delay),
            )
            session.add(analysis_record)
            await session.flush()
            deep_analysis_id = analysis_record.id

        notified_event_ids = _related_webhook_event_ids(current)
        if notified_event_ids:
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id.in_(notified_event_ids))
                .values(last_notified_at=now, forward_status="sent")
            )

        _state_logger.info(
            "[ForwardOutbox] Forward succeeded id=%s event_id=%s target_type=%s",
            current.id,
            current.webhook_event_id,
            current.target_type,
        )
    if deep_analysis_id is not None:
        await taskiq_retry_scheduler.schedule_deep_analysis_poll_best_effort(deep_analysis_id)


async def _finalize_outbox_failure(
    outbox_id: int,
    error_msg: str,
    *,
    policy: ForwardDeliveryPolicy | None = None,
    permanent: bool = False,
    quarantine_rule: bool = False,
    failure_data: ForwardResult | None = None,
) -> None:
    now = utcnow()
    retry_outbox_id: int | None = None
    retry_delay: int | None = None
    exhausted_record: ForwardOutbox | None = None
    quarantined_rule = False

    if policy is None:
        policy = ForwardDeliveryPolicy.from_config()

    async with session_scope() as session:
        record = await session.get(ForwardOutbox, outbox_id)
        if not record or _is_outbox_terminal(record.status):
            return
        record.last_error = error_msg[:2000]
        if failure_data is not None:
            record.response_data = dict(failure_data)
        record.updated_at = now
        if permanent or record.attempts >= record.max_attempts:
            record.status = ForwardOutboxStatus.EXHAUSTED
            record.next_attempt_at = None
            _state_logger.warning(
                "[ForwardOutbox] Forward exhausted id=%s attempts=%s/%s permanent=%s error=%s",
                record.id,
                record.attempts,
                record.max_attempts,
                permanent,
                error_msg,
            )
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "exhausted").inc()
            exhausted_record = record
            evt_ids = _related_webhook_event_ids(record)
            if evt_ids:
                await session.execute(
                    update(WebhookEvent).where(WebhookEvent.id.in_(evt_ids)).values(forward_status="failed")
                )
            if quarantine_rule and record.forward_rule_id is not None:
                rule = await session.get(ForwardRule, record.forward_rule_id)
                if rule is not None and rule.enabled:
                    from services.operations.audit_logger import add_audit

                    rule.enabled = False
                    rule.updated_at = now
                    add_audit(
                        session,
                        "forward_rule",
                        rule.id,
                        rule.name,
                        "auto_disabled",
                        f"Forward rule auto-disabled after permanent delivery failure: {error_msg[:300]}",
                        actor="system",
                    )
                    quarantined_rule = True
                    _state_logger.error(
                        "[ForwardOutbox] Forward rule auto-disabled rule_id=%s name=%s outbox_id=%s",
                        rule.id,
                        rule.name,
                        record.id,
                    )
        else:
            delay = policy.delay_for_attempt(record.attempts)
            record.status = ForwardOutboxStatus.RETRYING
            record.next_attempt_at = now + timedelta(seconds=delay)
            retry_outbox_id = record.id
            retry_delay = delay
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "retrying").inc()
            _state_logger.info("[ForwardOutbox] Forward failed id=%s delay=%ss error=%s", record.id, delay, error_msg)

    if quarantined_rule:
        forwarding_rules.invalidate_forward_rules_cache()
        await forwarding_rules.publish_rules_invalidation()

    if exhausted_record is not None:
        try:
            exhausted_event_type = str(getattr(exhausted_record, "event_type", "") or "")
            if exhausted_event_type != "outbox_exhausted":
                await enqueue_forward_notification(
                    event_type="outbox_exhausted",
                    formatted_payload=feishu.build_delivery_exhausted_card(exhausted_record),
                    webhook_id=exhausted_record.webhook_event_id,
                )
        except _OUTBOX_NOTIFICATION_ERRORS as exc:
            _state_logger.warning(
                "[ForwardOutbox] Failed to enqueue EXHAUSTED notification id=%s error=%s",
                outbox_id,
                exc,
            )
        # Out-of-band operator notice, deliberately OUTSIDE the event_type
        # guard above: when the outbox_exhausted meta-card itself dies, every
        # in-band channel is gone and this is the only signal left.
        from services.operations.self_notify import notify_delivery_exhausted

        await notify_delivery_exhausted(
            target_type=str(exhausted_record.target_type or "unknown"),
            error=error_msg,
            outbox_id=int(exhausted_record.id),
            event_id=exhausted_record.webhook_event_id,
        )
    if retry_outbox_id is not None and retry_delay is not None:
        await schedule_forward_outbox_retry(retry_outbox_id, retry_delay)


async def requeue_forward_outbox(outbox_id: int) -> bool:
    now = utcnow()
    # Conditional UPDATE rather than load-then-mutate: a manual requeue must not
    # race a worker that currently holds the row as PROCESSING (which would
    # reset attempts to 0 and double-schedule the delivery). Only requeue from a
    # quiescent state; PROCESSING is deliberately excluded.
    requeueable = {
        ForwardOutboxStatus.EXHAUSTED,
        ForwardOutboxStatus.EXPIRED,
        ForwardOutboxStatus.RETRYING,
        ForwardOutboxStatus.PENDING,
    }
    async with session_scope() as session:
        result = await session.execute(
            update(ForwardOutbox)
            .where(ForwardOutbox.id == outbox_id)
            .where(ForwardOutbox.status.in_(requeueable))
            .values(
                status=ForwardOutboxStatus.RETRYING,
                next_attempt_at=now,
                updated_at=now,
                attempts=0,
                last_error="manual_retry",
            )
            .returning(ForwardOutbox.id)
        )
        updated = result.scalar_one_or_none() is not None
    if updated:
        await schedule_forward_outbox_many([outbox_id])
    return updated


# ── List (read side) ─────────────────────────────────────────────────────────


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
