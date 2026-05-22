from __future__ import annotations

from typing import Any

import pytest


class _ServerConfig:
    RUN_MODE = "api"


class _AIConfig:
    ENABLE_AI_ANALYSIS = True
    OPENAI_API_KEY = "test-key"


class _Config:
    server = _ServerConfig()
    ai = _AIConfig()

    def __init__(self) -> None:
        self.calls: list[str] = []


class _Broker:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def startup(self) -> None:
        self.calls.append("broker.startup")

    async def shutdown(self) -> None:
        self.calls.append("broker.shutdown")


@pytest.mark.asyncio
async def test_check_database_ready_initializes_engine_before_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.service_lifecycle as lifecycle
    from core.app_context import AppContext, set_default_app_context

    calls: list[str] = []

    async def ensure_db(self: AppContext) -> object:
        calls.append("init")
        return object()

    async def test_db_connection() -> bool:
        calls.append("probe")
        return True

    context = AppContext(config=_Config())  # type: ignore[arg-type]
    monkeypatch.setattr(AppContext, "ensure_db", ensure_db)
    monkeypatch.setattr(lifecycle, "test_db_connection", test_db_connection)

    assert await lifecycle.check_database_ready(context) is True
    assert calls == ["init", "probe"]
    set_default_app_context(None)


@pytest.mark.asyncio
async def test_start_runtime_services_initializes_requested_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.service_lifecycle as lifecycle
    from core.app_context import AppContext, set_default_app_context

    config = _Config()
    calls = config.calls
    http_client = object()
    broker = _Broker(calls)
    context = AppContext(config=config)  # type: ignore[arg-type]

    def record(name: str) -> None:
        calls.append(name)

    async def ensure_http_client(self: AppContext) -> object:
        calls.append("http")
        return http_client

    async def ensure_db(self: AppContext) -> object:
        calls.append("db")
        return object()

    def ensure_redis_client(self: AppContext) -> object:
        calls.append("redis")
        return object()

    monkeypatch.setattr(lifecycle, "initialize_adapters", lambda: record("adapters"))
    monkeypatch.setattr(AppContext, "ensure_http_client", ensure_http_client)
    monkeypatch.setattr(AppContext, "ensure_db", ensure_db)
    monkeypatch.setattr(AppContext, "ensure_redis_client", ensure_redis_client)

    async def initialize_ai_client(*, http_client: Any) -> None:
        assert http_client is services_http_client
        calls.append("ai")

    services_http_client = http_client

    services = await lifecycle.start_runtime_services(
        config,  # type: ignore[arg-type]
        context=context,
        broker=broker,
        start_broker=True,
        initialize_logger=lambda: record("logger"),
        initialize_observability=lambda: record("observability"),
        initialize_redis_client=True,
        initialize_ai_client=True,
        initialize_ai_client_hook=initialize_ai_client,
    )

    assert services.http_client is http_client
    assert services.app_context is context
    assert calls == [
        "logger",
        "observability",
        "adapters",
        "http",
        "db",
        "redis",
        "ai",
        "broker.startup",
    ]
    set_default_app_context(None)


@pytest.mark.asyncio
async def test_stop_runtime_services_tears_down_requested_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.service_lifecycle as lifecycle
    from core.app_context import AppContext, set_default_app_context

    config = _Config()
    calls = config.calls
    broker = _Broker(calls)
    context = AppContext(config=config)  # type: ignore[arg-type]

    async def record_async(name: str) -> None:
        calls.append(name)

    async def close_context(self: AppContext, *, close_redis: bool = True, **kwargs: object) -> None:
        calls.append("db.dispose")
        if close_redis:
            calls.append("redis.dispose")
        calls.append("http.close")

    monkeypatch.setattr(AppContext, "close", close_context)
    monkeypatch.setattr(lifecycle, "stop_log_listener", lambda: calls.append("logger.stop"))

    await lifecycle.stop_runtime_services(
        config,  # type: ignore[arg-type]
        context=context,
        broker=broker,
        stop_broker=True,
        reset_ai_client=True,
        reset_ai_client_hook=lambda: record_async("ai.reset"),
        shutdown_observability=lambda: calls.append("observability.shutdown"),
        stop_logger=True,
    )

    assert calls == [
        "broker.shutdown",
        "ai.reset",
        "db.dispose",
        "redis.dispose",
        "http.close",
        "observability.shutdown",
        "logger.stop",
    ]
    set_default_app_context(None)
