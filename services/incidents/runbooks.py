"""Manual runbook execution tracking for incidents.

This module only records operator progress. It never evaluates or executes the
text extracted from a knowledge-base document.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import (
    Incident,
    IncidentIntelligenceFeedback,
    KBDocument,
    RunbookExecution,
)
from services.operations.audit_logger import add_audit

_MAX_RUNBOOK_STEPS = 30
_MAX_RUNBOOK_CHUNKS = 64
_MAX_STEP_TEXT_LENGTH = 1_000
_KB_CANDIDATE_RE = re.compile(r"^kb:(?P<document_id>[1-9][0-9]*)$")
_CHECKBOX_STEP_RE = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+(?P<text>.+?)\s*$")
_ORDERED_STEP_RE = re.compile(r"^\s*\d{1,4}[.)]\s+(?P<text>.+?)\s*$")
_UNORDERED_STEP_RE = re.compile(r"^\s*[-*+]\s+(?P<text>.+?)\s*$")
_TERMINAL_STATUSES = {"completed", "failed", "abandoned"}
_ALLOWED_TRANSITIONS = {
    "in_progress": {"in_progress", "completed", "failed", "abandoned"},
    "failed": {"failed", "in_progress", "abandoned"},
    "completed": {"completed"},
    "abandoned": {"abandoned"},
}
_EFFECTIVENESS_VALUES = {"effective", "ineffective", "unknown"}


class RunbookExecutionNotFoundError(LookupError):
    """The requested incident, execution, or published runbook does not exist."""


class RunbookExecutionConflictError(ValueError):
    """The requested update violates the runbook execution state machine."""


@dataclass(frozen=True, slots=True)
class _PublishedRunbook:
    title: str
    contents: tuple[str, ...]


def extract_manual_runbook_steps(contents: str) -> list[dict[str, object]]:
    """Extract up to 30 Markdown list items as inert, manual checklist steps."""
    steps: list[dict[str, object]] = []
    fence_marker: str | None = None
    for line in contents.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue

        match = _CHECKBOX_STEP_RE.match(line) or _ORDERED_STEP_RE.match(line) or _UNORDERED_STEP_RE.match(line)
        if match is None:
            continue
        text = " ".join(match.group("text").split()).strip()
        if not text:
            continue
        steps.append(
            {
                "index": len(steps),
                "text": text[:_MAX_STEP_TEXT_LENGTH],
                "completed": False,
                "completed_at": None,
                "completed_by": None,
            }
        )
        if len(steps) >= _MAX_RUNBOOK_STEPS:
            break
    return steps


async def _load_published_runbook(
    session: AsyncSession,
    candidate_ref: str,
) -> _PublishedRunbook | None:
    document_id_match = _KB_CANDIDATE_RE.fullmatch(candidate_ref)
    if document_id_match is not None:
        rows = (
            await session.execute(
                select(
                    KBDocument.title,
                    KBDocument.content,
                    KBDocument.chunk_index,
                )
                .where(
                    KBDocument.id == int(document_id_match.group("document_id")),
                    KBDocument.status == "published",
                )
                .limit(1)
            )
        ).all()
    else:
        rows = (
            await session.execute(
                select(
                    KBDocument.title,
                    KBDocument.content,
                    KBDocument.chunk_index,
                )
                .where(
                    KBDocument.source_ref == candidate_ref,
                    KBDocument.status == "published",
                )
                .order_by(KBDocument.chunk_index.asc(), KBDocument.id.asc())
                .limit(_MAX_RUNBOOK_CHUNKS)
            )
        ).all()
    if not rows:
        return None
    return _PublishedRunbook(
        title=str(rows[0].title or "Runbook")[:300],
        contents=tuple(str(row.content or "") for row in rows),
    )


async def _mark_runbook_used(
    session: AsyncSession,
    *,
    incident_id: int,
    candidate_ref: str,
    actor: str,
) -> None:
    feedback = (
        await session.execute(
            select(IncidentIntelligenceFeedback).where(
                IncidentIntelligenceFeedback.incident_id == incident_id,
                IncidentIntelligenceFeedback.recommendation_type == "runbook",
                IncidentIntelligenceFeedback.candidate_ref == candidate_ref,
            )
        )
    ).scalar_one_or_none()
    if feedback is None:
        feedback = IncidentIntelligenceFeedback(
            incident_id=incident_id,
            recommendation_type="runbook",
            candidate_ref=candidate_ref,
            verdict="used",
            actor=actor,
        )
        session.add(feedback)
        return
    feedback.verdict = "used"
    feedback.actor = actor
    feedback.updated_at = utcnow()


def runbook_execution_response(execution: RunbookExecution) -> dict[str, object]:
    """Serialize a runbook execution without exposing mutable ORM JSON values."""
    return {
        "id": execution.id,
        "incident_id": execution.incident_id,
        "candidate_ref": execution.candidate_ref,
        "title": execution.title,
        "status": execution.status,
        "steps": [dict(step) for step in (execution.steps or [])],
        "effectiveness": execution.effectiveness,
        "notes": execution.notes,
        "actor": execution.actor,
        "started_at": utc_isoformat(execution.started_at),
        "completed_at": utc_isoformat(execution.completed_at),
        "updated_at": utc_isoformat(execution.updated_at),
    }


async def load_runbook_execution_responses(
    session: AsyncSession,
    incident_id: int,
) -> list[dict[str, object]]:
    """Load a bounded execution list when the caller already verified the incident."""
    rows = list(
        (
            await session.execute(
                select(RunbookExecution)
                .where(RunbookExecution.incident_id == incident_id)
                .order_by(RunbookExecution.started_at.desc(), RunbookExecution.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [runbook_execution_response(row) for row in rows]


async def list_runbook_executions(
    session: AsyncSession,
    incident_id: int,
) -> list[dict[str, object]]:
    """List manual runbook executions for an existing incident."""
    if await session.get(Incident, incident_id) is None:
        raise RunbookExecutionNotFoundError(f"Incident {incident_id} not found")
    return await load_runbook_execution_responses(session, incident_id)


async def start_runbook_execution(
    session: AsyncSession,
    *,
    incident_id: int,
    candidate_ref: str,
    actor: str,
) -> tuple[RunbookExecution, bool]:
    """Start one idempotent manual execution and mark the recommendation used."""
    actor = str(actor).strip()[:100] or "operator"
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise RunbookExecutionNotFoundError(f"Incident {incident_id} not found")

    existing = (
        await session.execute(
            select(RunbookExecution).where(
                RunbookExecution.incident_id == incident_id,
                RunbookExecution.candidate_ref == candidate_ref,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    runbook = await _load_published_runbook(session, candidate_ref)
    if runbook is None:
        raise RunbookExecutionNotFoundError(f"Published runbook {candidate_ref!r} not found")

    now = utcnow()
    execution = RunbookExecution(
        incident_id=incident_id,
        candidate_ref=candidate_ref,
        title=runbook.title,
        status="in_progress",
        steps=extract_manual_runbook_steps("\n\n".join(runbook.contents)),
        actor=actor,
        started_at=now,
        updated_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(execution)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                select(RunbookExecution).where(
                    RunbookExecution.incident_id == incident_id,
                    RunbookExecution.candidate_ref == candidate_ref,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False

    await _mark_runbook_used(
        session,
        incident_id=incident_id,
        candidate_ref=candidate_ref,
        actor=actor,
    )
    add_audit(
        session,
        "runbook_execution",
        int(execution.id),
        execution.title,
        "runbook_started",
        f"Runbook execution started for incident #{incident_id}: {execution.title}",
        actor=actor,
    )
    await session.commit()
    await session.refresh(execution)
    return execution, True


def _validate_transition(current: str, target: str) -> None:
    if target not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise RunbookExecutionConflictError(f"Runbook execution cannot transition from {current} to {target}")


def _audit_action(current: str, target: str, changed_fields: set[str]) -> str:
    if current != target:
        return {
            "completed": "runbook_completed",
            "failed": "runbook_failed",
            "abandoned": "runbook_abandoned",
            "in_progress": "runbook_resumed",
        }[target]
    if "effectiveness" in changed_fields:
        return "runbook_reviewed"
    if "step" in changed_fields:
        return "runbook_step"
    return "runbook_updated"


async def update_runbook_execution(
    session: AsyncSession,
    *,
    incident_id: int,
    execution_id: int,
    changes: Mapping[str, Any],
    actor: str,
) -> RunbookExecution:
    """Apply one serialized manual-progress update under an explicit state machine."""
    actor = str(actor).strip()[:100] or "operator"
    execution = (
        await session.execute(
            select(RunbookExecution)
            .where(
                RunbookExecution.id == execution_id,
                RunbookExecution.incident_id == incident_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if execution is None:
        raise RunbookExecutionNotFoundError(f"Runbook execution {execution_id} not found for incident {incident_id}")

    current_status = str(execution.status)
    requested_status = changes.get("status")
    target_status = str(requested_status) if requested_status is not None else current_status
    _validate_transition(current_status, target_status)

    step_index = changes.get("step_index")
    step_completed = changes.get("step_completed")
    steps = [dict(step) for step in (execution.steps or [])]
    changed_fields: set[str] = set()
    summaries: list[str] = []
    now = utcnow()

    if step_index is not None or step_completed is not None:
        if step_index is None or step_completed is None:
            raise RunbookExecutionConflictError("step_index and step_completed must be provided together")
        if target_status != "in_progress":
            raise RunbookExecutionConflictError("Runbook steps can only be changed while execution is in progress")
        index = int(step_index)
        if index < 0 or index >= len(steps):
            raise RunbookExecutionConflictError(f"Runbook step index {index} is out of range")
        completed = bool(step_completed)
        if bool(steps[index].get("completed")) != completed:
            steps[index] = {
                **steps[index],
                "completed": completed,
                "completed_at": utc_isoformat(now) if completed else None,
                "completed_by": actor if completed else None,
            }
            changed_fields.add("step")
            summaries.append(f"step {index + 1} {'completed' if completed else 'reopened'}")

    effectiveness = changes.get("effectiveness")
    if effectiveness is not None:
        normalized_effectiveness = str(effectiveness)
        if normalized_effectiveness not in _EFFECTIVENESS_VALUES:
            raise RunbookExecutionConflictError("Unsupported runbook effectiveness value")
        if normalized_effectiveness in {"effective", "ineffective"} and target_status not in _TERMINAL_STATUSES:
            raise RunbookExecutionConflictError(
                "Runbook effectiveness can only be rated after the execution reaches a terminal status"
            )
        if execution.effectiveness != normalized_effectiveness:
            execution.effectiveness = normalized_effectiveness
            changed_fields.add("effectiveness")
            summaries.append(f"effectiveness={normalized_effectiveness}")

    if "notes" in changes and execution.notes != changes.get("notes"):
        execution.notes = changes.get("notes")
        changed_fields.add("notes")
        summaries.append("notes updated")

    if "step" in changed_fields:
        # Assign a fresh list only after all validation has succeeded. SQLAlchemy
        # then tracks the JSON update, and rejected compound requests cannot
        # leave an in-memory ORM object carrying partially applied step state.
        execution.steps = steps

    if current_status != target_status:
        execution.status = target_status
        execution.completed_at = now if target_status in _TERMINAL_STATUSES else None
        if target_status == "in_progress" and execution.effectiveness is not None:
            execution.effectiveness = None
            changed_fields.add("effectiveness")
            summaries.append("effectiveness reset after resume")
        changed_fields.add("status")
        summaries.append(f"status {current_status}->{target_status}")

    if not changed_fields:
        return execution

    execution.updated_at = now
    add_audit(
        session,
        "runbook_execution",
        int(execution.id),
        execution.title,
        _audit_action(current_status, target_status, changed_fields),
        f"Runbook execution updated for incident #{incident_id}: {'; '.join(summaries)}",
        actor=actor,
    )
    await session.commit()
    await session.refresh(execution)
    return execution


__all__ = [
    "RunbookExecutionConflictError",
    "RunbookExecutionNotFoundError",
    "extract_manual_runbook_steps",
    "list_runbook_executions",
    "load_runbook_execution_responses",
    "runbook_execution_response",
    "start_runbook_execution",
    "update_runbook_execution",
]
