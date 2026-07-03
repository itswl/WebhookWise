from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import sys
from contextlib import asynccontextmanager, contextmanager
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials

from tests.helpers.metric_helpers import MetricActionCall, StubMetric


@pytest.mark.asyncio
@pytest.mark.real_redis
async def test_redis_client_build_close_dispose_and_all_wrapper_coercions(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from core import redis_client
    from core.app_context import AppContext, set_default_app_context

    pools: list[dict[str, object]] = []

    class Pool:
        def __init__(self) -> None:
            self.disconnected = False

        async def disconnect(self) -> None:
            self.disconnected = True

    class Redis:
        def __init__(self, connection_pool: object | None = None) -> None:
            self.connection_pool = connection_pool or Pool()
            self.closed = False
            self.values: dict[str, object] = {
                "string": b"value",
                "json": '{"ok":true}',
                "json-list": "[1,2]",
                "json-bad": "{",
            }

        async def aclose(self) -> None:
            self.closed = True

        async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
            assert (key, value, nx, ex) == ("lock", "token", True, 30)
            return True

        async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
            return b"7"

        async def evalsha(self, _sha: str, _numkeys: int, *_args: object) -> object:
            return b"7"

        async def get(self, key: str) -> object:
            return self.values.get(key)

        async def setex(self, key: str, ttl: int, value: object) -> bool:
            self.values[key] = (ttl, value)
            return True

        async def delete(self, key: str) -> str:
            assert key == "gone"
            return "2"

        async def publish(self, channel: str, message: str) -> str:
            assert (channel, message) == ("events", "hello")
            return "3"

        async def incr(self, key: str) -> str:
            assert key == "counter"
            return "4"

        async def expire(self, key: str, ttl: int) -> bool:
            assert (key, ttl) == ("counter", 60)
            return True

        async def ping(self) -> bool:
            return True

    def from_url(url: str, **kwargs: object) -> Pool:
        pools.append({"url": url, **kwargs})
        return Pool()

    monkeypatch.setattr(redis_client.redis.ConnectionPool, "from_url", from_url)
    monkeypatch.setattr(redis_client.redis, "Redis", Redis)
    built = redis_client.build_redis_client(temp_config)
    assert isinstance(built, Redis)
    assert pools[0]["url"] == temp_config.redis.REDIS_URL
    assert pools[0]["decode_responses"] is True

    context = AppContext(config=temp_config, redis_client=built)
    set_default_app_context(context)
    try:
        assert redis_client.get_redis() is built
        assert await redis_client.redis_set_nx_ex("lock", "token", 30) is True
        assert await redis_client.redis_eval_int("return 7", 0) == 7
        assert await redis_client.redis_eval_str("return 7", 0) == "7"
        assert await redis_client.redis_get_str("string") == "value"
        await redis_client.redis_setex_str("k", 10, "v")
        assert await redis_client.redis_delete("gone") == 2
        assert await redis_client.redis_publish("events", "hello") == 3
        # redis_incr_with_expire now runs a single Lua script (INCR + EXPIRE),
        # so it goes through eval/evalsha rather than separate incr/expire calls.
        assert await redis_client.redis_incr_with_expire("counter", 60) == 7
        assert await redis_client.redis_ping() is True
        assert await redis_client.redis_get_json_dict("json") == {"ok": True}
        assert await redis_client.redis_get_json_dict("json-list") is None
        assert await redis_client.redis_get_json_dict("json-bad") is None
        await redis_client.redis_setex_json("payload", 9, {"a": 1})
        assert built.values["payload"] == (9, '{"a":1}')

        assert redis_client.parse_int(None) is None
        assert redis_client.parse_int("bad") is None
        assert redis_client.coerce_int("bad", default=12) == 12
        assert redis_client.coerce_str(None) is None
        assert redis_client.coerce_str(object()).startswith("<object object at")

        await redis_client.dispose_redis()
        assert built.closed is True
        assert context.redis_client is None
    finally:
        set_default_app_context(None)


@pytest.mark.asyncio
async def test_redis_record_operation_error_and_ping_failure_update_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import redis_client

    metric_calls: list[MetricActionCall] = []
    failures: list[tuple[str, str]] = []
    successes: list[str] = []

    @contextmanager
    def span(_name: str, _attrs: dict[str, object]) -> Any:
        yield None

    monkeypatch.setattr(
        "core.observability.metrics.REDIS_OPERATIONS_TOTAL",
        StubMetric(metric_calls, "total", record_kwargs=False),
    )
    monkeypatch.setattr(
        "core.observability.metrics.REDIS_OPERATION_DURATION_SECONDS",
        StubMetric(metric_calls, "duration", record_kwargs=False),
    )
    monkeypatch.setattr("core.redis_health.mark_redis_failure", lambda op, err: failures.append((op, str(err))))
    monkeypatch.setattr("core.redis_health.mark_redis_success", lambda op: successes.append(op))
    monkeypatch.setattr("core.observability.tracing.otel_span", span)

    async def fail() -> object:
        raise RuntimeError("redis down")

    with pytest.raises(RuntimeError, match="redis down"):
        await redis_client.record_redis_operation("get", fail())

    assert failures == [("get", "redis down")]
    assert any(call[:3] == ("total", ("get", "error"), "inc") for call in metric_calls)

    class Redis:
        async def ping(self) -> bool:
            raise RuntimeError("ping down")

    monkeypatch.setattr(redis_client, "get_redis", lambda: Redis())
    assert await redis_client.redis_ping() is False


@pytest.mark.asyncio
async def test_auth_api_key_and_admin_write_branches(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from core import auth

    class Request:
        client = SimpleNamespace(host="1.2.3.4")
        url = SimpleNamespace(path="/v1/admin")
        method = "POST"
        headers = {"authorization": " Bearer bad  "}
        query_params = {}

        async def body(self) -> bytes:
            return b'{"secret":"value"}'

    request = Request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="api-token")
    admin_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="admin-token")
    bad_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad")

    assert auth._body_meta(b"") == {"size": 0, "sha256": None}
    assert auth._body_meta(b"abc")["size"] == 3
    assert auth._matches_any_configured_token("api-token", "x", "api-token") is True
    assert auth._matches_any_configured_token(None, "api-token") is False

    monkeypatch.setattr(temp_config.security, "API_KEY", "")
    with pytest.raises(HTTPException) as missing_api_key:
        await auth.verify_api_key(request, credentials, temp_config)
    assert missing_api_key.value.status_code == 401

    monkeypatch.setattr(temp_config.security, "API_KEY", "api-token")
    monkeypatch.setattr(temp_config.security, "ADMIN_WRITE_KEY", "admin-token")
    assert await auth.verify_api_key(request, credentials, temp_config) is True
    with pytest.raises(HTTPException) as admin_key_rejected_by_api_endpoint:
        await auth.verify_api_key(request, admin_credentials, temp_config)
    assert admin_key_rejected_by_api_endpoint.value.status_code == 401

    monkeypatch.setattr(auth.logger, "isEnabledFor", lambda _level: False)
    with pytest.raises(HTTPException) as invalid_api_key:
        await auth.verify_api_key(request, bad_credentials, temp_config)
    assert invalid_api_key.value.status_code == 401

    monkeypatch.setattr(temp_config.security, "ADMIN_WRITE_KEY", "")
    with pytest.raises(HTTPException) as missing_admin_key:
        await auth.verify_admin_write(request, admin_credentials, temp_config)
    assert missing_admin_key.value.status_code == 403

    monkeypatch.setattr(temp_config.security, "ADMIN_WRITE_KEY", "admin-token")
    assert await auth.verify_admin_write(request, admin_credentials, temp_config) is True

    mixed_token_request = Request()
    mixed_token_request.headers = {"x-admin-write-key": "admin-token"}
    mixed_token_request.query_params = {}
    assert await auth.verify_admin_write(mixed_token_request, credentials, temp_config) is True

    with pytest.raises(HTTPException) as invalid_admin_key:
        await auth.verify_admin_write(request, bad_credentials, temp_config)
    assert invalid_admin_key.value.status_code == 403

    api_as_admin_request = Request()
    api_as_admin_request.client = None
    api_as_admin_request.headers = {}
    api_as_admin_request.query_params = {}
    with pytest.raises(HTTPException) as api_key_is_read_only:
        await auth.verify_admin_write(api_as_admin_request, credentials, temp_config)
    assert api_key_is_read_only.value.status_code == 403
    assert "API key is insufficient" in str(api_key_is_read_only.value.detail)

@pytest.mark.asyncio
async def test_url_security_edge_cases_private_policy_and_dns_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import url_security
    from core.url_security import OutboundURLPolicy, UnsafeTargetUrlError, validate_outbound_url

    url_security._DNS_CACHE.clear()
    public_policy = OutboundURLPolicy(allow_private_target_urls=False, target_allowlist=("*.example.com",))
    private_policy = OutboundURLPolicy(allow_private_target_urls=True, target_allowlist=())

    assert url_security._host_matches_pattern("api.example.com", "*.example.com") is True
    assert url_security._host_matches_pattern("example.com", ".example.com") is True
    assert url_security._host_matches_pattern("bad.test", "") is False

    for candidate in ("", "ftp://example.com", "https:///hook", "https://user:pass@example.com/hook"):
        with pytest.raises(UnsafeTargetUrlError):
            await validate_outbound_url(candidate, policy=public_policy)

    with pytest.raises(UnsafeTargetUrlError):
        await validate_outbound_url("https://localhost/hook", policy=OutboundURLPolicy(False, ()))

    with pytest.raises(UnsafeTargetUrlError):
        await validate_outbound_url("https://evil.test/hook", policy=public_policy)

    assert await validate_outbound_url("http://10.0.0.1/hook", policy=private_policy) == "http://10.0.0.1/hook"

    def no_ips(_host: str, _port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return []

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [])
    with pytest.raises(UnsafeTargetUrlError, match="no usable"):
        url_security._resolved_ips("empty.example", 443)

    monkeypatch.setattr(url_security, "_resolved_ips", no_ips)
    assert await url_security._resolve_ips_cached("empty.example", 443) == []


@pytest.mark.asyncio
async def test_compression_threshold_fallback_and_async_thread_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import compression

    monkeypatch.setattr(compression, "_threshold", lambda _attr: 1)
    compressed = compression.compress_payload("payload")
    assert compressed is not None and compressed[:4] == compression._ZSTD_MAGIC

    calls: list[bytes] = []

    async def to_thread(fn: object, data: bytes) -> str:
        calls.append(data)
        return fn(data)  # type: ignore[misc]

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    assert await compression.decompress_payload_async(compressed) == "payload"
    assert calls == [compressed]

    monkeypatch.undo()
    monkeypatch.setattr(
        "core.app_context.get_config_manager", lambda: (_ for _ in ()).throw(RuntimeError("config down"))
    )
    assert compression._threshold("PAYLOAD_COMPRESS_THRESHOLD_BYTES") == 4096


def test_otel_logging_installs_handler_once_and_flushes_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.observability import logging as otel_logging

    providers: list[object] = []
    processors: list[object] = []

    class LoggerProvider:
        def __init__(self, *, resource: object) -> None:
            self.resource = resource
            self.flushed = False
            self.closed = False

        def add_log_record_processor(self, processor: object) -> None:
            processors.append(processor)

        def force_flush(self) -> None:
            self.flushed = True

        def shutdown(self) -> None:
            self.closed = True

    class BatchLogRecordProcessor:
        def __init__(self, exporter: object) -> None:
            self.exporter = exporter

    class LoggingHandler(logging.Handler):
        def __init__(self, *, level: int, logger_provider: object) -> None:
            super().__init__(level)
            self.logger_provider = logger_provider

        def emit(self, record: logging.LogRecord) -> None:
            return None

    logs_module = ModuleType("opentelemetry._logs")
    logs_module.set_logger_provider = lambda provider: providers.append(provider)  # type: ignore[attr-defined]
    logs_module.get_logger_provider = lambda: providers[-1]  # type: ignore[attr-defined]
    sdk_logs = ModuleType("opentelemetry.sdk._logs")
    sdk_logs.LoggerProvider = LoggerProvider  # type: ignore[attr-defined]
    sdk_logs.LoggingHandler = LoggingHandler  # type: ignore[attr-defined]
    sdk_logs_export = ModuleType("opentelemetry.sdk._logs.export")
    sdk_logs_export.BatchLogRecordProcessor = BatchLogRecordProcessor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry._logs", logs_module)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk._logs", sdk_logs)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk._logs.export", sdk_logs_export)

    app_logger = logging.getLogger("webhook_service.test_logging_runtime")
    old_handlers = list(app_logger.handlers)
    try:
        app_logger.handlers = []
        monkeypatch.setattr(otel_logging, "_provider_initialized", False)
        monkeypatch.setattr(otel_logging, "_handler_installed", False)
        monkeypatch.setattr(otel_logging, "_log_provider", None)
        monkeypatch.setattr(otel_logging, "otel_enabled", lambda: True)
        monkeypatch.setattr(otel_logging, "env_flag", lambda _name, default=False: True)
        monkeypatch.setattr(otel_logging, "build_log_exporter", lambda: "log-exporter")
        monkeypatch.setattr(otel_logging, "build_resource", lambda service_name=None: {"service.name": service_name})

        otel_logging.setup_logging(service_name="api", logger_name=app_logger.name)
        otel_logging.setup_logging(service_name="api", logger_name=app_logger.name)

        provider = providers[0]
        assert provider.resource == {"service.name": "api"}
        assert processors[0].exporter == "log-exporter"
        assert (
            len([handler for handler in app_logger.handlers if getattr(handler, "_webhookwise_otel_handler", False)])
            == 1
        )

        otel_logging.shutdown_logging()
        assert provider.flushed is True
        assert provider.closed is True
    finally:
        app_logger.handlers = old_handlers


def test_profiling_disabled_missing_server_import_and_span_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.observability import profiling

    emitted: list[tuple[str, dict[str, object]]] = []
    warnings: list[str] = []

    class Logger:
        def warning(self, message: str, *args: object) -> None:
            warnings.append(message % args)

    original_get_logger = profiling.logging.getLogger

    def get_logger(name: str | None = None) -> object:
        if name == "webhook_service":
            return Logger()
        return original_get_logger(name)

    monkeypatch.setattr(profiling, "_initialized", False)
    monkeypatch.setattr(profiling, "env_flag", lambda _name, default=False: False)
    assert profiling.profiles_enabled() is False
    profiling.setup_profiling(service_name="api")
    assert profiling._initialized is False

    monkeypatch.setattr(
        profiling,
        "env_flag",
        lambda name, default=False: name in {"PYROSCOPE_ENABLED", "PYROSCOPE_SPAN_PROFILES_ENABLED"},
    )
    monkeypatch.delenv("PYROSCOPE_SERVER_ADDRESS", raising=False)
    monkeypatch.delenv("PYROSCOPE_URL", raising=False)
    monkeypatch.setattr(profiling.logging, "getLogger", get_logger)
    profiling.setup_profiling(service_name="api")
    assert warnings == ["[Profiles] profiling enabled but PYROSCOPE_SERVER_ADDRESS is not configured"]
    assert profiling._initialized is True

    monkeypatch.setattr(profiling, "_initialized", False)
    monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
    monkeypatch.setenv("PYROSCOPE_AUTH_TOKEN", "token")
    monkeypatch.setenv("PYROSCOPE_BASIC_AUTH_USERNAME", "user")
    monkeypatch.setenv("PYROSCOPE_BASIC_AUTH_PASSWORD", "pass")
    monkeypatch.setenv("PYROSCOPE_TENANT_ID", "tenant")
    monkeypatch.setenv("PYROSCOPE_TAGS", "1bad=value,novalue,team.name=platform")
    pyroscope_module = ModuleType("pyroscope")
    pyroscope_module.__path__ = []  # type: ignore[attr-defined]
    pyroscope_module.configure = lambda **kwargs: emitted.append(("configure", kwargs))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyroscope", pyroscope_module)

    span_processors: list[object] = []
    import opentelemetry

    trace_module = ModuleType("opentelemetry.trace")
    trace_module.get_tracer_provider = lambda: SimpleNamespace(
        add_span_processor=lambda processor: span_processors.append(processor)
    )  # type: ignore[attr-defined]
    pyroscope_otel = ModuleType("pyroscope.otel")
    pyroscope_otel.PyroscopeSpanProcessor = lambda: "span-processor"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace_module)
    monkeypatch.setitem(sys.modules, "pyroscope.otel", pyroscope_otel)
    monkeypatch.setattr(opentelemetry, "trace", trace_module, raising=False)
    monkeypatch.setattr(pyroscope_module, "otel", pyroscope_otel, raising=False)
    monkeypatch.setattr(profiling, "emit_event", lambda name, attrs: emitted.append((name, dict(attrs))))

    profiling.setup_profiling(service_name="api")

    configured = emitted[0][1]
    assert configured["auth_token"] == "token"
    assert configured["basic_auth_username"] == "user"
    assert configured["basic_auth_password"] == "pass"
    assert configured["tenant_id"] == "tenant"
    assert configured["tags"]["_1bad"] == "value"
    assert configured["tags"]["team_name"] == "platform"
    assert span_processors == ["span-processor"]
    assert emitted[-1] == ("profiles.started", {"profile.backend": "pyroscope", "profile.application": "api"})


def test_db_engine_build_pool_metrics_and_healthcheck_paths(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from db import engine as db_engine

    monkeypatch.setattr(temp_config.db, "DATABASE_URL", "postgres://user:p%40ss@example/db")
    assert db_engine._async_url(temp_config).startswith("postgresql+asyncpg://")
    kwargs = db_engine._build_engine_kwargs(temp_config)
    assert kwargs["connect_args"]["server_settings"]["statement_timeout"] == str(temp_config.db.DB_STATEMENT_TIMEOUT_MS)

    class Pool:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        def size(self) -> int:
            if self.fail:
                raise RuntimeError("pool down")
            return 5

        def overflow(self) -> int:
            return 2

        def checkedout(self) -> int:
            if self.fail:
                raise RuntimeError("pool down")
            return 3

    good_engine = SimpleNamespace(sync_engine=SimpleNamespace(pool=Pool()))
    bad_engine = SimpleNamespace(sync_engine=SimpleNamespace(pool=Pool(fail=True)))
    no_methods_engine = SimpleNamespace(sync_engine=SimpleNamespace(pool=object()))

    assert db_engine.get_db_pool_capacity(good_engine) == 7
    assert db_engine.get_db_pool_checked_out(good_engine) == 3
    assert db_engine.get_db_pool_capacity(bad_engine) is None
    assert db_engine.get_db_pool_checked_out(bad_engine) is None
    assert db_engine.get_db_pool_capacity(no_methods_engine) is None
    assert db_engine.get_db_pool_checked_out(no_methods_engine) is None


@pytest.mark.asyncio
async def test_db_session_context_selection_scope_metrics_and_count_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.app_context import AppContext, set_default_app_context
    from db import session as db_session

    metric_calls: list[MetricActionCall] = []
    monkeypatch.setattr(
        "core.observability.metrics.DB_SESSION_TOTAL",
        StubMetric(metric_calls, "total", record_kwargs=False),
    )
    monkeypatch.setattr(
        "core.observability.metrics.DB_SESSION_DURATION_SECONDS",
        StubMetric(metric_calls, "duration", record_kwargs=False),
    )

    @contextmanager
    def span(_name: str, _attrs: dict[str, object]) -> Any:
        yield None

    monkeypatch.setattr("core.observability.tracing.otel_span", span)

    from asyncpg.exceptions import QueryCanceledError

    class Session:
        async def execute(self, stmt: object) -> object:
            if "statement_timeout" in str(stmt):
                return SimpleNamespace(scalar=lambda: 0)
            # A genuine statement_timeout cancellation → count_with_timeout maps to None.
            raise QueryCanceledError("canceling statement due to statement timeout")

        @asynccontextmanager
        async def begin_nested(self) -> Any:
            yield self

    class SessionFactory:
        def __init__(self) -> None:
            self.session = Session()

        @asynccontextmanager
        async def begin(self) -> Any:
            yield self.session

        def __call__(self) -> Any:
            @asynccontextmanager
            async def cm() -> Any:
                yield self.session

            return cm()

    factory = SessionFactory()

    class Context(AppContext):
        async def ensure_db(self) -> object:
            return factory

    default_context = Context()
    set_default_app_context(default_context)
    try:
        request_context = Context()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_context=request_context)))
        assert db_session._app_context_from_request(request) is default_context

        request_context.session_factory = factory  # type: ignore[assignment]
        request_context.db_engine = object()  # type: ignore[assignment]
        assert db_session._app_context_from_request(request) is request_context
        assert await db_session._ensure_session_factory(request) is factory

        async with db_session.session_scope() as session:
            assert session is factory.session

        async with db_session.session_scope(existing_session=factory.session) as existing:
            assert existing is factory.session

        sessions = [session async for session in db_session.get_db_session(request)]
        assert sessions == [factory.session]

        assert await db_session.count_with_timeout(factory.session, "select count(*)") is None
    finally:
        set_default_app_context(None)

    assert any(call[:3] == ("total", ("transaction", "success"), "inc") for call in metric_calls)
    assert any(call[:3] == ("total", ("existing_session", "success"), "inc") for call in metric_calls)
    assert any(call[:3] == ("total", ("request_session", "success"), "inc") for call in metric_calls)
    assert any(call[:3] == ("total", ("count_query", "timeout"), "inc") for call in metric_calls)


@pytest.mark.asyncio
async def test_webhook_auth_dep_exception_branches_and_rate_limit_dep(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from core import webhook_security

    metric_calls: list[MetricActionCall] = []
    monkeypatch.setattr(
        webhook_security,
        "SECURITY_CHECKS_TOTAL",
        StubMetric(metric_calls, "security", record_kwargs=False),
    )
    monkeypatch.setattr(
        webhook_security,
        "REDIS_UNAVAILABLE_TOTAL",
        StubMetric(metric_calls, "redis_unavailable", record_kwargs=False),
    )

    class Request:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {"content-length": "2"}
            self.state = SimpleNamespace()
            self.path_params = {"source": "github"}
            self.query_params: dict[str, str] = {}

        async def body(self) -> bytes:
            return b"{}"

    request = Request()
    # Captured before later blocks stub it, so the multi-tier block can call the real impl.
    real_enforce_rate_limit = webhook_security.enforce_webhook_rate_limit
    monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", True)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "secret")

    monkeypatch.setattr(webhook_security, "get_config_manager", lambda: temp_config)
    assert webhook_security.verify_signature(b"{}", "bad") is False
    monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "")
    assert webhook_security.verify_signature(b"{}", "bad") is False
    monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "secret")

    monkeypatch.setattr(webhook_security, "ensure_webhook_auth", lambda *_args, **_kwargs: None)
    await webhook_security.verify_webhook_auth_dep(request, temp_config)
    assert request.state.raw_body == b"{}"

    monkeypatch.setattr(
        webhook_security,
        "ensure_webhook_auth",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad signature")),
    )
    with pytest.raises(HTTPException) as invalid_signature:
        await webhook_security.verify_webhook_auth_dep(Request(), temp_config)
    assert invalid_signature.value.status_code == 401

    monkeypatch.setattr(
        webhook_security,
        "ensure_webhook_auth",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("internal")),
    )
    with pytest.raises(HTTPException) as internal_error:
        await webhook_security.verify_webhook_auth_dep(Request(), temp_config)
    assert internal_error.value.status_code == 500

    response = Response()
    tier = webhook_security._TierResult(allowed=True, remaining=2, limit=5, reset_at=1234.0)

    async def redis_available(_operation: str) -> bool:
        return True

    async def rate_limit_allowed(*_args: object, **_kwargs: object) -> tuple[None, object]:
        return None, tier

    monkeypatch.setattr("core.redis_health.ensure_redis_available", redis_available)
    monkeypatch.setattr(webhook_security, "enforce_webhook_rate_limit", rate_limit_allowed)
    await webhook_security.check_rate_limit_dep(request, response, temp_config)
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "2"
    assert response.headers["X-RateLimit-Reset"] == "1234"

    limited_tier = webhook_security._TierResult(
        allowed=False, remaining=0, limit=5, reset_at=webhook_security.time.time() + 30
    )

    async def rate_limit_rejected(*_args: object, **_kwargs: object) -> tuple[str, object]:
        return "1.2.3.4", limited_tier

    monkeypatch.setattr(webhook_security, "enforce_webhook_rate_limit", rate_limit_rejected)
    monkeypatch.setattr(
        "core.observability.metrics.WEBHOOK_RECEIVED_TOTAL",
        StubMetric(metric_calls, "received", record_kwargs=False),
    )
    monkeypatch.setattr("core.observability.metrics.sanitize_source", lambda source: f"safe:{source}")
    response = Response()
    with pytest.raises(HTTPException) as rate_limited:
        await webhook_security.check_rate_limit_dep(request, response, temp_config)
    assert rate_limited.value.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1

    async def redis_unavailable(_operation: str) -> bool:
        return False

    monkeypatch.setattr("core.redis_health.ensure_redis_available", redis_unavailable)
    monkeypatch.setattr(temp_config.security, "RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR", True)
    await webhook_security.check_rate_limit_dep(request, Response(), temp_config)

    monkeypatch.setattr(temp_config.security, "RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR", False)
    with pytest.raises(HTTPException) as redis_closed:
        await webhook_security.check_rate_limit_dep(request, Response(), temp_config)
    assert redis_closed.value.status_code == 503

    monkeypatch.setattr("core.redis_health.ensure_redis_available", redis_available)

    async def rate_limit_error(*_args: object, **_kwargs: object) -> tuple[None, None]:
        raise RuntimeError("script down")

    monkeypatch.setattr(
        webhook_security,
        "enforce_webhook_rate_limit",
        rate_limit_error,
    )
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr("core.redis_health.mark_redis_failure", lambda op, err: marked.append((op, str(err))))
    monkeypatch.setattr(temp_config.security, "RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR", True)
    await webhook_security.check_rate_limit_dep(request, Response(), temp_config)
    assert marked == [("webhook_security:rate_limit", "script down")]

    monkeypatch.setattr(temp_config.security, "RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR", False)
    with pytest.raises(HTTPException) as redis_error:
        await webhook_security.check_rate_limit_dep(request, Response(), temp_config)
    assert redis_error.value.status_code == 503

    # Multi-tier rate limit now runs as a single script returning a flat list:
    # [failed_index, remaining...]. A non-zero failed_index means rejected.
    rl_config = SimpleNamespace(
        WEBHOOK_RATE_LIMIT_BURST=10,
        WEBHOOK_RATE_LIMIT_PER_MINUTE=0,
        WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE=0,
    )

    monkeypatch.setattr(webhook_security, "get_client_ip", lambda _req: "9.9.9.9")

    async def denied(_script: str, _numkeys: int, *_args: object) -> list[int]:
        return [1]  # tier 1 over limit

    monkeypatch.setattr(webhook_security, "redis_eval_int_list", denied)
    ip, res = await real_enforce_rate_limit(request, security_config=rl_config)
    assert ip is not None
    assert res is not None and res.allowed is False

    async def allowed(_script: str, _numkeys: int, *_args: object) -> list[int]:
        return [0, 5]  # all allow, tier 1 has 5 remaining

    monkeypatch.setattr(webhook_security, "redis_eval_int_list", allowed)
    ip2, res2 = await real_enforce_rate_limit(request, security_config=rl_config)
    assert ip2 is None
    assert res2 is not None and res2.allowed is True and res2.remaining == 5

    async def empty(_script: str, _numkeys: int, *_args: object) -> list[int]:
        return []

    monkeypatch.setattr(webhook_security, "redis_eval_int_list", empty)
    with pytest.raises(RuntimeError, match="no result"):
        await real_enforce_rate_limit(request, security_config=rl_config)


@pytest.mark.asyncio
async def test_redis_health_probe_recovery_failure_and_key_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import redis_health

    redis_health.reset_redis_health()

    redis_health.mark_redis_failure("get", RuntimeError("down"))

    class SyncRedis:
        def ping(self) -> bool:
            return True

    monkeypatch.setattr("core.redis_client.get_redis", lambda: SyncRedis())
    assert await redis_health.ensure_redis_available("recover", probe_interval=0) is True
    snapshot = redis_health.get_redis_health_snapshot()
    assert snapshot.state == redis_health.RedisHealthState.HEALTHY
    assert snapshot.last_operation == "recover:health_probe"

    redis_health.mark_redis_failure("get", RuntimeError("down again"))

    class FalseRedis:
        async def ping(self) -> bool:
            return False

    monkeypatch.setattr("core.redis_client.get_redis", lambda: FalseRedis())
    assert await redis_health.ensure_redis_available("recover", probe_interval=0) is False
    assert "Redis health probe returned false" in redis_health.get_redis_health_snapshot().last_error

    redis_health.reset_redis_health()

    assert redis_health.webhook_dedupe("a") == "webhook:dedupe:a"
    assert redis_health.webhook_processing_queue("a") == "queue:webhook:a"
    assert redis_health.webhook_processing_lock("a") == "lock:webhook:alert:a"
    assert redis_health.rate_limit_burst("1.2.3.4") == "rl:b:1.2.3.4"
    assert redis_health.rate_limit_sustained("1.2.3.4") == "rl:s:1.2.3.4"
    assert redis_health.rate_limit_global() == "rl:g"
    assert redis_health.ai_error_alert_lock("e") == "ai_error_alert_lock:e"
    assert redis_health.scheduled_task_lock("scan") == "scheduled-task-lock:scan"
    assert redis_health.openclaw_poller_stability(7) == "openclaw:poller:stability:7"


@pytest.mark.asyncio
async def test_retry_scheduler_schedules_tasks_and_best_effort_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations import taskiq_retry_scheduler as retry_scheduler
    from services.operations import tasks

    deleted: list[str] = []
    scheduled: list[tuple[str, object, dict[str, object]]] = []

    class DynamicScheduleSource:
        async def delete_schedule(self, schedule_id: str) -> None:
            deleted.append(schedule_id)

    class Kicker:
        def __init__(self, task_name: str) -> None:
            self.task_name = task_name
            self.schedule_id = ""

        def with_schedule_id(self, schedule_id: str) -> Kicker:
            self.schedule_id = schedule_id
            return self

        async def schedule_by_time(self, source: object, run_at: object, **kwargs: object) -> None:
            assert isinstance(source, DynamicScheduleSource)
            scheduled.append((self.schedule_id, run_at, kwargs))

    class Task:
        def __init__(self, task_name: str) -> None:
            self.task_name = task_name

        def kicker(self) -> Kicker:
            return Kicker(self.task_name)

    monkeypatch.setattr(retry_scheduler, "dynamic_schedule_source", DynamicScheduleSource())
    monkeypatch.setattr(tasks, "process_webhook_task", Task("webhook"))
    monkeypatch.setattr(tasks, "process_forward_outbox_task", Task("forward"))
    monkeypatch.setattr(tasks, "poll_openclaw_analysis_task", Task("poll"))

    assert retry_scheduler.compute_backoff_delay(0, initial_delay=5, max_delay=60, multiplier=2) == 5
    assert retry_scheduler.compute_backoff_delay(5, initial_delay=5, max_delay=60, multiplier=2) == 60
    assert retry_scheduler._raw_ingest_schedule_id("req-1", "github", "{}") == "webhook-ingest-retry:req-1"
    assert retry_scheduler._raw_ingest_schedule_id(None, "github", "{}").startswith("webhook-ingest-retry:")

    await retry_scheduler.schedule_webhook_ingest_retry(
        delay_seconds=-1,
        source="github",
        raw_headers={"h": "v"},
        raw_body="{}",
        client_ip="1.2.3.4",
        request_id="req-1",
        received_at="now",
        ingest_retry_count=2,
        traceparent="00-" + "a" * 32 + "-0123456789abcdef-01",
    )
    await retry_scheduler.schedule_forward_outbox(42, 3)
    await retry_scheduler.schedule_openclaw_poll(99, 4)

    assert deleted == ["webhook-ingest-retry:req-1", "forward-outbox:42", "openclaw-poll:99"]
    assert scheduled[0][2]["traceparent"]
    assert scheduled[1][2] == {"outbox_id": 42}
    assert scheduled[2][2] == {"analysis_id": 99}

    async def fail_schedule(_analysis_id: int, _delay_seconds: int) -> None:
        raise RuntimeError("schedule down")

    monkeypatch.setattr(retry_scheduler, "schedule_openclaw_poll", fail_schedule)
    await retry_scheduler.schedule_openclaw_poll_best_effort(100, 1)

    best_effort: list[tuple[int, int]] = []

    async def ok_schedule(analysis_id: int, delay_seconds: int) -> None:
        best_effort.append((analysis_id, delay_seconds))

    monkeypatch.setattr(retry_scheduler, "compute_openclaw_poll_delay", lambda attempts: 6)
    monkeypatch.setattr(retry_scheduler, "schedule_openclaw_poll", ok_schedule)
    await retry_scheduler.schedule_openclaw_poll_best_effort(101)
    assert best_effort == [(101, 6)]
