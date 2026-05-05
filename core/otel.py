from __future__ import annotations

import os
from contextlib import contextmanager, suppress
from typing import Any

_enabled_cache: bool | None = None
_httpx_instrumented = False
_redis_instrumented = False
_sqlalchemy_instrumented = False
_exporter_configured = False
_provider_initialized = False


def _otel_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = os.getenv("OTEL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    return bool(_enabled_cache)


def instrument_httpx() -> None:
    global _httpx_instrumented
    if _httpx_instrumented or not _otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except Exception:
        return
    HTTPXClientInstrumentor().instrument()
    _httpx_instrumented = True


def instrument_redis() -> None:
    global _redis_instrumented
    if _redis_instrumented or not _otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except Exception:
        return
    RedisInstrumentor().instrument()
    _redis_instrumented = True


def instrument_sqlalchemy(engine) -> None:
    global _sqlalchemy_instrumented
    if _sqlalchemy_instrumented or not _otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except Exception:
        return
    SQLAlchemyInstrumentor().instrument(engine=engine)
    _sqlalchemy_instrumented = True


def _parse_headers(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    items: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k:
            items[k] = v
    return items


def _setup_otlp_exporter(provider) -> None:
    global _exporter_configured
    if _exporter_configured:
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return

    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    if not protocol:
        protocol = "http/protobuf" if endpoint.startswith(("http://", "https://")) else "grpc"

    headers = _parse_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""))
    timeout = float(os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT", "10") or "10")

    exporter = None
    if protocol in {"http", "http/protobuf", "http-protobuf"}:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except Exception:
            exporter = None
        else:
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers or None, timeout=timeout)
    elif protocol in {"grpc"}:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except Exception:
            exporter = None
        else:
            insecure = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers or None, timeout=timeout, insecure=insecure)

    if exporter is None:
        return

    try:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return

    provider.add_span_processor(BatchSpanProcessor(exporter))
    _exporter_configured = True


def _init_tracer_provider() -> None:
    """Create TracerProvider + configure exporters. Idempotent."""
    global _provider_initialized
    if _provider_initialized or not _otel_enabled():
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.trace import set_tracer_provider
    except Exception:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "webhookwise").strip() or "webhookwise"
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    set_tracer_provider(provider)

    has_exporter = False
    if os.getenv("OTEL_CONSOLE_EXPORTER", "").strip().lower() in {"1", "true", "yes", "on"}:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        has_exporter = True

    _setup_otlp_exporter(provider)
    if _exporter_configured:
        has_exporter = True

    if not has_exporter:
        import logging
        logging.getLogger("webhook_service").warning(
            "[OTEL] OTEL_ENABLED=true 但未配置任何 exporter，span 将被静默丢弃。"
            "请设置 OTEL_EXPORTER_OTLP_ENDPOINT 或 OTEL_CONSOLE_EXPORTER=true"
        )

    _provider_initialized = True


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None):
    """返回 OTEL span 上下文管理器，OTEL 未启用或未安装时为 no-op。

    用法::

        with otel.span("webhook.process", attributes={"event_id": 123}) as s:
            if s:
                s.set_attribute("source", "grafana")
            ...
    """
    if not _otel_enabled():
        yield None
        return
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("webhookwise")
    except Exception:
        yield None
        return
    with tracer.start_as_current_span(name) as s:
        if attributes and s is not None:
            for k, v in attributes.items():
                with suppress(Exception):
                    s.set_attribute(k, str(v))
        yield s


def get_otel_trace_id() -> str:
    """返回当前活动 OTEL span 的 trace_id（32 位小写 hex），无活动 span 时返回空字符串。

    用于将 OTEL trace_id 注入到日志 trace_id 字段，实现日志与 APM 双向关联。
    """
    if not _otel_enabled():
        return ""
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return ""


def get_otel_span_id() -> str:
    """返回当前活动 OTEL span 的 span_id（16 位小写 hex），无活动 span 时返回空字符串。"""
    if not _otel_enabled():
        return ""
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")
    except Exception:
        pass
    return ""


def setup_otel(app) -> None:
    """初始化 receiver 进程 OTEL（含 FastAPI auto-instrumentation）。"""
    if not _otel_enabled():
        return

    _init_tracer_provider()

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except Exception:
        return

    exclude = os.getenv("OTEL_EXCLUDED_URLS", "/metrics,/static").strip()
    FastAPIInstrumentor.instrument_app(app, excluded_urls=exclude)
    instrument_httpx()
    instrument_redis()


def setup_otel_worker() -> None:
    """初始化 worker 进程 OTEL（无 HTTP server，仅 TracerProvider + httpx/redis）。"""
    if not _otel_enabled():
        return
    _init_tracer_provider()
    instrument_httpx()
    instrument_redis()
