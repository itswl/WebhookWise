"""Same-target readback: execution success is not recovery until the target agrees."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import ForwardOutbox, ForwardRule, Incident, RemediationProposal, WebhookEvent
from services.operations import remediation_verification as rv


def _proposal(**overrides: Any) -> RemediationProposal:
    defaults: dict[str, Any] = {
        "action": "retry_outbox",
        "resource_type": "forward_outbox",
        "resource_id": 1,
        "batch_size": 50,
        "reason": "test",
        "proposed_by": "agent",
        "status": "approved",
        "expires_at": utcnow() + timedelta(hours=1),
        "result": {},
    }
    defaults.update(overrides)
    return RemediationProposal(**defaults)


def _outbox(status: str) -> ForwardOutbox:
    return ForwardOutbox(
        idempotency_key=f"k-{status}",
        event_type="webhook_forward",
        target_type="webhook",
        target_url="https://example.com/hook",
        status=status,
    )


async def _wire_context(db_app_context_session_factory) -> None:  # noqa: ANN001 - fixture passthrough
    """session_scope inside the module resolves the test DB via the app context."""


# ── readback verdicts ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outbox_sent_is_verified_and_pending_is_not(db_session: AsyncSession) -> None:
    sent = _outbox("sent")
    stuck = _outbox("retrying")
    db_session.add_all([sent, stuck])
    await db_session.commit()

    status_ok, detail_ok = await rv._readback_outbox(db_session, sent.id)
    status_bad, detail_bad = await rv._readback_outbox(db_session, stuck.id)

    assert (status_ok, detail_ok["outbox_status"]) == (rv.VERIFIED, "sent")
    assert (status_bad, detail_bad["outbox_status"]) == (rv.UNRECOVERED, "retrying")


@pytest.mark.asyncio
async def test_missing_outbox_row_is_unverifiable(db_session: AsyncSession) -> None:
    status, detail = await rv._readback_outbox(db_session, 424242)
    assert status == rv.UNVERIFIABLE
    assert "not found" in detail["reason"]


@pytest.mark.asyncio
async def test_replayed_events_verify_only_when_all_completed(db_session: AsyncSession) -> None:
    done = WebhookEvent(source="s", processing_status="completed", timestamp=utcnow())
    still_dead = WebhookEvent(source="s", processing_status="dead_letter", timestamp=utcnow())
    db_session.add_all([done, still_dead])
    await db_session.commit()

    all_done, _ = await rv._readback_replayed_events(db_session, {"scheduled_event_ids": [done.id]})
    partial, detail = await rv._readback_replayed_events(
        db_session, {"scheduled_event_ids": [done.id, still_dead.id]}
    )

    assert all_done == rv.VERIFIED
    assert partial == rv.UNRECOVERED
    assert detail["completed"] == 1 and detail["not_completed"] == 1
    assert detail["sample_event_ids"] == [still_dead.id]


@pytest.mark.asyncio
async def test_empty_replay_batch_is_unverifiable(db_session: AsyncSession) -> None:
    status, _ = await rv._readback_replayed_events(db_session, {"scheduled_event_ids": []})
    assert status == rv.UNVERIFIABLE


@pytest.mark.asyncio
async def test_rule_readback_checks_the_expected_state(db_session: AsyncSession) -> None:
    rule = ForwardRule(name="r1", enabled=False, target_type="webhook", target_url="https://example.com")
    db_session.add(rule)
    await db_session.commit()

    disabled_ok, _ = await rv._readback_rule(db_session, "disable_rule", rule.id)
    enable_failed, detail = await rv._readback_rule(db_session, "test_enable_rule", rule.id)

    assert disabled_ok == rv.VERIFIED
    assert enable_failed == rv.UNRECOVERED
    assert detail == {"enabled": False, "expected": True}


@pytest.mark.asyncio
async def test_incident_summaries_verify_on_completed(db_session: AsyncSession) -> None:
    done = Incident(title="a", status="active", summary_status="completed", started_at=utcnow())
    pending = Incident(title="b", status="active", summary_status="pending", started_at=utcnow())
    db_session.add_all([done, pending])
    await db_session.commit()

    ok, _ = await rv._readback_incident_summaries(db_session, {"incident_ids": [done.id]})
    bad, detail = await rv._readback_incident_summaries(db_session, {"incident_ids": [done.id, pending.id]})

    assert ok == rv.VERIFIED
    assert bad == rv.UNRECOVERED and detail["not_completed"] == 1


@pytest.mark.asyncio
async def test_unknown_action_is_unverifiable_not_an_error(db_session: AsyncSession) -> None:
    status, detail = await rv._readback(
        db_session, action="brand_new_action", resource_id=None, resource_type=None, execution_result=None
    )
    assert status == rv.UNVERIFIABLE
    assert "brand_new_action" in detail["reason"]


# ── verdict persistence ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_remediation_lands_the_verdict_on_the_proposal(
    db_app_context_session_factory, monkeypatch
) -> None:
    async with db_app_context_session_factory() as session:
        outbox = _outbox("sent")
        session.add(outbox)
        await session.flush()
        proposal = _proposal(resource_id=outbox.id)
        session.add(proposal)
        await session.commit()
        outbox_id, proposal_id = outbox.id, proposal.id

    signals: list[tuple[str, str]] = []
    monkeypatch.setattr(rv, "record_signal", lambda name, state, attrs=None: signals.append((name, state)))

    verdict = await rv.verify_remediation(
        action="retry_outbox", resource_id=outbox_id, proposal_id=proposal_id, execution_result={}
    )

    assert verdict["verify_status"] == rv.VERIFIED
    async with db_app_context_session_factory() as session:
        row = await session.get(RemediationProposal, proposal_id)
        assert row is not None
        assert row.verify_status == rv.VERIFIED
        assert row.verified_at is not None
        assert row.verify_detail == {"outbox_status": "sent"}
    assert ("remediation.verify", "verified") in signals


@pytest.mark.asyncio
async def test_unrecovered_verdict_is_recorded_as_such(db_app_context_session_factory, monkeypatch) -> None:
    async with db_app_context_session_factory() as session:
        outbox = _outbox("exhausted")
        session.add(outbox)
        await session.flush()
        proposal = _proposal(resource_id=outbox.id)
        session.add(proposal)
        await session.commit()
        outbox_id, proposal_id = outbox.id, proposal.id

    monkeypatch.setattr(rv, "record_signal", lambda *a, **k: None)
    verdict = await rv.verify_remediation(
        action="retry_outbox", resource_id=outbox_id, proposal_id=proposal_id, execution_result={}
    )

    assert verdict["verify_status"] == rv.UNRECOVERED
    async with db_app_context_session_factory() as session:
        row = await session.get(RemediationProposal, proposal_id)
        assert row is not None and row.verify_status == rv.UNRECOVERED


# ── scheduling wiring ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_is_best_effort_and_reports_disabled(monkeypatch, temp_config) -> None:
    monkeypatch.setattr(temp_config.tasks, "REMEDIATION_VERIFY_DELAY_SECONDS", 0, raising=False)
    assert await rv.schedule_verification_best_effort(
        action="retry_outbox", resource_id=1, resource_type=None, proposal_id=None, execution_result={}
    ) == 0


@pytest.mark.asyncio
async def test_schedule_failure_never_raises(monkeypatch, temp_config) -> None:
    monkeypatch.setattr(temp_config.tasks, "REMEDIATION_VERIFY_DELAY_SECONDS", 60, raising=False)
    from services.operations import taskiq_retry_scheduler

    monkeypatch.setattr(
        taskiq_retry_scheduler,
        "schedule_remediation_verification",
        AsyncMock(side_effect=RuntimeError("scheduler down")),
    )

    delay = await rv.schedule_verification_best_effort(
        action="retry_outbox", resource_id=1, resource_type=None, proposal_id=7, execution_result={}
    )

    assert delay == 0  # failure reported, not raised — the execution already happened


@pytest.mark.asyncio
async def test_schedule_success_arms_the_task_with_the_execution_result(monkeypatch, temp_config) -> None:
    monkeypatch.setattr(temp_config.tasks, "REMEDIATION_VERIFY_DELAY_SECONDS", 120, raising=False)
    from services.operations import taskiq_retry_scheduler

    spy = AsyncMock()
    monkeypatch.setattr(taskiq_retry_scheduler, "schedule_remediation_verification", spy)

    delay = await rv.schedule_verification_best_effort(
        action="retry_dead_letters",
        resource_id=None,
        resource_type=None,
        proposal_id=3,
        execution_result={"scheduled_event_ids": [5, 6]},
    )

    assert delay == 120
    kwargs = spy.await_args.kwargs
    assert kwargs["delay_seconds"] == 120
    assert kwargs["proposal_id"] == 3
    assert kwargs["execution_result"] == {"scheduled_event_ids": [5, 6]}
