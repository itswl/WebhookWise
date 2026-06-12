"""SSRF-hardened HTTP transport that validates and pins DNS at connect time.

The outbound forwarding path validates a target URL (rejecting private/blocked
addresses) and then hands the URL to httpx for delivery. Because httpx performs
its *own* DNS resolution when it opens the connection, an attacker controlling
the target hostname can return a public IP during validation and a private/
metadata IP (e.g. 169.254.169.254) a moment later when httpx connects — a
classic DNS-rebinding TOCTOU.

This module closes that window by replacing the connection pool's network
backend with one that, for every TCP connect, resolves the host, validates the
resolved IP against the same outbound policy, and connects to *that* validated
IP. TLS ``server_hostname`` is still derived by httpcore from the original URL
host, so certificate verification continues to work against the hostname.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

from core.logger import get_logger
from core.url_security import (
    OutboundURLPolicy,
    UnsafeTargetUrlError,
    _reject_blocked_hostname,
    _reject_private_ip,
    _resolved_ips,
)

logger = get_logger("pinned_dns")


class _PinningBackend(httpcore.AsyncNetworkBackend):
    """Network backend that validates + pins the destination IP per connect."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        target_ip = await self._validate_and_pick_ip(host, port)
        return await self._inner.connect_tcp(
            target_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Unix sockets are never a valid outbound forwarding target.
        raise UnsafeTargetUrlError("unix socket targets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)

    @staticmethod
    async def _validate_and_pick_ip(host: str, port: int) -> str:
        import asyncio

        policy = OutboundURLPolicy.from_config()
        normalized = host.lower().rstrip(".")

        # Literal IP targets: validate directly, no resolution needed.
        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            _reject_private_ip(literal, policy)
            return normalized

        if not policy.allow_private_target_urls:
            _reject_blocked_hostname(normalized)

        ips = await asyncio.to_thread(_resolved_ips, normalized, port)
        for ip in ips:
            _reject_private_ip(ip, policy)
        # All resolved IPs passed validation; pin to the first one so the socket
        # connects to a checked address (no second, unchecked resolution).
        return str(ips[0])


def harden_transport_against_rebinding(transport: httpx.AsyncBaseTransport) -> None:
    """Wrap an httpx transport's pool backend with the pinning backend.

    Best-effort: if the transport does not expose a compatible connection pool
    (e.g. a custom mock transport used in tests), this is a no-op.
    """
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if pool is None or backend is None:
        return
    if isinstance(backend, _PinningBackend):
        return
    pool._network_backend = _PinningBackend(backend)
    logger.info("[HTTP] 已启用 DNS pinning 出网防护（防 SSRF rebinding）")
