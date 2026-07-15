"""API-level tests for the KB draft review endpoints (round-3 learning loop)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.fixture
def session(db_session: AsyncSession) -> AsyncSession:
    return db_session


async def _seed_incident_draft(session: AsyncSession) -> str:
    from models import Incident
    from services.kb.incident_sediment import draft_kb_from_incident

    incident = Incident(
        title="GPU OOM",
        source="volcengine",
        status="closed",
        alert_count=3,
        started_at=utcnow(),
        summary_analysis={
            "summary": "GPU ran out of memory",
            "root_cause": "leak",
            "impact": "degraded",
            "timeline_summary": "10:00 alert",
            "recommendations": ["add ceiling"],
            "confidence": 0.9,
        },
    )
    session.add(incident)
    await session.flush()
    await draft_kb_from_incident(session, int(incident.id))
    await session.commit()
    return f"incident:{incident.id}"


@pytest.mark.asyncio
async def test_list_publish_and_discard_kb_draft_flow(session: AsyncSession) -> None:
    from api.v1 import admin

    ref = await _seed_incident_draft(session)

    listed = _body(await admin.list_kb_drafts_endpoint(session=session))
    assert listed["success"] is True
    assert [d["source_ref"] for d in listed["data"]] == [ref]

    published = _body(await admin.publish_kb_draft_endpoint(ref, session=session))
    assert published["success"] is True
    assert published["data"]["published_chunks"] >= 1

    # Once published it is no longer a draft and now feeds retrieval.
    assert _body(await admin.list_kb_drafts_endpoint(session=session))["data"] == []

    # Publishing again finds nothing to publish → 404.
    missing = await admin.publish_kb_draft_endpoint(ref, session=session)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_discard_kb_draft(session: AsyncSession) -> None:
    from api.v1 import admin

    ref = await _seed_incident_draft(session)

    discarded = _body(await admin.discard_kb_draft_endpoint(ref, session=session))
    assert discarded["success"] is True
    assert discarded["data"]["discarded_chunks"] >= 1
    assert _body(await admin.list_kb_drafts_endpoint(session=session))["data"] == []

    # Nothing left to discard → 404.
    missing = await admin.discard_kb_draft_endpoint(ref, session=session)
    assert missing.status_code == 404
