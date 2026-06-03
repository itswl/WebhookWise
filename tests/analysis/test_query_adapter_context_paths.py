from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "period",
    ["day", "week", "month", "year"],
)
async def test_ai_usage_stats_periods_and_cache_math(
    monkeypatch: pytest.MonkeyPatch,
    period: str,
) -> None:
    from services.analysis import analysis_queries

    class RouteResult:
        def all(self) -> list[tuple[str, int]]:
            return [("ai", 2), ("redis_reuse", 3), ("db_reuse", 1), ("rechain", 1), ("reuse", 1), ("cache", 4)]

    class StatsResult:
        def first(self) -> tuple[int, int, float]:
            return (11, 7, 0.12)

    class CacheResult:
        def scalar(self) -> int:
            return 4

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _stmt: object) -> object:
            self.calls += 1
            return [RouteResult(), StatsResult(), CacheResult()][self.calls - 1]

    async def count_with_timeout(_session: object, _stmt: object) -> int:
        return 12

    monkeypatch.setattr(analysis_queries, "utcnow", lambda: datetime(2026, 5, 27, tzinfo=timezone.utc))
    monkeypatch.setattr(analysis_queries, "count_with_timeout", count_with_timeout)

    stats = await analysis_queries.get_ai_usage_stats(Session(), period=period)  # type: ignore[arg-type]

    assert stats["total_calls"] == 12
    assert stats["route_breakdown"]["redis_reuse"] == 3
    assert stats["percentages"]["cache"] == 33.33
    assert stats["tokens"] == {"input": 11, "output": 7, "total": 18}
    assert stats["cost"]["saved_estimate"] == 0.6
    assert stats["cache_statistics"]["avg_hits_per_entry"] == 2.5
    assert stats["cache_statistics"]["cache_hit_rate"] == 83.33


@pytest.mark.asyncio
async def test_deep_analysis_queries_return_cursor_page_and_webhook_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.analysis import analysis_queries

    created = datetime(2026, 5, 27, 8, 0, tzinfo=timezone.utc)
    records = [
        (
            SimpleNamespace(
                id=3,
                webhook_event_id=30,
                engine="openclaw",
                user_question="why",
                analysis_result={"summary": "third"},
                duration_seconds=1.2,
                created_at=created,
                openclaw_run_id="run-3",
                openclaw_session_key="session-3",
                status="completed",
                poll_attempts=1,
                next_poll_at=None,
                last_polled_at=created,
            ),
            SimpleNamespace(source="prometheus", is_duplicate=True),
        ),
        (
            SimpleNamespace(
                id=2,
                webhook_event_id=20,
                engine="openclaw",
                user_question="why",
                analysis_result={"summary": "second"},
                duration_seconds=1.0,
                created_at=created,
                openclaw_run_id="run-2",
                openclaw_session_key="session-2",
                status="completed",
                poll_attempts=1,
                next_poll_at=None,
                last_polled_at=created,
            ),
            None,
        ),
    ]

    class ListResult:
        def all(self) -> list[tuple[object, object | None]]:
            return records

    class ScalarList:
        def all(self) -> list[object]:
            return [records[0][0]]

    class AnalysesResult:
        def scalars(self) -> ScalarList:
            return ScalarList()

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _stmt: object) -> object:
            self.calls += 1
            if self.calls == 1:
                return ListResult()
            return AnalysesResult()

    async def count_with_timeout(_session: object, _stmt: object) -> int:
        return 3

    monkeypatch.setattr(analysis_queries, "count_with_timeout", count_with_timeout)

    session = Session()
    result = await analysis_queries.get_deep_analysis_list(
        session,  # type: ignore[arg-type]
        page=999,
        per_page=1,
        cursor=10,
        status_filter="completed",
        engine_filter="openclaw",
        max_page=2,
    )

    assert result["page"] == 2
    assert result["per_page"] == 1
    assert result["has_more"] is True
    assert result["next_cursor"] == 3
    assert result["items"] == [
        {
            "id": 3,
            "webhook_event_id": 30,
            "engine": "openclaw",
            "user_question": "why",
            "analysis_result": {"summary": "third"},
            "duration_seconds": 1.2,
            "created_at": "2026-05-27T08:00:00Z",
            "openclaw_run_id": "run-3",
            "openclaw_session_key": "session-3",
            "status": "completed",
            "poll_attempts": 1,
            "next_poll_at": None,
            "last_polled_at": "2026-05-27T08:00:00Z",
            "source": "prometheus",
            "is_duplicate": True,
        }
    ]

    analyses = await analysis_queries.get_deep_analyses_for_webhook(session, webhook_id=30)  # type: ignore[arg-type]
    assert analyses == [records[0][0]]


def test_ecosystem_adapter_helpers_volcengine_and_feishu_card() -> None:
    from adapters import ecosystem_adapters as adapters
    from adapters.normalized import extract_alert_identity

    assert adapters._header_get({"X-WEBHOOK-SOURCE": "Grafana"}, "x-webhook-source") == "Grafana"
    assert adapters._pick_first(None, "", "value") == "value"
    assert adapters._pick_label({"Severity": "critical"}, "severity") == "critical"
    assert adapters._extract_tag(["host:web-01", "service:api"], "service") == "api"
    assert adapters._safe_resource_list([{"id": "one"}, "bad"]) == [{"id": "one"}]
    assert adapters._pick_first_resource([{"Dimensions": [{"Name": "Pod", "Value": "pod-a"}]}]) == "pod-a"
    assert adapters.normalize_level("service recovered normally") == "info"

    volc = adapters.normalize_webhook_event(
        {
            "Namespace": "VCM_ECS",
            "Resources": [{"Dimensions": [{"Name": "InstanceId", "Value": "i-123"}]}],
            "RuleName": "CPUHigh",
            "Level": "严重",
            "AlertId": "alert-1",
        },
        "vcm",
        {},
    )
    assert volc.adapter == "volcengine"
    volc_identity = extract_alert_identity(volc.data)
    assert volc_identity["severity"] == "critical"
    assert volc_identity["resource"] == "i-123"

    feishu = adapters.normalize_webhook_event(
        {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"content": "日志告警"}},
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "\n".join(
                            [
                                "告警策略：错误日志突增",
                                "告警日志主题：app-log",
                                "告警级别：警告",
                                "首次触发时间：2026-05-27 08:00:00",
                                "触发条件：count > 10",
                                "当前查询结果：42",
                            ]
                        ),
                    }
                ],
            },
        },
        "volcengine_log",
        {},
    )
    assert feishu.adapter == "feishu_card"
    assert feishu.data["RuleName"] == "错误日志突增"
    assert feishu.data["MetricName"] == "app-log"
    assert feishu.data["Level"] == "warning"
    assert feishu.data["first_trigger_time"] == "2026-05-27 08:00:00"
    assert feishu.data["trigger_condition"] == "count > 10"
    assert feishu.data["query_result"] == "42"
    assert feishu.data["Resources"] == [{"InstanceId": "app-log"}]


@pytest.mark.asyncio
async def test_app_context_lazy_resources_close_and_dependency_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import app_context

    built: list[str] = []

    class HTTPClient:
        def __init__(self) -> None:
            self.is_closed = False

        async def aclose(self) -> None:
            self.is_closed = True
            built.append("http_closed")

    class RedisClient:
        pass

    class Engine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True
            built.append("db_disposed")

    http_client = HTTPClient()
    redis_client = RedisClient()
    engine = Engine()
    session_factory = object()

    monkeypatch.setattr("core.http_client.build_http_client", lambda _config: built.append("http") or http_client)
    monkeypatch.setattr("core.redis_client.build_redis_client", lambda _config: built.append("redis") or redis_client)
    monkeypatch.setattr(
        "db.engine.build_engine_and_session_factory",
        lambda _config: (built.append("db") or engine, session_factory),
    )

    async def close_redis_client(_client: object) -> None:
        built.append("redis_closed")

    monkeypatch.setattr("core.redis_client.close_redis_client", close_redis_client)

    context = app_context.AppContext()
    assert await context.ensure_http_client() is http_client
    assert context.ensure_redis_client() is redis_client
    assert await context.ensure_db() is session_factory
    await context.close()

    assert built == ["http", "redis", "db", "db_disposed", "redis_closed", "http_closed"]
    assert context.http_client is None
    assert context.redis_client is None
    assert context.db_engine is None
    assert context.session_factory is None

    config = context.config
    assert app_context.init_default_app_context(config).config is config
    assert app_context.get_or_create_default_app_context().config is config
    assert app_context.get_config_manager() is config

    live_client = httpx.AsyncClient()
    fallback_client = httpx.AsyncClient()
    try:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_context=app_context.AppContext(http_client=live_client))))
        assert app_context.get_http_client_dependency(request) is live_client
        request.app.state.app_context = object()
        monkeypatch.setattr("core.http_client.get_http_client", lambda: fallback_client)
        assert app_context.get_http_client_dependency(request) is fallback_client
    finally:
        await live_client.aclose()
        await fallback_client.aclose()
