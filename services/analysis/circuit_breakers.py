"""Circuit breakers for the analysis domain.

Currently just the LLM (main AI analysis) breaker. Built lazily from config on
first use so importing this module has no side effects and tests can reconfigure
before the breaker is created.
"""

from __future__ import annotations

from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreaker
from core.resilience import LazyCircuitBreaker


def _build_llm_breaker() -> CircuitBreaker:
    cfg = get_config_manager().circuit_breaker
    return CircuitBreaker(
        name="llm",
        failure_threshold=int(cfg.CIRCUIT_BREAKER_LLM_THRESHOLD),
        recovery_timeout=float(cfg.CIRCUIT_BREAKER_LLM_TIMEOUT_SECONDS),
    )


llm_cb = LazyCircuitBreaker(_build_llm_breaker)
