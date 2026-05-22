"""Client IP extraction helpers shared by API and security layers."""

from __future__ import annotations

import ipaddress

from fastapi import Request

from core.app_context import AppContext, get_default_config
from core.config import UnifiedConfigManager


def get_client_ip(request: Request) -> str:
    """Return the trusted client IP for a FastAPI request."""
    security = _request_config(request).security
    if _is_trusted_proxy(request.client.host if request.client else None, security=security):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for and (ip := _first_valid_header_ip(forwarded_for)):
            return ip
        real_ip = request.headers.get("x-real-ip")
        if real_ip and (ip := _first_valid_header_ip(real_ip)):
            return ip
    return request.client.host if request.client else "unknown"


def _request_config(request: Request) -> UnifiedConfigManager:
    context = getattr(request.app.state, "app_context", None)
    if isinstance(context, AppContext):
        return context.config
    return get_default_config()


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
    raw = str(getattr(security, "TRUSTED_PROXY_CIDRS", ""))
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _is_trusted_proxy(client_host: str | None, *, security: object | None = None) -> bool:
    security = security or get_default_config().security
    if not client_host or not getattr(security, "TRUST_PROXY_HEADERS", False):
        return False
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return client_host in set(_trusted_proxy_cidrs(security))

    for item in _trusted_proxy_cidrs(security):
        if not item:
            continue
        try:
            if client_ip in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            if item == client_host:
                return True
    return False
