"""Alert-timeline assembly: the three edges it walks, and what it refuses to do.

This module had no tests of its own — its coverage was incidental, which is
how the noise-graph and dedup-chain walks (the whole reason it exists) went
unexercised.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import WebhookEvent
from services.webhooks.timeline import build_alert_timeline


async def _add(session: AsyncSession, **kwargs: object) -> WebhookEvent:
    defaults: dict[str, object] = {
        "source": "grafana",
        "importance": "high",
        "is_duplicate": False,
        "timestamp": utcnow(),
    }
    defaults.update(kwargs)
    event = WebhookEvent(**defaults)  # type: ignore[arg-type]
    session.add(event)
    await session.flush()
    return event


@pytest.mark.asyncio
async def test_missing_event_yields_an_empty_timeline(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        assert await build_alert_timeline(session, 999999) == {"anchor": None, "events": []}


@pytest.mark.asyncio
async def test_noise_graph_edges_pull_in_root_cause_and_related(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The anchor's noise_reduction block names a root cause and siblings; all
    of them belong on the timeline, ordered oldest first."""
    async with db_session_factory.begin() as session:
        now = utcnow()
        root = await _add(session, timestamp=now - timedelta(minutes=10))
        sibling = await _add(session, timestamp=now - timedelta(minutes=5))
        anchor = await _add(
            session,
            timestamp=now,
            ai_analysis={
                "summary": "derived alert",
                "noise_reduction": {
                    "root_cause_event_id": int(root.id),
                    "related_alert_ids": [int(sibling.id), 0, "bogus"],
                },
            },
        )

        result = await build_alert_timeline(session, int(anchor.id))

        ids = [row["id"] for row in result["events"]]
        assert ids == [int(root.id), int(sibling.id), int(anchor.id)]
        assert result["anchor"]["id"] == int(anchor.id)
        # The projection carries causal context and never raw payload/headers.
        assert result["anchor"]["summary"] == "derived alert"
        assert result["anchor"]["noise_root_cause_id"] == int(root.id)
        assert "raw_payload" not in result["anchor"]
        assert "headers" not in result["anchor"]


@pytest.mark.asyncio
async def test_dedup_chain_is_walked_both_directions(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A repeat alert sits mid-chain: the timeline reaches its predecessor
    (prev_alert_id) and its successors (duplicate_of / prev_alert_id pointing
    back at it)."""
    async with db_session_factory.begin() as session:
        now = utcnow()
        first = await _add(session, timestamp=now - timedelta(minutes=6))
        middle = await _add(
            session, timestamp=now - timedelta(minutes=3), prev_alert_id=int(first.id), is_duplicate=True
        )
        later = await _add(
            session, timestamp=now, duplicate_of=int(middle.id), is_duplicate=True, prev_alert_id=int(middle.id)
        )

        result = await build_alert_timeline(session, int(middle.id))

        ids = [row["id"] for row in result["events"]]
        assert int(first.id) in ids, "backwards edge (prev_alert_id) not walked"
        assert int(later.id) in ids, "forwards edge (duplicate_of) not walked"
        assert ids == sorted(ids, key=lambda i: {int(first.id): 0, int(middle.id): 1, int(later.id): 2}[i])


@pytest.mark.asyncio
async def test_isolated_event_returns_only_itself(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        lonely = await _add(session, ai_analysis={"summary": "no relations"})

        result = await build_alert_timeline(session, int(lonely.id))

        assert [row["id"] for row in result["events"]] == [int(lonely.id)]
        assert result["anchor"]["noise_root_cause_id"] is None


@pytest.mark.asyncio
async def test_malformed_analysis_shapes_do_not_break_the_walk(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ai_analysis is operator/LLM-shaped data: a non-dict noise block, or a
    non-dict analysis entirely, must degrade rather than raise."""
    async with db_session_factory.begin() as session:
        weird = await _add(session, ai_analysis={"noise_reduction": "not-a-dict"})
        result = await build_alert_timeline(session, int(weird.id))
        assert [row["id"] for row in result["events"]] == [int(weird.id)]


@pytest.mark.asyncio
async def test_rule_audit_pure_noise_flag_reads_real_trace_outcomes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The pure_noise flag is only trustworthy if forward counts come from the
    decision traces. That join (_trace_forward_counts) was uncovered, so a rule
    that fires constantly and forwards nothing looked identical to one that
    forwards every time.
    """
    from models import DecisionTrace
    from services.webhooks.rule_audit import get_rule_audit

    async with db_session_factory.begin() as session:
        now = utcnow()
        noisy_name = "always-suppressed"
        useful_name = "always-forwarded"
        for index in range(4):
            noisy = await _add(
                session,
                timestamp=now - timedelta(hours=index),
                parsed_data={"_alert_identity": {"name": noisy_name}, "RuleName": noisy_name},
            )
            useful = await _add(
                session,
                timestamp=now - timedelta(hours=index),
                parsed_data={"_alert_identity": {"name": useful_name}, "RuleName": useful_name},
            )
            session.add_all(
                [
                    DecisionTrace(
                        webhook_event_id=int(noisy.id),
                        outcome="skipped",
                        skip_code="silenced",
                        source="grafana",
                        alert_name=noisy_name,
                        matched_rules=[noisy_name],
                        steps=[],
                    ),
                    DecisionTrace(
                        webhook_event_id=int(useful.id),
                        outcome="forwarded",
                        skip_code="none",
                        source="grafana",
                        alert_name=useful_name,
                        matched_rules=[useful_name],
                        steps=[],
                    ),
                ]
            )
        await session.flush()

        rows = await get_rule_audit(session, window_days=30, min_events=2)
        by_rule = {row["rule_name"]: row for row in rows}

        assert noisy_name in by_rule and useful_name in by_rule
        assert "pure_noise" in by_rule[noisy_name]["flags"], "a rule that never forwards must be flagged"
        assert "pure_noise" not in by_rule[useful_name]["flags"], "a forwarding rule must not be flagged"
        assert by_rule[useful_name]["forwarded"] == 4
        assert by_rule[noisy_name]["forwarded"] == 0
