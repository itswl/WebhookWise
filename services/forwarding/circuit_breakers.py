"""Runtime-configured circuit breaker wiring for forwarding paths."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreaker
from core.config import AppConfig
from core.resilience import LazyCircuitBreaker

ValidateURL = Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class CircuitBreakerSpec:
    name: str
    failure_threshold_attr: str
    recovery_timeout_attr: str

    def build(self, config: AppConfig) -> CircuitBreaker:
        circuit_config = config.circuit_breaker
        return CircuitBreaker(
            name=self.name,
            failure_threshold=getattr(circuit_config, self.failure_threshold_attr),
            recovery_timeout=getattr(circuit_config, self.recovery_timeout_attr),
        )

    def lazy(self) -> LazyCircuitBreaker:
        """A LazyCircuitBreaker that builds this spec from current config on first use."""
        return LazyCircuitBreaker(lambda: self.build(get_config_manager()))


_FEISHU_BREAKER_SPEC = CircuitBreakerSpec(
    name="feishu",
    failure_threshold_attr="CIRCUIT_BREAKER_FEISHU_THRESHOLD",
    recovery_timeout_attr="CIRCUIT_BREAKER_FEISHU_TIMEOUT_SECONDS",
)
_DEEP_ANALYSIS_BREAKER_SPEC = CircuitBreakerSpec(
    name="deep_analysis",
    failure_threshold_attr="CIRCUIT_BREAKER_DEEP_ANALYSIS_THRESHOLD",
    recovery_timeout_attr="CIRCUIT_BREAKER_DEEP_ANALYSIS_TIMEOUT_SECONDS",
)
_FORWARD_BREAKER_SPEC = CircuitBreakerSpec(
    name="forward",
    failure_threshold_attr="CIRCUIT_BREAKER_FORWARD_THRESHOLD",
    recovery_timeout_attr="CIRCUIT_BREAKER_FORWARD_TIMEOUT_SECONDS",
)


feishu_cb = _FEISHU_BREAKER_SPEC.lazy()

# One breaker PER GATEWAY, not one for "deep analysis".
#
# With a single shared breaker, a gateway that goes down trips it and then every
# OTHER gateway's deliveries are rejected too — they degrade to local AI because
# a different operator's service is unhealthy. Named gateways made that a real
# outage path rather than a theoretical one.
#
# Names come from configuration, so cardinality is bounded by the operator, not
# by traffic; the cap is belt-and-braces and mirrors the per-host map below.
_MAX_GATEWAY_BREAKERS = 64
_gateway_breakers: OrderedDict[str, LazyCircuitBreaker] = OrderedDict()
_gateway_breakers_lock = Lock()


def get_deep_analysis_breaker(gateway_name: str) -> LazyCircuitBreaker:
    key = (gateway_name or "").strip().lower() or "default"
    with _gateway_breakers_lock:
        breaker = _gateway_breakers.get(key)
        if breaker is None:
            breaker = _DEEP_ANALYSIS_BREAKER_SPEC.lazy()
            _gateway_breakers[key] = breaker
            if len(_gateway_breakers) > _MAX_GATEWAY_BREAKERS:
                _gateway_breakers.popitem(last=False)
        else:
            _gateway_breakers.move_to_end(key)
        return breaker


# Bounded LRU of per-host breakers. The map is keyed on the forward target's
# hostname, which can be attacker-influenced (rule targets) or high-cardinality,
# so an unbounded dict is a slow memory leak. Cap it and evict least-recently
# used; evicting a breaker only resets its (in-memory) state, which is harmless.
_MAX_HOST_BREAKERS = 512
_host_breakers: OrderedDict[str, LazyCircuitBreaker] = OrderedDict()
_host_breakers_lock = Lock()


def get_forward_breaker(target_url: str) -> LazyCircuitBreaker:
    from urllib.parse import urlsplit

    host = urlsplit(target_url).hostname or "_default_"
    with _host_breakers_lock:
        breaker = _host_breakers.get(host)
        if breaker is None:
            breaker = _FORWARD_BREAKER_SPEC.lazy()
            _host_breakers[host] = breaker
            if len(_host_breakers) > _MAX_HOST_BREAKERS:
                _host_breakers.popitem(last=False)
        else:
            _host_breakers.move_to_end(host)
        return breaker


@dataclass(frozen=True, slots=True)
class RemoteForwardDependencies:
    http_client: Any
    circuit_breaker: Any
    validate_url: ValidateURL


@dataclass(frozen=True, slots=True)
class DeepAnalysisForwardDependencies:
    http_client: Any
    circuit_breaker: Any


def build_remote_forward_dependencies(target_url: str = "") -> RemoteForwardDependencies:
    from functools import partial

    from core.http_client import get_http_client
    from core.url_security import validate_outbound_url

    return RemoteForwardDependencies(
        http_client=get_http_client(),
        circuit_breaker=get_forward_breaker(target_url or "_default_"),
        validate_url=partial(validate_outbound_url, bypass_dns_cache=True),
    )


def build_deep_analysis_forward_dependencies(gateway_name: str = "") -> DeepAnalysisForwardDependencies:
    from core.http_client import get_deep_analysis_client

    return DeepAnalysisForwardDependencies(
        http_client=get_deep_analysis_client(),
        circuit_breaker=get_deep_analysis_breaker(gateway_name),
    )
