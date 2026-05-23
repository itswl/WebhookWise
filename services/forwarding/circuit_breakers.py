"""Runtime-configured circuit breaker wiring for forwarding paths."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any, ParamSpec, TypeVar

from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreaker
from core.config import UnifiedConfigManager

_P = ParamSpec("_P")
_R = TypeVar("_R")
ValidateURL = Callable[[str], Awaitable[str]]


class LazyCircuitBreaker:
    """Create the configured breaker on first use, not at module import time."""

    def __init__(self, factory: Callable[[UnifiedConfigManager], CircuitBreaker]) -> None:
        self._factory = factory
        self._breaker: CircuitBreaker | None = None

    def _get(self) -> CircuitBreaker:
        if self._breaker is None:
            self._breaker = self._factory(get_config_manager())
        return self._breaker

    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await self._get().call_async(func, *args, **kwargs)


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

_host_breakers: dict[str, LazyCircuitBreaker] = {}
_host_breakers_lock = Lock()


def get_forward_breaker(target_url: str) -> LazyCircuitBreaker:
    from urllib.parse import urlsplit

    host = urlsplit(target_url).hostname or "_default_"
    if host not in _host_breakers:
        with _host_breakers_lock:
            if host not in _host_breakers:
                _host_breakers[host] = LazyCircuitBreaker(_build_forward_circuit_breaker)
    return _host_breakers[host]


@dataclass(frozen=True, slots=True)
class RemoteForwardDependencies:
    http_client: Any
    circuit_breaker: Any  # LazyCircuitBreaker or compatible (has call_async)
    validate_url: ValidateURL


@dataclass(frozen=True, slots=True)
class OpenClawForwardDependencies:
    http_client: Any
    circuit_breaker: Any  # LazyCircuitBreaker or compatible (has call_async)


def build_remote_forward_dependencies() -> RemoteForwardDependencies:
    from core.http_client import get_http_client
    from core.url_security import validate_outbound_url

    return RemoteForwardDependencies(
        http_client=get_http_client(),
        circuit_breaker=get_forward_breaker("_default_"),
        validate_url=validate_outbound_url,
    )


def build_openclaw_forward_dependencies() -> OpenClawForwardDependencies:
    from core.http_client import get_http_client

    return OpenClawForwardDependencies(http_client=get_http_client(), circuit_breaker=openclaw_cb)
