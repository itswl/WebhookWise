"""Lazy circuit-breaker wrapper shared across domains.

Both the analysis and forwarding domains independently grew the same pattern:
"don't build the CircuitBreaker at import time (so importing has no side effects
and tests can reconfigure first); build it from config on first use." This is
the single shared implementation of that wrapper.

The factory takes no arguments — callers close over whatever config/spec they
need — which keeps this wrapper agnostic to how each domain names its breakers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from core.circuit_breaker import CircuitBreaker

_P = ParamSpec("_P")
_R = TypeVar("_R")


class LazyCircuitBreaker:
    """Create the configured breaker on first use, not at import time."""

    # No __slots__ on purpose: tests monkeypatch `call_async` on the instance
    # (e.g. tests/analysis/test_llm_circuit_breaker.py), which __slots__ blocks.
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
        """Drop the built breaker so the next use rebuilds from current config."""
        self._breaker = None
