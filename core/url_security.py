"""Outbound URL validation helpers.

Forwarding rules are user-configurable, so every outbound target must be
validated before persistence and again before delivery.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from core.app_context import get_config_manager
from core.text import split_csv_lower


class UnsafeTargetUrlError(ValueError):
    """Raised when a forwarding target URL is unsafe or malformed."""


_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")
_DNS_CACHE_TTL_SECONDS = 60.0
_DNS_CACHE_MAX_ENTRIES = 2048
_DNS_CACHE: dict[
    tuple[str, int | None],
    tuple[float, tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]],
] = {}
_DNS_CACHE_LOCK = asyncio.Lock()


def _evict_dns_cache_if_full(now: float) -> None:
    """Bound the DNS cache so a flood of distinct target hosts can't grow it
    without limit. Drop expired entries first; if still over, drop the entries
    closest to expiry. Caller must hold _DNS_CACHE_LOCK."""
    if len(_DNS_CACHE) < _DNS_CACHE_MAX_ENTRIES:
        return
    expired = [k for k, (expires_at, _) in _DNS_CACHE.items() if now >= expires_at]
    for k in expired:
        _DNS_CACHE.pop(k, None)
    if len(_DNS_CACHE) < _DNS_CACHE_MAX_ENTRIES:
        return
    # Still full of live entries — evict the soonest-to-expire to make room.
    overflow = len(_DNS_CACHE) - _DNS_CACHE_MAX_ENTRIES + 1
    for k, _ in sorted(_DNS_CACHE.items(), key=lambda kv: kv[1][0])[:overflow]:
        _DNS_CACHE.pop(k, None)


@lru_cache(maxsize=8)
def _parse_target_allowlist(raw: str) -> tuple[str, ...]:
    # Keyed on the raw config string (mirrors request_ip._parse_proxy_cidrs):
    # from_config runs per delivery and again per pinned-DNS connect, so the
    # CSV split should not be re-done for an unchanged setting.
    return tuple(split_csv_lower(raw))


@dataclass(frozen=True, slots=True)
class OutboundURLPolicy:
    allow_private_target_urls: bool
    target_allowlist: tuple[str, ...]

    @classmethod
    def from_config(cls) -> OutboundURLPolicy:
        cfg = get_config_manager().security
        return cls(
            allow_private_target_urls=bool(cfg.ALLOW_PRIVATE_TARGET_URLS),
            target_allowlist=_parse_target_allowlist(str(cfg.FORWARD_TARGET_ALLOWLIST or "")),
        )


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


def _require_allowlisted_host(host: str, policy: OutboundURLPolicy) -> None:
    if not policy.target_allowlist:
        return
    if not any(_host_matches_pattern(host, pattern) for pattern in policy.target_allowlist):
        raise UnsafeTargetUrlError("target host is not in FORWARD_TARGET_ALLOWLIST")


def _reject_blocked_hostname(host: str) -> None:
    if host in _BLOCKED_HOSTNAMES or any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        raise UnsafeTargetUrlError("target host points to a local/private name")


def _reject_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, policy: OutboundURLPolicy) -> None:
    if policy.allow_private_target_urls:
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


async def _resolve_ips_cached(host: str, port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    key = (host, port)
    async with _DNS_CACHE_LOCK:
        now = time.monotonic()
        cached = _DNS_CACHE.get(key)
        if cached is not None:
            expires_at, cached_ips = cached
            if now < expires_at:
                return list(cached_ips)
            _DNS_CACHE.pop(key, None)

    resolved_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = await asyncio.to_thread(
        _resolved_ips, host, port
    )
    async with _DNS_CACHE_LOCK:
        now = time.monotonic()
        cached = _DNS_CACHE.get(key)
        if cached is not None:
            expires_at, cached_ips = cached
            if now < expires_at:
                return list(cached_ips)
        _evict_dns_cache_if_full(now)
        _DNS_CACHE[key] = (now + _DNS_CACHE_TTL_SECONDS, tuple(resolved_ips))
    return resolved_ips


async def validate_outbound_url(
    url: str, *, policy: OutboundURLPolicy | None = None, bypass_dns_cache: bool = False
) -> str:
    """Return a normalized URL if it is safe for server-side outbound calls.

    When bypass_dns_cache=True, DNS resolution skips the local cache to mitigate
    DNS rebinding attacks (use at delivery time, not at rule-save time).
    """
    policy = policy or OutboundURLPolicy.from_config()
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
    _require_allowlisted_host(host, policy)
    if not policy.allow_private_target_urls:
        _reject_blocked_hostname(host)

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        if bypass_dns_cache:
            ips = await asyncio.to_thread(_resolved_ips, host, parts.port)
        else:
            ips = await _resolve_ips_cached(host, parts.port)
        for ip in ips:
            _reject_private_ip(ip, policy)
    else:
        _reject_private_ip(literal_ip, policy)

    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "", parts.query, ""))
