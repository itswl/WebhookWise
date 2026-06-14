"""Shared resilience primitives (circuit breakers, lazy wiring).

The per-domain breaker *instances* still live in their domains
(services/analysis/circuit_breakers.py, services/forwarding/circuit_breakers.py)
where they belong; this package holds the machinery they previously duplicated
— the lazy "build the configured breaker on first use" wrapper — so there is one
implementation instead of two near-identical copies.
"""

from __future__ import annotations

from core.resilience.lazy_breaker import LazyCircuitBreaker

__all__ = ["LazyCircuitBreaker"]
