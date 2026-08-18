"""A proposal is inert until a person allows it, and then runs the button's own path.

The safety properties, as assertions: nothing executes without approval, nothing
unrunnable can be proposed, an expired proposal cannot be approved, a decided one
cannot be re-decided, and an approval whose execution failed does not read as a
rejection.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import AuditLog, RemediationProposal
from services.operations.remediation_proposals import (
    ALLOWED_ACTIONS,
    MAX_PENDING_PROPOSALS,
    ProposalConflict,
    ProposalError,
    decide_proposal,
    list_proposals,
    propose_remediation,
)


@pytest.fixture
def session(db_session: AsyncSession) -> AsyncSession:
    return db_session


async def _propose(session: AsyncSession, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "action": "retry_outbox",
        "resource_id": 7,
        "reason": "outbox record 7 has been retrying for 40 minutes with the same 503",
        "proposed_by": "hookprobe",
    }
    kwargs.update(overrides)
    return await propose_remediation(session, **kwargs)


class _Executor:
    """Stand-in for run_remediation, so tests can see whether it ran."""

    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result if result is not None else {"action": "retry_outbox", "changed": True, "resource_id": 7}
        self.error = error

    async def __call__(self, _session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> _Executor:
    import services.operations.remediation as remediation

    spy = _Executor()
    monkeypatch.setattr(remediation, "run_remediation", spy)
    return spy


# ── Nothing runs without approval ─────────────────────────────────────────────


class TestProposalIsInert:
    async def test_proposing_executes_nothing(self, session: AsyncSession, executor: _Executor) -> None:
        """The whole point. A proposal is a row, not an action."""
        proposal = await _propose(session)

        assert proposal["status"] == "pending"
        assert executor.calls == []

    async def test_approval_runs_the_action_center_path(self, session: AsyncSession, executor: _Executor) -> None:
        proposal = await _propose(session)

        decided = await decide_proposal(session, proposal_id=proposal["id"], approve=True, actor="alice")

        assert decided["status"] == "approved"
        assert decided["result"]["changed"] is True
        assert executor.calls == [{"action": "retry_outbox", "resource_id": 7, "resource_type": None, "batch_size": 50}]

    async def test_rejection_runs_nothing_and_says_who(self, session: AsyncSession, executor: _Executor) -> None:
        proposal = await _propose(session)

        decided = await decide_proposal(session, proposal_id=proposal["id"], approve=False, actor="bob")

        assert decided["status"] == "rejected"
        assert decided["decided_by"] == "bob"
        assert executor.calls == []


# ── Only runnable proposals can exist ─────────────────────────────────────────


class TestValidation:
    async def test_an_unknown_action_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ProposalError, match="Unsupported action"):
            await _propose(session, action="rm_minus_rf")

    async def test_the_proposable_actions_are_exactly_the_executable_ones(self) -> None:
        """Two lists would drift, and the drift would show up as a failed approval."""
        from services.operations.remediation import run_remediation

        assert "retry_outbox" in ALLOWED_ACTIONS
        assert "acknowledge" in ALLOWED_ACTIONS
        assert run_remediation is not None

    async def test_a_single_resource_action_without_a_resource_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ProposalError, match="resource_id is required"):
            await _propose(session, action="retry_outbox", resource_id=None)

    async def test_acknowledge_needs_a_resource_type(self, session: AsyncSession) -> None:
        with pytest.raises(ProposalError, match="resource_type is required"):
            await _propose(session, action="acknowledge", resource_id=3, resource_type=None)

    async def test_an_unknown_resource_type_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ProposalError, match="Unsupported resource_type"):
            await _propose(session, action="acknowledge", resource_id=3, resource_type="database")

    async def test_a_proposal_without_a_reason_is_refused(self, session: AsyncSession) -> None:
        """A proposal nobody can review is a proposal nobody should approve."""
        with pytest.raises(ProposalError, match="needs a reason"):
            await _propose(session, reason="   ")

    async def test_a_batch_action_needs_no_resource(self, session: AsyncSession) -> None:
        proposal = await _propose(session, action="retry_dead_letters", resource_id=None)

        assert proposal["status"] == "pending"
        assert proposal["resource_id"] is None


# ── Bounded ───────────────────────────────────────────────────────────────────


class TestBounds:
    async def test_the_same_suggestion_cannot_queue_twice(self, session: AsyncSession) -> None:
        """An agent in a retry loop must not fill a human's review queue."""
        first = await _propose(session)

        with pytest.raises(ProposalConflict, match=f"Proposal #{first['id']} already suggests"):
            await _propose(session)

    async def test_a_decided_proposal_stops_blocking_the_same_suggestion(
        self, session: AsyncSession, executor: _Executor
    ) -> None:
        first = await _propose(session)
        await decide_proposal(session, proposal_id=first["id"], approve=False, actor="bob")

        again = await _propose(session)

        assert again["id"] != first["id"]

    async def test_the_pending_queue_is_capped(self, session: AsyncSession) -> None:
        for index in range(MAX_PENDING_PROPOSALS):
            await _propose(session, resource_id=1000 + index)

        with pytest.raises(ProposalConflict, match="already awaiting review"):
            await _propose(session, resource_id=99)

    async def test_a_long_reason_is_truncated_not_rejected(self, session: AsyncSession) -> None:
        proposal = await _propose(session, reason="x" * 5000)

        assert len(proposal["reason"]) == 2000

    async def test_the_ttl_is_clamped(self, session: AsyncSession) -> None:
        proposal = await _propose(session, ttl_hours=100_000)

        assert proposal["expires_at"] is not None
        row = await session.get(RemediationProposal, proposal["id"])
        assert row is not None
        assert row.expires_at <= utcnow() + timedelta(hours=168)


# ── Expiry and idempotency ────────────────────────────────────────────────────


class TestExpiry:
    async def _expire(self, session: AsyncSession, proposal_id: int) -> None:
        row = await session.get(RemediationProposal, proposal_id)
        assert row is not None
        row.expires_at = utcnow() - timedelta(minutes=1)
        await session.commit()

    async def test_an_expired_proposal_cannot_be_approved(self, session: AsyncSession, executor: _Executor) -> None:
        """A stale suggestion reasoned about state that has moved on."""
        proposal = await _propose(session)
        await self._expire(session, proposal["id"])

        with pytest.raises(ProposalConflict, match="expired"):
            await decide_proposal(session, proposal_id=proposal["id"], approve=True, actor="alice")

        assert executor.calls == []
        row = await session.get(RemediationProposal, proposal["id"])
        assert row is not None and row.status == "expired"

    async def test_an_expired_pending_row_reads_as_expired(self, session: AsyncSession) -> None:
        """Enforced on read, so a stopped scheduler cannot make it look approvable."""
        proposal = await _propose(session)
        await self._expire(session, proposal["id"])

        assert [item["status"] for item in await list_proposals(session)] == ["expired"]
        assert await list_proposals(session, status="pending") == []

    async def test_a_decided_proposal_cannot_be_decided_again(self, session: AsyncSession, executor: _Executor) -> None:
        proposal = await _propose(session)
        await decide_proposal(session, proposal_id=proposal["id"], approve=True, actor="alice")

        with pytest.raises(ProposalConflict, match="already approved"):
            await decide_proposal(session, proposal_id=proposal["id"], approve=True, actor="alice")

        assert len(executor.calls) == 1, "a second approval must not run the action twice"

    async def test_deciding_a_missing_proposal_says_so(self, session: AsyncSession) -> None:
        with pytest.raises(ProposalError, match="was not found"):
            await decide_proposal(session, proposal_id=4242, approve=True, actor="alice")


# ── Approval and outcome are separate facts ───────────────────────────────────


class TestOutcome:
    async def test_a_failed_execution_is_not_a_rejection(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Collapsing them is how somebody re-approves a broken action forever."""
        import services.operations.remediation as remediation

        monkeypatch.setattr(remediation, "run_remediation", _Executor(error=RuntimeError("outbox is gone")))
        proposal = await _propose(session)

        decided = await decide_proposal(session, proposal_id=proposal["id"], approve=True, actor="alice")

        assert decided["status"] == "failed"
        assert "outbox is gone" in decided["result"]["error"]
        assert decided["decided_by"] == "alice", "the person did allow it"

    async def test_an_action_that_changed_nothing_is_still_approved(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.operations.remediation as remediation

        monkeypatch.setattr(remediation, "run_remediation", _Executor(result={"changed": False}))
        proposal = await _propose(session)

        decided = await decide_proposal(session, proposal_id=proposal["id"], approve=True, actor="alice")

        assert decided["status"] == "approved"
        assert decided["result"]["changed"] is False


# ── The audit trail ───────────────────────────────────────────────────────────


class TestAudit:
    async def _actions(self, session: AsyncSession) -> list[tuple[str, str]]:
        rows = (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        return [(row.action, row.actor) for row in rows]

    async def test_the_proposal_and_the_approval_are_both_recorded(
        self, session: AsyncSession, executor: _Executor
    ) -> None:
        proposal = await _propose(session)
        await decide_proposal(session, proposal_id=proposal["id"], approve=True, actor="alice")

        assert await self._actions(session) == [("propose", "hookprobe"), ("approve", "alice")]

    async def test_a_rejection_records_the_rejecter(self, session: AsyncSession, executor: _Executor) -> None:
        proposal = await _propose(session)
        await decide_proposal(session, proposal_id=proposal["id"], approve=False, actor="bob")

        assert ("reject", "bob") in await self._actions(session)
