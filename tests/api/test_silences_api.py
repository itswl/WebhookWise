"""Silence API handler tests (direct calls, in-memory sqlite)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def session(db_session):
    return db_session


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


# ── Silence routes ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_list_lift_silence_flow(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest

    created = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="prometheus", comment="maintenance"), session=session
    )
    assert created["success"] is True
    silence_id = created["data"]["id"]
    assert created["data"]["active"] is True

    listed = await api.list_silences_endpoint(active_only=True, session=session)
    assert len(listed["data"]) == 1

    lifted = await api.lift_silence_endpoint(silence_id, session=session)
    assert lifted["data"]["active"] is False

    # active_only now excludes it, but the full list still shows it.
    active = await api.list_silences_endpoint(active_only=True, session=session)
    assert active["data"] == []
    full = await api.list_silences_endpoint(active_only=False, session=session)
    assert len(full["data"]) == 1


@pytest.mark.asyncio
async def test_list_silences_annotates_suppression_counts(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from models import DecisionTrace
    from schemas.silences import SilenceCreateRequest

    # Two silences: one that has suppressed alerts, one "zombie" that hasn't.
    busy = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="volcengine", comment="busy"), session=session
    )
    zombie = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="aliyun", comment="zombie"), session=session
    )
    busy_id = busy["data"]["id"]

    # Seed two silenced decision traces attributed to the busy rule.
    session.add_all(
        [
            DecisionTrace(webhook_event_id=1, outcome="skipped", skip_code="silenced", silence_id=busy_id),
            DecisionTrace(webhook_event_id=2, outcome="skipped", skip_code="silenced", silence_id=busy_id),
        ]
    )
    await session.commit()

    listed = await api.list_silences_endpoint(session=session)
    by_id = {s["id"]: s for s in listed["data"]}
    assert by_id[busy_id]["suppressed_count"] == 2
    assert by_id[busy_id]["last_suppressed_at"] is not None
    # The zombie rule reports zero, with no last-suppressed timestamp.
    assert by_id[zombie["data"]["id"]]["suppressed_count"] == 0
    assert by_id[zombie["data"]["id"]]["last_suppressed_at"] is None


@pytest.mark.asyncio
async def test_create_silence_normalizes_aware_expiry(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest

    aware = datetime.now(tz=UTC) + timedelta(hours=2)
    created = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="prometheus", expires_at=aware), session=session
    )
    # Stored naive-UTC; serialized back with a trailing Z.
    assert created["data"]["expires_at"].endswith("Z")


@pytest.mark.asyncio
async def test_create_silence_requires_a_criterion() -> None:
    from schemas.silences import SilenceCreateRequest

    with pytest.raises(ValueError, match="At least one match criterion"):
        SilenceCreateRequest(comment="no criteria")


@pytest.mark.asyncio
async def test_lift_missing_silence_returns_404(session: AsyncSession) -> None:
    from api.v1 import silences as api

    resp = await api.lift_silence_endpoint(9999, session=session)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_silence(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest

    created = await api.create_silence_endpoint(SilenceCreateRequest(match_source="prometheus"), session=session)
    resp = await api.delete_silence_endpoint(created["data"]["id"], session=session)
    assert resp["success"] is True
    missing = await api.delete_silence_endpoint(created["data"]["id"], session=session)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_update_silence_make_permanent(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest, SilenceUpdateRequest

    created = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="prometheus", expires_at=datetime.now(tz=UTC) + timedelta(hours=1)),
        session=session,
    )
    updated = await api.update_silence_endpoint(
        created["data"]["id"], SilenceUpdateRequest(expires_at=None), session=session
    )
    assert updated["data"]["expires_at"] is None
    assert updated["data"]["active"] is True


@pytest.mark.asyncio
async def test_silence_backtest(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from core.datetime_utils import utcnow
    from models import WebhookEvent
    from schemas.silences import SilenceBacktestRequest

    # Seed some events
    session.add_all(
        [
            WebhookEvent(
                source="prometheus",
                importance="high",
                parsed_data={"alertname": "HostHighCpu", "summary": "CPU utilization high"},
                timestamp=utcnow(),
                is_duplicate=False,
            ),
            WebhookEvent(
                source="volcengine",
                importance="medium",
                parsed_data={"RuleName": "DBMemoryHigh", "summary": "DB memory utilization high"},
                timestamp=utcnow(),
                is_duplicate=False,
            ),
        ]
    )
    await session.commit()

    # Backtest with matching source
    res = await api.backtest_silence_endpoint(
        SilenceBacktestRequest(match_source="prometheus", lookback_days=1),
        session=session,
    )
    assert res["success"] is True
    assert res["data"]["total_scanned"] == 2
    assert res["data"]["total_matched"] == 1
    assert res["data"]["importance_counts"]["high"] == 1
    assert res["data"]["source_counts"]["prometheus"] == 1
    assert len(res["data"]["sample_matched_events"]) == 1
    assert res["data"]["sample_matched_events"][0]["summary"] == "CPU utilization high"


@pytest.mark.asyncio
async def test_silence_debt_endpoint(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from core.datetime_utils import utcnow
    from models import DecisionTrace, Silence

    silence = Silence(match_source="volcengine", comment="perm: GPU box", expires_at=None)
    session.add(silence)
    await session.flush()
    session.add_all(
        [
            DecisionTrace(
                webhook_event_id=1000 + i,
                outcome="skipped",
                skip_code="silenced",
                silence_id=silence.id,
                created_at=utcnow(),
            )
            for i in range(600)
        ]
    )
    await session.commit()

    res = await api.silence_debt_endpoint(window_days=30, session=session)
    assert res["success"] is True
    assert res["data"]["chronic_count"] == 1
    assert res["data"]["silences"][0]["chronic"] is True


@pytest.mark.asyncio
async def test_silence_backtest_reports_scan_truncation(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.datetime_utils import utcnow
    from models import WebhookEvent
    from services.silences import backtest as backtest_module

    session.add_all(
        [
            WebhookEvent(
                source="prometheus",
                importance="low",
                parsed_data={"RuleName": f"Rule{i}"},
                timestamp=utcnow(),
                is_duplicate=False,
            )
            for i in range(5)
        ]
    )
    await session.commit()

    # With the cap lowered below the row count, the scan must stop at the cap
    # and say so instead of silently understating the counts.
    monkeypatch.setattr(backtest_module, "_MAX_BACKTEST_SCAN", 3)
    result = await backtest_module.backtest_silence_rule(session, match_source="prometheus", lookback_days=1)

    assert result["total_scanned"] == 3
    assert result["scan_truncated"] is True
