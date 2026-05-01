from __future__ import annotations

import os

_enabled_cache: bool | None = None
_httpx_instrumented = False
_redis_instrumented = False
_sqlalchemy_instrumented = False
_exporter_configured = False


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

    _setup_otlp_exporter(provider)

    exclude = os.getenv("OTEL_EXCLUDED_URLS", "/metrics,/static").strip()
    FastAPIInstrumentor.instrument_app(app, excluded_urls=exclude)
    instrument_httpx()
    instrument_redis()
