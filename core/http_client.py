from __future__ import annotations

import httpx

from core.config import AppConfig
from core.logger import get_logger
from core.observability.tracing import inject_trace_headers

logger = get_logger("http_client")


async def _inject_trace_headers(request: httpx.Request) -> None:
    inject_trace_headers(request.headers)


def build_http_client(
    config: AppConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    if config is None:
        from core.app_context import get_config_manager

        config = get_config_manager()
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(config.retry.FORWARD_TIMEOUT_SECONDS, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        event_hooks={"request": [_inject_trace_headers]},
    )
    # Pin DNS at connect time so a target hostname cannot rebind to a private/
    # metadata IP between URL validation and the actual socket connect. Only the
    # default transport is hardened; an explicitly injected transport (tests,
    # mocks) is left untouched.
    if transport is None:
        from core.pinned_dns import harden_transport_against_rebinding

        harden_transport_against_rebinding(client._transport)
    return client


def get_http_client() -> httpx.AsyncClient:
    """Return the AsyncClient owned by the current AppContext."""
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is None:
        raise RuntimeError("default AppContext is not initialized")
    if context.http_client is None or context.http_client.is_closed:
        context.http_client = build_http_client(context.config)
        logger.info("[HTTP] Context async client initialized successfully")
    return context.http_client


_deep_analysis_client: httpx.AsyncClient | None = None


def get_deep_analysis_client() -> httpx.AsyncClient:
    """Client for the deep-analysis gateway, deliberately NOT DNS-hardened.

    The shared client pins DNS and rejects private/blocked addresses, which is
    right for forward targets: those URLs arrive from rules an operator writes
    about data a webhook sent, so the resolved address must be proven public or
    an alert payload becomes an SSRF primitive.

    The deep-analysis gateway is not that. Its URL comes from
    OPENCLAW_GATEWAY_URL — process configuration, set by whoever deploys the
    service, never derived from a payload — and the intended target IS private
    infrastructure: a sidecar on the container network. Under the shared client
    every request died with "target host resolves to a non-public IP", so the
    whole deep-analysis leg has been failing by design rather than by accident.

    Same reasoning, and same shape, as _FeishuRelayChannel's own client: a
    trusted internal hop, chosen by an operator, does not need the guard built
    for untrusted input.

    Passing an explicit transport is what opts out — build_http_client only
    hardens the default one.
    """
    global _deep_analysis_client
    if _deep_analysis_client is None or _deep_analysis_client.is_closed:
        _deep_analysis_client = build_http_client(transport=httpx.AsyncHTTPTransport())
        logger.info("[HTTP] Deep-analysis client initialized (private targets allowed)")
    return _deep_analysis_client
