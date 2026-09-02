"""Low-level forwarding outbox record creation helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import parse_utc_datetime, utcnow
from core.logger import get_logger
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from models import ForwardOutbox
from services.forwarding.channels import is_chat_target
from services.forwarding.policies import ForwardDeliveryPolicy
from services.forwarding.types import ForwardRuleSnapshot
from services.webhooks.inbound_rules import alert_rule_name, inbound_actions_for
from services.webhooks.types import (
    SKIP_AI,
    SKIP_DEEP_ANALYSIS,
    AnalysisResult,
    ForwardOutboxStatus,
    ForwardResult,
    analysis_digest_window,
    analysis_route,
)

logger = get_logger("forward_outbox")


# ── Digest windows ───────────────────────────────────────────────────────────


def digest_window_start(event_time: datetime, window_minutes: int) -> datetime:
    """Floor `event_time` (naive UTC) to the start of its digest window.

    Windows are aligned to UTC midnight: 60 minutes gives clock hours, 1440 the
    UTC day. Alignment is what lets two alerts processed on different workers
    agree on one key without talking to each other.
    """
    seconds = max(1, int(window_minutes)) * 60
    epoch = int(event_time.replace(tzinfo=UTC).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC).replace(tzinfo=None)


def digest_key_for(*, forward_rule_id: int | None, target_type: str, window_start: datetime) -> str:
    """The group a digested record belongs to: one forward rule, one target kind, one window."""
    rule = forward_rule_id if forward_rule_id is not None else "default"
    return f"{rule}:{target_type}:{window_start.isoformat(timespec='minutes')}"


def digest_window_start_from_key(digest_key: str | None) -> datetime | None:
    """The window start a digest key was built from, or None for a malformed key."""
    parts = str(digest_key or "").split(":", 2)
    if len(parts) != 3:
        return None
    return parse_utc_datetime(parts[2])


@dataclass(frozen=True, slots=True)
class DeferredDigest:
    """Outbox records that must not be kicked now: their digest window is still open.

    `ids` is every such record. `kicks` is the (id, due at) pairs worth a
    delayed kick — the first record of each group. One kick per group is
    enough: the record it wakes claims every due sibling, and the scheduled
    outbox scan re-kicks anything a lost kick leaves behind.
    """

    ids: frozenset[int] = frozenset()
    kicks: tuple[tuple[int, datetime], ...] = ()


async def deferred_digest_kicks(session: AsyncSession, outbox_ids: list[int], *, now: datetime) -> DeferredDigest:
    """Which of `outbox_ids` wait for a digest window, and which of those open a group."""
    if not outbox_ids:
        return DeferredDigest()
    rows = (
        await session.execute(
            select(ForwardOutbox.id, ForwardOutbox.digest_key, ForwardOutbox.next_attempt_at)
            .where(ForwardOutbox.id.in_(outbox_ids))
            .where(ForwardOutbox.digest_key.is_not(None))
            .where(ForwardOutbox.next_attempt_at > now)
        )
    ).all()
    if not rows:
        return DeferredDigest()
    keys = {str(row.digest_key) for row in rows}
    openers = {
        str(key): int(first_id)
        for key, first_id in (
            await session.execute(
                select(ForwardOutbox.digest_key, func.min(ForwardOutbox.id))
                .where(ForwardOutbox.digest_key.in_(keys))
                .group_by(ForwardOutbox.digest_key)
            )
        ).all()
    }
    kicks = tuple(
        (int(row.id), row.next_attempt_at)
        for row in rows
        if row.next_attempt_at is not None and openers.get(str(row.digest_key)) == int(row.id)
    )
    return DeferredDigest(ids=frozenset(int(row.id) for row in rows), kicks=kicks)


# ── Record creation ──────────────────────────────────────────────────────────


async def create_outbox_records(
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
    # An alert whose rule is excluded from AI analysis must not reach the
    # investigator either. The exclusion cannot be expressed by importance: the
    # rule pass still judges these high — correctly, they are payment alerts —
    # and the deep-analysis rule matches on high, so severity alone would send
    # every one of them to a $0.39 investigation nobody asked for.
    ai_excluded = analysis_route(analysis_result, default="rule") == "rule_excluded"
    if not ai_excluded and any(rule.target_type == "deep_analysis" for rule in matched_rules):
        # Only asked when a deep-analysis target is actually in play: this is a
        # cached lookup, but it is still work in the delivery path.
        #
        # Both actions, not just skip_deep_analysis. The analysis route says
        # `rule_excluded` only when THIS alert went through the exclusion; an
        # identical alert answered from the cache carries `cache` instead, and
        # the cached verdict is `high`, which is exactly what the deep-analysis
        # forward rule matches on. Asking only about skip_deep_analysis let a
        # cache hit fund an investigation the operator had excluded.
        actions = await inbound_actions_for(
            parsed_data=forward_data,
            event_type=event_type,
            importance=str(analysis_result.get("importance") or "") if analysis_result else "",
            rule_name=alert_rule_name(forward_data or {}),
        )
        ai_excluded = bool(actions & {SKIP_AI, SKIP_DEEP_ANALYSIS})

    # An inbound digest rule batches this alert's CHAT deliveries into one card
    # per window. The window is fixed by the alert's own time, so every alert
    # of the window lands in the same group whichever worker files it.
    digest_minutes, _digest_rule = analysis_digest_window(analysis_result)
    digest_start: datetime | None = None
    digest_end: datetime | None = None
    if digest_minutes:
        stamped = parse_utc_datetime(str((forward_data or {}).get("timestamp") or "") or None)
        digest_start = digest_window_start(stamped or now, digest_minutes)
        digest_end = digest_start + timedelta(minutes=digest_minutes)

    for rule in matched_rules:
        target_type = str(rule.target_type or "webhook")
        target_url = str(rule.target_url or "")
        digest_key: str | None = None
        digest_due: datetime | None = None
        if digest_start is not None and digest_end is not None and is_chat_target(target_type, target_url):
            digest_key = digest_key_for(forward_rule_id=rule.id, target_type=target_type, window_start=digest_start)
            digest_due = digest_end
        if digest_key is not None and is_periodic_reminder:
            # A reminder says "still firing, hours on". The digest already says
            # that every window; a reminder card would be a second voice
            # repeating the first, at exactly the cadence the rule removed.
            logger.info(
                "[%s] Rule '%s' periodic reminder skipped: this alert rule is delivered as a digest",
                log_tag,
                rule.name or rule.id,
            )
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_digest_reminder").inc()
            continue
        if ai_excluded and target_type == "deep_analysis":
            logger.info(
                "[%s] Rule '%s' targets deep analysis, but this alert rule is excluded from AI",
                log_tag,
                rule.name or rule.id,
            )
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_ai_excluded").inc()
            continue
        if target_type != "deep_analysis" and not target_url:
            logger.warning("[%s] Rule '%s' has empty target_url, skipping", log_tag, rule.name or rule.id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_empty_target").inc()
            continue

        rule_id = rule.id
        key = idempotency_key(
            webhook_id=webhook_id or 0,
            rule_id=rule_id,
            target_type=target_type,
            target_url=target_url,
            is_periodic_reminder=is_periodic_reminder,
            extra=idempotency_extra,
        )
        existing = await find_outbox_id_by_key(session, key)
        if existing is not None:
            logger.info("[%s] Idempotency hit key=%s id=%s", log_tag, key, existing)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
            outbox_ids.append(existing)
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
            # Snapshot, not a lookup: a rule edited while this row waits in the
            # queue must not redirect a delivery that was already decided.
            target_gateway=str(getattr(rule, "target_gateway", "") or ""),
            is_periodic_reminder=is_periodic_reminder,
            channel_name=target_type,
            event_type=event_type,
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            # A digested record is not due until its window closes; whichever
            # record of the group is claimed first then delivers for all.
            next_attempt_at=digest_due or now,
            digest_key=digest_key,
            digest_window_end=digest_due,
            forward_data=forward_data,
            analysis_result=analysis_result,
            formatted_payload=formatted_payload,
            created_at=now,
            updated_at=now,
        )
        outbox_id, created = await insert_outbox_or_existing(session, record)
        outbox_ids.append(outbox_id)
        if not created:
            # Same outcome as the pre-check hit above; only the timing differs.
            logger.info("[%s] Idempotency race lost key=%s id=%s", log_tag, key, outbox_id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
            continue
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "created").inc()
        logger.info(
            "[%s] Created forward intent id=%s event_id=%s event_type=%s rule=%s target=%s digest=%s",
            log_tag,
            outbox_id,
            webhook_id,
            event_type,
            rule.name,
            target_type,
            digest_key or "-",
        )

    return outbox_ids


async def find_outbox_id_by_key(session: AsyncSession, key: str) -> int | None:
    """Id of the committed-and-visible outbox row carrying ``key``, if any."""
    existing = (
        await session.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
    ).scalar_one_or_none()
    return int(existing) if existing is not None else None


async def insert_outbox_or_existing(session: AsyncSession, record: ForwardOutbox) -> tuple[int, bool]:
    """Flush ``record`` under a SAVEPOINT; on an idempotency-key collision adopt the winner's row.

    Returns ``(outbox_id, created)``. Two workers can both pass the SELECT
    pre-check for one key before either commits; the UNIQUE index then rejects
    the loser at flush time. Left uncaught, that IntegrityError poisoned the
    caller's whole transaction — on PostgreSQL every later statement fails
    until rollback — so the webhook's persist stage was recorded as failed
    even though the row it wanted already exists. Rolling back to the
    SAVEPOINT keeps the transaction usable, and the re-select returns the row
    the other worker committed: the same outcome as a pre-check hit.

    An IntegrityError that is NOT this collision (a foreign-key or NOT NULL
    violation) has no row to fall back on and is re-raised unchanged.
    """
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        existing = await find_outbox_id_by_key(session, record.idempotency_key)
        if existing is None:
            raise
        return existing, False
    return int(record.id), True


def outbox_result(outbox_ids: list[int]) -> ForwardResult:
    if not outbox_ids:
        return {"status": "skipped", "reason": "all matched rules already exist or are invalid", "outbox_ids": []}
    return {"status": "queued", "outbox_ids": outbox_ids, "outbox_id": outbox_ids[0]}


def idempotency_key(
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
