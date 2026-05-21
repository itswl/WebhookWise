"""Forwarding dependency bundles.

The focused forwarding modules receive these bundles explicitly so they do not
need to import process-global HTTP clients or circuit breakers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, Protocol, TypeVar

ValidateURL = Callable[[str], Awaitable[str]]

_P = ParamSpec("_P")
_R = TypeVar("_R")


class CircuitBreakerLike(Protocol):
    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R: ...


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
    from services.forwarding.circuit_breakers import forward_cb

    return RemoteForwardDependencies(
        http_client=get_http_client(),
        circuit_breaker=forward_cb,
        validate_url=validate_outbound_url,
    )


def build_openclaw_forward_dependencies() -> OpenClawForwardDependencies:
    from core.http_client import get_http_client
    from services.forwarding.circuit_breakers import openclaw_cb

    return OpenClawForwardDependencies(http_client=get_http_client(), circuit_breaker=openclaw_cb)
