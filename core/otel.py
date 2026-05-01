from __future__ import annotations

import os

_enabled_cache: bool | None = None
_httpx_instrumented = False
_redis_instrumented = False
_sqlalchemy_instrumented = False


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


def setup_otel(app) -> None:
    if not _otel_enabled():
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.trace import set_tracer_provider
    except Exception:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "webhookwise").strip() or "webhookwise"
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    set_tracer_provider(provider)

    if os.getenv("OTEL_CONSOLE_EXPORTER", "").strip().lower() in {"1", "true", "yes", "on"}:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    exclude = os.getenv("OTEL_EXCLUDED_URLS", "/metrics,/static").strip()
    FastAPIInstrumentor.instrument_app(app, excluded_urls=exclude)
    instrument_httpx()
    instrument_redis()
