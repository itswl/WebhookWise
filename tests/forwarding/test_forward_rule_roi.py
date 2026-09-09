"""The ROI panel reads a rule's count from whichever ledger its traffic lands in.

An alert forwarded by the decisioning pipeline writes a ``decision_trace``. An
internal system event (incident opened/resolved) is queued straight to the
outbox by ``resolve_notification_target`` and writes NO trace. A rule matching
only system events therefore counted 0 traces forever and wore the "matched
nothing" zombie badge while its delivery-health badge, computed from the same
outbox rows, said everything was fine.

Measured on production 2026-09-08: rule 29 ("示例事件通知规则",
match_event_type ``incident_created,incident_resolved``) had 44 sent outbox rows
and zero failures, next to a zombie badge.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import DecisionTrace, ForwardOutbox, ForwardRule
from services.forwarding.rules import get_forward_rule_roi, get_system_event_delivery_counts


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


def _rule(rule_id: int, name: str, event_types: str = "", **extra: Any) -> ForwardRule:
    return ForwardRule(
        id=rule_id,
        name=name,
        enabled=extra.get("enabled", True),
        priority=extra.get("priority", 0),
        match_event_type=event_types,
        target_type=extra.get("target_type", "feishu"),
        target_name=extra.get("target_name", "ops-group"),
    )


def _outbox(rule_id: int, event_type: str, *, status: str = "sent", age: timedelta | None = None) -> ForwardOutbox:
    created = utcnow() - age if age else utcnow()
    return ForwardOutbox(
        idempotency_key=f"k-{rule_id}-{event_type}-{created.isoformat()}-{status}",
        forward_rule_id=rule_id,
        rule_name=f"rule-{rule_id}",
        target_type="feishu",
        target_name="ops-group",
        event_type=event_type,
        status=status,
        created_at=created,
    )


def _trace(event_id: int, matched: list[str]) -> DecisionTrace:
    return DecisionTrace(
        webhook_event_id=event_id,
        outcome="forwarded",
        skip_code="none",
        source="grafana",
        matched_rules=matched,
    )


@pytest.mark.asyncio
async def test_system_event_rule_counts_its_deliveries_not_its_empty_traces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        session.add(_rule(29, "示例事件通知规则", "incident_created,incident_resolved"))
    async with session_factory.begin() as session:
        session.add_all(
            [
                _outbox(29, "incident_created"),
                _outbox(29, "incident_created"),
                _outbox(29, "incident_resolved"),
            ]
        )

    async with session_factory() as session:
        rules = [_rule(29, "示例事件通知规则", "incident_created,incident_resolved")]
        roi = await get_forward_rule_roi(session, rules)

    # The number the badge shows, and the noun it may use for it.
    assert roi["示例事件通知规则"]["count"] == 3
    assert roi["示例事件通知规则"]["hit_count_source"] == "system_event_delivery"
    assert roi["示例事件通知规则"]["last_matched_at"] is not None


@pytest.mark.asyncio
async def test_alert_rule_still_counts_decision_traces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An unconstrained rule matches alerts too, so its traces are the truth and
    # the outbox must not be consulted — a rule naming NO event type is not a
    # system-event rule, however many system cards happen to pass through it.
    async with session_factory.begin() as session:
        session.add_all([_trace(801, ["feishu"]), _trace(802, ["feishu"])])
        session.add_all([_outbox(7, "incident_created"), _outbox(7, "incident_created")])

    async with session_factory() as session:
        roi = await get_forward_rule_roi(session, [_rule(7, "feishu")])

    assert roi["feishu"]["count"] == 2
    assert roi["feishu"]["hit_count_source"] == "decision_trace"


@pytest.mark.asyncio
async def test_mixed_event_rule_reads_as_an_alert_rule(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # incident_created is a system event, webhook_forward is not: the rule can
    # carry an alert, so its traces remain the honest count.
    async with session_factory.begin() as session:
        session.add_all([_outbox(8, "incident_created"), _outbox(8, "incident_created")])

    async with session_factory() as session:
        roi = await get_forward_rule_roi(session, [_rule(8, "mixed", "incident_created,webhook_forward")])

    assert roi["mixed"]["hit_count_source"] == "decision_trace"
    assert roi["mixed"]["count"] == 0


@pytest.mark.asyncio
async def test_every_rule_gets_an_entry_including_the_quiet_ones(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # This is the view that answers "which enabled rule has gone quiet". A quiet
    # rule used to be absent from the answer rather than present with a 0, which
    # the MCP tool's own description already promised it would be.
    async with session_factory() as session:
        roi = await get_forward_rule_roi(
            session,
            [_rule(9, "quiet-alerts"), _rule(10, "quiet-system", "incident_created")],
        )

    assert roi["quiet-alerts"] == {"count": 0, "last_matched_at": None, "hit_count_source": "decision_trace"}
    assert roi["quiet-system"] == {"count": 0, "last_matched_at": None, "hit_count_source": "system_event_delivery"}


@pytest.mark.asyncio
async def test_delivery_counts_are_windowed_and_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The window keeps the badge's "(90d)" honest, and a non-system event type on
    # a system-event rule is not counted (it cannot legitimately be there, and
    # counting it would make the number mean two things).
    async with session_factory.begin() as session:
        session.add_all(
            [
                _outbox(11, "incident_created"),
                _outbox(11, "incident_created", age=timedelta(days=200)),
                _outbox(11, "webhook_forward"),
            ]
        )

    rules = [_rule(11, "system-only", "incident_created,incident_resolved")]
    async with session_factory() as session:
        counts = await get_system_event_delivery_counts(session, rules)
        unbounded = await get_system_event_delivery_counts(session, rules, window=None)

    assert counts["system-only"]["count"] == 1
    assert unbounded["system-only"]["count"] == 2


@pytest.mark.asyncio
async def test_delivery_counts_ignore_rules_that_are_not_system_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        session.add(_outbox(12, "incident_created"))

    async with session_factory() as session:
        assert await get_system_event_delivery_counts(session, [_rule(12, "plain")]) == {}


@pytest.mark.asyncio
async def test_a_queued_delivery_counts_before_it_is_sent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The count is the analogue of a "forwarded" decision trace: the rule matched
    # and produced a delivery. Whether that delivery then succeeded is what the
    # delivery-health badge beside it answers, so a pending or exhausted row
    # still counts as a hit here.
    async with session_factory.begin() as session:
        session.add_all(
            [
                _outbox(13, "incident_created", status="pending"),
                _outbox(13, "incident_created", status="exhausted"),
                _outbox(13, "incident_created", status="sent"),
            ]
        )

    async with session_factory() as session:
        counts = await get_system_event_delivery_counts(session, [_rule(13, "sys", "incident_created")])

    assert counts["sys"]["count"] == 3
