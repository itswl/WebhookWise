"""OpenTelemetry tracing setup and lightweight span helpers."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from typing import Any, Protocol, cast

from core.observability.attributes import normalize_attributes
from core.observability.exporters import build_span_exporter, env_flag, otel_enabled
from core.observability.resource import build_resource

_httpx_instrumented = False
_redis_instrumented = False
_sqlalchemy_instrumented = False
_fastapi_instrumented = False
_provider_initialized = False


class SpanLike(Protocol):
    def set_attribute(self, key: str, value: object) -> None: ...

    def set_status(self, status: object, description: str | None = None) -> None: ...


def instrument_httpx() -> None:
    global _httpx_instrumented
    if _httpx_instrumented or not otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        return
    HTTPXClientInstrumentor().instrument()
    _httpx_instrumented = True


def instrument_redis() -> None:
    global _redis_instrumented
    if _redis_instrumented or not otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError:
        return
    RedisInstrumentor().instrument()
    _redis_instrumented = True


def instrument_sqlalchemy(engine: Any) -> None:
    global _sqlalchemy_instrumented
    if _sqlalchemy_instrumented or not otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        return
    SQLAlchemyInstrumentor().instrument(engine=engine)
    _sqlalchemy_instrumented = True


def setup_tracing(app: Any | None = None, *, service_name: str | None = None) -> None:
    global _fastapi_instrumented, _provider_initialized
    if not otel_enabled():
        return

    if not _provider_initialized:
        try:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
            from opentelemetry.trace import set_tracer_provider
        except ImportError:
            return

        provider = TracerProvider(resource=build_resource(service_name))
        set_tracer_provider(provider)

        has_exporter = False
        if env_flag("OTEL_CONSOLE_EXPORTER", default=False):
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            has_exporter = True

        exporter = build_span_exporter()
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))
            has_exporter = True

        if not has_exporter:
            logging.getLogger("webhook_service").warning("[OTEL] tracing enabled but no trace exporter is configured")
        _provider_initialized = True

    if app is not None and not _fastapi_instrumented:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        except ImportError:
            return
        exclude = "/live,/ready,/health,/static"
        excluded_urls = (env_flag("OTEL_INCLUDE_HEALTHCHECKS", default=False) and "/static") or exclude
        excluded_urls = os.getenv("OTEL_EXCLUDED_URLS", excluded_urls).strip()
        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded_urls)
        _fastapi_instrumented = True

    instrument_httpx()
    instrument_redis()


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[SpanLike | None]:
    if not otel_enabled():
        yield None
        return
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    tracer = trace.get_tracer("webhookwise")
    with tracer.start_as_current_span(name) as current:
        span_obj = cast(SpanLike, current) if current is not None else None
        if span_obj is not None:
            for key, value in normalize_attributes(attributes).items():
                with suppress(Exception):
                    span_obj.set_attribute(key, value)
        yield span_obj


def get_otel_trace_id() -> str:
    if not otel_enabled():
        return ""
    with suppress(Exception):
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    return ""


def get_otel_span_id() -> str:
    if not otel_enabled():
        return ""
    with suppress(Exception):
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")
    return ""
