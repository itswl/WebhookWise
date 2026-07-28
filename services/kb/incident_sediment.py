"""Resolve → learn loop: sediment resolved incidents into KB drafts.

When an incident is summarized (quiet/closed with an AI ``summary_analysis``),
compose that already-generated analysis into a KB document and ingest it as a
``draft``. No new LLM call is made — this reuses the incident's existing summary
— so the loop is cheap. Drafts are excluded from RAG until an operator publishes
them, so the KB grows itself without unreviewed content ever steering analysis.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, and_, cast, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat
from core.logger import get_logger
from db.session import acquire_advisory_xact_lock, dml_rowcount, session_scope
from models import Incident, KBDocument
from services.kb.store import ingest_document

logger = get_logger("kb.incident_sediment")

# Bounded per-scan batch, mirroring the incident-summary sweep.
_DRAFT_BATCH = 5
_SOURCE_REF_PREFIX = "incident:"


def _incident_source_ref(incident_id: int) -> str:
    return f"{_SOURCE_REF_PREFIX}{incident_id}"


def _compose_kb_content(
    summary: dict[str, Any],
    resolution_record: dict[str, object] | None = None,
) -> str:
    """Render an incident's IncidentSummaryResult into a KB document body.

    Section labels are authored (English) scaffolding; the incident's own
    text is preserved verbatim (it is data, and may be Chinese). Human-confirmed
    resolution facts take precedence over generated root-cause and impact text.
    """
    confirmed = resolution_record or {}
    sections: list[str] = []
    for label, value in (
        ("Summary", summary.get("summary")),
        ("Root cause category", confirmed.get("root_cause_category")),
        ("Root cause", confirmed.get("root_cause") or summary.get("root_cause")),
        ("Impact", confirmed.get("impact") or summary.get("impact")),
        ("Resolution", confirmed.get("resolution")),
        ("Recovery evidence", confirmed.get("recovery_evidence")),
        ("Resolution owner", confirmed.get("owner")),
        ("Timeline", summary.get("timeline_summary")),
    ):
        text_value = str(value or "").strip()
        if text_value:
            sections.append(f"## {label}\n{text_value}")
    association = str(confirmed.get("change_association") or "").strip()
    related_change_id = confirmed.get("related_change_id")
    if association:
        change_text = association
        if isinstance(related_change_id, int):
            change_text = f"{association} (change #{related_change_id})"
        sections.append(f"## Change association\n{change_text}")
    if "follow_ups" in confirmed and isinstance(confirmed.get("follow_ups"), list):
        raw_recommendations: object = confirmed.get("follow_ups") or []
    else:
        raw_recommendations = summary.get("recommendations") or []
    recommendation_values = raw_recommendations if isinstance(raw_recommendations, list) else []
    recommendations = [str(r).strip() for r in recommendation_values if str(r).strip()]
    if recommendations:
        sections.append("## Follow-ups\n" + "\n".join(f"- {r}" for r in recommendations))
    return "\n\n".join(sections)


async def draft_kb_from_incident(session: AsyncSession, incident_id: int) -> bool:
    """Ingest one resolved incident's summary as a KB draft. Idempotent.

    Returns True when a draft was (re)written, False when the incident has no
    usable summary. Re-running updates the same draft in place (ingest is keyed
    by content hash), so the scheduled sweep and manual regeneration are safe.
    """
    source_ref = _incident_source_ref(incident_id)
    await acquire_advisory_xact_lock(session, f"kb_document:{source_ref}")
    incident = await session.get(Incident, incident_id)
    if incident is None or not isinstance(incident.summary_analysis, dict):
        return False
    has_published_version = await session.scalar(
        select(
            exists().where(
                KBDocument.source_ref == source_ref,
                KBDocument.status == "published",
            )
        )
    )
    if has_published_version:
        # Publishing is the operator's approval boundary. Later resolution
        # edits can inform a new manual revision, but must not silently replace
        # knowledge that has already entered RAG.
        return False
    content = _compose_kb_content(
        incident.summary_analysis,
        incident.resolution_record if isinstance(incident.resolution_record, dict) else None,
    )
    if not content.strip():
        return False
    identity_tags = {
        key: str(value)
        for key, value in (incident.correlation_dimensions or {}).items()
        if key in {"service", "project", "environment", "region"} and str(value).strip()
    }
    await ingest_document(
        session,
        title=f"Incident resolution: {incident.title}"[:300],
        content=content,
        source_ref=source_ref,
        tags={
            "kind": "incident_resolution",
            "incident_id": int(incident.id),
            "source": incident.source or "",
            **identity_tags,
        },
        status="draft",
    )
    return True


async def _incidents_pending_sediment(session: AsyncSession, limit: int) -> list[int]:
    """Find new incident drafts and stale unpublished drafts to refresh."""
    # "incident:" || incidents.id, so the NOT IN filter compares against the
    # stored KBDocument.source_ref values (concat is portable to the SQLite shim).
    incident_ref = _SOURCE_REF_PREFIX + cast(Incident.id, String)
    has_any_document = exists(select(KBDocument.id).where(KBDocument.source_ref == incident_ref))
    has_published_document = exists(
        select(KBDocument.id).where(
            KBDocument.source_ref == incident_ref,
            KBDocument.status == "published",
        )
    )
    has_fresh_draft = exists(
        select(KBDocument.id).where(
            KBDocument.source_ref == incident_ref,
            KBDocument.status == "draft",
            KBDocument.updated_at >= Incident.resolution_record_updated_at,
        )
    )
    stmt = (
        select(Incident.id)
        .where(
            Incident.summary_analysis.isnot(None),
            Incident.status.in_(["quiet", "closed"]),
            or_(
                ~has_any_document,
                and_(
                    Incident.resolution_record_updated_at.isnot(None),
                    ~has_published_document,
                    ~has_fresh_draft,
                ),
            ),
        )
        .order_by(Incident.updated_at.desc(), Incident.id.desc())
        .limit(limit)
    )
    return [int(row[0]) for row in (await session.execute(stmt)).all()]


async def run_pending_kb_drafts() -> dict[str, int]:
    """Scheduled sweep: draft KB entries for newly-summarized incidents."""
    async with session_scope() as session:
        incident_ids = await _incidents_pending_sediment(session, _DRAFT_BATCH)
        created = 0
        for incident_id in incident_ids:
            if await draft_kb_from_incident(session, incident_id):
                created += 1
    if created:
        logger.info("[KB] Sedimented %d resolved incident(s) into KB drafts", created)
    return {"candidates": len(incident_ids), "drafted": created}


async def list_kb_drafts(session: AsyncSession) -> list[dict[str, Any]]:
    """One row per draft document (grouped over its chunks), newest first."""
    stmt = (
        select(
            KBDocument.source_ref,
            func.min(KBDocument.title).label("title"),
            func.count(KBDocument.id).label("chunks"),
            func.max(KBDocument.updated_at).label("updated_at"),
        )
        .where(KBDocument.status == "draft")
        .group_by(KBDocument.source_ref)
        .order_by(func.max(KBDocument.updated_at).desc())
    )
    return [
        {
            "source_ref": row.source_ref,
            "title": row.title,
            "chunks": int(row.chunks),
            "updated_at": utc_isoformat(row.updated_at) if row.updated_at is not None else None,
        }
        for row in (await session.execute(stmt)).all()
    ]


async def publish_kb_draft(session: AsyncSession, source_ref: str) -> int:
    """Publish all draft chunks of a document into the RAG corpus."""
    await acquire_advisory_xact_lock(session, f"kb_document:{source_ref}")
    result = await session.execute(
        update(KBDocument)
        .where(KBDocument.source_ref == source_ref, KBDocument.status == "draft")
        .values(status="published")
    )
    return dml_rowcount(result)


async def discard_kb_draft(session: AsyncSession, source_ref: str) -> int:
    """Delete a draft document (all its chunks) without publishing."""
    await acquire_advisory_xact_lock(session, f"kb_document:{source_ref}")
    result = await session.execute(
        delete(KBDocument).where(KBDocument.source_ref == source_ref, KBDocument.status == "draft")
    )
    return dml_rowcount(result)


__all__ = [
    "discard_kb_draft",
    "draft_kb_from_incident",
    "list_kb_drafts",
    "publish_kb_draft",
    "run_pending_kb_drafts",
]
