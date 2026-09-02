"""List rows carry the recovery verdict the card builder already computes."""

from __future__ import annotations

from types import SimpleNamespace

from services.webhooks.query_service import _row_to_summary_dict


def _row(**overrides):
    base = {
        "id": 1,
        "request_id": "r",
        "source": "grafana",
        "source_connection_id": None,
        "client_ip": "1.2.3.4",
        "timestamp": None,
        "importance": "medium",
        "is_duplicate": False,
        "duplicate_of": None,
        "duplicate_count": 0,
        "forward_status": "sent",
        "summary": "s",
        "triage_verdict": "act_now",
        "triage_confidence": None,
        "created_at": None,
        "prev_alert_id": None,
        "prev_alert_timestamp": None,
        "workflow_status": "open",
        "assignee": None,
        "team": None,
        "acknowledged_at": None,
        "resolved_at": None,
        "sla_due_at": None,
        "parsed_status": None,
        "parsed_state": None,
        "parsed_alert_status": None,
        "parsed_event_status": None,
        "parsed_phase": None,
        "analysis_event_type": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_firing_row_is_not_a_recovery() -> None:
    assert _row_to_summary_dict(_row(parsed_status="firing"))["is_recovery"] is False


def test_resolved_status_marks_a_recovery() -> None:
    assert _row_to_summary_dict(_row(parsed_status="resolved"))["is_recovery"] is True


def test_chinese_recovery_event_type_marks_a_recovery() -> None:
    assert _row_to_summary_dict(_row(analysis_event_type="告警恢复"))["is_recovery"] is True


def test_projection_without_the_columns_defaults_to_false() -> None:
    row = _row()
    for attr in (
        "parsed_status",
        "parsed_state",
        "parsed_alert_status",
        "parsed_event_status",
        "parsed_phase",
        "analysis_event_type",
    ):
        delattr(row, attr)
    assert _row_to_summary_dict(row)["is_recovery"] is False
