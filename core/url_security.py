"""Outbound URL validation helpers.

Forwarding rules are user-configurable, so every outbound target must be
validated before persistence and again before delivery.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from core.config import Config


class UnsafeTargetUrlError(ValueError):
    """Raised when a forwarding target URL is unsafe or malformed."""


_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


def _split_csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _host_matches_pattern(host: str, pattern: str) -> bool:
    normalized = pattern.lower().strip()
    if not normalized:
        return False
    if normalized.startswith("*."):
        suffix = normalized[2:]
        return host == suffix or host.endswith(f".{suffix}")
    if normalized.startswith("."):
        suffix = normalized[1:]
        return host == suffix or host.endswith(f".{suffix}")
    return host == normalized


def _require_allowlisted_host(host: str) -> None:
    allowlist = _split_csv(Config.security.FORWARD_TARGET_ALLOWLIST)
    if not allowlist:
        return
    if not any(_host_matches_pattern(host, pattern) for pattern in allowlist):
        raise UnsafeTargetUrlError("target host is not in FORWARD_TARGET_ALLOWLIST")


def _reject_blocked_hostname(host: str) -> None:
    if host in _BLOCKED_HOSTNAMES or any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        raise UnsafeTargetUrlError("target host points to a local/private name")


def _reject_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if Config.security.ALLOW_PRIVATE_FORWARD_URLS:
        return
    if not ip.is_global:
        raise UnsafeTargetUrlError("target host resolves to a non-public IP")


def _resolved_ips(host: str, port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeTargetUrlError(f"target host cannot be resolved: {host}") from exc

    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise UnsafeTargetUrlError(f"target host has no usable IP address: {host}")
    return ips


async def validate_outbound_url(url: str) -> str:
    """Return a normalized URL if it is safe for server-side outbound calls."""
    candidate = str(url or "").strip()
    if not candidate:
        raise UnsafeTargetUrlError("target URL is empty")

    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeTargetUrlError("target URL must use http or https")
    if not parts.hostname:
        raise UnsafeTargetUrlError("target URL host is empty")
    if parts.username or parts.password:
        raise UnsafeTargetUrlError("target URL must not include credentials")

    host = parts.hostname.lower().rstrip(".")
    _require_allowlisted_host(host)
    if not Config.security.ALLOW_PRIVATE_FORWARD_URLS:
        _reject_blocked_hostname(host)

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        ips = await asyncio.to_thread(_resolved_ips, host, parts.port)
        for ip in ips:
            _reject_private_ip(ip)
    else:
        _reject_private_ip(literal_ip)

    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "", parts.query, ""))
