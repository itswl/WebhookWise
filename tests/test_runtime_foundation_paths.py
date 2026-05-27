from __future__ import annotations

import builtins
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response
from redis.exceptions import RedisError

from tests.metric_helpers import MetricCall, StubMetric


@pytest.mark.asyncio
async def test_redis_stream_helpers_coerce_pending_and_lag_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import redis_streams

    class Redis:
        def __init__(self) -> None:
            self.pending: object = {"pending": "4"}
            self.groups: object = [{"name": "workers", "lag": "9"}]

        async def xlen(self, stream: str) -> str:
            assert stream == "stream:test"
            return "12"

        async def xpending(self, stream: str, group: str) -> object:
            assert (stream, group) == ("stream:test", "workers")
            return self.pending

        async def xinfo_groups(self, stream: str) -> object:
            assert stream == "stream:test"
            return self.groups

    redis = Redis()

    async def record_redis_operation(_operation: str, awaitable: object) -> object:
        return await awaitable  # type: ignore[misc]

    monkeypatch.setattr(redis_streams, "get_redis", lambda: redis)
    monkeypatch.setattr(redis_streams, "record_redis_operation", record_redis_operation)

    assert await redis_streams.redis_xlen("stream:test") == 12
    assert await redis_streams.redis_xpending_pending("stream:test", "workers") == 4
    redis.pending = ("5", "consumer")
    assert await redis_streams.redis_xpending_pending("stream:test", "workers") == 5
    redis.pending = {"pending": "bad"}
    assert await redis_streams.redis_xpending_pending("stream:test", "workers") == 0

    assert await redis_streams.redis_xinfo_group_lag("stream:test", "workers") == 9
    redis.groups = [{"name": "other", "lag": 1}, ["not-a-dict"]]
    assert await redis_streams.redis_xinfo_group_lag("stream:test", "workers") == 0
    redis.groups = "not-a-list"
    assert await redis_streams.redis_xinfo_group_lag("stream:test", "workers") == 0


@pytest.mark.asyncio
async def test_metrics_poller_refreshes_db_and_mq_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations import metrics_poller

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(metrics_poller, "WEBHOOK_PROCESSING_STATUS_COUNT", StubMetric(metric_calls, "status"))
    monkeypatch.setattr(metrics_poller, "WEBHOOK_MQ_STREAM_LENGTH", StubMetric(metric_calls, "stream_length"))
    monkeypatch.setattr(metrics_poller, "WEBHOOK_MQ_GROUP_PENDING", StubMetric(metric_calls, "pending"))
    monkeypatch.setattr(metrics_poller, "WEBHOOK_MQ_GROUP_LAG", StubMetric(metric_calls, "lag"))
    monkeypatch.setattr(metrics_poller, "DATABASE_EVENTS_COUNT", StubMetric(metric_calls, "events"))

    class StatusResult:
        def all(self) -> list[tuple[str | None, int]]:
            return [("completed", 3), ("dead_letter", 2), ("ignored", 99), (None, 1)]

    class CountResult:
        def scalar(self) -> int:
            return 11

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _stmt: object) -> object:
            self.calls += 1
            return StatusResult() if self.calls == 1 else CountResult()

    session = Session()

    @asynccontextmanager
    async def session_scope() -> Any:
        yield session

    async def redis_xlen(queue: str) -> int:
        assert queue
        return 8

    async def redis_xpending_pending(queue: str, group: str) -> int:
        assert queue and group
        return 4

    async def redis_xinfo_group_lag(queue: str, group: str) -> int:
        assert queue and group
        return 6

    monkeypatch.setattr(metrics_poller, "session_scope", session_scope)
    monkeypatch.setattr(metrics_poller, "redis_xlen", redis_xlen)
    monkeypatch.setattr(metrics_poller, "redis_xpending_pending", redis_xpending_pending)
    monkeypatch.setattr(metrics_poller, "redis_xinfo_group_lag", redis_xinfo_group_lag)

    await metrics_poller.refresh_all_metrics(mq_queue="queue:test", mq_consumer_group="group:test")

    assert ("status", (), {"status": "completed"}, "set", 3) in metric_calls
    assert ("status", (), {"status": "dead_letter"}, "set", 2) in metric_calls
    assert ("stream_length", (), {"stream": "webhook:queue"}, "set", 8) in metric_calls
    assert ("pending", (), {"stream": "webhook:queue", "group": "webhook-processors"}, "set", 4) in metric_calls
    assert ("lag", (), {"stream": "webhook:queue", "group": "webhook-processors"}, "set", 6) in metric_calls
    assert ("events", (), {}, "set", 11) in metric_calls


@pytest.mark.asyncio
async def test_metrics_poller_tolerates_redis_and_event_count_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations import metrics_poller

    class Session:
        async def execute(self, _stmt: object) -> object:
            raise RuntimeError("database unavailable")

    @asynccontextmanager
    async def session_scope() -> Any:
        yield Session()

    async def fail_xlen(_queue: str) -> int:
        raise RedisError("xlen down")

    async def fail_pending(_queue: str, _group: str) -> int:
        raise RedisError("pending down")

    monkeypatch.setattr(metrics_poller, "session_scope", session_scope)
    monkeypatch.setattr(metrics_poller, "redis_xlen", fail_xlen)
    monkeypatch.setattr(metrics_poller, "redis_xpending_pending", fail_pending)

    await metrics_poller._refresh_db_event_count()
    await metrics_poller._refresh_mq_stats(mq_queue="queue:test", mq_consumer_group="group:test")


def test_webhook_signature_and_token_auth_contracts() -> None:
    from api import InvalidSignatureError
    from core import webhook_security

    payload = b'{"alertname":"HighCPU"}'
    signature = webhook_security.hmac.new(b"secret", payload, webhook_security.hashlib.sha256).hexdigest()

    assert webhook_security.verify_signature(payload, signature, "secret") is True
    assert webhook_security.verify_signature(payload, "bad", "secret") is False
    assert webhook_security.extract_token({"authorization": "Token secret"}) == "secret"
    assert webhook_security.extract_token({"token": "direct"}) == "direct"

    webhook_security.ensure_webhook_auth({"x-webhook-signature": signature}, payload, secret="secret")
    webhook_security.ensure_webhook_auth({"token": "secret"}, payload, secret="secret")
    with pytest.raises(InvalidSignatureError):
        webhook_security.ensure_webhook_auth({"token": "wrong"}, payload, secret="secret")
    with pytest.raises(InvalidSignatureError):
        webhook_security.ensure_webhook_auth({"x-webhook-signature": signature}, payload, secret="")


@pytest.mark.asyncio
async def test_webhook_auth_dependency_body_size_and_auth_branches(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from core import webhook_security

    class Request:
        def __init__(self, headers: dict[str, str], body: bytes = b"{}") -> None:
            self.headers = headers
            self.state = SimpleNamespace()
            self._body = body

        async def body(self) -> bytes:
            return self._body

    monkeypatch.setattr(temp_config.security, "MAX_WEBHOOK_BODY_BYTES", 4)
    monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", True)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "secret")

    with pytest.raises(HTTPException) as oversized:
        await webhook_security.verify_webhook_auth_dep(Request({"content-length": "5"}), config=temp_config)
    assert oversized.value.status_code == 413

    monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", False)
    await webhook_security.verify_webhook_auth_dep(Request({"content-length": "not-int"}), config=temp_config)

    monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", True)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "")
    with pytest.raises(HTTPException) as missing_secret:
        await webhook_security.verify_webhook_auth_dep(Request({}), config=temp_config)
    assert missing_secret.value.status_code == 401

    monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "secret")
    request = Request({"token": "secret"}, body=b'{"ok":true}')
    await webhook_security.verify_webhook_auth_dep(request, config=temp_config)
    assert request.state.raw_body == b'{"ok":true}'


@pytest.mark.asyncio
async def test_rate_limit_enforcement_and_dependency_fail_open_closed(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from core import redis_health, webhook_security

    request = SimpleNamespace(path_params={"source": "prometheus"}, query_params={}, headers={})
    response = Response()
    tiers_seen: list[tuple[str, int, int]] = []

    monkeypatch.setattr(webhook_security, "get_client_ip", lambda _request: "1.2.3.4")

    async def check_tier(prefix: str, window: int, limit: int, now: float) -> object:
        tiers_seen.append((prefix, window, limit))
        remaining = 3 if "b:" in prefix else 1
        return webhook_security._TierResult(allowed=True, remaining=remaining, limit=limit, reset_at=now + window)

    monkeypatch.setattr(webhook_security, "_check_tier", check_tier)
    limited_ip, tier = await webhook_security.enforce_webhook_rate_limit(
        request,  # type: ignore[arg-type]
        security_config=SimpleNamespace(
            WEBHOOK_RATE_LIMIT_PER_MINUTE=10,
            WEBHOOK_RATE_LIMIT_BURST=5,
            WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE=20,
        ),
    )

    assert limited_ip is None
    assert tier is not None
    assert tier.remaining == 1
    assert tiers_seen[0][0] == "rl:b:1.2.3.4"
    assert tiers_seen[-1][0] == "rl:g"

    async def deny_tier(prefix: str, window: int, limit: int, now: float) -> object:
        return webhook_security._TierResult(allowed=False, remaining=0, limit=limit, reset_at=now + window)

    monkeypatch.setattr(webhook_security, "_check_tier", deny_tier)
    limited_ip, denied = await webhook_security.enforce_webhook_rate_limit(
        request,  # type: ignore[arg-type]
        security_config=SimpleNamespace(
            WEBHOOK_RATE_LIMIT_PER_MINUTE=0,
            WEBHOOK_RATE_LIMIT_BURST=1,
            WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE=0,
        ),
    )
    assert limited_ip == "1.2.3.4"
    assert denied is not None
    assert denied.allowed is False

    async def redis_unavailable(_operation: str) -> bool:
        return False

    monkeypatch.setattr(redis_health, "ensure_redis_available", redis_unavailable)
    monkeypatch.setattr(temp_config.security, "RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR", True)
    await webhook_security.check_rate_limit_dep(request, response, config=temp_config)  # type: ignore[arg-type]

    monkeypatch.setattr(temp_config.security, "RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR", False)
    with pytest.raises(HTTPException) as rejected:
        await webhook_security.check_rate_limit_dep(request, response, config=temp_config)  # type: ignore[arg-type]
    assert rejected.value.status_code == 503


def test_otel_exporter_env_parsing_and_signal_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.observability import exporters

    monkeypatch.setenv("INT_VALUE", "12")
    monkeypatch.setenv("BAD_INT", "bad")
    monkeypatch.setenv("FLAG_ON", "yes")
    monkeypatch.setenv("FLAG_OFF", "0")
    assert exporters.env_int("INT_VALUE", 1) == 12
    assert exporters.env_int("BAD_INT", 7) == 7
    assert exporters.env_flag("FLAG_ON") is True
    assert exporters.env_flag("FLAG_OFF", default=True) is False
    assert exporters.parse_headers("a=b, empty, c = d=e ") == {"a": "b", "c": "d=e"}

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    assert exporters.otlp_protocol() == "http/protobuf"
    assert exporters.signal_endpoint("traces") == "http://collector:4318/v1/traces"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "collector:4317")
    assert exporters.signal_endpoint("metrics") == "collector:4317"


def test_otel_build_exporter_uses_protocol_specific_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.observability import exporters

    built: list[tuple[str, dict[str, object]]] = []

    class Exporter:
        def __init__(self, **kwargs: object) -> None:
            built.append((self.__class__.__name__, dict(kwargs)))

    def fake_import(name: str, globals: object = None, locals: object = None, fromlist: tuple[str, ...] = (), level: int = 0) -> object:
        if name.startswith("opentelemetry.exporter.otlp"):
            return SimpleNamespace(OTLPSpanExporter=Exporter)
        return original_import(name, globals, locals, fromlist, level)

    original_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "3.5")

    exporter = exporters.build_span_exporter()

    assert exporter is not None
    assert built == [
        (
            "Exporter",
            {
                "endpoint": "http://collector:4318/v1/traces",
                "headers": {"authorization": "secret"},
                "timeout": 3,
            },
        )
    ]


def test_otel_logging_setup_and_shutdown_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.observability import logging as otel_logging

    warnings: list[str] = []

    class Logger:
        def warning(self, message: str, *args: object) -> None:
            warnings.append(message % args)

        def debug(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(otel_logging, "_provider_initialized", False)
    monkeypatch.setattr(otel_logging, "_handler_installed", False)
    monkeypatch.setattr(otel_logging, "_log_provider", None)
    monkeypatch.setattr(otel_logging, "otel_enabled", lambda: True)
    monkeypatch.setattr(otel_logging, "env_flag", lambda _name, default=False: True)
    monkeypatch.setattr(otel_logging, "build_log_exporter", lambda: None)
    original_get_logger = otel_logging.logging.getLogger

    def get_logger(name: str | None = None) -> object:
        if name == "webhook_service":
            return Logger()
        return original_get_logger(name)

    monkeypatch.setattr(otel_logging.logging, "getLogger", get_logger)

    otel_logging.setup_logging(logger_name="webhook_service")
    assert warnings == ["[OTEL] logs enabled but no log exporter is configured"]

    class Provider:
        def force_flush(self) -> None:
            raise RuntimeError("flush failed")

        def shutdown(self) -> None:
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(otel_logging, "_log_provider", Provider())
    monkeypatch.setattr(otel_logging, "_provider_initialized", True)
    monkeypatch.setattr(otel_logging, "_handler_installed", True)

    otel_logging.shutdown_logging()

    assert otel_logging._log_provider is None
    assert otel_logging._provider_initialized is False
    assert otel_logging._handler_installed is False


@pytest.mark.real_httpx
def test_compression_and_http_client_lazy_context_paths(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from core import compression
    from core.app_context import AppContext, set_default_app_context
    from core.http_client import build_http_client, get_http_client

    monkeypatch.setattr(temp_config.server, "PAYLOAD_COMPRESS_THRESHOLD_BYTES", 16)
    monkeypatch.setattr(temp_config.server, "PAYLOAD_DECOMPRESS_ASYNC_THRESHOLD_BYTES", 16)
    monkeypatch.setattr(temp_config.retry, "FORWARD_TIMEOUT", 2)

    small = compression.compress_payload("hello")
    large = compression.compress_payload("x" * 64)

    assert small == b"hello"
    assert large is not None and large != b"x" * 64
    assert compression.decompress_payload(large) == "x" * 64
    assert compression.decompress_payload("\\x68656c6c6f") == "hello"

    client = build_http_client(temp_config)
    assert client.timeout.read == 2
    assert client.follow_redirects is False

    context = AppContext(config=temp_config)
    set_default_app_context(context)
    try:
        assert get_http_client() is context.http_client
    finally:
        set_default_app_context(None)
