"""FastMCP server definition and its read-only WebhookWise tools.

Each tool is a thin wrapper over an existing service-layer query function; it
opens a ``session_scope()`` transaction, calls the query, and returns the same
dict the REST API returns. No business logic lives here — the goal is to expose
the read side, not reimplement it.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from api.mcp.auth import MCPAuthMiddleware
from core.app_context import get_config_manager
from db.session import session_scope
from services.webhooks.decision_trace_queries import (
    get_decision_trace_for_event,
    get_forward_rule_hit_counts,
    get_overview_stats,
    list_decision_traces,
)
from services.webhooks.query_service import (
    get_dead_letter_detail,
    list_dead_letters,
    list_webhook_summaries,
    window_to_time_from,
)

# Stateless HTTP + JSON responses: every request is authenticated and served
# independently (no session affinity needed for read-only queries), which also
# keeps the endpoint friendly behind a reverse proxy / load balancer.
#
# streamable_http_path="/" is important: the app is mounted at "/mcp" in
# api/app.py, so the transport's own route must be the mount root ("/"),
# otherwise the effective path becomes "/mcp/mcp" and clients hitting /mcp 404.
mcp_server = FastMCP(
    name="webhookwise",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)

# Clamp page sizes so a recursing agent cannot request unbounded rows.
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 20
_VALID_PERIODS = ("day", "week", "month", "year")


def _clamp_page_size(page_size: int) -> int:
    if page_size < 1:
        return _DEFAULT_PAGE_SIZE
    return min(page_size, _MAX_PAGE_SIZE)


def _valid_period(period: str) -> str:
    return period if period in _VALID_PERIODS else "day"


@mcp_server.tool(
    title="Get alert decision trace",
    description="Return the full decision trace for a single webhook alert by its event id: why it was "
    "forwarded or skipped, which rules matched, the ordered decision steps, and the delivery status. "
    "Returns null if no trace exists for that event.",
)
async def get_alert_decision_trace(webhook_event_id: int) -> dict[str, Any] | None:
    async with session_scope() as session:
        return await get_decision_trace_for_event(session, webhook_event_id)


@mcp_server.tool(
    title="List decision traces",
    description="List recent alert decision traces (newest first), each with its full decision chain inline. "
    "Optional filters: outcome ('forwarded' | 'skipped'), skip_code, source, delivery ('failed' selects "
    "forwarded alerts whose delivery ultimately failed). page_size is capped at 200.",
)
async def list_alert_decision_traces(
    outcome: str = "",
    skip_code: str = "",
    source: str = "",
    delivery: str = "",
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    async with session_scope() as session:
        items, has_more, next_cursor = await list_decision_traces(
            session,
            outcome=outcome if outcome in ("forwarded", "skipped") else "",
            skip_code=skip_code[:40],
            source=source[:100],
            delivery="failed" if delivery == "failed" else "",
            page=max(page, 1),
            page_size=_clamp_page_size(page_size),
        )
    return {"items": items, "has_more": has_more, "next_cursor": next_cursor}


@mcp_server.tool(
    title="List recent alerts",
    description="List recent webhook alert summaries (newest first). Optional filters: importance, source, "
    "and window ('today' | '7d' | '30d' | 'all'). Each item includes id, source, importance, timestamp, "
    "duplicate info, forward status, and the AI summary. page_size is capped at 200.",
)
async def list_recent_alerts(
    importance: str = "",
    source: str = "",
    window: str = "all",
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    async with session_scope() as session:
        items, has_more, next_cursor = await list_webhook_summaries(
            session,
            importance=importance[:50],
            source=source[:100],
            time_from=window_to_time_from(window),
            page=max(page, 1),
            page_size=_clamp_page_size(page_size),
        )
    return {"items": items, "has_more": has_more, "next_cursor": next_cursor}


@mcp_server.tool(
    title="Get overview stats",
    description="One-screen operational summary over a time window (period: 'day' | 'week' | 'month' | 'year'): "
    "processed / forwarded / skipped counts, forward rate, skip-reason breakdown, top sources by volume, and "
    "the delivery success rate.",
)
async def get_alert_overview_stats(period: str = "day") -> dict[str, Any]:
    async with session_scope() as session:
        return await get_overview_stats(session, _valid_period(period))


@mcp_server.tool(
    title="Get forward-rule hit counts",
    description="Return per-forward-rule match counts (lifetime) and last-matched timestamps — the ROI view that "
    "answers which rule is carrying the load and which enabled rule has gone quiet (a zombie rule). "
    "Returns a mapping of rule_name -> {count, last_matched_at}.",
)
async def get_forward_rule_roi() -> dict[str, dict[str, Any]]:
    async with session_scope() as session:
        return await get_forward_rule_hit_counts(session)


@mcp_server.tool(
    title="List dead-letter alerts",
    description="List alerts that landed in the dead-letter state (processing permanently failed). Optional "
    "filters: source and search (matches error message / failure reason). page_size is capped at 200. "
    "Use get_dead_letter_alert for the full detail of one event.",
)
async def list_dead_letter_alerts(
    source: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    async with session_scope() as session:
        items = await list_dead_letters(
            session,
            page=max(page, 1),
            page_size=_clamp_page_size(page_size),
            source=source[:100] or None,
            search=search[:200] or None,
        )
    return {"items": items}


@mcp_server.tool(
    title="Get dead-letter alert detail",
    description="Return the full detail of a single dead-letter alert by its event id (payload, error/failure "
    "reason, retry count). Returns null if the event is not a dead letter.",
)
async def get_dead_letter_alert(event_id: int) -> dict[str, Any] | None:
    async with session_scope() as session:
        return await get_dead_letter_detail(session, event_id)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _configure_transport_security() -> None:
    """Apply the configured Host/Origin allowlist for DNS-rebinding protection.

    FastMCP defaults to loopback-only. Behind a reverse proxy the public Host
    (e.g. dejavu.example.com) must be added explicitly, or every request 421s.
    Loopback hosts/origins are always kept so local health checks still work.
    """
    security = get_config_manager().security
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *_split_csv(security.MCP_ALLOWED_HOSTS)]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        *_split_csv(security.MCP_ALLOWED_ORIGINS),
    ]
    mcp_server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_mcp_app() -> ASGIApp:
    """Build the Streamable-HTTP ASGI app for mounting, wrapped with auth.

    The returned app must have its ``session_manager`` lifecycle driven by the
    parent application's lifespan (Starlette does not run a mounted sub-app's
    lifespan). See ``mcp_server.session_manager.run()`` used in ``api/app.py``.
    """
    _configure_transport_security()
    return MCPAuthMiddleware(mcp_server.streamable_http_app())
