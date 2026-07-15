"""Resolve → learn loop: sediment resolved incidents into KB drafts.

When an incident is summarized (quiet/closed with an AI ``summary_analysis``),
compose that already-generated analysis into a KB document and ingest it as a
``draft``. No new LLM call is made — this reuses the incident's existing summary
— so the loop is cheap. Drafts are excluded from RAG until an operator publishes
them, so the KB grows itself without unreviewed content ever steering analysis.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat
from core.logger import get_logger
from db.session import dml_rowcount, session_scope
from models import Incident, KBDocument
from services.kb.store import ingest_document

logger = get_logger("kb.incident_sediment")

# Bounded per-scan batch, mirroring the incident-summary sweep.
_DRAFT_BATCH = 5
_SOURCE_REF_PREFIX = "incident:"


def _incident_source_ref(incident_id: int) -> str:
    return f"{_SOURCE_REF_PREFIX}{incident_id}"


def _compose_kb_content(summary: dict[str, Any]) -> str:
    """Render an incident's IncidentSummaryResult into a KB document body.

    Section labels are authored (English) scaffolding; the incident's own
    summary text is preserved verbatim (it is data, and may be Chinese).
    """
    sections: list[str] = []
    for label, key in (
        ("Summary", "summary"),
        ("Root cause", "root_cause"),
        ("Impact", "impact"),
        ("Timeline", "timeline_summary"),
    ):
        value = str(summary.get(key) or "").strip()
        if value:
            sections.append(f"## {label}\n{value}")
    recommendations = [str(r).strip() for r in (summary.get("recommendations") or []) if str(r).strip()]
    if recommendations:
        sections.append("## Recommendations\n" + "\n".join(f"- {r}" for r in recommendations))
    return "\n\n".join(sections)


async def draft_kb_from_incident(session: AsyncSession, incident_id: int) -> bool:
    """Ingest one resolved incident's summary as a KB draft. Idempotent.

    Returns True when a draft was (re)written, False when the incident has no
    usable summary. Re-running updates the same draft in place (ingest is keyed
    by content hash), so the scheduled sweep and manual regeneration are safe.
    """
    incident = await session.get(Incident, incident_id)
    if incident is None or not isinstance(incident.summary_analysis, dict):
        return False
    content = _compose_kb_content(incident.summary_analysis)
    if not content.strip():
        return False
    await ingest_document(
        session,
        title=f"Incident resolution: {incident.title}"[:300],
        content=content,
        source_ref=_incident_source_ref(int(incident.id)),
        tags={
            "kind": "incident_resolution",
            "incident_id": int(incident.id),
            "source": incident.source or "",
        },
        status="draft",
    )
    return True


async def _incidents_pending_sediment(session: AsyncSession, limit: int) -> list[int]:
    """Summarized incidents that have no KB row yet (draft or published)."""
    already = select(KBDocument.source_ref).where(KBDocument.source_ref.like(f"{_SOURCE_REF_PREFIX}%"))
    # "incident:" || incidents.id, so the NOT IN filter compares against the
    # stored KBDocument.source_ref values (concat is portable to the SQLite shim).
    incident_ref = _SOURCE_REF_PREFIX + cast(Incident.id, String)
    stmt = (
        select(Incident.id)
        .where(
            Incident.summary_analysis.isnot(None),
            Incident.status.in_(["quiet", "closed"]),
            incident_ref.notin_(already),
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
    result = await session.execute(
        update(KBDocument)
        .where(KBDocument.source_ref == source_ref, KBDocument.status == "draft")
        .values(status="published")
    )
    return dml_rowcount(result)


async def discard_kb_draft(session: AsyncSession, source_ref: str) -> int:
    """Delete a draft document (all its chunks) without publishing."""
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
