"""Auto-SLA arming + SLA-breach escalation notification behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import ForwardOutbox, Incident
from services.incidents.auto_sla import AutoSlaPolicy, apply_auto_sla, parse_importance_minutes
from services.incidents.notifications import queue_sla_breach_notifications


def _incident(**over: object) -> Incident:
    now = utcnow()
    base: dict[str, object] = {
        "title": "gpu incident — OOM",
        "status": "active",
        "source": "volcengine",
        "started_at": now - timedelta(minutes=45),
        "updated_at": now - timedelta(minutes=45),
        "alert_count": 1,
        "top_importance": "high",
        "workflow_status": "open",
        "correlation_dimensions": {},
        "correlation_confidence": 1.0,
    }
    base.update(over)
    return Incident(**base)  # type: ignore[arg-type]


def test_parse_importance_minutes_drops_invalid_entries() -> None:
    assert parse_importance_minutes("high=30,medium=240") == {"high": 30, "medium": 240}
    # Unknown level, non-numeric, non-positive, and empty entries are dropped.
    assert parse_importance_minutes("critical=5,high=abc,low=0,,medium=60") == {"medium": 60}
    assert parse_importance_minutes("") == {}


def test_apply_auto_sla_arms_only_matching_unset_open_incidents() -> None:
    policy = AutoSlaPolicy(minutes_by_importance={"high": 30})

    armed = _incident()
    assert apply_auto_sla(armed, policy) is True
    assert armed.sla_due_at == armed.updated_at + timedelta(minutes=30)

    # Already-set SLA is never moved.
    preset = _incident(sla_due_at=utcnow() + timedelta(hours=4))
    original = preset.sla_due_at
    assert apply_auto_sla(preset, policy) is False
    assert preset.sla_due_at == original

    # Importance not covered by the mapping.
    assert apply_auto_sla(_incident(top_importance="medium"), policy) is False
    # Resolved incidents are off the hook.
    assert apply_auto_sla(_incident(workflow_status="resolved"), policy) is False
    # Disabled policy.
    assert apply_auto_sla(_incident(), AutoSlaPolicy()) is False


@pytest.fixture
async def session(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with db_session_factory.begin() as sess:
        yield sess


@pytest.mark.asyncio
async def test_breach_sets_escalated_at_and_builds_loud_card(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.app_context import get_config_manager

    cfg = get_config_manager().notifications
    monkeypatch.setattr(cfg, "SLA_BREACH_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/esc")
    monkeypatch.setattr(cfg, "SLA_BREACH_MENTION_ALL", True)

    now = utcnow()
    incident = _incident(sla_due_at=now - timedelta(minutes=5), assignee="")
    session.add(incident)
    await session.flush()

    outbox_ids = await queue_sla_breach_notifications(session, now)
    assert len(outbox_ids) == 1
    assert incident.escalated_at == now

    record = (await session.execute(select(ForwardOutbox))).scalars().one()
    assert record.target_url.endswith("/esc")  # dedicated escalation webhook wins
    assert record.rule_name == "system:sla-breached"
    body = record.formatted_payload["card"]["elements"][0]["content"]
    assert '<at id="all"></at>' in body
    assert "unassigned" in body

    # Second sweep with the same due_at is idempotent: no new outbox row and
    # escalated_at keeps its first value.
    again = await queue_sla_breach_notifications(session, now + timedelta(minutes=1))
    assert again == []
    assert incident.escalated_at == now


@pytest.mark.asyncio
async def test_breach_skips_resolved_and_future_slas(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.app_context import get_config_manager

    cfg = get_config_manager().notifications
    monkeypatch.setattr(cfg, "SLA_BREACH_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/esc")

    now = utcnow()
    session.add(_incident(sla_due_at=now - timedelta(minutes=5), workflow_status="resolved"))
    session.add(_incident(title="future", sla_due_at=now + timedelta(minutes=30)))
    await session.flush()

    assert await queue_sla_breach_notifications(session, now) == []
