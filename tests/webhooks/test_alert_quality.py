from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import Incident, IncidentMember, SourceConnection, WebhookEvent
from services.operations import runtime_settings as rt
from services.webhooks import alert_quality
from services.webhooks.alert_quality import get_alert_quality_overview


@pytest.fixture(autouse=True)
def _clear_overview_cache():
    alert_quality._reset_overview_cache_for_tests()
    yield
    alert_quality._reset_overview_cache_for_tests()


def _connection(*, public_id: str, name: str, source_type: str = "grafana") -> SourceConnection:
    now = utcnow()
    return SourceConnection(
        public_id=public_id,
        name=name,
        source_type=source_type,
        token_hash=public_id.ljust(64, "0")[:64],
        token_hint=public_id[-6:],
        enabled=True,
        event_count=0,
        auth_failure_count=0,
        schema_change_count=0,
        created_by="test",
        created_at=now,
        updated_at=now,
    )


def _event(
    *,
    connection_id: int | None,
    request_id: str,
    hours_ago: int,
    parsed_data: dict[str, object],
    dedup_key: str,
    is_duplicate: bool = False,
    source: str = "grafana",
) -> WebhookEvent:
    timestamp = utcnow() - timedelta(hours=hours_ago)
    return WebhookEvent(
        source=source,
        source_connection_id=connection_id,
        request_id=request_id,
        timestamp=timestamp,
        raw_payload=json.dumps(parsed_data).encode(),
        parsed_data=parsed_data,
        ai_analysis={"event_type": "recovery"} if parsed_data.get("status") == "resolved" else {},
        alert_hash=dedup_key,
        dedup_key=dedup_key,
        importance="medium",
        processing_status="completed",
        is_duplicate=is_duplicate,
        duplicate_count=1,
        workflow_status="open",
    )


def _complete_payload(*, status: str = "firing") -> dict[str, object]:
    return {
        "status": status,
        "environment": "prod",
        "_alert_identity": {
            "source": "grafana",
            "name": "CheckoutDown",
            "service": "checkout",
            "severity": "warning",
        },
    }


async def test_alert_quality_reports_explainable_read_only_findings(
    db_session: AsyncSession,
) -> None:
    now = utcnow()
    active = _connection(public_id="src_quality_active", name="Production Grafana")
    empty = _connection(public_id="src_quality_empty", name="Unused Alertmanager", source_type="prometheus")
    active.schema_change_count = 2
    active.schema_changed_at = now - timedelta(hours=1)
    db_session.add_all([active, empty])
    await db_session.flush()

    firing_events = [
        _event(
            connection_id=int(active.id),
            request_id=f"quality-firing-{index}",
            hours_ago=10 - index,
            parsed_data=_complete_payload(),
            dedup_key="stable-checkout-key",
            is_duplicate=index > 0,
        )
        for index in range(3)
    ]
    unmatched_recovery = _event(
        connection_id=int(active.id),
        request_id="quality-unmatched-recovery",
        hours_ago=1,
        parsed_data={**_complete_payload(status="resolved"), "RuleName": "OtherAlert"},
        dedup_key="unmatched-recovery-key",
    )
    malformed = _event(
        connection_id=int(active.id),
        request_id="quality-malformed",
        hours_ago=1,
        parsed_data={"timestamp": (now + timedelta(days=1)).isoformat()},
        dedup_key="volatile-key",
    )
    db_session.add_all([*firing_events, unmatched_recovery, malformed])
    await db_session.flush()
    incident = Incident(
        title="Unattended checkout incident",
        status="active",
        source="grafana",
        source_connection_id=int(active.id),
        started_at=now - timedelta(hours=3),
        alert_count=1,
        workflow_status="open",
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    db_session.add(incident)
    await db_session.flush()

    result = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)

    assert result["read_only"] is True
    assert cast(dict[str, object], result["scan"])["event_truncated"] is False
    summary = cast(dict[str, Any], result["summary"])
    assert summary["events_scanned"] == 5
    assert summary["source_count"] == 2
    assert summary["no_data_source_count"] == 1
    assert float(summary["quality_score"]) < 100

    sources = cast(list[dict[str, Any]], result["sources"])
    active_result = next(item for item in sources if item["source_connection_id"] == active.id)
    finding_codes = {finding["code"] for finding in active_result["findings"]}
    assert {
        "missing_identity",
        "missing_service",
        "missing_environment",
        "missing_severity",
        "timestamp_anomaly",
        "no_recovery_signals",
        "schema_drift",
        "unattended_incidents",
    } <= finding_codes
    # The lone recovery has no firing side inside an incident, so it is a
    # standalone pair — informational, and never an unmatched_recovery finding.
    assert "unmatched_recovery" not in finding_codes
    assert active_result["recovery"] == {
        "events": 1,
        "matched": 0,
        "standalone": 1,
        "attributable": 0,
        "match_rate": 0.0,
    }
    assert active_result["unattended_incidents"] == 1

    empty_result = next(item for item in sources if item["source_connection_id"] == empty.id)
    assert empty_result["quality_score"] is None
    assert empty_result["grade"] == "no_data"
    assert [finding["code"] for finding in empty_result["findings"]] == ["no_recent_events"]


async def test_alert_quality_counts_incident_linked_recovery_as_matched(
    db_session: AsyncSession,
) -> None:
    connection = _connection(public_id="src_quality_match", name="Matched Grafana")
    db_session.add(connection)
    await db_session.flush()
    recovery = _event(
        connection_id=int(connection.id),
        request_id="quality-matched-recovery",
        hours_ago=1,
        parsed_data=_complete_payload(status="resolved"),
        dedup_key="matched-recovery-key",
    )
    db_session.add(recovery)
    await db_session.flush()
    incident = Incident(
        title="Recovered checkout incident",
        status="closed",
        source="grafana",
        source_connection_id=int(connection.id),
        started_at=utcnow() - timedelta(hours=2),
        ended_at=utcnow() - timedelta(hours=1),
        alert_count=1,
        workflow_status="resolved",
        acknowledged_at=utcnow() - timedelta(hours=2),
        assignee="alice",
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    db_session.add(incident)
    await db_session.flush()
    db_session.add(
        IncidentMember(
            incident_id=int(incident.id),
            event_id=int(recovery.id),
            event_timestamp=recovery.timestamp,
        )
    )
    await db_session.flush()

    result = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)
    source = cast(list[dict[str, Any]], result["sources"])[0]

    assert source["quality_score"] == 100
    assert source["grade"] == "healthy"
    assert source["recovery"] == {
        "events": 1,
        "matched": 1,
        "standalone": 0,
        "attributable": 1,
        "match_rate": 100.0,
    }
    assert source["findings"] == []


async def test_alert_quality_flags_conservative_identity_churn_for_unmanaged_source(
    db_session: AsyncSession,
) -> None:
    now = utcnow()
    events = [
        WebhookEvent(
            source="legacy-grafana",
            request_id=f"quality-churn-{index}",
            timestamp=now - timedelta(minutes=index),
            raw_payload=b"{}",
            parsed_data={
                "environment": "prod",
                "timestamp": int((now - timedelta(minutes=index)).timestamp() * 1000),
                "_alert_identity": {
                    "source": "grafana",
                    "name": "CheckoutLatency",
                    "service": "checkout",
                    "severity": "warning",
                },
            },
            ai_analysis={},
            alert_hash=f"alert-{index}",
            dedup_key=f"volatile-{index}",
            importance="medium",
            processing_status="completed",
            is_duplicate=False,
            duplicate_count=1,
            workflow_status="open",
        )
        for index in range(10)
    ]
    db_session.add_all(events)
    await db_session.flush()

    result = await get_alert_quality_overview(db_session, window_days=1, source_limit=100)
    source = cast(list[dict[str, Any]], result["sources"])[0]
    finding = next(item for item in source["findings"] if item["code"] == "identity_churn")

    assert source["managed"] is False
    assert source["credential_state"] == "unmanaged"
    assert source["identity"]["unique_dedup_keys"] == 10
    assert finding["rate"] == 100.0
    assert finding["evidence"] == {
        "unique_dedup_keys": 10,
        "identity_anchors": 1,
        "duplicate_rate": 0.0,
    }


async def test_alert_quality_serves_cached_overview_without_requerying(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection(public_id="src_quality_cache", name="Cached Grafana")
    db_session.add(connection)
    await db_session.flush()
    db_session.add(
        _event(
            connection_id=int(connection.id),
            request_id="quality-cache-event",
            hours_ago=1,
            parsed_data=_complete_payload(),
            dedup_key="cache-event-key",
        )
    )
    await db_session.flush()

    executed_statements = 0
    original_execute = db_session.execute

    async def counting_execute(*args: Any, **kwargs: Any) -> Any:
        nonlocal executed_statements
        executed_statements += 1
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(db_session, "execute", counting_execute)

    first = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)
    statements_after_first_call = executed_statements
    second = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)

    assert statements_after_first_call > 0
    assert executed_statements == statements_after_first_call
    assert second == first


async def test_alert_quality_sets_truncation_flags_when_scan_cap_is_hit(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(alert_quality, "_MAX_PAYLOAD_SCAN_EVENTS", 2)
    events = [
        _event(
            connection_id=None,
            request_id=f"quality-cap-{index}",
            hours_ago=index + 1,
            parsed_data=_complete_payload(),
            dedup_key=f"cap-key-{index}",
        )
        for index in range(5)
    ]
    db_session.add_all(events)
    await db_session.flush()

    result = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)

    scan = cast(dict[str, Any], result["scan"])
    summary = cast(dict[str, Any], result["summary"])
    assert scan["event_limit"] == 2
    assert scan["event_truncated"] is True
    assert summary["events_in_window"] == 5
    assert summary["events_scanned"] == 2
    source = cast(list[dict[str, Any]], result["sources"])[0]
    assert source["event_count"] == 5
    expected_oldest_scanned = sorted((event.timestamp for event in events), reverse=True)[1]
    assert scan["oldest_scanned_event_at"] == utc_isoformat(expected_oldest_scanned)


async def test_a_fire_and_resolve_pair_that_made_no_incident_is_not_a_defect(
    db_session: AsyncSession,
) -> None:
    """An incident needs two correlated non-recovery alerts, so a plain
    fire -> resolve pair can never join one. Calling that "unmatched" blamed the
    source for this system's own grouping policy — 87% of the flagged recoveries
    measured on 2026-09-02 were exactly this shape."""
    connection = _connection(public_id="src_quality_pair", name="Paired Grafana")
    db_session.add(connection)
    await db_session.flush()
    db_session.add_all(
        [
            _event(
                connection_id=int(connection.id),
                request_id="quality-pair-firing",
                hours_ago=2,
                parsed_data=_complete_payload(),
                dedup_key="paired-key",
            ),
            _event(
                connection_id=int(connection.id),
                request_id="quality-pair-recovery",
                hours_ago=1,
                parsed_data=_complete_payload(status="resolved"),
                dedup_key="paired-key",
            ),
        ]
    )
    await db_session.flush()

    result = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)

    source = cast(list[dict[str, Any]], result["sources"])[0]
    assert [finding["code"] for finding in source["findings"]] == []
    assert source["quality_score"] == 100
    assert source["recovery"] == {
        "events": 1,
        "matched": 0,
        "standalone": 1,
        "attributable": 0,
        "match_rate": 0.0,
    }
    summary = cast(dict[str, Any], result["summary"])
    assert cast(dict[str, Any], summary["recovery"])["standalone"] == 1


async def test_a_recovery_that_missed_its_open_incident_is_unmatched(
    db_session: AsyncSession,
) -> None:
    """The one shape a source can actually fix: the firing made an incident and
    the recovery, carrying the same identity, failed to reach it."""
    now = utcnow()
    connection = _connection(public_id="src_quality_missed", name="Missed Grafana")
    db_session.add(connection)
    await db_session.flush()
    firing = _event(
        connection_id=int(connection.id),
        request_id="quality-missed-firing",
        hours_ago=2,
        parsed_data=_complete_payload(),
        dedup_key="missed-key",
    )
    recovery = _event(
        connection_id=int(connection.id),
        request_id="quality-missed-recovery",
        hours_ago=1,
        parsed_data=_complete_payload(status="resolved"),
        dedup_key="missed-key",
    )
    db_session.add_all([firing, recovery])
    await db_session.flush()
    incident = Incident(
        title="Checkout incident",
        status="active",
        source="grafana",
        source_connection_id=int(connection.id),
        started_at=now - timedelta(hours=2),
        alert_count=2,
        workflow_status="open",
        acknowledged_at=now - timedelta(hours=2),
        assignee="alice",
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    db_session.add(incident)
    await db_session.flush()
    db_session.add(
        IncidentMember(
            incident_id=int(incident.id),
            event_id=int(firing.id),
            event_timestamp=firing.timestamp,
        )
    )
    await db_session.flush()

    result = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)

    source = cast(list[dict[str, Any]], result["sources"])[0]
    finding = next(item for item in source["findings"] if item["code"] == "unmatched_recovery")
    assert finding["count"] == 1
    assert finding["rate"] == 100.0
    assert finding["sample_event_ids"] == [recovery.id]
    assert cast(dict[str, Any], finding["evidence"])["standalone_recovery_pairs"] == 0
    assert source["recovery"]["standalone"] == 0
    assert source["recovery"]["attributable"] == 1
    assert cast(int, source["quality_score"]) < 100


async def test_a_synthetic_source_is_left_out_of_source_diagnostics(
    db_session: AsyncSession,
) -> None:
    connection = _connection(public_id="src_quality_probe", name="Rotation probe", source_type="rotation-probe")
    db_session.add(connection)
    await db_session.flush()
    db_session.add_all(
        [
            _event(
                connection_id=int(connection.id),
                request_id=f"quality-probe-{index}",
                hours_ago=index + 1,
                parsed_data={"probe": "credential rotation"},
                dedup_key=f"probe-key-{index}",
                source="rotation-probe",
            )
            for index in range(3)
        ]
    )
    await db_session.flush()

    rt._swap_snapshot({"SYNTHETIC_SOURCES": "Rotation-Probe"})
    try:
        result = await get_alert_quality_overview(db_session, window_days=7, source_limit=100)
    finally:
        rt._reset_snapshot_for_tests()

    assert cast(list[dict[str, Any]], result["sources"]) == []
    summary = cast(dict[str, Any], result["summary"])
    assert summary["source_count"] == 0
    assert summary["events_scanned"] == 0
