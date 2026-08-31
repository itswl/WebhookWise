"""Read model for operator-visible problems that need action."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.logger import mask_url
from db.session import count_with_timeout
from models import AnalysisFeedback, AuditLog, ForwardOutbox, ForwardRule, Incident, RemediationProposal, WebhookEvent
from services.operations.queue_health import get_queue_health
from services.webhooks.flapping import list_active_flapping
from services.webhooks.query_service import STUCK_STATUSES, STUCK_THRESHOLD
from services.webhooks.types import ForwardOutboxStatus, WebhookProcessingStatus

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_PERMANENT_DELIVERY_ERROR_CODES = {"19001", "unsafe_target"}


def _safe_error(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "No error detail was recorded"
    return _URL_PATTERN.sub(lambda match: mask_url(match.group(0)), text)[:300]


def _is_permanent_delivery_failure(record: Any) -> bool:
    """Accepts a ForwardOutbox entity or a projected row with the same fields."""
    response = record.response_data if isinstance(record.response_data, dict) else {}
    if response.get("retryable") is False:
        return True
    error_code = str(response.get("error_code") or "")
    if error_code in _PERMANENT_DELIVERY_ERROR_CODES:
        return True
    return "code=19001" in str(record.last_error or "")


def _item(
    *,
    item_id: str,
    kind: str,
    severity: str,
    title: str,
    title_key: str = "",
    title_params: dict[str, Any] | None = None,
    detail: str,
    count: int = 1,
    occurred_at: datetime | None = None,
    resource_type: str = "",
    resource_id: int | None = None,
    view: str = "",
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "kind": kind,
        "severity": severity,
        "title": title,
        # The English title stays authoritative; title_key/title_params let the
        # dashboard render a localized equivalent without parsing the string.
        "title_key": title_key,
        "title_params": title_params or {},
        "detail": detail,
        "count": count,
        "occurred_at": utc_isoformat(occurred_at),
        "resource_type": resource_type,
        "resource_id": resource_id,
        "view": view,
        "actions": actions or [],
    }


async def get_action_center(session: AsyncSession) -> dict[str, Any]:
    """Return a bounded, deduplicated list of current operator actions."""
    now = utcnow()
    recent_cutoff = now - timedelta(days=7)
    stuck_cutoff = now - STUCK_THRESHOLD
    outbox_stale_cutoff = now - timedelta(minutes=5)
    items: list[dict[str, Any]] = []

    auto_disabled = list(
        (
            await session.execute(
                select(AuditLog, ForwardRule)
                .join(ForwardRule, ForwardRule.id == AuditLog.resource_id)
                .where(
                    AuditLog.resource_type == "forward_rule",
                    AuditLog.action == "auto_disabled",
                    ForwardRule.enabled.is_(False),
                )
                .order_by(AuditLog.created_at.desc())
                .limit(20)
            )
        ).all()
    )
    seen_rules: set[int] = set()
    for audit, rule in auto_disabled:
        if rule.id in seen_rules:
            continue
        seen_rules.add(rule.id)
        items.append(
            _item(
                item_id=f"rule:{rule.id}",
                kind="integration_disabled",
                severity="critical",
                title=f"Forwarding rule disabled: {rule.name}",
                title_key="rule_disabled",
                title_params={"name": rule.name},
                detail=_safe_error(audit.summary),
                occurred_at=audit.created_at,
                resource_type="forward_rule",
                resource_id=rule.id,
                view="routing",
                actions=[{"action": "test_enable_rule", "label": "Test and enable", "resource_id": rule.id}],
            )
        )

    exhausted_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ForwardOutbox)
                .where(
                    ForwardOutbox.status == ForwardOutboxStatus.EXHAUSTED,
                    ForwardOutbox.updated_at >= recent_cutoff,
                )
            )
        ).scalar_one()
    )
    recent_exhausted = list(
        (
            await session.execute(
                # Project only the fields the grouping below reads; the full
                # entity would drag forward_data/analysis_result/
                # formatted_payload JSONB along for 100 rows (response_data is
                # needed by the permanent-failure check).
                select(
                    ForwardOutbox.id,
                    ForwardOutbox.forward_rule_id,
                    ForwardOutbox.rule_name,
                    ForwardOutbox.target_type,
                    ForwardOutbox.last_error,
                    ForwardOutbox.updated_at,
                    ForwardOutbox.response_data,
                )
                .where(
                    ForwardOutbox.status == ForwardOutboxStatus.EXHAUSTED,
                    ForwardOutbox.updated_at >= recent_cutoff,
                )
                .order_by(ForwardOutbox.updated_at.desc(), ForwardOutbox.id.desc())
                .limit(100)
            )
        ).all()
    )
    grouped_exhausted: dict[tuple[object, ...], list[Any]] = {}
    for record in recent_exhausted:
        key = (
            record.forward_rule_id,
            str(record.rule_name or ""),
            str(record.target_type or "unknown"),
            _is_permanent_delivery_failure(record),
        )
        grouped_exhausted.setdefault(key, []).append(record)

    for records in grouped_exhausted.values():
        record = records[0]
        if record.forward_rule_id in seen_rules:
            continue
        permanent = _is_permanent_delivery_failure(record)
        items.append(
            _item(
                item_id=(
                    f"outbox-rule:{record.forward_rule_id}:{'permanent' if permanent else 'exhausted'}"
                    if record.forward_rule_id is not None
                    else f"outbox:{record.id}"
                ),
                kind="delivery_exhausted",
                severity="critical",
                title=(
                    f"Permanent delivery fault: {record.rule_name or record.target_type}"
                    if permanent
                    else f"Delivery exhausted: {record.rule_name or record.target_type}"
                ),
                title_key="delivery_fault_permanent" if permanent else "delivery_exhausted",
                title_params={"name": record.rule_name or record.target_type},
                detail=_safe_error(record.last_error),
                count=len(records),
                occurred_at=record.updated_at,
                resource_type="outbox",
                resource_id=record.id,
                view="decision-trace",
                actions=(
                    []
                    if permanent
                    else [{"action": "retry_outbox", "label": "Retry delivery", "resource_id": record.id}]
                ),
            )
        )

    dead_letter_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(WebhookEvent)
                .where(WebhookEvent.processing_status == WebhookProcessingStatus.DEAD_LETTER)
            )
        ).scalar_one()
    )
    if dead_letter_count:
        latest_dead_letter = (
            await session.execute(
                select(WebhookEvent)
                .where(WebhookEvent.processing_status == WebhookProcessingStatus.DEAD_LETTER)
                .order_by(WebhookEvent.updated_at.desc(), WebhookEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        items.append(
            _item(
                item_id="dead-letters",
                kind="dead_letter",
                severity="critical",
                title=f"{dead_letter_count} dead-letter event(s) need review",
                title_key="dead_letters",
                title_params={"count": dead_letter_count},
                detail=_safe_error(latest_dead_letter.error_message if latest_dead_letter else None),
                count=dead_letter_count,
                occurred_at=latest_dead_letter.updated_at if latest_dead_letter else None,
                resource_type="webhook_event",
                resource_id=latest_dead_letter.id if latest_dead_letter else None,
                view="alerts",
                actions=[{"action": "retry_dead_letters", "label": "Replay batch"}],
            )
        )

    # Guarded count: non-terminal statuses are a tiny fraction of the table
    # (served by the (processing_status, id) index), but a regression here must
    # degrade to "unknown" instead of stalling the whole action center.
    stuck_count = int(
        await count_with_timeout(
            session,
            select(func.count())
            .select_from(WebhookEvent)
            .where(
                WebhookEvent.processing_status.in_(STUCK_STATUSES),
                WebhookEvent.updated_at < stuck_cutoff,
            ),
        )
        or 0
    )
    if stuck_count:
        items.append(
            _item(
                item_id="stuck-events",
                kind="stuck_processing",
                severity="warning",
                title=f"{stuck_count} event(s) appear stuck",
                title_key="stuck_events",
                title_params={"count": stuck_count},
                detail="Events have remained non-terminal for more than 15 minutes",
                count=stuck_count,
                occurred_at=now,
                resource_type="webhook_event",
                view="alerts",
                actions=[{"action": "retry_stuck_events", "label": "Retry stuck events"}],
            )
        )

    # Unconsumed ingest backlog nearing MAXLEN: warn BEFORE the stream silently
    # trims its oldest un-acked entries (already-200'd webhooks lost forever).
    # Keyed on the unconsumed backlog (lag + pending), not total depth — a busy
    # stream sits at MAXLEN of already-acked entries, which is not a problem.
    # Best-effort probe — a Redis hiccup must not fail the whole action center.
    queue = await get_queue_health()
    if queue.get("backlogged"):
        backlog_pct = round(float(queue["backlog_fraction"]) * 100, 1)
        items.append(
            _item(
                item_id="queue-backlog",
                kind="queue_backlog",
                severity="critical",
                title=f"Ingest queue backlog at {backlog_pct}% of capacity",
                title_key="queue_backlog",
                title_params={"pct": backlog_pct},
                detail=(
                    f"Unconsumed backlog {queue['backlog']} / MAXLEN {queue['maxlen']} "
                    f"(pending {queue['pending']}, lag {queue['lag']}). Consumers are falling behind; beyond "
                    "MAXLEN the stream trims un-acked entries and accepted webhooks are lost. Scale workers."
                ),
                count=int(queue["backlog"]),  # backlogged ⇒ backlog_fraction set ⇒ backlog is not None
                occurred_at=now,
                resource_type="queue",
                view="overview",
            )
        )

    # Identities oscillating firing↔recovered right now (best-effort Redis
    # read). Each one is a notification storm dedup cannot absorb; suggest a
    # threshold/for-duration fix upstream, or FLAPPING_SUPPRESS_ENABLED.
    flapping = await list_active_flapping(limit=50)
    if flapping:
        labels = ", ".join(item["identity"] for item in flapping[:3])
        items.append(
            _item(
                item_id="flapping-identities",
                kind="flapping_identity",
                severity="warning",
                title=f"{len(flapping)} alert identit{'y is' if len(flapping) == 1 else 'ies are'} flapping",
                title_key="flapping",
                title_params={"count": len(flapping)},
                detail=(
                    f"Rapid firing↔recovered oscillation: {labels}. Fix the upstream threshold or add a "
                    "for-duration; FLAPPING_SUPPRESS_ENABLED=true withholds notifications while they flap."
                ),
                count=len(flapping),
                occurred_at=now,
                resource_type="alert_identity",
                view="noise",
            )
        )

    stale_outbox_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ForwardOutbox)
                .where(
                    ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]),
                    ForwardOutbox.created_at < outbox_stale_cutoff,
                )
            )
        ).scalar_one()
    )
    if stale_outbox_count:
        items.append(
            _item(
                item_id="outbox-backlog",
                kind="delivery_backlog",
                severity="warning",
                title=f"{stale_outbox_count} delivery record(s) are delayed",
                title_key="delivery_delayed",
                title_params={"count": stale_outbox_count},
                detail="Pending or retrying deliveries are older than five minutes",
                count=stale_outbox_count,
                occurred_at=now,
                resource_type="outbox",
                view="decision-trace",
            )
        )

    summary_failure_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Incident)
                .where(
                    Incident.alert_count >= 2,
                    Incident.summary_status.in_(["retrying", "failed"]),
                )
            )
        ).scalar_one()
    )
    if summary_failure_count:
        latest_summary_failure = (
            await session.execute(
                select(Incident)
                .where(
                    Incident.alert_count >= 2,
                    Incident.summary_status.in_(["retrying", "failed"]),
                )
                .order_by(Incident.updated_at.desc(), Incident.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        items.append(
            _item(
                item_id="incident-summary-failures",
                kind="ai_provider",
                severity="warning",
                title=f"{summary_failure_count} incident summary job(s) are degraded",
                title_key="summaries_degraded",
                title_params={"count": summary_failure_count},
                detail=_safe_error(latest_summary_failure.summary_last_error if latest_summary_failure else None),
                count=summary_failure_count,
                occurred_at=latest_summary_failure.updated_at if latest_summary_failure else None,
                resource_type="incident",
                resource_id=latest_summary_failure.id if latest_summary_failure else None,
                view="incidents",
                actions=[{"action": "retry_incident_summaries", "label": "Retry summaries"}],
            )
        )

    overdue_incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.sla_due_at.isnot(None),
                    Incident.sla_due_at <= now,
                    Incident.workflow_status.notin_(["resolved", "ignored"]),
                )
                .order_by(Incident.sla_due_at, Incident.id)
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    overdue_events = list(
        (
            await session.execute(
                # The SLA card renders only these three fields; skip the full
                # entity (raw_payload and both JSONB blobs) for the 10 rows.
                select(WebhookEvent.id, WebhookEvent.sla_due_at, WebhookEvent.workflow_status)
                .where(
                    WebhookEvent.sla_due_at.isnot(None),
                    WebhookEvent.sla_due_at <= now,
                    WebhookEvent.workflow_status.notin_(["resolved", "ignored"]),
                )
                .order_by(WebhookEvent.sla_due_at, WebhookEvent.id)
                .limit(10)
            )
        ).all()
    )
    items.extend(
        [
            _item(
                item_id=f"incident-sla:{incident.id}",
                kind="sla_breached",
                severity="critical",
                title=f"Incident SLA breached: {incident.title}",
                title_key="incident_sla",
                title_params={"title": incident.title},
                detail=f"Due at {utc_isoformat(incident.sla_due_at)}; status is {incident.workflow_status}",
                occurred_at=incident.sla_due_at,
                resource_type="incident",
                resource_id=incident.id,
                view="incidents",
                actions=[
                    {
                        "action": "acknowledge",
                        "label": "Acknowledge",
                        "resource_id": incident.id,
                        "resource_type": "incident",
                    }
                ],
            )
            for incident in overdue_incidents
        ]
    )
    items.extend(
        [
            _item(
                item_id=f"event-sla:{event.id}",
                kind="sla_breached",
                severity="critical",
                title=f"Alert SLA breached: #{event.id}",
                title_key="alert_sla",
                title_params={"id": event.id},
                detail=f"Due at {utc_isoformat(event.sla_due_at)}; status is {event.workflow_status}",
                occurred_at=event.sla_due_at,
                resource_type="webhook_event",
                resource_id=event.id,
                view="alerts",
                actions=[
                    {
                        "action": "acknowledge",
                        "label": "Acknowledge",
                        "resource_id": event.id,
                        "resource_type": "webhook_event",
                    }
                ],
            )
            for event in overdue_events
        ]
    )

    # Approved remediations whose readback said the target did NOT recover.
    # This is the consumer of the verification loop: an approval that failed to
    # fix is exactly what needs a person, and exactly what used to be invisible.
    unrecovered_proposals = list(
        (
            await session.execute(
                select(RemediationProposal)
                .where(
                    RemediationProposal.verify_status == "unrecovered",
                    RemediationProposal.verified_at >= now - timedelta(days=14),
                )
                .order_by(RemediationProposal.verified_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for proposal in unrecovered_proposals:
        detail_bits = ", ".join(
            f"{key}={value}" for key, value in sorted((proposal.verify_detail or {}).items())
        )
        items.append(
            _item(
                item_id=f"remediation-verify:{proposal.id}",
                kind="remediation_unrecovered",
                severity="critical",
                title=f"Approved remediation did not recover: {proposal.action}",
                title_key="remediation_unrecovered",
                title_params={"action": proposal.action},
                detail=_safe_error(f"readback after execution: {detail_bits}" if detail_bits else None),
                occurred_at=proposal.verified_at,
                resource_type="remediation",
                resource_id=proposal.id,
                view="overview",
            )
        )

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda item: str(item["occurred_at"] or ""), reverse=True)
    items.sort(key=lambda item: severity_order.get(str(item["severity"]), 3))
    critical = sum(1 for item in items if item["severity"] == "critical")
    warning = sum(1 for item in items if item["severity"] == "warning")
    feedback_rows = (
        await session.execute(
            select(AnalysisFeedback.verdict, func.count(AnalysisFeedback.id))
            .where(AnalysisFeedback.created_at >= now - timedelta(days=30))
            .group_by(AnalysisFeedback.verdict)
        )
    ).all()
    feedback_breakdown = {str(verdict): int(count) for verdict, count in feedback_rows}
    feedback_total = sum(feedback_breakdown.values())
    return {
        "summary": {
            "total": len(items),
            "critical": critical,
            "warning": warning,
            "exhausted_deliveries_7d": exhausted_count,
            "dead_letters": dead_letter_count,
            "stuck_events": stuck_count,
            "delayed_deliveries": stale_outbox_count,
            "sla_breaches": len(overdue_incidents) + len(overdue_events),
            # Deliberately no agreement percentage. It was computed from
            # self-selected samples — only the people who felt like clicking,
            # only on the alerts they felt like clicking about — and a number
            # like that reads as a quality metric while measuring nothing. The
            # count stays: how many corrections were made IS a real fact.
            "feedback_total_30d": feedback_total,
        },
        "items": items[:30],
        "generated_at": utc_isoformat(now),
    }
