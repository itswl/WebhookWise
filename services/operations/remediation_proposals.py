"""Propose an Action Center command; a person allows it; the existing path runs it.

The three rules this module exists to keep:

1. **A proposal cannot do anything.** Creating one writes a row. Only
   `decide_proposal(approve=True)` executes, and it executes by calling
   `run_remediation` — the same function the dashboard button calls. The set of
   things that can happen to this deployment is unchanged by adding a proposer.
2. **A proposal that could not run cannot be created.** Arguments are validated
   by constructing a `RemediationRequest`, the exact model the HTTP endpoint
   validates. An action outside that literal set, or a single-resource action
   with no resource, is rejected at proposal time rather than discovered by the
   operator who approved it.
3. **Approval and outcome are separate facts.** `approved` means a person
   allowed it; `failed` means it was allowed and the execution raised. Collapsing
   those is how somebody re-approves a broken action forever.

Bounded on purpose: one pending proposal per action+resource, a global pending
ceiling, a required reason, and an expiry enforced when read and when decided —
never by a sweeper, because an unrun scheduler must not be able to make a stale
proposal approvable.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal, cast, get_args

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.logger import get_logger
from models import RemediationProposal
from schemas.operations import RemediationAction, RemediationRequest
from services.operations.audit_logger import add_audit

logger = get_logger("operations.remediation_proposals")

# The proposable actions ARE the executable actions, read off the executor's own
# type. A second hand-written list here would drift, and the drift would only
# show up as an approval that fails.
ALLOWED_ACTIONS: frozenset[str] = frozenset(get_args(RemediationAction))
ALLOWED_RESOURCE_TYPES: frozenset[str] = frozenset({"webhook_event", "incident"})

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
FAILED = "failed"

DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 168
MAX_REASON_CHARS = 2000
# A review queue nobody can read is a review queue nobody reads. The per-action
# uniqueness rule already bounds a looping proposer; this bounds a creative one.
MAX_PENDING_PROPOSALS = 50


class ProposalError(Exception):
    """The proposal is not acceptable, and the message says why to a human."""


class ProposalConflict(ProposalError):
    """The proposal collides with one that already exists."""


def proposal_to_dict(row: RemediationProposal) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "batch_size": row.batch_size,
        "reason": row.reason,
        "proposed_by": row.proposed_by,
        "status": row.status,
        "expires_at": utc_isoformat(row.expires_at),
        "decided_by": row.decided_by,
        "decided_at": utc_isoformat(row.decided_at) if row.decided_at else None,
        "result": dict(row.result or {}),
        "verify_status": str(row.verify_status or ""),
        "verify_detail": dict(row.verify_detail) if isinstance(row.verify_detail, dict) else None,
        "verified_at": utc_isoformat(row.verified_at) if row.verified_at else None,
        "created_at": utc_isoformat(row.created_at),
    }


def _validated_request(
    *, action: str, resource_type: str | None, resource_id: int | None, batch_size: int
) -> RemediationRequest:
    """Reject anything the executor would reject, at proposal time."""
    if action not in ALLOWED_ACTIONS:
        raise ProposalError(f"Unsupported action {action!r}; allowed: {', '.join(sorted(ALLOWED_ACTIONS))}")
    if resource_type is not None and resource_type not in ALLOWED_RESOURCE_TYPES:
        raise ProposalError(
            f"Unsupported resource_type {resource_type!r}; allowed: {', '.join(sorted(ALLOWED_RESOURCE_TYPES))}"
        )
    try:
        # Cast after the membership checks above: the strings are narrowed by
        # those checks, and pydantic re-validates everything on construction.
        return RemediationRequest(
            action=cast(RemediationAction, action),
            resource_type=cast(Literal["webhook_event", "incident"] | None, resource_type),
            resource_id=resource_id,
            batch_size=batch_size,
        )
    except ValueError as error:
        raise ProposalError(f"Invalid remediation arguments: {error}") from error


async def propose_remediation(
    session: AsyncSession,
    *,
    action: str,
    reason: str,
    resource_type: str | None = None,
    resource_id: int | None = None,
    batch_size: int = 50,
    proposed_by: str = "agent",
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> dict[str, Any]:
    """Record an inert suggestion. Raises ProposalError when it is not reviewable."""
    request = _validated_request(
        action=action, resource_type=resource_type, resource_id=resource_id, batch_size=batch_size
    )
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise ProposalError("A proposal needs a reason an operator can review")

    pending_total = (
        await session.execute(
            select(func.count()).select_from(RemediationProposal).where(RemediationProposal.status == PENDING)
        )
    ).scalar_one()
    if int(pending_total or 0) >= MAX_PENDING_PROPOSALS:
        raise ProposalConflict(
            f"{pending_total} proposals are already awaiting review (limit {MAX_PENDING_PROPOSALS}); "
            "decide some before adding more"
        )

    duplicate = (
        await session.execute(
            select(RemediationProposal.id).where(
                RemediationProposal.status == PENDING,
                RemediationProposal.action == request.action,
                RemediationProposal.resource_type == request.resource_type,
                RemediationProposal.resource_id == request.resource_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ProposalConflict(f"Proposal #{duplicate} already suggests this action for this resource")

    row = RemediationProposal(
        action=request.action,
        resource_type=request.resource_type,
        resource_id=request.resource_id,
        batch_size=request.batch_size,
        reason=cleaned_reason[:MAX_REASON_CHARS],
        proposed_by=(proposed_by.strip() or "agent")[:100],
        status=PENDING,
        expires_at=utcnow() + timedelta(hours=max(1, min(int(ttl_hours), MAX_TTL_HOURS))),
        result={},
        created_at=utcnow(),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as error:
        # The partial unique index caught a race the count above could not.
        await session.rollback()
        raise ProposalConflict("An identical proposal is already awaiting review") from error

    add_audit(
        session,
        "remediation",
        row.id,
        row.action,
        "propose",
        f"{row.proposed_by} proposed {row.action} (awaiting approval): {cleaned_reason[:200]}",
        actor=row.proposed_by,
    )
    await session.commit()
    logger.info(
        "[Proposal] %s proposed action=%s resource=%s/%s id=%s",
        row.proposed_by,
        row.action,
        row.resource_type,
        row.resource_id,
        row.id,
    )
    return proposal_to_dict(row)


async def list_proposals(
    session: AsyncSession, *, status: str = "", limit: int = 50, now: Any = None
) -> list[dict[str, Any]]:
    """Proposals newest first. Pending rows past their expiry read as expired.

    The expiry is applied here rather than by a background sweep, so a stopped
    scheduler cannot leave a stale proposal looking approvable.
    """
    moment = now or utcnow()
    statement = select(RemediationProposal).order_by(RemediationProposal.id.desc()).limit(max(1, min(limit, 200)))
    if status.strip():
        statement = statement.where(RemediationProposal.status == status.strip().lower())
    rows = (await session.execute(statement)).scalars().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = proposal_to_dict(row)
        if row.status == PENDING and row.expires_at <= moment:
            item["status"] = EXPIRED
        items.append(item)
    if status.strip().lower() == PENDING:
        return [item for item in items if item["status"] == PENDING]
    return items


async def decide_proposal(
    session: AsyncSession,
    *,
    proposal_id: int,
    approve: bool,
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Approve (and execute) or reject one proposal. Idempotent by status."""
    row = await session.get(RemediationProposal, proposal_id, with_for_update=True)
    if row is None:
        raise ProposalError(f"Proposal #{proposal_id} was not found")
    if row.status != PENDING:
        raise ProposalConflict(f"Proposal #{proposal_id} is already {row.status}")

    now = utcnow()
    if row.expires_at <= now:
        row.status = EXPIRED
        row.decided_at = now
        row.result = {"changed": False, "reason": "The proposal expired before it was decided"}
        add_audit(
            session,
            "remediation",
            row.id,
            row.action,
            "expire",
            f"Proposal #{row.id} ({row.action}) expired before a decision",
            actor=actor,
        )
        await session.commit()
        raise ProposalConflict(f"Proposal #{proposal_id} expired at {utc_isoformat(row.expires_at)}")

    row.decided_by = actor[:100]
    row.decided_at = now

    if not approve:
        row.status = REJECTED
        row.result = {"changed": False, "reason": "Rejected by operator"}
        add_audit(
            session,
            "remediation",
            row.id,
            row.action,
            "reject",
            f"{actor} rejected proposal #{row.id} ({row.action})",
            actor=actor,
        )
        await session.commit()
        return proposal_to_dict(row)

    # Approved. Executed through the button's own path — not a second executor.
    from services.operations.remediation import run_remediation

    try:
        result = await run_remediation(
            session,
            action=row.action,
            resource_id=row.resource_id,
            resource_type=row.resource_type,
            batch_size=row.batch_size,
        )
    except Exception as error:  # noqa: BLE001 - the failure is the record, not a 500
        # An approval that failed to execute is not a rejection: the person did
        # allow it, and hiding that would make the audit trail lie.
        row.status = FAILED
        row.result = {"changed": False, "error": f"{type(error).__name__}: {error}"}
        add_audit(
            session,
            "remediation",
            row.id,
            row.action,
            "approve",
            f"{actor} approved proposal #{row.id} ({row.action}); execution failed: {error}",
            actor=actor,
        )
        await session.commit()
        logger.error("[Proposal] Approved proposal %s failed to execute: %s", row.id, error, exc_info=True)
        return proposal_to_dict(row)

    row.status = APPROVED
    row.result = dict(result)
    from services.operations.remediation_verification import SCHEDULED, schedule_verification_best_effort
    from services.operations.remediation_verification import (
        configured_verify_delay_seconds as _verify_delay,
    )

    verify_armed = bool(result.get("changed")) and _verify_delay() > 0
    if verify_armed:
        # Marked before the commit so a scheduling failure after it stays
        # visible: a proposal stuck at "scheduled" with no verdict says the
        # readback never ran, which beats silently never verifying.
        row.verify_status = SCHEDULED
    add_audit(
        session,
        "remediation",
        row.id,
        row.action,
        "approve",
        f"{actor} approved proposal #{row.id} ({row.action}); changed={result.get('changed')}",
        actor=actor,
    )
    await session.commit()
    if verify_armed:
        await schedule_verification_best_effort(
            action=row.action,
            resource_id=row.resource_id,
            resource_type=row.resource_type,
            proposal_id=row.id,
            execution_result=dict(result),
        )
    logger.info(
        "[Proposal] %s approved proposal=%s action=%s changed=%s", actor, row.id, row.action, result.get("changed")
    )
    return proposal_to_dict(row)
