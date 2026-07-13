"""Durable, post-commit incident summarization."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
from models import Incident, IncidentMember, WebhookEvent
from schemas.analysis import IncidentSummaryResult
from services.analysis.analysis_policies import AIProviderPolicy

logger = get_logger("incidents.summary")

_SUMMARY_BATCH_SIZE = 5
_SUMMARY_MAX_ATTEMPTS = 5
_SUMMARY_LEASE_SECONDS = 180


async def _load_summary_input(incident_id: int) -> tuple[str, str] | None:
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            return None
        stmt = (
            select(WebhookEvent)
            .join(IncidentMember, IncidentMember.event_id == WebhookEvent.id)
            .where(IncidentMember.incident_id == incident_id)
            .order_by(IncidentMember.event_timestamp.desc(), IncidentMember.id.desc())
            .limit(30)
        )
        members = list((await session.execute(stmt)).scalars().all())
        members.reverse()
        alert_briefs = _build_alert_briefs(members)
        if not alert_briefs:
            return None
        return incident.title, alert_briefs


async def summarize_incident(incident_id: int) -> dict[str, Any] | None:
    """Call the LLM without holding a DB transaction, then persist atomically."""
    loaded = await _load_summary_input(incident_id)
    if loaded is None:
        return None
    title, alert_briefs = loaded

    from services.analysis.ai_llm_client import create_structured_completion
    from services.analysis.ai_prompt import load_incident_summary_prompt_template
    from services.analysis.ai_usage import log_ai_usage

    policy = AIProviderPolicy.from_config()
    if not policy.available:
        logger.info("[Incidents] AI provider unavailable; summary remains pending id=%s", incident_id)
        return None
    template = await load_incident_summary_prompt_template()
    prompt = template.format(alert_briefs=alert_briefs)
    result, tokens_in, tokens_out = await create_structured_completion(
        response_model=IncidentSummaryResult,
        user_prompt=prompt,
        source="incident_summary",
        policy=policy,
    )
    summary_data = result.model_dump(mode="json")

    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            return None
        incident.summary_analysis = summary_data
        incident.summary_status = "completed"
        incident.summary_next_attempt_at = None
        incident.summary_last_error = None
        incident.updated_at = utcnow()

    await log_ai_usage(
        route_type="incident_summary",
        alert_hash=f"incident:{incident_id}",
        source="incident",
        model=policy.model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        policy=policy,
    )
    logger.info("[Incidents] Summary persisted incident_id=%s title=%s", incident_id, title[:80])
    return {"id": incident_id, "summary_analysis": summary_data}


async def _claim_pending_summaries() -> list[int]:
    now = utcnow()
    lease_until = now + timedelta(seconds=_SUMMARY_LEASE_SECONDS)
    async with session_scope() as session:
        stmt = (
            select(Incident)
            .where(
                Incident.status.in_(["quiet", "closed"]),
                Incident.summary_analysis.is_(None),
                Incident.summary_status.in_(["pending", "retrying", "processing"]),
                or_(
                    Incident.summary_next_attempt_at.is_(None),
                    Incident.summary_next_attempt_at <= now,
                ),
                Incident.summary_attempts < _SUMMARY_MAX_ATTEMPTS,
            )
            .order_by(Incident.summary_next_attempt_at.asc(), Incident.id.asc())
            .limit(_SUMMARY_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        incidents = list((await session.execute(stmt)).scalars().all())
        for incident in incidents:
            incident.summary_status = "processing"
            incident.summary_attempts += 1
            incident.summary_next_attempt_at = lease_until
        return [int(incident.id) for incident in incidents]


async def _mark_summary_retry(incident_id: int, error: BaseException | str) -> None:
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None or incident.summary_analysis is not None:
            return
        message = str(error)[:1000]
        incident.summary_last_error = message
        if incident.summary_attempts >= _SUMMARY_MAX_ATTEMPTS:
            incident.summary_status = "failed"
            incident.summary_next_attempt_at = None
            return
        delay = min(3600, 30 * (2 ** max(0, incident.summary_attempts - 1)))
        incident.summary_status = "retrying"
        incident.summary_next_attempt_at = utcnow() + timedelta(seconds=delay)


async def run_pending_incident_summaries() -> dict[str, int]:
    """Process one claimed batch; durable state retries crashes and failures."""
    if not AIProviderPolicy.from_config().available:
        return {"claimed": 0, "completed": 0, "failed": 0}
    incident_ids = await _claim_pending_summaries()
    completed = 0
    failed = 0
    for incident_id in incident_ids:
        try:
            result = await summarize_incident(incident_id)
            if result is None:
                await _mark_summary_retry(incident_id, "summary input unavailable")
                failed += 1
            else:
                completed += 1
        except Exception as exc:  # noqa: BLE001 - background boundary must persist retry state
            logger.warning(
                "[Incidents] Summary generation failed incident_id=%s error=%s",
                incident_id,
                exc,
            )
            await _mark_summary_retry(incident_id, exc)
            failed += 1
    return {"claimed": len(incident_ids), "completed": completed, "failed": failed}


def _build_alert_briefs(members: list[WebhookEvent]) -> str:
    briefs: list[str] = []
    for event in members:
        timestamp = event.timestamp
        timestamp_text = timestamp.isoformat() if timestamp is not None else "?"
        parsed = event.parsed_data or {}
        rule_name = ""
        if isinstance(parsed, dict):
            rule_name = str(parsed.get("RuleName") or parsed.get("AlertName") or parsed.get("alert_name") or "")[:100]
        summary = ""
        if isinstance(event.ai_analysis, dict):
            summary = str(event.ai_analysis.get("summary", "") or "")[:200]
        duplicate = " [duplicate]" if event.is_duplicate else ""
        line = (
            f"[{timestamp_text}] {event.source or 'unknown'} | " f"{event.importance or '?'} | {rule_name}{duplicate}"
        )
        if summary:
            line += f"\n  {summary}"
        briefs.append(line.strip())
    return "\n".join(briefs)
