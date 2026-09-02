"""Read-only alert-source quality diagnostics.

The quality center evaluates data already persisted by the ingest and incident
pipelines. It intentionally returns findings and upstream configuration advice
only; it never mutates source connections, alerts, incidents, or routing rules.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.normalized import extract_alert_identity
from core import json
from core.datetime_utils import naive_utc, utc_isoformat, utcnow
from models import Incident, IncidentMember, SourceConnection, WebhookEvent
from services.analysis.alert_identity_context import build_alert_identity_context
from services.incidents.grouping import is_recovery_payload
from services.webhooks.decisioning import extract_forward_match_fields
from services.webhooks.policies import is_synthetic_source
from services.webhooks.source_onboarding import payload_schema_fingerprint
from services.webhooks.source_stats import get_source_event_stats

_MAX_PAYLOAD_SCAN_EVENTS = 2_000
_PAYLOAD_SCAN_YIELD_EVERY = 200
_MAX_CONNECTION_SCAN = 500
_RECOVERY_LOOKUP_CHUNK = 500
_MAX_ISSUE_SAMPLES = 5
_UNATTENDED_AFTER = timedelta(hours=2)
_UNRECOVERED_AFTER = timedelta(hours=6)
_TIMESTAMP_FUTURE_TOLERANCE = timedelta(minutes=10)
_TIMESTAMP_STALE_TOLERANCE = timedelta(days=7)
_TERMINAL_WORKFLOW_STATES = ("resolved", "ignored")
_TIMESTAMP_KEYS = frozenset(
    {
        "timestamp",
        "time",
        "eventtime",
        "alerttime",
        "updatedat",
        "createdat",
        "datetime",
        "localdatetime",
    }
)

# Per-process TTL cache for the whole overview result. The dashboard Quality
# tab can be polled by several sessions at once and this is the most expensive
# read path in the service; a result up to 60 seconds stale is acceptable for
# read-only diagnostics. Keyed by the request parameters.
_OVERVIEW_CACHE_TTL_SECONDS = 60.0
_OVERVIEW_CACHE: dict[tuple[int, int], tuple[float, dict[str, object]]] = {}


def _reset_overview_cache_for_tests() -> None:
    _OVERVIEW_CACHE.clear()


@dataclass(slots=True)
class _SourceAccumulator:
    key: str
    source: str
    display_name: str
    source_connection_id: int | None = None
    connection: SourceConnection | None = None
    event_count: int = 0
    scanned_count: int = 0
    duplicate_count: int = 0
    recovery_count: int = 0
    matched_recovery_count: int = 0
    standalone_recovery_count: int = 0
    missing_service: int = 0
    missing_environment: int = 0
    missing_severity: int = 0
    missing_identity: int = 0
    timestamp_anomalies: int = 0
    max_timestamp_offset_seconds: int = 0
    last_event_at: datetime | None = None
    unique_dedup_keys: set[str] = field(default_factory=set)
    identity_anchors: set[str] = field(default_factory=set)
    schema_fingerprints: set[str] = field(default_factory=set)
    identity_states: dict[str, list[tuple[datetime, bool]]] = field(default_factory=lambda: defaultdict(list))
    # Firing event ids per alert identity, so a recovery can be told apart from
    # a recovery whose FIRING side never made an incident. See _classify_recoveries.
    identity_firing_ids: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    issue_samples: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    unattended_incidents: int = 0


def _normalized_source(source: str | None) -> str:
    return str(source or "unknown").strip().lower() or "unknown"


def _source_key(source: str | None, source_connection_id: int | None) -> str:
    if source_connection_id is not None:
        return f"connection:{source_connection_id}"
    return f"source:{_normalized_source(source)}"


def _record_sample(accumulator: _SourceAccumulator, code: str, event_id: int | None) -> None:
    if event_id is None:
        return
    samples = accumulator.issue_samples[code]
    if len(samples) < _MAX_ISSUE_SAMPLES and event_id not in samples:
        samples.append(event_id)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        if numeric < 1_000_000_000:
            return None
        try:
            return datetime.fromtimestamp(numeric, tz=UTC).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_timestamp(int(text))
    try:
        return naive_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _timestamp_candidates(value: object, *, depth: int = 0) -> list[datetime]:
    if depth > 4:
        return []
    candidates: list[datetime] = []
    if isinstance(value, dict):
        for key, nested in list(value.items())[:100]:
            normalized_key = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized_key in _TIMESTAMP_KEYS:
                parsed = _parse_timestamp(nested)
                if parsed is not None:
                    candidates.append(parsed)
            if isinstance(nested, dict | list):
                candidates.extend(_timestamp_candidates(nested, depth=depth + 1))
    elif isinstance(value, list):
        for nested in value[:5]:
            candidates.extend(_timestamp_candidates(nested, depth=depth + 1))
    return candidates


def _timestamp_anomaly_offset(
    parsed_data: dict[str, object],
    received_at: datetime,
) -> int | None:
    candidates = _timestamp_candidates(parsed_data)
    if not candidates:
        return None
    closest = min(candidates, key=lambda candidate: abs((candidate - received_at).total_seconds()))
    delta = closest - received_at
    if -_TIMESTAMP_STALE_TOLERANCE <= delta <= _TIMESTAMP_FUTURE_TOLERANCE:
        return None
    return int(delta.total_seconds())


def _issue_severity(code: str, rate: float) -> str:
    if code in {"missing_identity", "missing_severity", "unmatched_recovery"} and rate >= 50:
        return "high"
    if code == "missing_service" and rate >= 50:
        return "high"
    if code in {"missing_environment", "schema_drift", "timestamp_anomaly"} and rate < 25:
        return "low"
    return "medium"


def _proportional_penalty(count: int, denominator: int, maximum: int) -> int:
    if count <= 0 or denominator <= 0:
        return 0
    ratio = min(1.0, count / denominator / 0.5)
    return max(1, round(maximum * ratio))


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _finding_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, object], item) for item in value if isinstance(item, dict)]


def _finding(
    accumulator: _SourceAccumulator,
    *,
    code: str,
    count: int,
    denominator: int,
    maximum_penalty: int,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    rate = round(count / denominator * 100, 1) if denominator else 0.0
    return {
        "code": code,
        "severity": _issue_severity(code, rate),
        "count": count,
        "rate": rate,
        "penalty": _proportional_penalty(count, denominator, maximum_penalty),
        "sample_event_ids": list(accumulator.issue_samples.get(code, [])),
        "evidence": evidence or {},
        "recommendation_code": f"fix_{code}",
    }


async def _incident_member_ids(
    session: AsyncSession,
    event_ids: list[int],
) -> set[int]:
    """Which of these events belong to an incident, looked up in bounded chunks."""
    members: set[int] = set()
    for offset in range(0, len(event_ids), _RECOVERY_LOOKUP_CHUNK):
        chunk = event_ids[offset : offset + _RECOVERY_LOOKUP_CHUNK]
        rows = await session.execute(select(IncidentMember.event_id).where(IncidentMember.event_id.in_(chunk)))
        members.update(int(event_id) for event_id in rows.scalars().all())
    return members


async def _classify_recoveries(
    session: AsyncSession,
    recovery_events: dict[int, tuple[_SourceAccumulator, str]],
) -> None:
    """Sort every scanned recovery into matched, unmatched, or standalone.

    - **matched**: the recovery is itself an incident member — it joined.
    - **unmatched**: it did not, but a FIRING event of the same alert identity
      did. The incident existed and the recovery failed to reach it, which is
      the only shape of this that a source can fix.
    - **standalone**: nothing of this identity ever made an incident, so there
      was nothing to join. An incident needs two correlated non-recovery alerts,
      so a plain fire -> resolve pair can never match by construction; it is
      counted for information and costs the source no score.
    """
    if not recovery_events:
        return
    # One identity can be seen by more than one source accumulator, so collect
    # the firing ids per identity once instead of re-walking the recoveries.
    firing_ids_by_identity: dict[str, set[int]] = defaultdict(set)
    seen: set[tuple[str, str]] = set()
    for accumulator, identity in recovery_events.values():
        if identity and (accumulator.key, identity) not in seen:
            seen.add((accumulator.key, identity))
            firing_ids_by_identity[identity].update(accumulator.identity_firing_ids.get(identity, []))

    candidates = set(recovery_events)
    for firing_ids in firing_ids_by_identity.values():
        candidates.update(firing_ids)
    members = await _incident_member_ids(session, sorted(candidates))
    firing_made_incident = {
        identity: not members.isdisjoint(firing_ids) for identity, firing_ids in firing_ids_by_identity.items()
    }
    for event_id, (accumulator, identity) in recovery_events.items():
        if event_id in members:
            accumulator.matched_recovery_count += 1
        elif not firing_made_incident.get(identity, False):
            accumulator.standalone_recovery_count += 1
        else:
            # A real unmatched recovery: the sample stays as evidence.
            continue
        samples = accumulator.issue_samples.get("unmatched_recovery", [])
        if event_id in samples:
            samples.remove(event_id)


async def _unattended_by_source(
    session: AsyncSession,
    *,
    start: datetime,
    now: datetime,
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(
                Incident.source,
                Incident.source_connection_id,
                func.count(Incident.id),
            )
            .where(
                Incident.started_at >= start,
                Incident.started_at <= now - _UNATTENDED_AFTER,
                Incident.workflow_status.notin_(_TERMINAL_WORKFLOW_STATES),
                Incident.acknowledged_at.is_(None),
                Incident.assignee.is_(None),
            )
            .group_by(Incident.source, Incident.source_connection_id)
        )
    ).all()
    return {_source_key(source, source_connection_id): int(count or 0) for source, source_connection_id, count in rows}


def _credential_state(connection: SourceConnection | None) -> str:
    if connection is None:
        return "unmanaged"
    if connection.revoked_at is not None:
        return "revoked"
    return "active" if connection.enabled else "disabled"


def _unrecovered_identity_count(accumulator: _SourceAccumulator, now: datetime) -> int:
    unresolved = 0
    for states in accumulator.identity_states.values():
        ordered = sorted(states, key=lambda item: item[0])
        firing_count = sum(1 for _, is_recovery in ordered if not is_recovery)
        if firing_count < 3:
            continue
        latest_at, latest_is_recovery = ordered[-1]
        if not latest_is_recovery and latest_at <= now - _UNRECOVERED_AFTER:
            unresolved += 1
    return unresolved


def _attributable_recoveries(accumulator: _SourceAccumulator) -> int:
    """Recoveries for which matching an incident was ever possible.

    Everything else is a standalone recovery pair: the firing side never made
    an incident, so the recovery had nothing to join. Counting those as failures
    blamed the source for this system's own grouping threshold.
    """
    return max(0, accumulator.recovery_count - accumulator.standalone_recovery_count)


def _source_findings(accumulator: _SourceAccumulator, *, start: datetime, now: datetime) -> list[dict[str, object]]:
    total = accumulator.event_count
    if total == 0:
        connection = accumulator.connection
        if (
            connection is not None
            and connection.enabled
            and connection.revoked_at is None
            and (connection.last_event_at is None or connection.last_event_at < start)
        ):
            return [
                {
                    "code": "no_recent_events",
                    "severity": "medium",
                    "count": 1,
                    "rate": 100.0,
                    "penalty": 0,
                    "sample_event_ids": [],
                    "evidence": {
                        "window_start": utc_isoformat(start),
                        "last_event_at": utc_isoformat(connection.last_event_at),
                    },
                    "recommendation_code": "fix_no_recent_events",
                }
            ]
        return []

    # Payload-derived signals only see the bounded scan, so their rates use the
    # scanned-row denominator, not the full-window event count.
    scanned = accumulator.scanned_count
    findings: list[dict[str, object]] = []
    field_rules = (
        ("missing_identity", accumulator.missing_identity, 25),
        ("missing_service", accumulator.missing_service, 15),
        ("missing_environment", accumulator.missing_environment, 10),
        ("missing_severity", accumulator.missing_severity, 15),
        ("timestamp_anomaly", accumulator.timestamp_anomalies, 8),
    )
    for code, count, maximum_penalty in field_rules:
        if count:
            evidence: dict[str, object] = {"events_scanned": scanned}
            if code == "timestamp_anomaly":
                evidence["max_offset_seconds"] = accumulator.max_timestamp_offset_seconds
            findings.append(
                _finding(
                    accumulator,
                    code=code,
                    count=count,
                    denominator=scanned,
                    maximum_penalty=maximum_penalty,
                    evidence=evidence,
                )
            )

    # Only the recoveries whose firing side actually formed an incident can be
    # said to have failed to match one. A plain fire -> resolve pair that never
    # became an incident is grouping policy (an incident needs two correlated
    # non-recovery alerts), not a defect in what the source sent: 87% of the
    # "unmatched" recoveries measured on 2026-09-02 were exactly that.
    attributable = _attributable_recoveries(accumulator)
    unmatched_recovery = attributable - accumulator.matched_recovery_count
    if unmatched_recovery > 0:
        findings.append(
            _finding(
                accumulator,
                code="unmatched_recovery",
                count=unmatched_recovery,
                denominator=attributable,
                maximum_penalty=18,
                evidence={
                    "recovery_events": accumulator.recovery_count,
                    "matched_recovery_events": accumulator.matched_recovery_count,
                    "standalone_recovery_pairs": accumulator.standalone_recovery_count,
                },
            )
        )

    dedup_key_count = len(accumulator.unique_dedup_keys)
    identity_churn_rate = round(dedup_key_count / scanned * 100, 1) if scanned else 0.0
    anchor_limit = max(3, scanned // 5)
    duplicate_rate = accumulator.duplicate_count / total if total else 0.0
    if (
        scanned >= 10
        and identity_churn_rate >= 80
        and duplicate_rate <= 0.1
        and len(accumulator.identity_anchors) <= anchor_limit
    ):
        findings.append(
            _finding(
                accumulator,
                code="identity_churn",
                count=dedup_key_count,
                denominator=scanned,
                maximum_penalty=15,
                evidence={
                    "unique_dedup_keys": dedup_key_count,
                    "identity_anchors": len(accumulator.identity_anchors),
                    "duplicate_rate": round(duplicate_rate * 100, 1),
                },
            )
        )

    unrecovered = _unrecovered_identity_count(accumulator, now)
    if unrecovered:
        findings.append(
            _finding(
                accumulator,
                code="no_recovery_signals",
                count=unrecovered,
                denominator=max(1, len(accumulator.identity_states)),
                maximum_penalty=10,
                evidence={"repeated_open_identities": unrecovered, "stale_after_hours": 6},
            )
        )

    connection = accumulator.connection
    if (
        connection is not None
        and connection.schema_changed_at is not None
        and connection.schema_changed_at >= start
        and connection.schema_change_count > 0
    ):
        findings.append(
            {
                "code": "schema_drift",
                "severity": "medium",
                "count": int(connection.schema_change_count),
                "rate": 0.0,
                "penalty": 8,
                "sample_event_ids": [],
                "evidence": {
                    "schema_changed_at": utc_isoformat(connection.schema_changed_at),
                    "observed_shapes": len(accumulator.schema_fingerprints),
                },
                "recommendation_code": "fix_schema_drift",
            }
        )

    if accumulator.unattended_incidents:
        findings.append(
            {
                "code": "unattended_incidents",
                "severity": "medium",
                "count": accumulator.unattended_incidents,
                "rate": 0.0,
                "penalty": min(8, accumulator.unattended_incidents * 2),
                "sample_event_ids": [],
                "evidence": {"unacknowledged_after_hours": 2},
                "recommendation_code": "fix_unattended_incidents",
            }
        )

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        findings,
        key=lambda item: (
            severity_rank.get(str(item["severity"]), 3),
            -_as_int(item["count"]),
            str(item["code"]),
        ),
    )


def _coverage(present: int, total: int) -> float:
    return round(present / total * 100, 1) if total else 0.0


def _source_payload(
    accumulator: _SourceAccumulator,
    *,
    findings: list[dict[str, object]],
) -> dict[str, object]:
    total = accumulator.event_count
    scanned = accumulator.scanned_count
    penalty = sum(_as_int(item["penalty"]) for item in findings)
    score = max(0, 100 - penalty) if total else None
    if score is None:
        grade = "no_data"
    elif score >= 90:
        grade = "healthy"
    elif score >= 75:
        grade = "fair"
    elif score >= 60:
        grade = "needs_attention"
    else:
        grade = "poor"
    return {
        "source_key": accumulator.key,
        "source": accumulator.source,
        "source_connection_id": accumulator.source_connection_id,
        "display_name": accumulator.display_name,
        "managed": accumulator.connection is not None,
        "credential_state": _credential_state(accumulator.connection),
        "quality_score": score,
        "grade": grade,
        "event_count": total,
        "last_event_at": utc_isoformat(accumulator.last_event_at),
        "coverage": {
            "stable_identity": _coverage(scanned - accumulator.missing_identity, scanned),
            "service": _coverage(scanned - accumulator.missing_service, scanned),
            "environment": _coverage(scanned - accumulator.missing_environment, scanned),
            "severity": _coverage(scanned - accumulator.missing_severity, scanned),
        },
        "recovery": {
            "events": accumulator.recovery_count,
            "matched": accumulator.matched_recovery_count,
            "standalone": accumulator.standalone_recovery_count,
            "attributable": _attributable_recoveries(accumulator),
            "match_rate": _coverage(accumulator.matched_recovery_count, _attributable_recoveries(accumulator)),
        },
        "identity": {
            "unique_dedup_keys": len(accumulator.unique_dedup_keys),
            "anchors": len(accumulator.identity_anchors),
            "duplicate_rate": _coverage(accumulator.duplicate_count, total),
        },
        "schema": {
            "observed_shapes": len(accumulator.schema_fingerprints),
            "change_count": int(accumulator.connection.schema_change_count or 0)
            if accumulator.connection is not None
            else 0,
            "changed_at": utc_isoformat(accumulator.connection.schema_changed_at)
            if accumulator.connection is not None
            else None,
        },
        "unattended_incidents": accumulator.unattended_incidents,
        "findings": findings,
    }


async def get_alert_quality_overview(
    session: AsyncSession,
    *,
    window_days: int = 7,
    source_limit: int = 100,
) -> dict[str, object]:
    """Return bounded, explainable alert quality diagnostics.

    The full result is cached in-process for a short TTL so dashboard polling
    cannot repeatedly trigger the payload scan.
    """
    cache_key = (int(window_days), int(source_limit))
    cached = _OVERVIEW_CACHE.get(cache_key)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]

    now = utcnow()
    start = now - timedelta(days=window_days)
    totals_rows = await get_source_event_stats(session, window_days=window_days, granularity="connection")
    total_events = sum(row.total for row in totals_rows)
    events = (
        await session.execute(
            select(
                WebhookEvent.id,
                WebhookEvent.source,
                WebhookEvent.source_connection_id,
                WebhookEvent.timestamp,
                WebhookEvent.parsed_data,
                WebhookEvent.ai_analysis,
                WebhookEvent.alert_hash,
                WebhookEvent.dedup_key,
            )
            .where(WebhookEvent.timestamp >= start)
            .order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())
            .limit(_MAX_PAYLOAD_SCAN_EVENTS)
        )
    ).all()
    connection_rows = list(
        (
            await session.execute(
                select(SourceConnection).order_by(SourceConnection.id.desc()).limit(_MAX_CONNECTION_SCAN + 1)
            )
        )
        .scalars()
        .all()
    )
    connection_truncated = len(connection_rows) > _MAX_CONNECTION_SCAN
    connections = connection_rows[:_MAX_CONNECTION_SCAN]

    accumulators: dict[str, _SourceAccumulator] = {}
    for connection in connections:
        if is_synthetic_source(connection.source_type):
            # A probe is judged, traced and delivered like anything else; it is
            # simply not a source whose data quality anyone is grading.
            continue
        key = _source_key(connection.source_type, int(connection.id))
        accumulators[key] = _SourceAccumulator(
            key=key,
            source=_normalized_source(connection.source_type),
            display_name=connection.name,
            source_connection_id=int(connection.id),
            connection=connection,
        )

    for row in totals_rows:
        if is_synthetic_source(row.source):
            continue
        key = _source_key(row.source, row.source_connection_id)
        accumulator = accumulators.get(key)
        if accumulator is None:
            source = _normalized_source(row.source)
            accumulator = _SourceAccumulator(key=key, source=source, display_name=source)
            accumulators[key] = accumulator
        accumulator.event_count = int(row.total or 0)
        accumulator.duplicate_count = int(row.duplicates or 0)
        accumulator.last_event_at = row.last_seen

    recovery_events: dict[int, tuple[_SourceAccumulator, str]] = {}
    for index, event in enumerate(events):
        if index and index % _PAYLOAD_SCAN_YIELD_EVERY == 0:
            # The payload inspection below is pure CPU on the request's event
            # loop; yield periodically so a full scan cannot stall ingress.
            await asyncio.sleep(0)
        if is_synthetic_source(event.source):
            continue
        key = _source_key(event.source, event.source_connection_id)
        accumulator = accumulators.get(key)
        if accumulator is None:
            source = _normalized_source(event.source)
            accumulator = _SourceAccumulator(key=key, source=source, display_name=source)
            accumulators[key] = accumulator

        accumulator.scanned_count += 1
        event_timestamp = event.timestamp or now
        parsed_data = event.parsed_data if isinstance(event.parsed_data, dict) else {}
        analysis = event.ai_analysis if isinstance(event.ai_analysis, dict) else {}
        stored_identity = extract_alert_identity(parsed_data) or {}
        identity_context = build_alert_identity_context(event.source, parsed_data)
        context_identity_value = identity_context.get("identity")
        context_identity = context_identity_value if isinstance(context_identity_value, dict) else {}
        forward_fields = extract_forward_match_fields(parsed_data)

        stable_identity = (
            stored_identity.get("name")
            or stored_identity.get("fingerprint")
            or context_identity.get("rule_name")
            or context_identity.get("rule_id")
        )
        if not stable_identity:
            accumulator.missing_identity += 1
            _record_sample(accumulator, "missing_identity", event.id)
        else:
            accumulator.identity_anchors.add(str(stable_identity).strip().lower())

        if not (stored_identity.get("service") or context_identity.get("service")):
            accumulator.missing_service += 1
            _record_sample(accumulator, "missing_service", event.id)
        if not forward_fields.get("environment"):
            accumulator.missing_environment += 1
            _record_sample(accumulator, "missing_environment", event.id)
        if not (stored_identity.get("severity") or context_identity.get("severity")):
            accumulator.missing_severity += 1
            _record_sample(accumulator, "missing_severity", event.id)

        timestamp_offset = _timestamp_anomaly_offset(parsed_data, event_timestamp)
        if timestamp_offset is not None:
            accumulator.timestamp_anomalies += 1
            accumulator.max_timestamp_offset_seconds = max(
                accumulator.max_timestamp_offset_seconds,
                abs(timestamp_offset),
            )
            _record_sample(accumulator, "timestamp_anomaly", event.id)

        is_recovery = is_recovery_payload(parsed_data, analysis)
        identity_key = str(event.dedup_key or event.alert_hash or "").strip()
        if is_recovery:
            accumulator.recovery_count += 1
            if event.id is not None:
                recovery_events[int(event.id)] = (accumulator, identity_key)
                _record_sample(accumulator, "unmatched_recovery", event.id)
        elif identity_key and event.id is not None:
            accumulator.identity_firing_ids[identity_key].append(int(event.id))

        if identity_key:
            accumulator.unique_dedup_keys.add(identity_key)
            accumulator.identity_states[identity_key].append((event_timestamp, is_recovery))
        source_connection = accumulator.connection
        if (
            source_connection is not None
            and source_connection.schema_changed_at is not None
            and source_connection.schema_changed_at >= start
        ):
            fingerprint = payload_schema_fingerprint(json.dumps_bytes(parsed_data))
            if fingerprint:
                accumulator.schema_fingerprints.add(fingerprint)

    await _classify_recoveries(session, recovery_events)

    unattended = await _unattended_by_source(session, start=start, now=now)
    for key, count in unattended.items():
        accumulator = accumulators.get(key)
        if accumulator is not None:
            accumulator.unattended_incidents = count

    source_payloads: list[dict[str, object]] = []
    issue_rollup: dict[str, dict[str, object]] = {}
    for accumulator in accumulators.values():
        findings = _source_findings(accumulator, start=start, now=now)
        source_payloads.append(_source_payload(accumulator, findings=findings))
        for finding in findings:
            code = str(finding["code"])
            rollup = issue_rollup.setdefault(
                code,
                {
                    "code": code,
                    "severity": finding["severity"],
                    "source_count": 0,
                    "affected_count": 0,
                },
            )
            rollup["source_count"] = _as_int(rollup["source_count"]) + 1
            rollup["affected_count"] = _as_int(rollup["affected_count"]) + _as_int(finding["count"])
            if finding["severity"] == "high":
                rollup["severity"] = "high"
            elif finding["severity"] == "medium" and rollup["severity"] == "low":
                rollup["severity"] = "medium"

    source_payloads.sort(
        key=lambda item: (
            item["quality_score"] is None,
            _as_int(item["quality_score"]) if item["quality_score"] is not None else 101,
            -_as_int(item["event_count"]),
            str(item["display_name"]).lower(),
        )
    )
    source_total = len(source_payloads)
    visible_sources = source_payloads[:source_limit]
    scored_sources = [_as_int(item["quality_score"]) for item in source_payloads if item["quality_score"] is not None]
    scanned_events = sum(accumulator.scanned_count for accumulator in accumulators.values())
    missing_identity = sum(accumulator.missing_identity for accumulator in accumulators.values())
    missing_service = sum(accumulator.missing_service for accumulator in accumulators.values())
    missing_environment = sum(accumulator.missing_environment for accumulator in accumulators.values())
    missing_severity = sum(accumulator.missing_severity for accumulator in accumulators.values())
    recovery_count = sum(accumulator.recovery_count for accumulator in accumulators.values())
    matched_recovery_count = sum(accumulator.matched_recovery_count for accumulator in accumulators.values())
    standalone_recovery_count = sum(accumulator.standalone_recovery_count for accumulator in accumulators.values())
    attributable_recovery_count = sum(_attributable_recoveries(accumulator) for accumulator in accumulators.values())
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for source_payload in source_payloads:
        for finding in _finding_list(source_payload["findings"]):
            severity = str(finding.get("severity") or "")
            if severity in severity_counts:
                severity_counts[severity] += 1
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    top_findings = sorted(
        issue_rollup.values(),
        key=lambda item: (
            severity_rank.get(str(item["severity"]), 3),
            -_as_int(item["source_count"]),
            str(item["code"]),
        ),
    )

    overview: dict[str, object] = {
        "window": {
            "days": window_days,
            "start": utc_isoformat(start),
            "end": utc_isoformat(now),
        },
        "summary": {
            "quality_score": round(sum(scored_sources) / len(scored_sources), 1) if scored_sources else None,
            "source_count": source_total,
            "scored_source_count": len(scored_sources),
            "no_data_source_count": source_total - len(scored_sources),
            "events_in_window": total_events,
            "events_scanned": scanned_events,
            "finding_count": sum(severity_counts.values()),
            "severity_counts": severity_counts,
            "field_coverage": {
                "stable_identity": _coverage(scanned_events - missing_identity, scanned_events),
                "service": _coverage(scanned_events - missing_service, scanned_events),
                "environment": _coverage(scanned_events - missing_environment, scanned_events),
                "severity": _coverage(scanned_events - missing_severity, scanned_events),
            },
            "recovery": {
                "events": recovery_count,
                "matched": matched_recovery_count,
                "standalone": standalone_recovery_count,
                "attributable": attributable_recovery_count,
                "match_rate": _coverage(matched_recovery_count, attributable_recovery_count),
            },
        },
        "top_findings": top_findings,
        "sources": visible_sources,
        "scan": {
            "event_limit": _MAX_PAYLOAD_SCAN_EVENTS,
            "event_truncated": total_events > len(events),
            "connection_limit": _MAX_CONNECTION_SCAN,
            "connection_truncated": connection_truncated,
            "source_limit": source_limit,
            "source_total": source_total,
            "source_truncated": source_total > len(visible_sources),
            "oldest_scanned_event_at": utc_isoformat(events[-1].timestamp) if events else None,
        },
        "read_only": True,
    }
    _OVERVIEW_CACHE[cache_key] = (time.monotonic() + _OVERVIEW_CACHE_TTL_SECONDS, overview)
    return overview


__all__ = ["get_alert_quality_overview"]
