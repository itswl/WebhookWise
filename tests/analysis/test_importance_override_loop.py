"""The loop: a correction an operator makes must reach the next occurrence."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import WebhookEvent
from services.analysis.importance_overrides import (
    OVERRIDE_KEY,
    apply_override,
    forget_override,
    list_overrides,
    remember_override,
)


@pytest.fixture
def session(db_session):
    return db_session


@pytest.mark.asyncio
async def test_a_correction_reaches_the_next_occurrence(session: AsyncSession) -> None:
    """This is the whole point. Before it, the same condition fired an hour
    later and the model called it `low` again, because nothing in the analysis
    path had ever read a correction."""
    await remember_override(session, alert_hash="h-pay-5xx", importance="high", actor="alice")
    await session.commit()

    fresh = {"importance": "low", "summary": "gateway errors"}
    result = await apply_override(session, alert_hash="h-pay-5xx", analysis=fresh)

    assert result["importance"] == "high"
    assert result["summary"] == "gateway errors", "only the importance is overridden"


@pytest.mark.asyncio
async def test_the_override_declares_itself(session: AsyncSession) -> None:
    """An importance the model did not produce must never look like one it did,
    or the operator ends up arguing with a judgement nobody made."""
    await remember_override(session, alert_hash="h1", importance="high", actor="alice")
    await session.commit()

    result = await apply_override(session, alert_hash="h1", analysis={"importance": "low"})

    marker = result[OVERRIDE_KEY]
    assert marker["applied"] is True
    assert marker["model_importance"] == "low", "what the model actually said is preserved"
    assert marker["importance"] == "high"
    assert marker["actor"] == "alice"


@pytest.mark.asyncio
async def test_it_touches_only_the_condition_that_was_corrected(session: AsyncSession) -> None:
    """The reason this is not few-shot in the prompt: a correction must not
    move judgements on alerts nobody corrected."""
    await remember_override(session, alert_hash="h-corrected", importance="high")
    await session.commit()

    other = await apply_override(session, alert_hash="h-untouched", analysis={"importance": "low"})

    assert other["importance"] == "low"
    assert OVERRIDE_KEY not in other


@pytest.mark.asyncio
async def test_correcting_twice_replaces_rather_than_races(session: AsyncSession) -> None:
    """Which correction applies must never be a question of insert order."""
    await remember_override(session, alert_hash="h1", importance="high", actor="alice")
    await session.commit()
    await remember_override(session, alert_hash="h1", importance="low", actor="bob")
    await session.commit()

    rows = await list_overrides(session)
    mine = [r for r in rows if r["alert_hash"] == "h1"]
    assert len(mine) == 1, "one override per condition"
    assert mine[0]["importance"] == "low" and mine[0]["actor"] == "bob"


@pytest.mark.asyncio
async def test_hits_are_counted_so_a_stale_override_can_be_found(session: AsyncSession) -> None:
    """An override nobody has hit since it was set is the one worth
    questioning. Without a count there is no way to ask."""
    await remember_override(session, alert_hash="h1", importance="high")
    await session.commit()

    for _ in range(3):
        await apply_override(session, alert_hash="h1", analysis={"importance": "low"})
    await session.commit()

    row = next(r for r in await list_overrides(session) if r["alert_hash"] == "h1")
    assert row["hit_count"] == 3
    assert row["last_applied_at"] is not None


@pytest.mark.asyncio
async def test_an_override_can_be_taken_back(session: AsyncSession) -> None:
    await remember_override(session, alert_hash="h1", importance="high")
    await session.commit()
    row = next(r for r in await list_overrides(session) if r["alert_hash"] == "h1")

    assert await forget_override(session, override_id=int(row["id"])) is True

    after = await apply_override(session, alert_hash="h1", analysis={"importance": "low"})
    assert after["importance"] == "low", "the model's judgement stands again"


@pytest.mark.asyncio
async def test_a_lookup_failure_degrades_to_the_model(session: AsyncSession) -> None:
    """An override is an improvement, not a dependency: it must never be the
    reason an alert is dropped."""
    result = await apply_override(session, alert_hash="", analysis={"importance": "low"})
    assert result["importance"] == "low"


@pytest.mark.asyncio
async def test_correcting_an_alert_writes_the_override(session: AsyncSession) -> None:
    """End to end from the operator's side: the dashboard's 改判等级 button must
    be what creates the override, not a separate admin step nobody performs."""
    from services.operations.workflow import add_feedback

    event = WebhookEvent(source="prometheus", timestamp=utcnow(), importance="low", alert_hash="h-live")
    session.add(event)
    await session.commit()

    await add_feedback(
        session,
        resource_type="webhook_event",
        resource_id=int(event.id),
        verdict="incorrect",
        corrected_importance="high",
        corrected_event_type=None,
        comment=None,
        actor="alice",
    )

    override = (await list_overrides(session))[0]
    assert override["alert_hash"] == "h-live"
    assert override["importance"] == "high"
    assert event.importance == "high", "and the alert in hand changes too"
