"""Client IP extraction helpers shared by API and security layers."""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request

from core.app_context import AppContext, get_config_manager

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=16)
def _parse_proxy_cidrs(raw: str) -> tuple[tuple[str, _IpNetwork | None], ...]:
    """Parse the TRUSTED_PROXY_CIDRS string into (literal, parsed-network) pairs.

    Keyed on the raw string value (not the config object), so it re-reads on
    every call but skips re-parsing ip_network on the per-request hot path; a
    config change produces a new key and is picked up immediately.
    """
    parsed: list[tuple[str, _IpNetwork | None]] = []
    for item in (p.strip() for p in raw.split(",")):
        if not item:
            continue
        try:
            parsed.append((item, ipaddress.ip_network(item, strict=False)))
        except ValueError:
            parsed.append((item, None))  # not a CIDR; matched as a literal host
    return tuple(parsed)


def get_client_ip(request: Request) -> str:
    """Return the trusted client IP for a FastAPI request."""
    context = getattr(request.app.state, "app_context", None)
    config = context.config if isinstance(context, AppContext) else get_config_manager()
    security = config.security
    if _is_trusted_proxy(request.client.host if request.client else None, security=security):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for and (ip := _first_valid_header_ip(forwarded_for)):
            return ip
        real_ip = request.headers.get("x-real-ip")
        if real_ip and (ip := _first_valid_header_ip(real_ip)):
            return ip
    return request.client.host if request.client else "unknown"


def _first_valid_header_ip(value: str) -> str | None:
    for raw in value.split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def _trusted_proxy_cidrs(security: object) -> tuple[str, ...]:
    return tuple(item for item, _ in _parse_proxy_cidrs(str(getattr(security, "TRUSTED_PROXY_CIDRS", ""))))


def _is_trusted_proxy(client_host: str | None, *, security: object | None = None) -> bool:
    security = security or get_config_manager().security
    if not client_host or not getattr(security, "TRUST_PROXY_HEADERS", False):
        return False
    entries = _parse_proxy_cidrs(str(getattr(security, "TRUSTED_PROXY_CIDRS", "")))
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return any(item == client_host for item, _ in entries)

    for item, network in entries:
        if network is not None:
            if client_ip in network:
                return True
        elif item == client_host:
            return True
    return False
