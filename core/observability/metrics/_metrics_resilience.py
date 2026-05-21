"""Resilience and circuit-breaker metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter, Gauge

CIRCUIT_BREAKER_REQUESTS_TOTAL = Counter(
    "circuit_breaker.requests",
    "Circuit breaker request decisions and outcomes",
    ("circuit_breaker.name", "circuit_breaker.outcome"),
)

CIRCUIT_BREAKER_TRANSITIONS_TOTAL = Counter(
    "circuit_breaker.transitions",
    "Circuit breaker state transitions",
    ("circuit_breaker.name", "circuit_breaker.state"),
)

CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker.state",
    "Current circuit breaker state as 1 for the active state and 0 for inactive states",
    ("circuit_breaker.name", "circuit_breaker.state"),
)
