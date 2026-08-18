"""Proposal endpoints (direct handler calls, in-memory sqlite).

The status codes carry meaning an operator acts on, so they are the contract
here: 409 for "already suggested / already decided", 400 for "not runnable", and
502 for "you approved it and it did not run" — that last one must never look
like a success.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.operations import RemediationProposalCreateRequest


@pytest.fixture
def session(db_session: AsyncSession) -> AsyncSession:
    return db_session


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


class _Executor:
    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result if result is not None else {"changed": True}
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


def _request(**overrides: Any) -> RemediationProposalCreateRequest:
    payload: dict[str, Any] = {
        "action": "retry_outbox",
        "resource_id": 7,
        "reason": "outbox 7 stuck on the same 503 for 40 minutes",
        "proposed_by": "hookprobe",
    }
    payload.update(overrides)
    return RemediationProposalCreateRequest(**payload)


@pytest.mark.asyncio
async def test_propose_then_approve_flow(session: AsyncSession, executor: _Executor) -> None:
    from api.v1 import operations as api

    created = _body(await api.propose_remediation_endpoint(_request(), session=session))
    assert created["success"] is True
    assert created["data"]["status"] == "pending"
    assert executor.calls == [], "proposing must not execute"

    listed = _body(await api.list_remediation_proposals_endpoint(status="pending", limit=50, session=session))
    assert [item["id"] for item in listed["data"]["items"]] == [created["data"]["id"]]

    approved = _body(await api.decide_remediation_proposal_endpoint(created["data"]["id"], "approve", session=session))
    assert approved["data"]["status"] == "approved"
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_rejecting_never_executes(session: AsyncSession, executor: _Executor) -> None:
    from api.v1 import operations as api

    created = _body(await api.propose_remediation_endpoint(_request(), session=session))
    rejected = _body(await api.decide_remediation_proposal_endpoint(created["data"]["id"], "reject", session=session))

    assert rejected["data"]["status"] == "rejected"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_a_duplicate_proposal_is_a_conflict(session: AsyncSession) -> None:
    from api.v1 import operations as api

    await api.propose_remediation_endpoint(_request(), session=session)
    response = await api.propose_remediation_endpoint(_request(), session=session)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_an_unrunnable_proposal_is_rejected_with_400(session: AsyncSession) -> None:
    from api.v1 import operations as api

    # Valid for the request model (resource_id present) but acknowledge also
    # needs a resource_type, which the service checks through RemediationRequest.
    response = await api.propose_remediation_endpoint(
        _request(action="acknowledge", resource_id=3, resource_type=None), session=session
    )

    assert response.status_code == 400
    assert "resource_type is required" in _body(response)["error"]


@pytest.mark.asyncio
async def test_deciding_twice_is_a_conflict(session: AsyncSession, executor: _Executor) -> None:
    from api.v1 import operations as api

    created = _body(await api.propose_remediation_endpoint(_request(), session=session))
    await api.decide_remediation_proposal_endpoint(created["data"]["id"], "approve", session=session)
    again = await api.decide_remediation_proposal_endpoint(created["data"]["id"], "approve", session=session)

    assert again.status_code == 409
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_a_missing_proposal_is_a_404(session: AsyncSession) -> None:
    from api.v1 import operations as api

    response = await api.decide_remediation_proposal_endpoint(4242, "approve", session=session)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_an_unknown_decision_is_a_404(session: AsyncSession, executor: _Executor) -> None:
    from api.v1 import operations as api

    created = _body(await api.propose_remediation_endpoint(_request(), session=session))
    response = await api.decide_remediation_proposal_endpoint(created["data"]["id"], "delete", session=session)

    assert response.status_code == 404
    assert executor.calls == []


@pytest.mark.asyncio
async def test_an_approval_that_failed_to_run_is_not_reported_as_success(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator has to learn that the thing they allowed did not happen."""
    import services.operations.remediation as remediation
    from api.v1 import operations as api

    monkeypatch.setattr(remediation, "run_remediation", _Executor(error=RuntimeError("outbox is gone")))
    created = _body(await api.propose_remediation_endpoint(_request(), session=session))

    response = await api.decide_remediation_proposal_endpoint(created["data"]["id"], "approve", session=session)

    assert response.status_code == 502
    assert "outbox is gone" in _body(response)["error"]
