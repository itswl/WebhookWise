"""MCP server definition and its read-only WebhookWise tools.

Each tool is a thin wrapper over an existing service-layer query function; it
opens a ``session_scope()`` transaction, calls the query, and returns the same
dict the REST API returns. No business logic lives here — the goal is to expose
the read side, not reimplement it.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp

from api.mcp.agent_guide import AGENT_GUIDE
from api.mcp.auth import MCPAuthMiddleware
from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from db.session import session_scope
from models import DeepAnalysis, WebhookEvent
from schemas.analysis import deep_analysis_to_dict, deep_analysis_to_summary_dict
from schemas.silences import silence_to_dict
from services.analysis.analysis_queries import get_ai_usage_stats, get_deep_analyses_for_webhook
from services.forwarding.outbox_queries import list_outbox_records as query_outbox_records
from services.incidents.queries import list_incidents as query_incidents
from services.incidents.service_profiles import global_response_metrics
from services.kb.retrieval import retrieve as kb_retrieve
from services.operations.handoff import get_handoff_summary
from services.silences.store import list_silences
from services.webhooks.decision_trace_queries import (
    get_decision_trace_for_event,
    get_decision_trace_quality_stats,
    get_forward_rule_hit_counts,
    get_overview_stats,
    get_silence_suppression_counts,
    list_decision_traces,
)
from services.webhooks.query_service import (
    get_dead_letter_detail,
    list_dead_letters,
    list_webhook_summaries,
    window_to_time_from,
)
from services.webhooks.sandbox import test_webhook_payload

# MCP 2.0 moved the transport knobs off the server object: stateless_http,
# json_response, streamable_http_path and transport_security are arguments to
# streamable_http_app() now (see build_mcp_app), not constructor/settings
# fields. The server itself only declares identity and tools.
mcp_server = MCPServer(name="webhookwise")

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


async def _attach_deep_analysis_markers(session: AsyncSession, items: list[dict[str, Any]]) -> None:
    """Annotate each alert with a lightweight deep-analysis marker.

    A single batched IN query (no N+1); we attach only a small marker
    (availability + status + one-line preview + id), NOT the full ~49 KB
    report — that stays behind get_ai_analysis so a list page stays small.
    Newest analysis wins when an event has several.
    """
    event_ids = [int(item["id"]) for item in items if item.get("id")]
    if not event_ids:
        return
    rows = (
        (
            await session.execute(
                select(DeepAnalysis)
                .where(DeepAnalysis.webhook_event_id.in_(event_ids))
                .order_by(DeepAnalysis.id.desc())
            )
        )
        .scalars()
        .all()
    )

    # First row per event id is the newest (ordered desc).
    latest: dict[int, DeepAnalysis] = {}
    for row in rows:
        latest.setdefault(int(row.webhook_event_id), row)

    for item in items:
        record = latest.get(int(item["id"])) if item.get("id") else None
        if record is None:
            item["deep_analysis"] = {"available": False}
            continue
        summary = deep_analysis_to_summary_dict(record)
        item["deep_analysis"] = {
            "available": True,
            "analysis_id": summary.get("id"),
            "status": summary.get("status"),
            "engine": summary.get("engine"),
            "summary_preview": summary.get("summary_preview", ""),
        }


@mcp_server.tool(
    title="List recent alerts",
    description="List recent webhook alert summaries (newest first). Optional filters: importance, source, "
    "and window ('today' | '7d' | '30d' | 'all'). Each item includes id, source, importance, timestamp, "
    "duplicate info, forward status, the lightweight AI summary, and a `deep_analysis` marker "
    "({available, status, summary_preview, analysis_id}) — call get_ai_analysis for the full deep report. "
    "page_size is capped at 200.",
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
        await _attach_deep_analysis_markers(session, items)
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
    description="Return per-forward-rule match counts over a rolling 90-day window and last-matched timestamps — the "
    "ROI view that answers which rule is carrying the load and which enabled rule has gone quiet (a zombie rule). "
    "Counts are windowed, unlike get_silence_roi which is lifetime: a rule idle for over 90 days reports 0. "
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


@mcp_server.tool(
    title="Get AI analysis for an alert",
    description="Return WebhookWise's AI analysis for a webhook event id. Prefers the full deep-analysis reports "
    "(summary, root cause, evidence, recommendations); if the event was never deep-analysed, falls back to the "
    "lightweight per-alert AI (importance + one-line summary). The `analysis_level` field is 'deep', "
    "'lightweight', or 'none'. This is the analysis WebhookWise already did — reuse it instead of re-deriving.",
)
async def get_ai_analysis(webhook_event_id: int, limit: int = 10) -> dict[str, Any]:
    async with session_scope() as session:
        records = await get_deep_analyses_for_webhook(session, webhook_event_id, limit=min(max(limit, 1), 50))
        if records:
            return {"analysis_level": "deep", "items": [deep_analysis_to_dict(r) for r in records]}

        # No deep analysis → fall back to the event's lightweight AI verdict so a
        # single lookup is never empty for an event that exists.
        event = await session.get(WebhookEvent, webhook_event_id)
        if event is None:
            return {"analysis_level": "none", "items": []}
        light = dict(event.ai_analysis) if event.ai_analysis else {}
        if not light:
            return {"analysis_level": "none", "items": []}
        return {
            "analysis_level": "lightweight",
            "items": [
                {
                    "webhook_event_id": webhook_event_id,
                    "source": event.source,
                    "importance": light.get("importance") or event.importance,
                    "summary": light.get("summary"),
                    "analysis": light,
                }
            ],
        }


@mcp_server.tool(
    title="Search knowledge base",
    description="Semantic search over WebhookWise's internal knowledge base / runbooks. Returns the top matching "
    "chunks (title, content, source reference, similarity score) for a natural-language query. Returns an empty "
    "list when the KB is disabled or nothing clears the similarity floor.",
)
async def search_knowledge_base(query: str) -> dict[str, Any]:
    async with session_scope() as session:
        chunks = await kb_retrieve(session, query)
    return {
        "items": [
            {"title": c.title, "content": c.content, "source_ref": c.source_ref, "score": round(c.score, 4)}
            for c in chunks
        ]
    }


@mcp_server.tool(
    title="List active silences",
    description="List the silence rules currently in effect (not lifted, not expired), each annotated with how "
    "many alerts it has suppressed and when it last fired. Answers 'why is it quiet / what is being muted'.",
)
async def list_active_silences() -> dict[str, Any]:
    now = utcnow()
    async with session_scope() as session:
        silences = await list_silences(session, active_only=True)
        suppression = await get_silence_suppression_counts(session, silence_ids=[s.id for s in silences])
    items = []
    for s in silences:
        item = silence_to_dict(s, now=now)
        stat = suppression.get(s.id)
        item["suppressed_count"] = stat["count"] if stat else 0
        item["last_suppressed_at"] = stat["last_suppressed_at"] if stat else None
        items.append(item)
    return {"items": items}


@mcp_server.tool(
    title="Get silence suppression counts",
    description="Return per-silence suppression counts (lifetime) and last-suppressed timestamps — the ROI view "
    "for silence rules: which rule is muting the most noise, and which active rule is a zombie (zero count). "
    "Returns a mapping of silence_id -> {count, last_suppressed_at}.",
)
async def get_silence_roi() -> dict[str, dict[str, Any]]:
    async with session_scope() as session:
        counts = await get_silence_suppression_counts(session)
    # JSON object keys must be strings; the query keys by int silence id.
    return {str(sid): stat for sid, stat in counts.items()}


@mcp_server.tool(
    title="Get AI cost stats",
    description="Return AI usage/cost statistics over a time window (period: 'day' | 'week' | 'month' | 'year'): "
    "token consumption, request counts and estimated spend, so an agent can reason about AI cost.",
)
async def get_ai_cost_stats(period: str = "day") -> dict[str, Any]:
    async with session_scope() as session:
        return await get_ai_usage_stats(session, _valid_period(period))


@mcp_server.tool(
    title="Get decision-trace quality stats",
    description="Return decision-quality meta-stats over a time window (period: 'day' | 'week' | 'month' | "
    "'year'): AI vs rule routing breakdown, importance-override rate, degraded-analysis rate and reasons. "
    "Useful for an agent doing meta-analysis of how WebhookWise is deciding.",
)
async def get_decision_quality_stats(period: str = "day") -> dict[str, Any]:
    async with session_scope() as session:
        return await get_decision_trace_quality_stats(session, _valid_period(period))


@mcp_server.tool(
    title="Test a webhook payload (dry run)",
    description="Dry-run a raw webhook payload through WebhookWise's pre-AI pipeline with ZERO side effects "
    "(no enqueue, no AI call, no persistence). Given a source and a JSON payload, reports which adapter parses "
    "it, the alert identity/hash, the rule-based importance, and which forward rules / silences would match. "
    "Use it to test a new integration's payload before wiring it up.",
)
async def test_alert_payload(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with session_scope() as session:
        return await test_webhook_payload(session, source=source, payload=payload)


# ── Resources: stable reference material an agent can read like a document ────


@mcp_server.tool(
    title="List incidents",
    description="List incidents (grouped alerts) newest first, each with status, workflow state "
    "(open/acknowledged/resolved/ignored), assignee, alert count, top importance, and timing. Optional "
    "status filter: 'active' | 'quiet' | 'closed'. THE tool for \"what is going on / what happened\" "
    "questions; follow up per alert with get_alert_decision_trace or get_ai_analysis.",
)
async def list_incidents(status: str = "", page_size: int = 20) -> dict[str, Any]:
    async with session_scope() as session:
        rows, has_more, _cursor = await query_incidents(
            session, status=status.strip().lower(), page_size=_clamp_page_size(page_size)
        )
    return {"items": rows, "has_more": has_more}


@mcp_server.tool(
    title="Get on-call handoff brief",
    description="One-screen shift-handoff digest over the last N hours (default 8, max 168): alert counts, "
    "high-priority share, top sources, active/quieted incidents, and a ready-to-paste markdown brief "
    '(summary_text). The fastest way for an agent to answer "what happened while I was away".',
)
async def get_handoff_brief(hours: int = 8) -> dict[str, Any]:
    async with session_scope() as session:
        return await get_handoff_summary(session, hours=max(1, min(int(hours), 168)))


@mcp_server.tool(
    title="Get response metrics",
    description="Global MTTA / MTTR / acknowledgement rate over recent incidents (window_days, default 30, "
    "max 365) — the same arithmetic as the per-service profiles, ungrouped. Values are null when nothing "
    "in the window was acknowledged/resolved; that is an honest answer, not an error.",
)
async def get_response_metrics(window_days: int = 30) -> dict[str, Any]:
    async with session_scope() as session:
        return await global_response_metrics(session, window_days=max(1, min(int(window_days), 365)))


@mcp_server.tool(
    title="List forwarding outbox records",
    description="List outbound delivery intents (the transactional outbox) newest first: target, status "
    "(pending | processing | retrying | sent | exhausted | expired), attempts, last error, timestamps. "
    'Optional status filter. Answers "did the notification for alert X actually go out, and why not" — '
    "dead-lettered ALERTS (processing failures) are list_dead_letter_alerts instead.",
)
async def list_forward_outbox(status: str = "", page_size: int = 20) -> dict[str, Any]:
    data = await query_outbox_records(page_size=_clamp_page_size(page_size), status=status.strip().lower())
    return {"items": data.get("items", []), "total": data.get("total"), "has_more": data.get("has_more")}


_DECISION_TRACE_FIELD_GUIDE = """\
# WebhookWise decision-trace fields

- outcome: "forwarded" | "skipped" — the final routing decision for the alert.
- skip_code: why a skipped alert was not forwarded. Common values:
  - silenced: matched an active silence rule (see the silence_id field).
  - duplicate: deduplicated against a recent identical alert.
  - noise_reduced: suppressed by noise-reduction scoring.
  - cooldown / periodic_reminder: within a per-alert cooldown window.
  - no_rule_match: no forward rule matched.
- route: how importance/summary was decided: "ai" (model), "rule" (deterministic
  rules), or "redis_reuse" (reused a cached prior verdict for the same alert).
- importance_override: true when a rule/policy overrode the AI's importance.
- degraded_reason: set when analysis ran in a degraded mode (e.g. ai_error).
- silence_id: the silence rule that suppressed the alert (only when silenced).
- matched_rules: names of the forward rules the alert matched.

Analysis payloads (get_ai_analysis, list_recent_alerts) also carry:
- triage_verdict: "act_now" | "monitor" | "defer" — should an operator act now.
- triage_confidence: 0-1 confidence in that verdict (rule-pass verdicts are
  deliberately modest; analyses cached before the field existed omit both).
"""


@mcp_server.resource(
    "webhookwise://reference/decision-trace-fields",
    name="decision-trace-field-guide",
    title="Decision-trace field guide",
    description="Reference for interpreting decision-trace fields (outcome, skip_code, route, etc.).",
    mime_type="text/markdown",
)
def decision_trace_field_guide() -> str:
    return _DECISION_TRACE_FIELD_GUIDE


@mcp_server.resource(
    "webhookwise://reference/agent-guide",
    name="agent-guide",
    title="Agent usage guide",
    description="How an agent should use this MCP surface: which tool answers which question, "
    "investigation recipes, field semantics, and the read-only boundary.",
    mime_type="text/markdown",
)
def agent_guide() -> str:
    return AGENT_GUIDE


# ── Prompts: reusable investigation templates an agent can invoke ────────────


@mcp_server.prompt(
    name="investigate_alert",
    title="Investigate an alert",
    description="Guide a root-cause investigation of a single alert using the WebhookWise MCP tools.",
)
def investigate_alert_prompt(webhook_event_id: str) -> str:
    return (
        f"Investigate WebhookWise alert with webhook_event_id={webhook_event_id}.\n"
        "1. Call get_alert_decision_trace to see whether it was forwarded or skipped and why "
        "(check skip_code, route, silence_id, matched_rules).\n"
        "2. Call get_ai_analysis for the existing verdict — triage_verdict/confidence, root cause, "
        "summary, recommendations. Reuse it, don't re-derive.\n"
        "3. Call list_incidents to see whether this alert belongs to a wider incident — sibling alerts "
        "and the incident's workflow state are the strongest context.\n"
        "4. If it was silenced, call list_active_silences to identify the muting rule.\n"
        "5. Optionally call search_knowledge_base with the alert's key terms for relevant runbooks.\n"
        "Then explain, in plain language, what happened to this alert and what to do next."
    )


@mcp_server.prompt(
    name="review_silence_roi",
    title="Review silence-rule ROI",
    description="Find zombie silence rules (active but suppressing nothing) worth cleaning up.",
)
def review_silence_roi_prompt() -> str:
    return (
        "Review the ROI of WebhookWise silence rules.\n"
        "1. Call list_active_silences to get the currently-active rules with their suppressed_count.\n"
        "2. Call get_silence_roi for lifetime counts and last_suppressed_at per rule.\n"
        "Flag any active rule with a zero or very stale count as a candidate for removal, and summarize "
        "which rules are pulling their weight versus which are zombies."
    )


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _transport_security() -> TransportSecuritySettings:
    """Build the Host/Origin allowlist for DNS-rebinding protection.

    The transport defaults to loopback-only. Behind a reverse proxy the public
    Host (e.g. webhookwise.example.com) must be added explicitly, or every request
    421s. Loopback hosts/origins are always kept so local health checks work.
    """
    security = get_config_manager().security
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *_split_csv(security.MCP_ALLOWED_HOSTS)]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        *_split_csv(security.MCP_ALLOWED_ORIGINS),
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_mcp_app() -> ASGIApp:
    """Build the Streamable-HTTP ASGI app for mounting, wrapped with auth.

    Stateless HTTP + JSON responses: every request is authenticated and served
    independently (read-only queries need no session affinity), which keeps the
    endpoint friendly behind a reverse proxy.

    streamable_http_path="/" matters: the app is mounted at "/mcp" in
    api/app.py, so the transport's own route must be the mount root, otherwise
    the effective path becomes "/mcp/mcp" and clients hitting /mcp 404.

    The returned app must have its ``session_manager`` lifecycle driven by the
    parent application's lifespan (Starlette does not run a mounted sub-app's
    lifespan). See ``mcp_server.session_manager.run()`` used in ``api/app.py``.
    """
    return MCPAuthMiddleware(
        mcp_server.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            transport_security=_transport_security(),
        )
    )
