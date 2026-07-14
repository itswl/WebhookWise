from __future__ import annotations

import json
from typing import Any

import pytest


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_stats_endpoint_returns_aggregate_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1 import decision_trace

    stats = {
        "period": "day",
        "total": 3,
        "forwarded": 1,
        "skipped": 2,
        "outcome_breakdown": {"forwarded": 1, "skipped": 2},
        "skip_code_breakdown": {"silenced": 2},
    }

    async def fake_stats(_session: object, period: str) -> dict[str, Any]:
        assert period == "day"
        return stats

    cached: dict[str, Any] = {}

    async def fake_get(_key: str) -> dict[str, Any] | None:
        return cached.get(_key)

    async def fake_setex(key: str, _ttl: int, value: dict[str, Any]) -> None:
        cached[key] = value

    monkeypatch.setattr(decision_trace, "get_decision_trace_stats", fake_stats)
    monkeypatch.setattr("core.redis_client.redis_get_json_dict", fake_get)
    monkeypatch.setattr("core.redis_client.redis_setex_json", fake_setex)

    result = await decision_trace.get_decision_trace_stats_endpoint(period="day", session=object())  # type: ignore[arg-type]
    assert result == {"success": True, "data": stats}
    # The result was written to the cache; a second call returns the cached copy.
    assert cached
    second = await decision_trace.get_decision_trace_stats_endpoint(period="day", session=object())  # type: ignore[arg-type]
    assert second == {"success": True, "data": stats}


@pytest.mark.asyncio
async def test_quality_stats_endpoint_returns_proxy_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1 import decision_trace

    quality = {
        "period": "day",
        "total": 5,
        "ai_total": 3,
        "route_breakdown": {"ai": 3, "rule": 1, "redis_reuse": 1},
        "override_count": 1,
        "override_rate": 33.3,
        "degraded_total": 1,
        "degraded_rate": 20.0,
        "degraded_reasons": {"ai_error: boom": 1},
        "ai_importance_breakdown": {"high": 1, "medium": 1, "low": 1},
        "ai_importance_by_source": {"grafana": {"low": 1}},
    }

    async def fake_quality(_session: object, period: str) -> dict[str, Any]:
        assert period == "week"
        return quality

    async def fake_get(_key: str) -> dict[str, Any] | None:
        return None

    async def fake_setex(_key: str, _ttl: int, _value: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(decision_trace, "get_decision_trace_quality_stats", fake_quality)
    monkeypatch.setattr("core.redis_client.redis_get_json_dict", fake_get)
    monkeypatch.setattr("core.redis_client.redis_setex_json", fake_setex)

    result = await decision_trace.get_decision_trace_quality_stats_endpoint(period="week", session=object())  # type: ignore[arg-type]
    assert result == {"success": True, "data": quality}


@pytest.mark.asyncio
async def test_list_endpoint_returns_pagination_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1 import decision_trace

    items = [
        {
            "id": 5,
            "webhook_event_id": 42,
            "created_at": "2026-06-19T11:22:19+00:00",
            "outcome": "skipped",
            "skip_code": "silenced",
            "source": "volcengine",
            "importance": "medium",
            "is_periodic_reminder": False,
            "matched_rules": [],
            "steps": [{"step": "forward", "outcome": "skipped", "skip_code": "silenced"}],
        }
    ]

    async def fake_list(_session: object, **kwargs: object) -> tuple[list[dict[str, Any]], bool, int | None]:
        assert kwargs["outcome"] == "skipped"
        assert kwargs["skip_code"] == "silenced"
        return items, True, 5

    monkeypatch.setattr(decision_trace, "list_decision_traces", fake_list)

    response = await decision_trace.list_decision_traces_endpoint(
        page=1,
        page_size=20,
        cursor=None,
        outcome="skipped",
        skip_code="silenced",
        source="",
        session=object(),  # type: ignore[arg-type]
    )
    result = _body(response)
    assert result["success"] is True
    assert result["data"][0]["webhook_event_id"] == 42
    assert result["pagination"] == {"next_cursor": 5, "has_more": True, "page_size": 20, "total": None}


@pytest.mark.asyncio
async def test_by_event_endpoint_found_and_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1 import decision_trace

    trace = {"id": 1, "webhook_event_id": 42, "outcome": "forwarded", "skip_code": "none", "steps": []}

    async def found(_session: object, webhook_event_id: int) -> dict[str, Any] | None:
        assert webhook_event_id == 42
        return trace

    monkeypatch.setattr(decision_trace, "get_decision_trace_for_event", found)
    ok = await decision_trace.get_decision_trace_by_event_endpoint(42, session=object())  # type: ignore[arg-type]
    assert ok == {"success": True, "data": trace}

    async def missing(_session: object, _webhook_event_id: int) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(decision_trace, "get_decision_trace_for_event", missing)
    not_found = await decision_trace.get_decision_trace_by_event_endpoint(999, session=object())  # type: ignore[arg-type]
    assert not_found.status_code == 404
    assert _body(not_found)["success"] is False
