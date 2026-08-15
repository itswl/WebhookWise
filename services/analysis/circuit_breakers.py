"""Circuit breakers for the analysis domain.

Currently just the LLM (main AI analysis) breaker. Built lazily from config on
first use so importing this module has no side effects and tests can reconfigure
before the breaker is created.
"""

from __future__ import annotations

from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreaker
from core.resilience import LazyCircuitBreaker


def _llm_settings() -> tuple[int, float]:
    from services.operations import runtime_settings as rt

    cfg = get_config_manager().circuit_breaker
    return (
        rt.override_or("CIRCUIT_BREAKER_LLM_THRESHOLD", int(cfg.CIRCUIT_BREAKER_LLM_THRESHOLD)),
        rt.override_or("CIRCUIT_BREAKER_LLM_TIMEOUT_SECONDS", float(cfg.CIRCUIT_BREAKER_LLM_TIMEOUT_SECONDS)),
    )


def _build_llm_breaker() -> CircuitBreaker:
    failure_threshold, recovery_timeout = _llm_settings()
    return CircuitBreaker(name="llm", failure_threshold=failure_threshold, recovery_timeout=recovery_timeout)


# Rebuilt when its thresholds change, so they can be retuned from the dashboard
# during the incident that makes you want to.
llm_cb = LazyCircuitBreaker(_build_llm_breaker, signature=_llm_settings)
