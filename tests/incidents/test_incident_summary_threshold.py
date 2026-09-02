"""The incident-summary eligibility rule honours the importance floor."""

from __future__ import annotations

from datetime import datetime

import pytest

from models import Incident
from services.incidents import summary as summary_module


def _incident(alert_count: int, importance: str | None) -> Incident:
    incident = Incident(title="t", source="grafana", status="quiet")
    incident.alert_count = alert_count
    incident.top_importance = importance
    incident.summary_analysis = None
    return incident


@pytest.mark.parametrize(
    ("floor", "importance", "expected"),
    [
        ("low", "low", "pending"),
        ("medium", "low", "skipped"),
        ("medium", "medium", "pending"),
        ("high", "medium", "skipped"),
        ("high", "high", "pending"),
        ("high", None, "pending"),  # unknown importance fails open
        ("high", "weird", "pending"),
    ],
)
def test_summary_floor_decides_eligibility(
    monkeypatch: pytest.MonkeyPatch, floor: str, importance: str | None, expected: str
) -> None:
    monkeypatch.setattr(summary_module, "_summary_min_importance", lambda: floor)
    incident = _incident(3, importance)
    summary_module.queue_summary_if_needed(incident, datetime(2026, 9, 2))
    assert incident.summary_status == expected
    if expected == "skipped":
        assert incident.summary_next_attempt_at is None
        assert "INCIDENT_SUMMARY_MIN_IMPORTANCE" in (incident.summary_last_error or "")


def test_singletons_stay_skipped_regardless_of_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary_module, "_summary_min_importance", lambda: "low")
    incident = _incident(1, "high")
    summary_module.queue_summary_if_needed(incident, datetime(2026, 9, 2))
    assert incident.summary_status == "skipped"
    assert "singleton" in (incident.summary_last_error or "")


def test_floor_reads_runtime_override_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.operations import runtime_settings as rt

    monkeypatch.setattr(rt, "override_or", lambda key, fallback: "medium")
    assert summary_module._summary_min_importance() == "medium"
    monkeypatch.setattr(rt, "override_or", lambda key, fallback: "nonsense")
    assert summary_module._summary_min_importance() == "low"
