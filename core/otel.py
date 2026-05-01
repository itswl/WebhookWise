from __future__ import annotations

import os


def setup_otel(app) -> None:
    enabled = os.getenv("OTEL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
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
