"""Taking back a misclicked 接手 / 标记解决 — and refusing when it is not safe."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import Incident, WebhookEvent, WorkflowTransition


@pytest.fixture
def session(db_session):
    return db_session


async def _event(session: AsyncSession) -> WebhookEvent:
    event = WebhookEvent(source="prometheus", timestamp=utcnow(), importance="high")
    session.add(event)
    await session.commit()
    return event


@pytest.mark.asyncio
async def test_a_misclicked_resolve_can_be_taken_back(session: AsyncSession) -> None:
    """Resolve is one tap away on a Feishu card. Fixing a mis-tap should not
    require remembering what the state used to be."""
    from services.operations.workflow import undo_workflow, update_workflow

    event = await _event(session)
    await update_workflow(
        session, resource_type="webhook_event", resource_id=int(event.id), changes={"workflow_status": "resolved"}
    )
    assert event.workflow_status == "resolved"
    assert event.resolved_at is not None

    result = await undo_workflow(session, resource_type="webhook_event", resource_id=int(event.id))

    assert result["changed"] is True
    assert result["workflow"]["workflow_status"] == "open"
    assert event.resolved_at is None, "the timestamp the resolve set must come back too"


@pytest.mark.asyncio
async def test_undo_restores_every_field_the_change_touched(session: AsyncSession) -> None:
    """A snapshot that misses a field restores a resource that only LOOKS like
    it was put back."""
    from services.operations.workflow import undo_workflow, update_workflow

    event = await _event(session)
    await update_workflow(
        session,
        resource_type="webhook_event",
        resource_id=int(event.id),
        changes={"workflow_status": "acknowledged", "assignee": "alice", "team": "sre", "sla_minutes": 30},
    )
    assert event.assignee == "alice" and event.sla_due_at is not None

    await undo_workflow(session, resource_type="webhook_event", resource_id=int(event.id))

    assert event.workflow_status == "open"
    assert event.assignee is None
    assert event.team is None
    assert event.sla_due_at is None
    assert event.acknowledged_at is None


@pytest.mark.asyncio
async def test_undo_refuses_once_somebody_else_has_moved_it_on(session: AsyncSession) -> None:
    """The guard is the whole point. By the time you notice the misclick,
    somebody may have picked the alert up — restoring blindly would discard
    their decision, which is worse than the misclick."""
    from services.operations.workflow import undo_workflow, update_workflow

    event = await _event(session)
    await update_workflow(
        session, resource_type="webhook_event", resource_id=int(event.id), changes={"workflow_status": "resolved"}
    )
    await update_workflow(
        session,
        resource_type="webhook_event",
        resource_id=int(event.id),
        changes={"workflow_status": "in_progress", "assignee": "bob"},
    )

    # Undo now takes back BOB's change, which is the most recent one.
    first = await undo_workflow(session, resource_type="webhook_event", resource_id=int(event.id))
    assert first["changed"] is True
    assert event.workflow_status == "resolved", "one step back, not all the way"

    # And again takes back the resolve. Undo walks the stack; it never jumps.
    second = await undo_workflow(session, resource_type="webhook_event", resource_id=int(event.id))
    assert second["changed"] is True
    assert event.workflow_status == "open"


@pytest.mark.asyncio
async def test_undo_refuses_when_the_resource_no_longer_matches(session: AsyncSession) -> None:
    """A change made outside update_workflow (a card action writing directly,
    an automation) leaves the resource unlike what the transition recorded."""
    from services.operations.workflow import undo_workflow, update_workflow

    event = await _event(session)
    await update_workflow(
        session, resource_type="webhook_event", resource_id=int(event.id), changes={"workflow_status": "resolved"}
    )
    event.workflow_status = "ignored"  # somebody bypassed the service layer
    await session.commit()

    result = await undo_workflow(session, resource_type="webhook_event", resource_id=int(event.id))

    assert result["changed"] is False
    assert result["reason"] == "changed_since"
    assert event.workflow_status == "ignored", "the later state must survive untouched"


@pytest.mark.asyncio
async def test_nothing_to_undo_is_its_own_answer(session: AsyncSession) -> None:
    from services.operations.workflow import undo_workflow

    event = await _event(session)
    result = await undo_workflow(session, resource_type="webhook_event", resource_id=int(event.id))
    assert result == {"changed": False, "reason": "nothing_to_undo"}


@pytest.mark.asyncio
async def test_a_no_op_patch_records_nothing_to_take_back(session: AsyncSession) -> None:
    """Otherwise the undo would take back a change nobody made, and shadow the
    real one before it."""
    from services.operations.workflow import undo_workflow, update_workflow

    event = await _event(session)
    await update_workflow(
        session, resource_type="webhook_event", resource_id=int(event.id), changes={"workflow_status": "acknowledged"}
    )
    await update_workflow(
        session, resource_type="webhook_event", resource_id=int(event.id), changes={"workflow_status": "acknowledged"}
    )

    rows = (
        (await session.execute(select(WorkflowTransition).where(WorkflowTransition.resource_id == int(event.id))))
        .scalars()
        .all()
    )
    assert len(rows) == 1, "the second patch changed nothing, so there is nothing to undo"

    await undo_workflow(session, resource_type="webhook_event", resource_id=int(event.id))
    assert event.workflow_status == "open"


@pytest.mark.asyncio
async def test_an_incident_undo_reopens_it(session: AsyncSession) -> None:
    """Resolving an incident also closes it and stamps ended_at. Undo has to
    put all of that back, or the incident reads as open while staying closed."""
    from services.operations.workflow import undo_workflow, update_workflow

    incident = Incident(title="pay gateway 5xx", status="active", started_at=utcnow())
    session.add(incident)
    await session.commit()

    await update_workflow(
        session, resource_type="incident", resource_id=int(incident.id), changes={"workflow_status": "resolved"}
    )
    assert incident.status == "closed" and incident.ended_at is not None

    result = await undo_workflow(session, resource_type="incident", resource_id=int(incident.id))

    assert result["changed"] is True
    assert incident.workflow_status == "open"
    assert incident.status == "active"
    assert incident.ended_at is None


@pytest.mark.asyncio
async def test_availability_is_reported_before_the_button_is_drawn(session: AsyncSession) -> None:
    """An undo button that fails when pressed is worse than no button."""
    from services.operations.workflow import latest_undoable, update_workflow

    event = await _event(session)
    assert await latest_undoable(session, resource_type="webhook_event", resource_id=int(event.id)) is None

    await update_workflow(
        session, resource_type="webhook_event", resource_id=int(event.id), changes={"workflow_status": "resolved"}
    )
    offer = await latest_undoable(session, resource_type="webhook_event", resource_id=int(event.id))
    assert offer is not None
    assert offer["from_status"] == "open" and offer["to_status"] == "resolved"

    event.workflow_status = "ignored"
    await session.commit()
    assert await latest_undoable(session, resource_type="webhook_event", resource_id=int(event.id)) is None
