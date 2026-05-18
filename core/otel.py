"""Backward-compatible OpenTelemetry facade.

New code should import from ``core.observability`` directly. This module keeps
older call sites stable while tracing, metrics, and logs are all configured via
OTLP.
"""

from __future__ import annotations

from typing import Any

from core.observability import setup_observability, setup_observability_worker
from core.observability.events import add_span_event, emit_event
from core.observability.exporters import otel_enabled
from core.observability.signals import record_signal
from core.observability.tracing import (
    SpanLike,
    get_otel_span_id,
    get_otel_trace_id,
    instrument_httpx,
    instrument_redis,
    instrument_sqlalchemy,
    setup_tracing,
    span,
)

_enabled_cache: bool | None = None


def _otel_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = otel_enabled()
    return _enabled_cache


def setup_otel(app: Any) -> None:
    setup_observability(app)


def setup_otel_worker() -> None:
    setup_observability_worker()


__all__ = [
    "SpanLike",
    "_enabled_cache",
    "_otel_enabled",
    "add_span_event",
    "emit_event",
    "get_otel_span_id",
    "get_otel_trace_id",
    "instrument_httpx",
    "instrument_redis",
    "instrument_sqlalchemy",
    "record_signal",
    "setup_observability",
    "setup_otel",
    "setup_otel_worker",
    "setup_tracing",
    "span",
]
