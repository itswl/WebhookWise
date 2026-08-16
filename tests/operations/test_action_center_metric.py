"""The product's own faults, published where the existing alerting can page on them."""

from typing import Any

import pytest

from tests.helpers.metric_helpers import StubMetric


@pytest.mark.asyncio
async def test_open_items_are_published_by_kind_and_severity(monkeypatch) -> None:
    from services.operations import metrics_poller

    async def report(session: Any) -> dict[str, Any]:
        return {
            "items": [
                {"kind": "delivery_exhausted", "severity": "critical"},
                {"kind": "delivery_exhausted", "severity": "critical"},
                {"kind": "queue_backlog", "severity": "warning"},
            ]
        }

    calls = _stub(monkeypatch, metrics_poller, report)
    await metrics_poller._refresh_action_center()

    assert _value(calls, "delivery_exhausted", "critical") == 2
    assert _value(calls, "queue_backlog", "warning") == 1


@pytest.mark.asyncio
async def test_a_cleared_fault_comes_back_down(monkeypatch) -> None:
    """A gauge that never falls keeps an alert firing after the fix.

    That is how an operator learns to ignore an alert, which costs more than
    the alert was ever worth.
    """
    from services.operations import metrics_poller

    items: list[dict[str, Any]] = [{"kind": "delivery_exhausted", "severity": "critical"}]

    async def report(session: Any) -> dict[str, Any]:
        return {"items": items}

    calls = _stub(monkeypatch, metrics_poller, report)
    await metrics_poller._refresh_action_center()
    assert _value(calls, "delivery_exhausted", "critical") == 1

    items.clear()
    calls.clear()
    await metrics_poller._refresh_action_center()

    assert _value(calls, "delivery_exhausted", "critical") == 0


@pytest.mark.asyncio
async def test_a_failing_lookup_leaves_the_gauge_alone(monkeypatch) -> None:
    from services.operations import metrics_poller

    async def broken(session: Any) -> dict[str, Any]:
        raise RuntimeError("db down")

    _stub(monkeypatch, metrics_poller, broken)

    await metrics_poller._refresh_action_center()  # must not raise


class _FakeSessionScope:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _fake_session_scope() -> _FakeSessionScope:
    return _FakeSessionScope()


def _stub(monkeypatch: pytest.MonkeyPatch, poller: Any, report: Any) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr("services.operations.action_center.get_action_center", report)
    monkeypatch.setattr(poller, "session_scope", _fake_session_scope)
    monkeypatch.setattr(poller, "ACTION_CENTER_ACTIVE", StubMetric(calls, "action_center_open_count", record_action=False))
    poller._published_action_labels = set()
    return calls


def _value(calls: list[Any], kind: str, severity: str) -> float:
    """The last value set for one label pair."""
    for call in reversed(calls):
        args = call[1]
        if tuple(args) == (kind, severity):
            return float(call[-1])
    return -1.0
