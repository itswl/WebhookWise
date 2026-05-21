from __future__ import annotations

from typing import Any

import pytest


class _ServerConfig:
    ENABLE_RUNTIME_CONFIG = True


class _AIConfig:
    ENABLE_AI_ANALYSIS = True
    OPENAI_API_KEY = "test-key"


class _Config:
    server = _ServerConfig()
    ai = _AIConfig()

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def load_from_db(self) -> None:
        self.calls.append("config.load_from_db")

    async def start_subscriber(self) -> None:
        self.calls.append("config.start_subscriber")

    async def stop_subscriber(self) -> None:
        self.calls.append("config.stop_subscriber")


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

    calls: list[str] = []

    async def init_engine() -> None:
        calls.append("init")

    async def test_db_connection() -> bool:
        calls.append("probe")
        return True

    monkeypatch.setattr(lifecycle, "init_engine", init_engine)
    monkeypatch.setattr(lifecycle, "test_db_connection", test_db_connection)

    assert await lifecycle.check_database_ready() is True
    assert calls == ["init", "probe"]


@pytest.mark.asyncio
async def test_start_runtime_services_initializes_requested_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.service_lifecycle as lifecycle

    config = _Config()
    calls = config.calls
    http_client = object()
    broker = _Broker(calls)

    def record(name: str) -> None:
        calls.append(name)

    async def record_async(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(lifecycle, "initialize_adapters", lambda: record("adapters"))
    monkeypatch.setattr(lifecycle, "get_http_client", lambda: http_client)
    monkeypatch.setattr(lifecycle, "init_redis", lambda: record("redis"))
    monkeypatch.setattr(lifecycle, "init_engine", lambda: record_async("db"))

    async def initialize_ai_client(*, http_client: Any) -> None:
        assert http_client is services_http_client
        calls.append("ai")

    services_http_client = http_client
    monkeypatch.setattr(lifecycle, "initialize_openai_client", initialize_ai_client)

    services = await lifecycle.start_runtime_services(
        config,  # type: ignore[arg-type]
        broker=broker,
        start_broker=True,
        initialize_logger=lambda: record("logger"),
        initialize_observability=lambda: record("observability"),
        initialize_redis_client=True,
        initialize_ai_client=True,
    )

    assert services.http_client is http_client
    assert calls == [
        "logger",
        "observability",
        "adapters",
        "db",
        "redis",
        "config.load_from_db",
        "config.start_subscriber",
        "ai",
        "broker.startup",
    ]


@pytest.mark.asyncio
async def test_stop_runtime_services_tears_down_requested_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.service_lifecycle as lifecycle

    config = _Config()
    calls = config.calls
    broker = _Broker(calls)

    async def record_async(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(lifecycle, "dispose_engine", lambda: record_async("db.dispose"))
    monkeypatch.setattr(lifecycle, "dispose_redis", lambda: record_async("redis.dispose"))
    monkeypatch.setattr(lifecycle, "reset_openai_client", lambda: record_async("ai.reset"))
    monkeypatch.setattr(lifecycle, "close_http_client", lambda: record_async("http.close"))
    monkeypatch.setattr(lifecycle, "stop_log_listener", lambda: calls.append("logger.stop"))

    await lifecycle.stop_runtime_services(
        config,  # type: ignore[arg-type]
        broker=broker,
        stop_broker=True,
        reset_ai_client=True,
        shutdown_observability=lambda: calls.append("observability.shutdown"),
        stop_logger=True,
    )

    assert calls == [
        "config.stop_subscriber",
        "broker.shutdown",
        "db.dispose",
        "redis.dispose",
        "ai.reset",
        "http.close",
        "observability.shutdown",
        "logger.stop",
    ]
