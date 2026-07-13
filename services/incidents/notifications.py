"""Durable incident notification intents backed by the forwarding outbox."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from models import ForwardOutbox, Incident
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import ForwardOutboxStatus


def _incident_card(incident: Incident) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"🚨 {incident.title[:80]}"}},
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**Source:** {incident.source or 'unknown'}\n"
                        f"**Alerts:** {incident.alert_count}\n"
                        f"**Started:** {incident.started_at.isoformat()}\n"
                        f"**Importance:** {incident.top_importance or '?'}"
                    ),
                }
            ],
        },
    }


async def queue_incident_notifications(
    session: AsyncSession,
    incidents: list[Incident],
) -> list[int]:
    """Insert idempotent Feishu intents in the incident transaction."""
    cfg = get_config_manager().notifications
    target_url = str(cfg.DEEP_ANALYSIS_FEISHU_WEBHOOK or cfg.WEEKLY_REPORT_FEISHU_WEBHOOK or "").strip()
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
            target_type="feishu",
            target_url=target_url,
            target_name="incident-notification",
            channel_name="feishu",
            event_type="incident_created",
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            formatted_payload=_incident_card(incident),
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        await session.flush()
        outbox_ids.append(int(record.id))
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("feishu", "created").inc()
    return outbox_ids
