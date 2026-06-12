"""Circuit breakers for the analysis domain.

Currently just the LLM (main AI analysis) breaker. Built lazily from config on
first use so importing this module has no side effects and tests can reconfigure
before the breaker is created.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreaker

_P = ParamSpec("_P")
_R = TypeVar("_R")


class _LazyCircuitBreaker:
    """Create the configured breaker on first use, not at import time."""

    def __init__(self, factory: Callable[[], CircuitBreaker]) -> None:
        self._factory = factory
        self._breaker: CircuitBreaker | None = None

    def _get(self) -> CircuitBreaker:
        if self._breaker is None:
            self._breaker = self._factory()
        return self._breaker

    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await self._get().call_async(func, *args, **kwargs)

    def reset(self) -> None:
        self._breaker = None


def _build_llm_breaker() -> CircuitBreaker:
    cfg = get_config_manager().circuit_breaker
    return CircuitBreaker(
        name="llm",
        failure_threshold=int(cfg.CIRCUIT_BREAKER_LLM_THRESHOLD),
        recovery_timeout=float(cfg.CIRCUIT_BREAKER_LLM_TIMEOUT),
    )


llm_cb = _LazyCircuitBreaker(_build_llm_breaker)
