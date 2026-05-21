"""Metrics about emitted observability events and domain signals."""

from __future__ import annotations

from core.observability.metrics.base import Counter

OBSERVABILITY_EVENTS_TOTAL = Counter(
    "observability.events",
    "Structured observability events emitted by the application",
    ("event.name",),
)
OBSERVABILITY_SIGNAL_TOTAL = Counter(
    "observability.signals",
    "Domain signal state transitions emitted by the application",
    ("signal.name", "signal.state"),
)
