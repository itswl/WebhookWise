"""Bounded, auditable Action Center remediation commands."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.compression import decompress_payload
from core.datetime_utils import utc_isoformat, utcnow
from models import ForwardRule, Incident, WebhookEvent
from services.operations.audit_logger import add_audit
from services.operations.workflow import update_workflow
from services.webhooks.types import WebhookProcessingStatus


async def run_remediation(
    session: AsyncSession,
    *,
    action: str,
    resource_id: int | None,
    resource_type: str | None,
    batch_size: int,
) -> dict[str, Any]:
    if action == "retry_outbox":
        from services.forwarding.outbox import requeue_forward_outbox

        changed = bool(resource_id is not None and await requeue_forward_outbox(resource_id))
        return {"action": action, "changed": changed, "resource_id": resource_id}
    if action in {"retry_dead_letters", "retry_stuck_events"}:
        return await _replay_events(session, action=action, batch_size=batch_size)
    if action == "retry_incident_summaries":
        return await _retry_incident_summaries(session, batch_size=batch_size)
    if action in {"test_enable_rule", "disable_rule"}:
        return await _change_rule(session, action=action, rule_id=int(resource_id or 0))
    if action == "acknowledge":
        data = await update_workflow(
            session,
            resource_type=str(resource_type),
            resource_id=int(resource_id or 0),
            changes={"workflow_status": "acknowledged"},
        )
        return {"action": action, "changed": data is not None, "workflow": data}
    raise ValueError("Unsupported remediation action")


async def _replay_events(session: AsyncSession, *, action: str, batch_size: int) -> dict[str, Any]:
    query = select(WebhookEvent)
    if action == "retry_dead_letters":
        query = query.where(WebhookEvent.processing_status == WebhookProcessingStatus.DEAD_LETTER)
    else:
        query = query.where(
            WebhookEvent.processing_status.in_(
                [WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.RETRY]
            ),
            WebhookEvent.updated_at < utcnow() - timedelta(minutes=15),
        )
    events = list(
        (await session.execute(query.order_by(WebhookEvent.updated_at, WebhookEvent.id).limit(batch_size)))
        .scalars()
        .all()
    )
    scheduled: list[int] = []
    for event in events:
        await _enqueue_event(event)
        scheduled.append(int(event.id))
        add_audit(
            session,
            "webhook_event",
            event.id,
            str(event.request_id or event.id),
            "replayed",
            f"Event replay scheduled from Action Center: {event.id}",
        )
    await session.commit()
    return {"action": action, "changed": bool(scheduled), "scheduled_event_ids": scheduled}


async def _enqueue_event(event: WebhookEvent) -> None:
    from services.operations.tasks import process_webhook_task
    from services.webhooks.repository import load_event_payload

    headers = {str(key): str(value) for key, value in dict(event.headers or {}).items()}
    try:
        _, raw_body = await load_event_payload(event)
    except (TypeError, ValueError):
        raw = event.raw_payload
        raw_body = (decompress_payload(bytes(raw)) or "") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    await process_webhook_task.kiq(
        source_name=event.source or "unknown",
        raw_headers=headers,
        raw_body=raw_body,
        client_ip=event.client_ip or "action-center-replay",
        request_id=event.request_id,
        received_at=utc_isoformat(event.timestamp),
        ingest_retry_count=max(0, int(event.retry_count or 0)),
    )


async def _retry_incident_summaries(session: AsyncSession, *, batch_size: int) -> dict[str, Any]:
    incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.summary_status.in_(["retrying", "failed"]))
                .order_by(Incident.updated_at, Incident.id)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    ids: list[int] = []
    for incident in incidents:
        incident.summary_status = "pending"
        incident.summary_attempts = 0
        incident.summary_next_attempt_at = now
        incident.summary_last_error = None
        ids.append(int(incident.id))
    await session.commit()
    if ids:
        from services.incidents.summary import run_pending_incident_summaries

        await run_pending_incident_summaries()
    return {"action": "retry_incident_summaries", "changed": bool(ids), "incident_ids": ids}


async def _change_rule(session: AsyncSession, *, action: str, rule_id: int) -> dict[str, Any]:
    rule = await session.get(ForwardRule, rule_id)
    if rule is None:
        return {"action": action, "changed": False, "reason": "not_found"}
    if action == "test_enable_rule":
        from services.forwarding.remote import send_forward_rule_test

        result = await send_forward_rule_test(
            rule_name=rule.name,
            target_url=str(rule.target_url or ""),
            target_type=rule.target_type,
        )
        if result.get("status") != "success":
            return {"action": action, "changed": False, "reason": "target_test_failed"}
        rule.enabled = True
        audit_action = "enabled"
        undo = {"action": "disable_rule", "resource_id": rule_id}
    else:
        rule.enabled = False
        audit_action = "disabled"
        undo = {"action": "test_enable_rule", "resource_id": rule_id}
    add_audit(
        session,
        "forward_rule",
        rule.id,
        rule.name,
        audit_action,
        f"Forward rule {audit_action} from Action Center: {rule.name}",
    )
    await session.commit()
    return {"action": action, "changed": True, "resource_id": rule_id, "undo": undo}
