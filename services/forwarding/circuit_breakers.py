"""Runtime-configured circuit breaker wiring for forwarding paths."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Any, ParamSpec, TypeVar

from core.circuit_breaker import CircuitBreaker
from core.config import Config

_P = ParamSpec("_P")
_R = TypeVar("_R")


class LazyCircuitBreaker:
    """Create the configured breaker on first use, not at module import time."""

    def __init__(self, factory: Callable[[], CircuitBreaker]) -> None:
        self._factory = factory
        self._lock = Lock()
        self._breaker: CircuitBreaker | None = None

    def _get(self) -> CircuitBreaker:
        breaker = self._breaker
        if breaker is not None:
            return breaker
        with self._lock:
            if self._breaker is None:
                self._breaker = self._factory()
            return self._breaker

    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await self._get().call_async(func, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._breaker = None


def _build_feishu_circuit_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        name="feishu",
        failure_threshold=Config.circuit_breaker.CIRCUIT_BREAKER_FEISHU_THRESHOLD,
        recovery_timeout=Config.circuit_breaker.CIRCUIT_BREAKER_FEISHU_TIMEOUT,
    )


def _build_openclaw_circuit_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        name="openclaw",
        failure_threshold=Config.circuit_breaker.CIRCUIT_BREAKER_OPENCLAW_THRESHOLD,
        recovery_timeout=Config.circuit_breaker.CIRCUIT_BREAKER_OPENCLAW_TIMEOUT,
    )


def _build_forward_circuit_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        name="forward",
        failure_threshold=Config.circuit_breaker.CIRCUIT_BREAKER_FORWARD_THRESHOLD,
        recovery_timeout=Config.circuit_breaker.CIRCUIT_BREAKER_FORWARD_TIMEOUT,
    )


feishu_cb = LazyCircuitBreaker(_build_feishu_circuit_breaker)
openclaw_cb = LazyCircuitBreaker(_build_openclaw_circuit_breaker)
forward_cb = LazyCircuitBreaker(_build_forward_circuit_breaker)


def reset_circuit_breakers_for_tests() -> None:
    feishu_cb.reset_for_tests()
    openclaw_cb.reset_for_tests()
    forward_cb.reset_for_tests()
