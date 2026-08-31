"""Same-target readback after an executed remediation.

Absorbed from Flawless/CISRE's closing stance: API success, model claims, or a
previous health check do not equal fixed infrastructure. ``run_remediation``
returning ``changed=True`` means an API call worked — not that the replayed
dead letters completed, that the requeued outbox row went out, or that the
re-enabled rule now delivers. This module closes that gap: a delay after
execution, a worker reads the SAME target back and records a verdict —

- ``verified``      the target's own state confirms the fix held;
- ``unrecovered``   the target still shows the condition (or never moved);
- ``unverifiable``  there is nothing to read back (row gone, empty batch).

The verdict lands on the proposal row when the execution came from one, and in
the audit trail always, so an approval whose fix did not hold surfaces in the
Action Center instead of in next week's incident. Scheduling is best-effort by
design: a proposal stuck at ``scheduled`` with no verdict is itself a visible
symptom (the verification never ran), preferred over failing the execution
path a person just clicked.
"""

from __future__ import annotations

from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.events import record_signal
from db.session import session_scope
from models import ForwardOutbox, ForwardRule, Incident, RemediationProposal, WebhookEvent
from services.operations.audit_logger import add_audit
from services.webhooks.types import ForwardOutboxStatus, WebhookProcessingStatus

logger = get_logger("operations.remediation_verification")

VERIFIED = "verified"
UNRECOVERED = "unrecovered"
UNVERIFIABLE = "unverifiable"
SCHEDULED = "scheduled"

_SCHEDULING_ERRORS = (OSError, RedisError, RuntimeError, TimeoutError, TypeError, ValueError)


def configured_verify_delay_seconds() -> int:
    """Seconds to wait before the readback; 0 disables verification."""
    from core.app_context import get_config_manager
    from services.operations import runtime_settings as rt

    cfg = get_config_manager().tasks
    return int(
        rt.override_or(
            "REMEDIATION_VERIFY_DELAY_SECONDS",
            int(getattr(cfg, "REMEDIATION_VERIFY_DELAY_SECONDS", 0) or 0),
        )
    )


async def _readback_outbox(session: AsyncSession, resource_id: int | None) -> tuple[str, dict[str, Any]]:
    record = await session.get(ForwardOutbox, int(resource_id or 0))
    if record is None:
        return UNVERIFIABLE, {"reason": "outbox row not found"}
    status = str(record.status or "")
    if status == ForwardOutboxStatus.SENT:
        return VERIFIED, {"outbox_status": status}
    # pending/retrying after the whole verification window is not "in
    # progress", it is "not recovered yet" — the Flawless stance, kept.
    return UNRECOVERED, {"outbox_status": status}


async def _readback_replayed_events(
    session: AsyncSession, execution_result: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    ids = [int(i) for i in (execution_result or {}).get("scheduled_event_ids") or []]
    if not ids:
        return UNVERIFIABLE, {"reason": "nothing was replayed"}
    rows = (
        await session.execute(select(WebhookEvent.id, WebhookEvent.processing_status).where(WebhookEvent.id.in_(ids)))
    ).all()
    statuses = {int(event_id): str(status or "") for event_id, status in rows}
    completed = [i for i in ids if statuses.get(i) == WebhookProcessingStatus.COMPLETED]
    missing = [i for i in ids if i not in statuses]
    stuck = [i for i in ids if i in statuses and statuses[i] != WebhookProcessingStatus.COMPLETED]
    detail: dict[str, Any] = {"replayed": len(ids), "completed": len(completed)}
    if stuck:
        detail["not_completed"] = len(stuck)
        detail["sample_event_ids"] = stuck[:5]
        detail["sample_statuses"] = sorted({statuses[i] for i in stuck[:5]})
    if missing:
        # Retention may legitimately remove a replayed event before readback;
        # count it rather than guess a verdict from an absent row.
        detail["missing"] = len(missing)
    if len(completed) == len(ids):
        return VERIFIED, detail
    return UNRECOVERED, detail


async def _readback_incident_summaries(
    session: AsyncSession, execution_result: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    ids = [int(i) for i in (execution_result or {}).get("incident_ids") or []]
    if not ids:
        return UNVERIFIABLE, {"reason": "no incident summaries were retried"}
    rows = (await session.execute(select(Incident.id, Incident.summary_status).where(Incident.id.in_(ids)))).all()
    statuses = {int(incident_id): str(status or "") for incident_id, status in rows}
    completed = [i for i in ids if statuses.get(i) == "completed"]
    detail: dict[str, Any] = {"retried": len(ids), "completed": len(completed)}
    pending = [i for i in ids if statuses.get(i, "") != "completed"]
    if pending:
        detail["not_completed"] = len(pending)
        detail["sample_incident_ids"] = pending[:5]
    if len(completed) == len(ids):
        return VERIFIED, detail
    return UNRECOVERED, detail


async def _readback_rule(session: AsyncSession, action: str, resource_id: int | None) -> tuple[str, dict[str, Any]]:
    rule = await session.get(ForwardRule, int(resource_id or 0))
    if rule is None:
        return UNVERIFIABLE, {"reason": "forward rule not found"}
    expected = action == "test_enable_rule"
    detail = {"enabled": bool(rule.enabled), "expected": expected}
    return (VERIFIED, detail) if bool(rule.enabled) is expected else (UNRECOVERED, detail)


async def _readback_acknowledge(
    session: AsyncSession, resource_type: str | None, resource_id: int | None
) -> tuple[str, dict[str, Any]]:
    from services.operations.workflow import get_resource

    resource = await get_resource(session, str(resource_type or ""), int(resource_id or 0))
    if resource is None:
        return UNVERIFIABLE, {"reason": "resource not found"}
    status = str(getattr(resource, "workflow_status", "") or "")
    detail = {"workflow_status": status}
    return (VERIFIED, detail) if status == "acknowledged" else (UNRECOVERED, detail)


async def _readback(
    session: AsyncSession,
    *,
    action: str,
    resource_id: int | None,
    resource_type: str | None,
    execution_result: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    if action == "retry_outbox":
        return await _readback_outbox(session, resource_id)
    if action in {"retry_dead_letters", "retry_stuck_events"}:
        return await _readback_replayed_events(session, execution_result)
    if action == "retry_incident_summaries":
        return await _readback_incident_summaries(session, execution_result)
    if action in {"test_enable_rule", "disable_rule"}:
        return await _readback_rule(session, action, resource_id)
    if action == "acknowledge":
        return await _readback_acknowledge(session, resource_type, resource_id)
    # A new remediation action without a readback is legal but must be visible:
    # unverifiable is a verdict, not an error.
    return UNVERIFIABLE, {"reason": f"no readback defined for {action}"}


async def verify_remediation(
    *,
    action: str,
    resource_id: int | None = None,
    resource_type: str | None = None,
    proposal_id: int | None = None,
    execution_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the target back and record the verdict; returns the verdict dict."""
    async with session_scope() as session:
        status, detail = await _readback(
            session,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            execution_result=execution_result,
        )
        if proposal_id is not None:
            row = await session.get(RemediationProposal, proposal_id)
            if row is not None:
                row.verify_status = status
                row.verify_detail = detail
                row.verified_at = utcnow()
        summary_bits = ", ".join(f"{key}={value}" for key, value in sorted(detail.items()))
        add_audit(
            session,
            "remediation",
            proposal_id if proposal_id is not None else resource_id,
            action,
            "verify",
            f"readback {status} for {action}: {summary_bits}"[:500],
            actor="system",
        )
    record_signal("remediation.verify", status, {"remediation.action": action})
    log = logger.warning if status == UNRECOVERED else logger.info
    log(
        "[RemediationVerify] action=%s proposal_id=%s resource_id=%s verdict=%s detail=%s",
        action,
        proposal_id,
        resource_id,
        status,
        detail,
    )
    return {"verify_status": status, "verify_detail": detail}


async def schedule_verification_best_effort(
    *,
    action: str,
    resource_id: int | None,
    resource_type: str | None,
    proposal_id: int | None,
    execution_result: dict[str, Any] | None,
) -> int:
    """Arm the readback task; returns the delay, or 0 when disabled/failed.

    Best-effort on purpose: the execution a person just approved must not fail
    because the verification could not be scheduled. The cost is honest — a
    proposal left at ``scheduled`` with no verdict says the readback never ran.
    """
    delay = configured_verify_delay_seconds()
    if delay <= 0:
        return 0
    try:
        from services.operations import taskiq_retry_scheduler

        await taskiq_retry_scheduler.schedule_remediation_verification(
            delay_seconds=delay,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            proposal_id=proposal_id,
            execution_result=execution_result,
        )
    except _SCHEDULING_ERRORS as error:
        logger.error(
            "[RemediationVerify] could not schedule readback action=%s proposal_id=%s: %s",
            action,
            proposal_id,
            error,
            exc_info=True,
        )
        return 0
    return delay
