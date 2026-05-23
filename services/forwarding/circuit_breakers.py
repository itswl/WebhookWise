"""Runtime-configured circuit breaker wiring for forwarding paths."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any, ParamSpec, Protocol, TypeVar

from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreaker
from core.config import UnifiedConfigManager

_P = ParamSpec("_P")
_R = TypeVar("_R")
ValidateURL = Callable[[str], Awaitable[str]]


class CircuitBreakerLike(Protocol):
    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R: ...


class LazyCircuitBreaker:
    """Create the configured breaker on first use, not at module import time."""

    def __init__(self, factory: Callable[[UnifiedConfigManager], CircuitBreaker]) -> None:
        self._factory = factory
        self._lock = Lock()
        self._breaker: CircuitBreaker | None = None
        self._config_id: int | None = None

    def _get(self) -> CircuitBreaker:
        config = get_config_manager()
        config_id = id(config)
        breaker = self._breaker
        if breaker is not None and self._config_id == config_id:
            return breaker
        with self._lock:
            if self._breaker is None or self._config_id != config_id:
                self._breaker = self._factory(config)
                self._config_id = config_id
            return self._breaker

    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await self._get().call_async(func, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


def _build_feishu_circuit_breaker(config: UnifiedConfigManager) -> CircuitBreaker:
    return CircuitBreaker(
        name="feishu",
        failure_threshold=config.circuit_breaker.CIRCUIT_BREAKER_FEISHU_THRESHOLD,
        recovery_timeout=config.circuit_breaker.CIRCUIT_BREAKER_FEISHU_TIMEOUT,
    )


def _build_openclaw_circuit_breaker(config: UnifiedConfigManager) -> CircuitBreaker:
    return CircuitBreaker(
        name="openclaw",
        failure_threshold=config.circuit_breaker.CIRCUIT_BREAKER_OPENCLAW_THRESHOLD,
        recovery_timeout=config.circuit_breaker.CIRCUIT_BREAKER_OPENCLAW_TIMEOUT,
    )


def _build_forward_circuit_breaker(config: UnifiedConfigManager) -> CircuitBreaker:
    return CircuitBreaker(
        name="forward",
        failure_threshold=config.circuit_breaker.CIRCUIT_BREAKER_FORWARD_THRESHOLD,
        recovery_timeout=config.circuit_breaker.CIRCUIT_BREAKER_FORWARD_TIMEOUT,
    )


feishu_cb = LazyCircuitBreaker(_build_feishu_circuit_breaker)
openclaw_cb = LazyCircuitBreaker(_build_openclaw_circuit_breaker)
forward_cb = LazyCircuitBreaker(_build_forward_circuit_breaker)


@dataclass(frozen=True, slots=True)
class RemoteForwardDependencies:
    http_client: Any
    circuit_breaker: CircuitBreakerLike
    validate_url: ValidateURL


@dataclass(frozen=True, slots=True)
class OpenClawForwardDependencies:
    http_client: Any
    circuit_breaker: CircuitBreakerLike


def build_remote_forward_dependencies() -> RemoteForwardDependencies:
    from core.http_client import get_http_client
    from core.url_security import validate_outbound_url

    return RemoteForwardDependencies(
        http_client=get_http_client(),
        circuit_breaker=forward_cb,
        validate_url=validate_outbound_url,
    )


def build_openclaw_forward_dependencies() -> OpenClawForwardDependencies:
    from core.http_client import get_http_client

    return OpenClawForwardDependencies(http_client=get_http_client(), circuit_breaker=openclaw_cb)
