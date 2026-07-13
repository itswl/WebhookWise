"""Read model for operator-visible problems that need action."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.logger import mask_url
from models import AnalysisFeedback, AuditLog, ForwardOutbox, ForwardRule, Incident, WebhookEvent
from services.webhooks.types import ForwardOutboxStatus, WebhookProcessingStatus

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _safe_error(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "No error detail was recorded"
    return _URL_PATTERN.sub(lambda match: mask_url(match.group(0)), text)[:300]


def _item(
    *,
    item_id: str,
    kind: str,
    severity: str,
    title: str,
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
    stuck_cutoff = now - timedelta(minutes=15)
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
    latest_exhausted = list(
        (
            await session.execute(
                select(ForwardOutbox)
                .where(
                    ForwardOutbox.status == ForwardOutboxStatus.EXHAUSTED,
                    ForwardOutbox.updated_at >= recent_cutoff,
                )
                .order_by(ForwardOutbox.updated_at.desc(), ForwardOutbox.id.desc())
                .limit(8)
            )
        )
        .scalars()
        .all()
    )
    for record in latest_exhausted:
        if record.forward_rule_id in seen_rules:
            continue
        items.append(
            _item(
                item_id=f"outbox:{record.id}",
                kind="delivery_exhausted",
                severity="critical",
                title=f"Delivery exhausted: {record.rule_name or record.target_type}",
                detail=_safe_error(record.last_error),
                occurred_at=record.updated_at,
                resource_type="outbox",
                resource_id=record.id,
                view="decision-trace",
                actions=[{"action": "retry_outbox", "label": "Retry delivery", "resource_id": record.id}],
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
                detail=_safe_error(latest_dead_letter.error_message if latest_dead_letter else None),
                count=dead_letter_count,
                occurred_at=latest_dead_letter.updated_at if latest_dead_letter else None,
                resource_type="webhook_event",
                resource_id=latest_dead_letter.id if latest_dead_letter else None,
                view="alerts",
                actions=[{"action": "retry_dead_letters", "label": "Replay batch"}],
            )
        )

    stuck_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(WebhookEvent)
                .where(
                    WebhookEvent.processing_status.in_(
                        [
                            WebhookProcessingStatus.RECEIVED,
                            WebhookProcessingStatus.ANALYZING,
                            WebhookProcessingStatus.RETRY,
                        ]
                    ),
                    WebhookEvent.updated_at < stuck_cutoff,
                )
            )
        ).scalar_one()
    )
    if stuck_count:
        items.append(
            _item(
                item_id="stuck-events",
                kind="stuck_processing",
                severity="warning",
                title=f"{stuck_count} event(s) appear stuck",
                detail="Events have remained non-terminal for more than 15 minutes",
                count=stuck_count,
                occurred_at=now,
                resource_type="webhook_event",
                view="alerts",
                actions=[{"action": "retry_stuck_events", "label": "Retry stuck events"}],
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
                select(WebhookEvent)
                .where(
                    WebhookEvent.sla_due_at.isnot(None),
                    WebhookEvent.sla_due_at <= now,
                    WebhookEvent.workflow_status.notin_(["resolved", "ignored"]),
                )
                .order_by(WebhookEvent.sla_due_at, WebhookEvent.id)
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    items.extend(
        [
            _item(
                item_id=f"incident-sla:{incident.id}",
                kind="sla_breached",
                severity="critical",
                title=f"Incident SLA breached: {incident.title}",
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
            "feedback_total_30d": feedback_total,
            "feedback_agreement_pct": (
                round(100.0 * feedback_breakdown.get("correct", 0) / feedback_total, 1)
                if feedback_total
                else None
            ),
        },
        "items": items[:30],
        "generated_at": utc_isoformat(now),
    }
