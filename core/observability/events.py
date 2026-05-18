"""Structured observability events with optional span correlation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import suppress
from typing import Any, Protocol

from core.observability.attributes import normalize_attributes
from core.observability.exporters import otel_enabled
from core.observability.metrics import OBSERVABILITY_EVENTS_TOTAL


class _SpanWithEvents(Protocol):
    def add_event(self, name: str, attributes: Mapping[str, str | bool | int | float] | None = None) -> None: ...


def add_span_event(name: str, attributes: Mapping[str, Any] | None = None) -> None:
    if not otel_enabled():
        return

    normalized = normalize_attributes(attributes)
    with suppress(Exception):
        from opentelemetry import trace

        current_span = trace.get_current_span()
        context = current_span.get_span_context()
        if context and context.is_valid:
            span = current_span
            span.add_event(name, normalized)


def emit_event(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    body: str | None = None,
    severity: int = logging.INFO,
) -> None:
    """Emit a point-in-time event as both a span event and structured log."""
    normalized = normalize_attributes(attributes)
    normalized["event.name"] = name
    OBSERVABILITY_EVENTS_TOTAL.labels(name).inc()
    add_span_event(name, normalized)
    logging.getLogger("webhook_service.events").log(severity, body or name, extra=normalized)
