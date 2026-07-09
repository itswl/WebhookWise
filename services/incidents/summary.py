"""Incident summarization — one LLM call per incident to produce a structured
post-incident narrative.

Uses the same OpenAI client / model / key as the main AI analyzer so it
inherits the existing config without new knobs. Summarization is best-effort:
failures are logged but never block incident closure.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.json import JSONDecodeError, loads
from core.logger import get_logger
from models import Incident, WebhookEvent

logger = get_logger("incidents.summary")

_INCIDENT_SUMMARY_PROMPT = """You are an SRE analyzing an operational incident. Below are the alerts
that fired during this incident, ordered chronologically.

Produce a JSON object with these fields:
- "summary": a 2-3 sentence plain-English overview of what happened.
- "root_cause": the most likely root cause based on the alert pattern.
- "impact": the blast radius and severity assessment.
- "timeline_summary": a chronological bullet description of the key events.
- "recommendations": a list of 1-3 concrete prevention steps.
- "confidence": a float 0-1 representing your confidence in this analysis.

Only output the JSON object. Do not include markdown fences or commentary.

ALERTS:
{alert_briefs}
"""


async def summarize_incident(session: AsyncSession, incident_id: int) -> dict[str, Any] | None:
    """Generate an LLM summary for an incident. Returns the summary dict or None.

    The result is persisted on the incident row so subsequent reads don't re-call
    the LLM.
    """
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None

    member_ids = incident.member_ids or []
    if not member_ids:
        return None

    # Load up to 30 most recent member alerts for the prompt.
    recent_ids = member_ids[-30:]
    members = list(
        (
            await session.execute(
                select(WebhookEvent).where(WebhookEvent.id.in_(recent_ids))
            )
        ).scalars().all()
    )
    members.sort(key=lambda e: getattr(e, "timestamp", 0) or 0)

    alert_briefs = _build_alert_briefs(members)
    if not alert_briefs:
        return None

    prompt = _INCIDENT_SUMMARY_PROMPT.format(alert_briefs=alert_briefs)

    summary_data = await _call_llm_for_summary(prompt)
    if summary_data is None:
        return None

    incident.summary_analysis = dict(summary_data)
    await session.flush()
    logger.info("[Incidents] Summary persisted incident_id=%s title=%s", incident_id, incident.title[:80])
    return {"id": incident_id, "summary_analysis": summary_data}


async def _call_llm_for_summary(prompt: str) -> dict[str, Any] | None:
    """Call the OpenAI-compatible LLM with a plain-text prompt, return parsed JSON.

    Does NOT use instructor structured output — the incident summary schema is
    simple enough for a plain JSON completion, and using the same OpenAI client
    as the main analyzer ensures config (model, key, URL) is shared.
    """

    import httpx

    from core.observability.tracing import get_current_trace_id

    cfg = get_config_manager().ai
    if not cfg.OPENAI_API_KEY:
        logger.warning("[Incidents] No API key configured; skipping LLM summary")
        return None

    headers: dict[str, str] = {
        "Authorization": f"Bearer {cfg.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    trace_id = get_current_trace_id()
    if trace_id:
        headers["x-trace-id"] = trace_id

    body: dict[str, Any] = {
        "model": str(cfg.OPENAI_MODEL),
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    url = (str(cfg.OPENAI_API_URL).rstrip("/")) + "/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=float(cfg.AI_HTTP_TIMEOUT_SECONDS)) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        logger.warning("[Incidents] LLM call failed: %s", e)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.warning("[Incidents] Unexpected LLM response shape: %s", e)
        return None

    text = str(content or "").strip()
    # Strip markdown fences if the model wraps the output.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()

    try:
        parsed = loads(text)
    except (TypeError, JSONDecodeError):
        logger.warning("[Incidents] LLM returned non-JSON: %s...", text[:200])
        return None

    return parsed if isinstance(parsed, dict) else None


def _build_alert_briefs(members: list[WebhookEvent]) -> str:
    """Build a compact text representation of each alert for the LLM prompt."""
    briefs: list[str] = []
    for e in members:
        ts = getattr(e, "timestamp", None)
        ts_str = ts.isoformat() if ts is not None else "?"
        source = e.source or "unknown"
        importance = e.importance or "?"
        rule_name = ""
        parsed = e.parsed_data or {}
        if isinstance(parsed, dict):
            rule_name = str(
                parsed.get("RuleName") or parsed.get("AlertName") or parsed.get("alert_name") or ""
            )[:100]
        summary = ""
        if isinstance(e.ai_analysis, dict):
            summary = str(e.ai_analysis.get("summary", "") or "")[:200]
        dup = " [duplicate]" if e.is_duplicate else ""
        line = f"[{ts_str}] {source} | {importance} | {rule_name}{dup}"
        if summary:
            line += f"\n  {summary}"
        briefs.append(line.strip())
    return "\n\n".join(briefs)
