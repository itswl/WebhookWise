"""Durable incident notification intents backed by the forwarding outbox."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from models import ForwardOutbox, Incident, WebhookEvent
from services.forwarding.policies import ForwardDeliveryPolicy
from services.notifications.feishu_actions import build_incident_action_value
from services.webhooks.types import ForwardOutboxStatus


def _incident_card(
    incident: Incident,
    *,
    interactive_actions: bool = False,
    dashboard_url: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**Source:** {incident.source or 'unknown'}\n"
                f"**Alerts:** {incident.alert_count}\n"
                f"**Started:** {incident.started_at.isoformat()}\n"
                f"**Importance:** {incident.top_importance or '?'}"
            ),
        }
    ]
    if interactive_actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Acknowledge"},
                        "type": "primary",
                        "value": build_incident_action_value("acknowledge", int(incident.id)),
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Resolve"},
                        "type": "danger",
                        "confirm": {
                            "title": {"tag": "plain_text", "content": "Resolve incident?"},
                            "text": {
                                "tag": "plain_text",
                                "content": "This closes the incident and records the Feishu operator.",
                            },
                        },
                        "value": build_incident_action_value("resolve", int(incident.id)),
                    },
                ],
            }
        )
        elements.append(
            {
                "tag": "form",
                "name": f"incident-note-{incident.id}",
                "elements": [
                    {
                        "tag": "input",
                        "name": "note",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "Add incident evidence or a handoff note",
                        },
                    },
                    {
                        "tag": "button",
                        "name": "submit-note",
                        "text": {"tag": "plain_text", "content": "Add note"},
                        "form_action_type": "submit",
                        "value": build_incident_action_value("add_note", int(incident.id)),
                    },
                ],
            }
        )
    if dashboard_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Open WebhookWise"},
                        "type": "default",
                        "url": dashboard_url,
                    }
                ],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {
                "enable_forward": False,
                "update_multi": True,
            },
            "header": {"title": {"tag": "plain_text", "content": f"🚨 {incident.title[:80]}"}},
            "elements": elements,
        },
    }


async def queue_incident_notifications(
    session: AsyncSession,
    incidents: list[Incident],
) -> list[int]:
    """Insert idempotent Feishu intents in the incident transaction."""
    app_config = get_config_manager()
    cfg = app_config.notifications
    app_enabled = bool(
        cfg.FEISHU_CARD_ACTIONS_ENABLED
        and cfg.FEISHU_APP_ID.strip()
        and cfg.FEISHU_APP_SECRET.strip()
        and cfg.FEISHU_INCIDENT_CHAT_ID.strip()
        and app_config.security.FEISHU_CARD_VERIFICATION_TOKEN.strip()
        and app_config.security.FEISHU_CARD_ACTION_SECRET.strip()
    )
    target_url = (
        f"feishu-app://{cfg.FEISHU_INCIDENT_CHAT_ID.strip()}"
        if app_enabled
        else str(cfg.DEEP_ANALYSIS_FEISHU_WEBHOOK or cfg.WEEKLY_REPORT_FEISHU_WEBHOOK or "").strip()
    )
    if not target_url:
        return []

    policy = ForwardDeliveryPolicy.from_config()
    now = utcnow()
    outbox_ids: list[int] = []
    for incident in incidents:
        if incident.id is None:
            continue
        key = f"incident-created:{incident.id}"
        existing = (
            await session.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is not None:
            outbox_ids.append(int(existing))
            continue
        record = ForwardOutbox(
            idempotency_key=key,
            webhook_event_id=None,
            original_event_id=None,
            forward_rule_id=None,
            rule_name="system:incident-created",
            target_type="feishu_app" if app_enabled else "feishu",
            target_url=target_url,
            target_name="incident-notification",
            channel_name="feishu_app" if app_enabled else "feishu",
            event_type="incident_created",
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            formatted_payload=_incident_card(
                incident,
                interactive_actions=app_enabled,
                dashboard_url=str(cfg.DASHBOARD_PUBLIC_URL or "").strip(),
            ),
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        await session.flush()
        outbox_ids.append(int(record.id))
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("feishu_app" if app_enabled else "feishu", "created").inc()
    return outbox_ids


async def queue_sla_breach_notifications(session: AsyncSession, now: Any) -> list[int]:
    """Create idempotent notifications for newly breached alert and incident SLAs.

    This is the escalation path: with the auto-SLA policy armed (see
    services/incidents/auto_sla.py), an unacknowledged incident lands here N
    minutes later. A dedicated escalation webhook and an @all mention can make
    the breach louder than the original alert.
    """
    cfg = get_config_manager().notifications
    target_url = str(
        cfg.SLA_BREACH_FEISHU_WEBHOOK or cfg.DEEP_ANALYSIS_FEISHU_WEBHOOK or cfg.WEEKLY_REPORT_FEISHU_WEBHOOK or ""
    ).strip()
    if not target_url:
        return []
    mention_all = bool(cfg.SLA_BREACH_MENTION_ALL)

    incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.sla_due_at.isnot(None),
                    Incident.sla_due_at <= now,
                    Incident.workflow_status.notin_(["resolved", "ignored"]),
                    # The escalation exists to find a human; an acknowledged
                    # resource already has one.
                    Incident.acknowledged_at.is_(None),
                )
                .order_by(Incident.sla_due_at, Incident.id)
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    events = list(
        (
            await session.execute(
                select(WebhookEvent)
                .where(
                    WebhookEvent.sla_due_at.isnot(None),
                    WebhookEvent.sla_due_at <= now,
                    WebhookEvent.workflow_status.notin_(["resolved", "ignored"]),
                    WebhookEvent.acknowledged_at.is_(None),
                )
                .order_by(WebhookEvent.sla_due_at, WebhookEvent.id)
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    policy = ForwardDeliveryPolicy.from_config()
    outbox_ids: list[int] = []
    incidents_by_id = {int(item.id): item for item in incidents}
    resources: list[tuple[str, int, str, str, Any, str]] = [
        ("incident", int(item.id), item.title, item.workflow_status, item.sla_due_at, str(item.assignee or ""))
        for item in incidents
    ]
    resources.extend(
        (
            "alert",
            int(item.id),
            str(item.request_id or f"Alert #{item.id}"),
            item.workflow_status,
            item.sla_due_at,
            str(item.assignee or ""),
        )
        for item in events
    )
    # One batched existence check instead of a point-SELECT per breached
    # resource: a breach stays in this result set until resolved, so the scan
    # re-runs every tick and the per-key queries would repeat indefinitely.
    keys_by_resource = [
        (resource, f"sla-breached:{resource[0]}:{resource[1]}:{resource[4].isoformat()}") for resource in resources
    ]
    dashboard_url = str(cfg.DASHBOARD_PUBLIC_URL or "").strip()
    already_queued: set[str] = set()
    if keys_by_resource:
        already_queued = set(
            (
                await session.execute(
                    select(ForwardOutbox.idempotency_key).where(
                        ForwardOutbox.idempotency_key.in_([key for _, key in keys_by_resource])
                    )
                )
            ).scalars()
        )
    for (resource_type, resource_id, title, status, due_at, assignee), key in keys_by_resource:
        if key in already_queued:
            continue
        body = (
            f"**Resource:** {resource_type} #{resource_id}\n"
            f"**Title:** {title[:160]}\n"
            f"**Workflow status:** {status}\n"
            f"**Assignee:** {assignee or 'unassigned'}\n"
            f"**SLA due:** {due_at.isoformat()}"
        )
        if dashboard_url:
            body += f"\n[Open dashboard]({dashboard_url})"
        if mention_all:
            body += '\n<at id="all"></at> unacknowledged past its SLA — please claim it.'
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "⏰ WebhookWise SLA breached"},
                    "template": "red",
                },
                "elements": [{"tag": "markdown", "content": body}],
            },
        }
        record = ForwardOutbox(
            idempotency_key=key,
            rule_name="system:sla-breached",
            target_type="feishu",
            target_url=target_url,
            target_name="sla-notification",
            channel_name="feishu",
            event_type="sla_breached",
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            formatted_payload=card,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        await session.flush()
        outbox_ids.append(int(record.id))
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("feishu", "created").inc()
        # Mark the escalation on the incident itself so it is visible without
        # joining the outbox (dashboard badge, postmortem timeline).
        if resource_type == "incident":
            incident = incidents_by_id.get(resource_id)
            if incident is not None and incident.escalated_at is None:
                incident.escalated_at = now
    return outbox_ids
