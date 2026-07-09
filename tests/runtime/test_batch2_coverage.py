"""Coverage for batch-2 service modules: handoff, rule_audit, source_health,
timeline, incident grouping helpers, and activity API endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    from db.session import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _s(f: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with f() as s:
        yield s


# ══ Handoff ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handoff_empty(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from services.operations.handoff import get_handoff_summary

    async for s in _s(session_factory):
        summary = await get_handoff_summary(s, hours=8)
    assert summary["total_alerts"] == 0
    assert "Handoff" in summary["summary_text"]


@pytest.mark.asyncio
async def test_handoff_with_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from datetime import timedelta

    from core.datetime_utils import utcnow
    from models import Incident, WebhookEvent

    now = utcnow()
    async for s in _s(session_factory):
        e = WebhookEvent(source="volcengine", timestamp=now - timedelta(hours=1))
        s.add(e)
        await s.flush()
        inc = Incident(title="test", status="active", source="volcengine",
                       started_at=now - timedelta(minutes=30), alert_count=1)
        s.add(inc)
        await s.commit()
        from services.operations.handoff import get_handoff_summary

        summary = await get_handoff_summary(s, hours=2)
        assert summary["total_alerts"] == 1
        assert summary["active_incidents"] == 1


# ══ Activity API ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sparkline_empty(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from json import loads

    async for s in _s(session_factory):
        from api.v1.activity import sparkline_endpoint

        r = await sparkline_endpoint(days=7, session=s)
        d = loads(r.body)
    assert d["success"] is True


@pytest.mark.asyncio
async def test_handoff_endpoint(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from json import loads

    async for s in _s(session_factory):
        from api.v1.activity import handoff_summary_endpoint

        r = await handoff_summary_endpoint(hours=1, session=s)
        d = loads(r.body)
    assert d["success"] is True
    assert "summary_text" in d["data"]


# ══ Rule audit ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rule_audit_empty(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from services.webhooks.rule_audit import get_rule_audit

    async for s in _s(session_factory):
        rows = await get_rule_audit(s, window_days=30)
    assert rows == []


@pytest.mark.asyncio
async def test_rule_audit_with_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from datetime import timedelta

    from core.datetime_utils import utcnow
    from models import WebhookEvent

    now = utcnow()
    async for s in _s(session_factory):
        e = WebhookEvent(source="volcengine", timestamp=now - timedelta(days=2),
                         parsed_data={"RuleName": "test_rule"})
        s.add(e)
        await s.commit()
        from services.webhooks.rule_audit import get_rule_audit

        rows = await get_rule_audit(s, window_days=7, min_events=1)
        assert len(rows) >= 1
        assert rows[0]["rule_name"] == "test_rule"


# ══ Source health ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_source_health_empty(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from services.webhooks.source_health import get_source_health

    async for s in _s(session_factory):
        rows = await get_source_health(s, window_days=7)
    assert rows == []


@pytest.mark.asyncio
async def test_source_health_with_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from datetime import timedelta

    from core.datetime_utils import utcnow
    from models import WebhookEvent

    now = utcnow()
    async for s in _s(session_factory):
        e = WebhookEvent(source="volcengine", timestamp=now - timedelta(days=1))
        s.add(e)
        await s.commit()
        from services.webhooks.source_health import get_source_health

        rows = await get_source_health(s, window_days=7)
        assert len(rows) == 1
        assert rows[0]["source"] == "volcengine"


# ══ Timeline ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_timeline_not_found(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from services.webhooks.timeline import build_alert_timeline

    async for s in _s(session_factory):
        result = await build_alert_timeline(s, 99999)
    assert result["anchor"] is None


@pytest.mark.asyncio
async def test_timeline_with_anchor(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from datetime import timedelta

    from core.datetime_utils import utcnow
    from models import WebhookEvent

    now = utcnow()
    async for s in _s(session_factory):
        e = WebhookEvent(source="volcengine", timestamp=now - timedelta(minutes=30))
        s.add(e)
        await s.flush()
        eid = e.id
        await s.commit()
        from services.webhooks.timeline import build_alert_timeline

        result = await build_alert_timeline(s, eid)
    assert result["anchor"] is not None
    assert result["anchor"]["source"] == "volcengine"


# ══ Incident grouping helpers ══════════════════════════════════════════════════════


def test_event_rule_name():
    from unittest.mock import MagicMock

    from services.incidents.grouping import _event_rule_name

    e = MagicMock()
    e.parsed_data = {"RuleName": "GPU alert"}
    assert _event_rule_name(e) == "GPU alert"
    e.parsed_data = None
    assert _event_rule_name(e) == ""


def test_incident_rule_matches_helper():
    from models import Incident
    from services.incidents.grouping import _incident_rule_matches

    inc = Incident(title="volcengine incident — GPU alert")
    assert _incident_rule_matches("GPU alert", inc) is True
    assert _incident_rule_matches("storage", inc) is False


@pytest.mark.asyncio
async def test_create_incident_from_event(session_factory: async_sessionmaker[AsyncSession]) -> None:

    from core.datetime_utils import utcnow
    from models import WebhookEvent
    from services.incidents.grouping import _create_incident_from_event

    now = utcnow()
    async for s in _s(session_factory):
        e = WebhookEvent(source="volcengine", timestamp=now,
                         importance="high", parsed_data={"RuleName": "test"})
        s.add(e)
        await s.flush()
        inc = _create_incident_from_event(e)
    assert "test" in inc.title
    assert inc.alert_count == 1


@pytest.mark.asyncio
async def test_add_event_to_incident(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from datetime import timedelta

    from core.datetime_utils import utcnow
    from models import WebhookEvent
    from services.incidents.grouping import _add_event_to_incident, _create_incident_from_event

    now = utcnow()
    async for s in _s(session_factory):
        e1 = WebhookEvent(source="volcengine", timestamp=now)
        e2 = WebhookEvent(source="volcengine", timestamp=now - timedelta(minutes=1))
        s.add_all([e1, e2])
        await s.flush()
        inc = _create_incident_from_event(e1)
        _add_event_to_incident(inc, e2)
    assert inc.alert_count == 2


@pytest.mark.asyncio
async def test_close_quiet_incidents(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from datetime import timedelta

    from core.datetime_utils import utcnow
    from models import Incident
    from services.incidents.grouping import _close_quiet_incidents

    now = utcnow()
    async for s in _s(session_factory):
        inc = Incident(title="old", status="active", source="x",
                       started_at=now - timedelta(hours=2),
                       updated_at=now - timedelta(minutes=30))
        s.add(inc)
        await s.commit()
        closed = await _close_quiet_incidents(s, now)
    assert closed >= 1


@pytest.mark.asyncio
async def test_grouping_run(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from unittest.mock import patch

    from core.datetime_utils import utcnow
    from models import WebhookEvent

    now = utcnow()

    class FakeScope:
        def __init__(self, sess): self._s = sess
        async def __aenter__(self): return self._s
        async def __aexit__(self, *a): pass

    async for s in _s(session_factory):
        e = WebhookEvent(source="volcengine", timestamp=now,
                         parsed_data={"RuleName": "gpu"})
        s.add(e)
        await s.commit()
        with patch("services.incidents.grouping.session_scope", return_value=FakeScope(s)):
            from services.incidents.grouping import run_incident_grouping

            stats = await run_incident_grouping()
    assert stats["created"] >= 1


# ══ Summary helpers ════════════════════════════════════════════════════════════════


def test_build_alert_briefs():
    from models import WebhookEvent
    from services.incidents.summary import _build_alert_briefs

    e = WebhookEvent(source="volcengine", importance="high",
                     parsed_data={"RuleName": "GPU alert"},
                     ai_analysis={"summary": "test summary"})
    lines = _build_alert_briefs([e])
    assert "volcengine" in lines
    assert "GPU alert" in lines


@pytest.mark.asyncio
async def test_summarize_incident_nonexistent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from services.incidents.summary import summarize_incident

    async for s in _s(session_factory):
        result = await summarize_incident(s, 99999)
    assert result is None
