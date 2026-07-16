"""Postmortem markdown assembly from incident + members + traces + summary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import DecisionTrace, Incident, IncidentMember, KBDocument, WebhookEvent
from services.incidents.postmortem import build_postmortem_markdown


@pytest.fixture
async def session(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with db_session_factory.begin() as sess:
        yield sess


@pytest.mark.asyncio
async def test_postmortem_assembles_header_timeline_and_actions(session: AsyncSession) -> None:
    now = utcnow()
    incident = Incident(
        title="volcengine incident — gpu-mem-high",
        status="closed",
        source="volcengine",
        started_at=now - timedelta(hours=2),
        resolved_at=now - timedelta(hours=1),
        ended_at=now - timedelta(hours=1),
        alert_count=2,
        top_importance="high",
        workflow_status="resolved",
        assignee="ops-a",
        acknowledged_at=now - timedelta(hours=1, minutes=50),
        escalated_at=now - timedelta(hours=1, minutes=45),
        correlation_dimensions={},
        correlation_confidence=1.0,
        summary_analysis={
            "summary": "GPU memory exhausted on node 2.",
            "root_cause": "Model cache never evicted.",
            "impact": "Rendering paused 40 minutes.",
            "recommendations": ["Add cache eviction", "Alert at 85% instead of 95%"],
        },
    )
    session.add(incident)
    await session.flush()

    first = WebhookEvent(
        source="volcengine",
        timestamp=now - timedelta(hours=2),
        importance="high",
        ai_analysis={"summary": "GPU 显存超过 95%"},
        duplicate_count=1,
    )
    second = WebhookEvent(
        source="volcengine",
        timestamp=now - timedelta(hours=1, minutes=30),
        importance="high",
        is_duplicate=True,
        ai_analysis={"summary": "GPU 显存持续超限"},
        duplicate_count=2,
    )
    session.add_all([first, second])
    await session.flush()
    session.add_all(
        [
            IncidentMember(incident_id=incident.id, event_id=first.id, event_timestamp=first.timestamp),
            IncidentMember(incident_id=incident.id, event_id=second.id, event_timestamp=second.timestamp),
            DecisionTrace(webhook_event_id=first.id, outcome="forwarded", skip_code="none"),
            DecisionTrace(webhook_event_id=second.id, outcome="skipped", skip_code="cooldown"),
            KBDocument(
                title="Incident resolution: gpu-mem-high",
                content="body",
                content_hash="pm-hash",
                chunk_index=0,
                status="draft",
                source_ref=f"incident:{incident.id}",
            ),
        ]
    )
    await session.flush()

    markdown = await build_postmortem_markdown(session, int(incident.id))
    assert markdown is not None

    assert "# Postmortem draft: volcengine incident — gpu-mem-high" in markdown
    assert "- **Duration:** 1h 0m" in markdown
    assert "- **Escalated (SLA breach):**" in markdown
    assert "## Root cause" in markdown and "Model cache never evicted." in markdown
    # Timeline rows carry the per-alert summary, dup marker, and trace outcome.
    assert "GPU 显存超过 95% · forwarded" in markdown
    assert "duplicate · skipped (cooldown)" in markdown
    assert "- [ ] Add cache eviction" in markdown
    assert "Knowledge base: “Incident resolution: gpu-mem-high” (draft)" in markdown


@pytest.mark.asyncio
async def test_postmortem_handles_bare_incident_and_missing(session: AsyncSession) -> None:
    incident = Incident(
        title="bare incident",
        status="active",
        started_at=utcnow(),
        alert_count=0,
        workflow_status="open",
        correlation_dimensions={},
        correlation_confidence=1.0,
    )
    session.add(incident)
    await session.flush()

    markdown = await build_postmortem_markdown(session, int(incident.id))
    assert markdown is not None
    assert "- **Duration:** ongoing" in markdown
    assert "_No member alerts recorded._" in markdown
    assert "- [ ] _Fill in follow-ups._" in markdown

    assert await build_postmortem_markdown(session, 999999) is None
