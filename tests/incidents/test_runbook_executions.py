"""Manual runbook execution extraction, state, persistence, and API contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow


async def _seed_incident_and_runbook(
    session: AsyncSession,
    *,
    candidate_ref: str = "wiki:checkout-recovery",
    status: str = "published",
) -> tuple[int, str]:
    from models import Incident, KBDocument

    incident = Incident(
        title="checkout latency",
        status="active",
        source="grafana",
        started_at=utcnow(),
        alert_count=2,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    session.add_all(
        [
            incident,
            KBDocument(
                title="Checkout recovery",
                source_ref=candidate_ref,
                chunk_index=0,
                content=(
                    "# Checkout recovery\n"
                    "- [ ] Confirm the affected environment\n"
                    "1. Inspect the latest deployment\n"
                    "```sh\n"
                    "- this line is inert code, not a checklist step\n"
                    "```\n"
                ),
                content_hash="a" * 64,
                tags={"kind": "runbook", "service": "checkout"},
                status=status,
            ),
            KBDocument(
                title="Checkout recovery",
                source_ref=candidate_ref,
                chunk_index=1,
                content="* Roll back manually if the deployment is suspect\n",
                content_hash="b" * 64,
                tags={"kind": "runbook", "service": "checkout"},
                status=status,
            ),
        ]
    )
    await session.commit()
    return int(incident.id), candidate_ref


def test_extracts_markdown_lists_as_bounded_inert_manual_steps() -> None:
    from services.incidents.runbooks import extract_manual_runbook_steps

    content = "\n".join(
        [
            "- [ ] Check the incident",
            "* [x] Template checkbox state is reset",
            "1. Validate the service",
            "2) Ask the owner to approve rollback",
            "```bash",
            "- never parse a command block as a step",
            "```",
            "Plain prose is ignored.",
            *[f"- Extra manual step {index}" for index in range(40)],
        ]
    )

    steps = extract_manual_runbook_steps(content)

    assert len(steps) == 30
    assert steps[0] == {
        "index": 0,
        "text": "Check the incident",
        "completed": False,
        "completed_at": None,
        "completed_by": None,
    }
    assert steps[1]["text"] == "Template checkbox state is reset"
    assert steps[2]["text"] == "Validate the service"
    assert all("command block" not in str(step["text"]) for step in steps)


@pytest.mark.asyncio
async def test_start_is_idempotent_and_marks_used_without_claiming_effectiveness(
    db_session: AsyncSession,
) -> None:
    from models import AuditLog, IncidentIntelligenceFeedback, RunbookExecution
    from services.incidents.runbooks import start_runbook_execution

    incident_id, candidate_ref = await _seed_incident_and_runbook(db_session)

    first, first_created = await start_runbook_execution(
        db_session,
        incident_id=incident_id,
        candidate_ref=candidate_ref,
        actor="alice",
    )
    second, second_created = await start_runbook_execution(
        db_session,
        incident_id=incident_id,
        candidate_ref=candidate_ref,
        actor="bob",
    )

    execution_count = int(await db_session.scalar(select(func.count(RunbookExecution.id))) or 0)
    feedback = (
        await db_session.execute(
            select(IncidentIntelligenceFeedback).where(
                IncidentIntelligenceFeedback.incident_id == incident_id,
                IncidentIntelligenceFeedback.candidate_ref == candidate_ref,
            )
        )
    ).scalar_one()
    audits = list(
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.resource_type == "runbook_execution").order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert execution_count == 1
    assert [step["text"] for step in first.steps] == [
        "Confirm the affected environment",
        "Inspect the latest deployment",
        "Roll back manually if the deployment is suspect",
    ]
    assert feedback.recommendation_type == "runbook"
    assert feedback.verdict == "used"
    assert feedback.actor == "alice"
    assert first.effectiveness is None
    assert [audit.action for audit in audits] == ["runbook_started"]


@pytest.mark.asyncio
async def test_start_rejects_missing_incident_and_unpublished_document(
    db_session: AsyncSession,
) -> None:
    from services.incidents.runbooks import (
        RunbookExecutionNotFoundError,
        start_runbook_execution,
    )

    incident_id, candidate_ref = await _seed_incident_and_runbook(db_session, status="draft")

    with pytest.raises(RunbookExecutionNotFoundError, match="Published runbook"):
        await start_runbook_execution(
            db_session,
            incident_id=incident_id,
            candidate_ref=candidate_ref,
            actor="alice",
        )
    with pytest.raises(RunbookExecutionNotFoundError, match="Incident"):
        await start_runbook_execution(
            db_session,
            incident_id=999_999,
            candidate_ref=candidate_ref,
            actor="alice",
        )


@pytest.mark.asyncio
async def test_start_accepts_kb_id_candidate_without_source_reference(
    db_session: AsyncSession,
) -> None:
    from models import Incident, KBDocument
    from services.incidents.runbooks import start_runbook_execution

    incident = Incident(
        title="queue saturation",
        status="active",
        source="prometheus",
        started_at=utcnow(),
        alert_count=2,
    )
    document = KBDocument(
        title="Queue recovery",
        source_ref=None,
        chunk_index=0,
        content="- Drain the queue manually",
        content_hash="c" * 64,
        tags={"kind": "runbook"},
        status="published",
    )
    db_session.add_all([incident, document])
    await db_session.commit()

    execution, created = await start_runbook_execution(
        db_session,
        incident_id=int(incident.id),
        candidate_ref=f"kb:{document.id}",
        actor="alice",
    )

    assert created is True
    assert execution.candidate_ref == f"kb:{document.id}"
    assert execution.steps[0]["text"] == "Drain the queue manually"


@pytest.mark.asyncio
async def test_updates_copy_steps_enforce_state_machine_and_store_effectiveness_separately(
    db_session: AsyncSession,
) -> None:
    from models import AuditLog, IncidentIntelligenceFeedback
    from services.incidents.runbooks import (
        RunbookExecutionConflictError,
        start_runbook_execution,
        update_runbook_execution,
    )

    incident_id, candidate_ref = await _seed_incident_and_runbook(db_session)
    execution, _created = await start_runbook_execution(
        db_session,
        incident_id=incident_id,
        candidate_ref=candidate_ref,
        actor="alice",
    )
    original_steps = execution.steps

    execution = await update_runbook_execution(
        db_session,
        incident_id=incident_id,
        execution_id=int(execution.id),
        changes={"step_index": 0, "step_completed": True},
        actor="bob",
    )

    assert execution.steps is not original_steps
    assert original_steps[0]["completed"] is False
    assert execution.steps[0]["completed"] is True
    assert execution.steps[0]["completed_by"] == "bob"
    assert execution.steps[0]["completed_at"]
    assert execution.steps[1]["completed"] is False
    assert execution.status == "in_progress"

    execution = await update_runbook_execution(
        db_session,
        incident_id=incident_id,
        execution_id=int(execution.id),
        changes={
            "status": "completed",
            "effectiveness": "unknown",
            "notes": "One optional step was not needed.",
        },
        actor="bob",
    )
    assert execution.status == "completed"
    assert execution.completed_at is not None
    assert execution.effectiveness == "unknown"

    execution = await update_runbook_execution(
        db_session,
        incident_id=incident_id,
        execution_id=int(execution.id),
        changes={"effectiveness": "effective"},
        actor="carol",
    )
    assert execution.effectiveness == "effective"

    feedback = (
        await db_session.execute(
            select(IncidentIntelligenceFeedback).where(
                IncidentIntelligenceFeedback.incident_id == incident_id,
                IncidentIntelligenceFeedback.candidate_ref == candidate_ref,
            )
        )
    ).scalar_one()
    assert feedback.verdict == "used"

    with pytest.raises(RunbookExecutionConflictError, match="transition"):
        await update_runbook_execution(
            db_session,
            incident_id=incident_id,
            execution_id=int(execution.id),
            changes={"status": "in_progress"},
            actor="carol",
        )
    with pytest.raises(RunbookExecutionConflictError, match="in progress"):
        await update_runbook_execution(
            db_session,
            incident_id=incident_id,
            execution_id=int(execution.id),
            changes={"step_index": 0, "step_completed": False},
            actor="carol",
        )

    audit_actions = list(
        (
            await db_session.execute(
                select(AuditLog.action).where(AuditLog.resource_type == "runbook_execution").order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert audit_actions == [
        "runbook_started",
        "runbook_step",
        "runbook_completed",
        "runbook_reviewed",
    ]


@pytest.mark.asyncio
async def test_failed_execution_can_resume_then_be_abandoned(
    db_session: AsyncSession,
) -> None:
    from services.incidents.runbooks import start_runbook_execution, update_runbook_execution

    incident_id, candidate_ref = await _seed_incident_and_runbook(db_session)
    execution, _created = await start_runbook_execution(
        db_session,
        incident_id=incident_id,
        candidate_ref=candidate_ref,
        actor="alice",
    )
    execution = await update_runbook_execution(
        db_session,
        incident_id=incident_id,
        execution_id=int(execution.id),
        changes={"effectiveness": "unknown"},
        actor="alice",
    )
    assert execution.status == "in_progress"
    assert execution.effectiveness == "unknown"

    execution = await update_runbook_execution(
        db_session,
        incident_id=incident_id,
        execution_id=int(execution.id),
        changes={"status": "failed", "effectiveness": "ineffective"},
        actor="alice",
    )
    assert execution.status == "failed"
    assert execution.effectiveness == "ineffective"

    execution = await update_runbook_execution(
        db_session,
        incident_id=incident_id,
        execution_id=int(execution.id),
        changes={"status": "in_progress"},
        actor="alice",
    )
    assert execution.status == "in_progress"
    assert execution.effectiveness == "ineffective"
    assert execution.completed_at is None

    execution = await update_runbook_execution(
        db_session,
        incident_id=incident_id,
        execution_id=int(execution.id),
        changes={"status": "abandoned"},
        actor="alice",
    )
    assert execution.status == "abandoned"
    assert execution.completed_at is not None


@pytest.mark.asyncio
async def test_intelligence_and_list_include_runbook_executions(
    db_session: AsyncSession,
) -> None:
    from services.incidents.intelligence import get_incident_intelligence
    from services.incidents.runbooks import (
        RunbookExecutionNotFoundError,
        list_runbook_executions,
        start_runbook_execution,
    )

    incident_id, candidate_ref = await _seed_incident_and_runbook(db_session)
    execution, _created = await start_runbook_execution(
        db_session,
        incident_id=incident_id,
        candidate_ref=candidate_ref,
        actor="alice",
    )

    listed = await list_runbook_executions(db_session, incident_id)
    intelligence = await get_incident_intelligence(db_session, incident_id)

    assert [item["id"] for item in listed] == [execution.id]
    assert intelligence is not None
    assert intelligence["runbook_executions"][0]["candidate_ref"] == candidate_ref
    assert intelligence["runbook_executions"][0]["steps"][0]["text"] == "Confirm the affected environment"
    with pytest.raises(RunbookExecutionNotFoundError, match="Incident"):
        await list_runbook_executions(db_session, 999_999)


@pytest.mark.asyncio
async def test_runbook_execution_api_contract(
    db_session: AsyncSession,
) -> None:
    from api.v1.incidents import (
        list_runbook_executions_endpoint,
        start_runbook_execution_endpoint,
        update_runbook_execution_endpoint,
    )
    from schemas.intelligence import (
        RunbookExecutionStartRequest,
        RunbookExecutionUpdateRequest,
    )

    incident_id, candidate_ref = await _seed_incident_and_runbook(db_session)
    started = await start_runbook_execution_endpoint(
        incident_id,
        RunbookExecutionStartRequest(candidate_ref=candidate_ref, actor="dashboard"),
        db_session,
    )
    started_body = json.loads(started.body)
    execution_id = int(started_body["data"]["id"])
    repeated = await start_runbook_execution_endpoint(
        incident_id,
        RunbookExecutionStartRequest(candidate_ref=candidate_ref, actor="dashboard"),
        db_session,
    )
    listed = await list_runbook_executions_endpoint(incident_id, db_session)
    stepped = await update_runbook_execution_endpoint(
        incident_id,
        execution_id,
        RunbookExecutionUpdateRequest(step_index=0, step_completed=True, actor="dashboard"),
        db_session,
    )
    completed = await update_runbook_execution_endpoint(
        incident_id,
        execution_id,
        RunbookExecutionUpdateRequest(
            status="completed",
            effectiveness="unknown",
            notes="Done manually.",
            actor="dashboard",
        ),
        db_session,
    )
    conflict = await update_runbook_execution_endpoint(
        incident_id,
        execution_id,
        RunbookExecutionUpdateRequest(status="in_progress", actor="dashboard"),
        db_session,
    )
    missing = await update_runbook_execution_endpoint(
        incident_id,
        999_999,
        RunbookExecutionUpdateRequest(status="abandoned", actor="dashboard"),
        db_session,
    )

    assert started.status_code == 201
    assert repeated.status_code == 200
    assert json.loads(listed.body)["data"][0]["id"] == execution_id
    assert json.loads(stepped.body)["data"]["steps"][0]["completed"] is True
    assert json.loads(completed.body)["data"]["effectiveness"] == "unknown"
    assert conflict.status_code == 409
    assert missing.status_code == 404


def test_runbook_update_schema_requires_an_action_and_complete_step_pair() -> None:
    from schemas.intelligence import RunbookExecutionUpdateRequest

    with pytest.raises(ValidationError):
        RunbookExecutionUpdateRequest(actor="dashboard")
    with pytest.raises(ValidationError):
        RunbookExecutionUpdateRequest(step_index=0)
    with pytest.raises(ValidationError):
        RunbookExecutionUpdateRequest(step_completed=True)
    with pytest.raises(ValidationError):
        RunbookExecutionUpdateRequest(step_index=30, step_completed=True)


def test_runbook_execution_routes_have_read_and_write_auth_dependencies() -> None:
    from api.v1.incidents import incidents_router

    expected = {
        ("GET", "/incidents/{incident_id}/runbook-executions"): "verify_api_key",
        ("POST", "/incidents/{incident_id}/runbook-executions"): "verify_admin_write",
        (
            "PUT",
            "/incidents/{incident_id}/runbook-executions/{execution_id}",
        ): "verify_admin_write",
    }
    for (method, path), auth_dependency in expected.items():
        route = next(
            route
            for route in incidents_router.routes
            if path == str(getattr(route, "path", "")) and method in getattr(route, "methods", set())
        )
        dependency_names = {
            getattr(dependency.call, "__name__", str(dependency.call))
            for dependency in getattr(route, "dependant", object()).dependencies
        }
        assert "check_admin_rate_limit_dep" in dependency_names
        assert auth_dependency in dependency_names


def test_runbook_service_contains_no_command_execution_surface() -> None:
    source = (Path(__file__).resolve().parents[2] / "services" / "incidents" / "runbooks.py").read_text()

    assert "subprocess" not in source
    assert "create_subprocess" not in source
    assert "os.system" not in source
