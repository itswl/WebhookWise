"""Regression tests for the slimmed deep-analysis list payload.

The list endpoint must return a lightweight summary (cheap preview, no full
normalized_report, no raw analysis_result blob); the full record is fetched on
demand via the detail serializer.
"""

from __future__ import annotations

from models import DeepAnalysis


def test_summary_dict_is_lightweight_and_has_preview() -> None:
    from schemas.analysis import deep_analysis_to_summary_dict

    record = DeepAnalysis(
        id=1,
        webhook_event_id=5,
        engine="openclaw",
        status="completed",
        analysis_result={
            "summary": "disk full on prod-01",
            "root_cause": "x" * 5000,
            "_openclaw_text": "y" * 30000,  # large raw blob that must NOT be shipped
        },
    )
    data = deep_analysis_to_summary_dict(record)

    assert data["summary_preview"] == "disk full on prod-01"
    # Heavy fields are omitted from the list item.
    assert "analysis_result" not in data
    assert "normalized_report" not in data
    # The whole serialized item stays small regardless of blob size.
    import json

    assert len(json.dumps(data)) < 1000


def test_summary_preview_falls_back_to_root_cause_then_truncates() -> None:
    from schemas.analysis import deep_analysis_to_summary_dict

    record = DeepAnalysis(
        id=2,
        webhook_event_id=6,
        engine="local",
        status="completed",
        analysis_result={"root_cause": "z" * 1000},
    )
    data = deep_analysis_to_summary_dict(record)
    assert data["summary_preview"]
    assert len(data["summary_preview"]) <= 260


def test_full_dict_still_has_heavy_fields() -> None:
    from schemas.analysis import deep_analysis_to_dict

    record = DeepAnalysis(
        id=3,
        webhook_event_id=7,
        engine="openclaw",
        status="completed",
        analysis_result={"summary": "s", "root_cause": "rc"},
    )
    data = deep_analysis_to_dict(record)
    # Detail serializer keeps the full payload for on-demand fetch.
    assert "analysis_result" in data
    assert "normalized_report" in data
