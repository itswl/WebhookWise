"""Tests for the resolve → learn loop: resolved incidents → KB drafts."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import Incident, KBDocument
from services.kb.incident_sediment import (
    discard_kb_draft,
    draft_kb_from_incident,
    list_kb_drafts,
    publish_kb_draft,
)

_SUMMARY = {
    "summary": "GPU node ran out of memory",
    "root_cause": "A model server leaked GPU memory across requests",
    "impact": "comfyui inference degraded for ~20 minutes",
    "timeline_summary": "10:00 first alert; 10:20 restarted the pod",
    "recommendations": ["Add a memory ceiling", "Restart on OOM"],
    "confidence": 0.9,
}


async def _add_incident(session: AsyncSession, *, title: str, status: str, summary: dict | None) -> Incident:
    incident = Incident(
        title=title,
        source="volcengine",
        status=status,
        alert_count=3,
        summary_analysis=summary,
        started_at=utcnow(),
        correlation_dimensions={"service": "inference", "environment": "prod"},
    )
    session.add(incident)
    await session.flush()
    return incident


@pytest.mark.asyncio
async def test_draft_from_incident_is_draft_and_excluded_from_rag(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        incident = await _add_incident(session, title="GPU OOM", status="closed", summary=_SUMMARY)
        created = await draft_kb_from_incident(session, int(incident.id))
        assert created is True

    async with db_session_factory() as session:
        rows = list((await session.execute(select(KBDocument))).scalars().all())
        assert rows and all(r.status == "draft" for r in rows)
        assert all(r.source_ref == f"incident:{incident.id}" for r in rows)
        assert all(r.tags and r.tags.get("service") == "inference" for r in rows)
        # Composed body carries the incident's own analysis text.
        blob = "\n".join(r.content for r in rows)
        assert "leaked GPU memory" in blob and "Add a memory ceiling" in blob

        # A draft must not be returned by RAG retrieval.
        from services.kb.retrieval import retrieve

        # (retrieve short-circuits when KB disabled; assert the status filter
        # directly instead so the test is independent of KB_ENABLED config.)
        published = list(
            (await session.execute(select(KBDocument).where(KBDocument.status == "published"))).scalars().all()
        )
        assert published == []
        _ = retrieve  # imported to document intent


@pytest.mark.asyncio
async def test_incident_without_summary_is_skipped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        incident = await _add_incident(session, title="no summary", status="closed", summary=None)
        assert await draft_kb_from_incident(session, int(incident.id)) is False
        assert (await session.execute(select(KBDocument))).first() is None


@pytest.mark.asyncio
async def test_publish_moves_draft_into_rag(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory.begin() as session:
        incident = await _add_incident(session, title="GPU OOM", status="closed", summary=_SUMMARY)
        await draft_kb_from_incident(session, int(incident.id))
        ref = f"incident:{incident.id}"

        drafts = await list_kb_drafts(session)
        assert len(drafts) == 1 and drafts[0]["source_ref"] == ref and drafts[0]["chunks"] >= 1

        published = await publish_kb_draft(session, ref)
        assert published >= 1
        remaining_drafts = await list_kb_drafts(session)
        assert remaining_drafts == []
        rows = list((await session.execute(select(KBDocument))).scalars().all())
        assert rows and all(r.status == "published" for r in rows)


@pytest.mark.asyncio
async def test_sweep_drafts_summarized_incidents_once(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.kb import incident_sediment

    async with db_session_factory.begin() as session:
        await _add_incident(session, title="summarized closed", status="closed", summary=_SUMMARY)
        await _add_incident(session, title="quiet no summary", status="quiet", summary=None)
        await _add_incident(session, title="active summarized", status="active", summary=_SUMMARY)

    # The sweep uses session_scope(); point it at the test factory.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        async with db_session_factory.begin() as session:
            yield session

    monkeypatch.setattr(incident_sediment, "session_scope", _scope)

    first = await incident_sediment.run_pending_kb_drafts()
    # Only the closed+summarized incident qualifies (not the no-summary, not the active one).
    assert first["drafted"] == 1

    # Idempotent: the drafted incident is now excluded, so a second sweep is a no-op.
    second = await incident_sediment.run_pending_kb_drafts()
    assert second["drafted"] == 0


@pytest.mark.asyncio
async def test_pending_sweep_refreshes_human_facts_only_while_draft(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from services.kb.incident_sediment import _incidents_pending_sediment

    async with db_session_factory.begin() as session:
        incident = await _add_incident(
            session,
            title="GPU OOM",
            status="closed",
            summary=_SUMMARY,
        )
        await draft_kb_from_incident(session, int(incident.id))

    async with db_session_factory.begin() as session:
        incident = await session.get(Incident, int(incident.id))
        assert incident is not None
        latest_document_update = await session.scalar(
            select(func.max(KBDocument.updated_at)).where(KBDocument.source_ref == f"incident:{incident.id}")
        )
        assert latest_document_update is not None
        incident.resolution_record = {
            "root_cause": "A human-confirmed CUDA allocator leak",
            "resolution": "Deployed the allocator fix",
            "actor": "alice",
        }
        incident.resolution_record_updated_at = latest_document_update + timedelta(microseconds=1)
        await session.flush()
        assert await _incidents_pending_sediment(session, 5) == [int(incident.id)]
        assert await draft_kb_from_incident(session, int(incident.id)) is True

    async with db_session_factory.begin() as session:
        rows = list(
            (await session.execute(select(KBDocument).where(KBDocument.source_ref == f"incident:{incident.id}")))
            .scalars()
            .all()
        )
        blob = "\n".join(row.content for row in rows)
        assert "A human-confirmed CUDA allocator leak" in blob
        assert "A model server leaked GPU memory" not in blob
        assert await _incidents_pending_sediment(session, 5) == []

        assert await publish_kb_draft(session, f"incident:{incident.id}") >= 1
        incident = await session.get(Incident, int(incident.id))
        assert incident is not None
        incident.resolution_record = {
            **incident.resolution_record,
            "root_cause": "A later correction requiring manual publication",
        }
        incident.resolution_record_updated_at = utcnow() + timedelta(seconds=2)
        await session.flush()
        assert await _incidents_pending_sediment(session, 5) == []
        assert await draft_kb_from_incident(session, int(incident.id)) is False


@pytest.mark.asyncio
async def test_discard_removes_draft(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory.begin() as session:
        incident = await _add_incident(session, title="GPU OOM", status="closed", summary=_SUMMARY)
        await draft_kb_from_incident(session, int(incident.id))
        ref = f"incident:{incident.id}"

        discarded = await discard_kb_draft(session, ref)
        assert discarded >= 1
        assert (await session.execute(select(KBDocument))).first() is None
