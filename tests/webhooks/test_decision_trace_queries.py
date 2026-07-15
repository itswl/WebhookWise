from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import DecisionTrace, ForwardOutbox
from services.webhooks.decision_trace_queries import (
    get_decision_trace_for_event,
    get_decision_trace_quality_stats,
    get_decision_trace_stats,
    get_forward_rule_hit_counts,
    get_overview_stats,
    get_silence_suppression_counts,
    list_ai_rule_disagreements,
    list_decision_traces,
)


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


def _outbox(event_id: int, status: str, **extra: Any) -> ForwardOutbox:
    return ForwardOutbox(
        idempotency_key=extra.get("idempotency_key", f"k-{event_id}-{status}-{extra.get('target_name', 'x')}"),
        webhook_event_id=event_id,
        original_event_id=extra.get("original_event_id"),
        target_type=extra.get("target_type", "feishu"),
        target_name=extra.get("target_name", "ops-group"),
        status=status,
        attempts=extra.get("attempts", 1),
        last_error=extra.get("last_error"),
    )


def _trace(event_id: int, outcome: str, skip_code: str, **extra: Any) -> DecisionTrace:
    return DecisionTrace(
        webhook_event_id=event_id,
        outcome=outcome,
        skip_code=skip_code,
        source=extra.get("source", "volcengine"),
        importance=extra.get("importance", "medium"),
        is_periodic_reminder=extra.get("is_periodic_reminder", False),
        route=extra.get("route", "ai"),
        importance_override=extra.get("importance_override", False),
        degraded_reason=extra.get("degraded_reason"),
        silence_id=extra.get("silence_id"),
        matched_rules=extra.get("matched_rules", []),
        steps=extra.get("steps", [{"step": "forward", "outcome": outcome, "skip_code": skip_code}]),
    )


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        session.add_all(
            [
                _trace(1, "forwarded", "none", matched_rules=["feishu"]),
                _trace(2, "skipped", "silenced"),
                _trace(3, "skipped", "silenced"),
                _trace(4, "skipped", "cooldown"),
            ]
        )


@pytest.mark.asyncio
async def test_stats_aggregates_outcome_and_skip_code(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        stats = await get_decision_trace_stats(session, "day")

    assert stats["total"] == 4
    assert stats["forwarded"] == 1
    assert stats["skipped"] == 3
    assert stats["outcome_breakdown"] == {"forwarded": 1, "skipped": 3}
    # Skip distribution is over skipped traces only (forwarded's "none" excluded).
    assert stats["skip_code_breakdown"] == {"silenced": 2, "cooldown": 1}
    assert "none" not in stats["skip_code_breakdown"]


@pytest.mark.asyncio
async def test_list_filters_by_skip_code_and_carries_steps(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        items, has_more, next_cursor = await list_decision_traces(session, skip_code="silenced")

    assert {item["webhook_event_id"] for item in items} == {2, 3}
    assert all(item["skip_code"] == "silenced" for item in items)
    assert has_more is False
    assert next_cursor is None
    # The full chain ships inline with each row.
    assert items[0]["steps"]


@pytest.mark.asyncio
async def test_list_filters_by_outcome(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        items, _, _ = await list_decision_traces(session, outcome="forwarded")
    assert len(items) == 1
    assert items[0]["webhook_event_id"] == 1
    assert items[0]["matched_rules"] == ["feishu"]


@pytest.mark.asyncio
async def test_list_paginates_with_cursor(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        first, has_more, next_cursor = await list_decision_traces(session, page_size=2)
        assert has_more is True
        assert next_cursor is not None
        # Newest first: ids 4, 3 on the first page.
        assert [item["webhook_event_id"] for item in first] == [4, 3]

        second, has_more2, _ = await list_decision_traces(session, page_size=2, cursor=next_cursor)
        assert [item["webhook_event_id"] for item in second] == [2, 1]
        assert has_more2 is False


@pytest.mark.asyncio
async def test_get_for_event_returns_latest_or_none(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        found = await get_decision_trace_for_event(session, 2)
        assert found is not None
        assert found["webhook_event_id"] == 2
        assert found["outcome"] == "skipped"

        missing = await get_decision_trace_for_event(session, 999)
        assert missing is None


@pytest.mark.asyncio
async def test_get_for_event_attaches_delivery(session_factory: async_sessionmaker[AsyncSession]) -> None:
    # The per-alert (by-event) view must carry delivery status too, like the list.
    async with session_factory.begin() as session:
        session.add(_trace(70, "forwarded", "none"))
        session.add(_outbox(70, "sent", target_name="feishu"))
    async with session_factory() as session:
        found = await get_decision_trace_for_event(session, 70)
    assert found is not None
    assert found["delivery"]["state"] == "sent"
    assert found["delivery"]["targets"][0]["target_name"] == "feishu"


async def _seed_quality(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        session.add_all(
            [
                # 3 fresh AI judgments; one was overridden by a rule.
                _trace(1, "forwarded", "none", route="ai", importance="high", importance_override=True),
                _trace(2, "forwarded", "none", route="ai", importance="medium"),
                _trace(3, "skipped", "no_match", route="ai", importance="low", source="grafana"),
                # A reuse and a degradation — excluded from AI-only signals.
                _trace(4, "forwarded", "none", route="redis_reuse", importance="high"),
                _trace(5, "skipped", "no_match", route="rule", importance="medium", degraded_reason="ai_error: boom"),
            ]
        )


@pytest.mark.asyncio
async def test_quality_stats_proxy_signals(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed_quality(session_factory)
    async with session_factory() as session:
        q = await get_decision_trace_quality_stats(session, "day")

    assert q["total"] == 5
    assert q["ai_total"] == 3
    assert q["route_breakdown"] == {"ai": 3, "redis_reuse": 1, "rule": 1}
    # Override rate is over fresh AI judgments only: 1 of 3.
    assert q["override_count"] == 1
    assert q["override_rate"] == round(1 / 3 * 100, 1)
    # Degradation: 1 of 5 total, reason captured.
    assert q["degraded_total"] == 1
    assert q["degraded_reasons"] == {"ai_error: boom": 1}
    # Importance distribution is over ai-route only (the redis_reuse high + rule medium excluded).
    assert q["ai_importance_breakdown"] == {"high": 1, "medium": 1, "low": 1}
    # Per-source only counts ai-route rows.
    assert q["ai_importance_by_source"].get("grafana") == {"low": 1}


@pytest.mark.asyncio
async def test_list_attaches_delivery_status(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as session:
        session.add_all(
            [
                _trace(10, "forwarded", "none"),  # delivered
                _trace(11, "forwarded", "none"),  # failed
                _trace(12, "forwarded", "none"),  # no outbox row (e.g. pre-feature)
                _trace(13, "skipped", "silenced"),  # skipped → never gets a delivery badge
            ]
        )
        session.add_all(
            [
                _outbox(10, "sent", target_name="ops-feishu"),
                _outbox(11, "exhausted", target_name="ops-feishu", last_error="HTTP 500 from Feishu"),
            ]
        )

    async with session_factory() as session:
        items, _, _ = await list_decision_traces(session)

    by_event = {it["webhook_event_id"]: it for it in items}
    assert by_event[10]["delivery"]["state"] == "sent"
    assert by_event[10]["delivery"]["target_name"] == "ops-feishu"
    assert by_event[11]["delivery"]["state"] == "failed"
    assert by_event[11]["delivery"]["last_error"] == "HTTP 500 from Feishu"
    # No outbox row → no delivery key; skipped rows never get one.
    assert "delivery" not in by_event[12]
    assert "delivery" not in by_event[13]


@pytest.mark.asyncio
async def test_delivery_multi_target_precedence_failed_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An event forwarded to two targets: one sent, one exhausted → failed wins
    # (the operator most needs to see the failure).
    async with session_factory.begin() as session:
        session.add(_trace(20, "forwarded", "none"))
        session.add_all(
            [
                _outbox(20, "sent", target_name="t1", idempotency_key="k20a"),
                _outbox(20, "exhausted", target_name="t2", last_error="boom", idempotency_key="k20b"),
            ]
        )
    async with session_factory() as session:
        items, _, _ = await list_decision_traces(session)
    d = next(it for it in items if it["webhook_event_id"] == 20)["delivery"]
    assert d["state"] == "failed"
    assert d["target_count"] == 2
    assert d["last_error"] == "boom"
    # Full per-target detail is included for the expanded view.
    assert len(d["targets"]) == 2
    failed_tgt = next(tg for tg in d["targets"] if tg["status"] == "exhausted")
    assert failed_tgt["target_name"] == "t2"
    assert failed_tgt["last_error"] == "boom"
    assert failed_tgt["retryable"] is True  # exhausted → can re-enqueue
    sent_tgt = next(tg for tg in d["targets"] if tg["status"] == "sent")
    assert sent_tgt["retryable"] is False
    assert "max_attempts" in sent_tgt and "outbox_id" in sent_tgt


@pytest.mark.asyncio
async def test_delivery_does_not_absorb_dedup_chain_descendants(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Regression: in a dedup chain, a later duplicate's forward carries the chain
    # HEAD as its original_event_id. The head's delivery must show only ITS OWN
    # outbox row, not also the duplicate's — else it falsely reads "delivered 2x".
    async with session_factory.begin() as session:
        session.add_all([_trace(100, "forwarded", "none"), _trace(101, "forwarded", "none")])
        session.add_all(
            [
                _outbox(100, "sent", target_name="feishu", idempotency_key="own-100"),
                # event 101 is a duplicate of 100; its forward points back to 100.
                _outbox(101, "sent", target_name="feishu", original_event_id=100, idempotency_key="own-101"),
            ]
        )
    async with session_factory() as session:
        items, _, _ = await list_decision_traces(session)
    by_event = {it["webhook_event_id"]: it for it in items}
    # Each occurrence shows exactly its own single delivery — not the chain's.
    assert by_event[100]["delivery"]["target_count"] == 1
    assert by_event[100]["delivery"]["targets"][0]["outbox_id"] is not None
    assert by_event[101]["delivery"]["target_count"] == 1


@pytest.mark.asyncio
async def test_list_delivery_failed_filter(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as session:
        session.add_all(
            [
                _trace(30, "forwarded", "none"),  # delivered OK
                _trace(31, "forwarded", "none"),  # exhausted → failed
                _trace(32, "skipped", "silenced"),  # skipped (no delivery)
            ]
        )
        session.add_all(
            [
                _outbox(30, "sent", idempotency_key="k30"),
                _outbox(31, "exhausted", last_error="boom", idempotency_key="k31"),
            ]
        )
    async with session_factory() as session:
        items, _, _ = await list_decision_traces(session, delivery="failed")
    # Only the forwarded-and-failed row comes back.
    assert [it["webhook_event_id"] for it in items] == [31]
    assert items[0]["delivery"]["state"] == "failed"


@pytest.mark.asyncio
async def test_silence_suppression_counts_group_by_rule(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Silence rule 5 suppressed two alerts, rule 6 one; a non-silenced skip and a
    # forward carry no silence_id and must not be counted.
    async with session_factory.begin() as session:
        session.add_all(
            [
                _trace(40, "skipped", "silenced", silence_id=5),
                _trace(41, "skipped", "silenced", silence_id=5),
                _trace(42, "skipped", "silenced", silence_id=6),
                _trace(43, "skipped", "cooldown"),  # not a silence
                _trace(44, "forwarded", "none"),  # not skipped
            ]
        )
    async with session_factory() as session:
        counts = await get_silence_suppression_counts(session)

    assert counts[5]["count"] == 2
    assert counts[6]["count"] == 1
    assert counts[5]["last_suppressed_at"] is not None
    # Only the two silence rules appear — nothing for the cooldown/forward rows.
    assert set(counts.keys()) == {5, 6}


@pytest.mark.asyncio
async def test_forward_rule_hit_counts_group_by_rule_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # feishu matched 3 alerts, openclaw 1 (one forward matched BOTH rules, since
    # stop_on_match=false lets several fire). Skipped rows carry no matched_rules
    # and must not count.
    async with session_factory.begin() as session:
        session.add_all(
            [
                _trace(60, "forwarded", "none", matched_rules=["feishu"]),
                _trace(61, "forwarded", "none", matched_rules=["feishu", "openclaw"]),
                _trace(62, "forwarded", "none", matched_rules=["feishu"]),
                _trace(63, "skipped", "no_match", matched_rules=[]),  # nothing matched
                _trace(64, "skipped", "silenced", silence_id=5),  # skipped, not forwarded
            ]
        )
    async with session_factory() as session:
        hits = await get_forward_rule_hit_counts(session)

    assert hits["feishu"]["count"] == 3
    assert hits["openclaw"]["count"] == 1
    assert hits["feishu"]["last_matched_at"] is not None
    # Only the two rules that actually matched appear.
    assert set(hits.keys()) == {"feishu", "openclaw"}


@pytest.mark.asyncio
async def test_forward_rule_hit_counts_scoped_to_requested_names(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Scoped to the currently-shown rules: a since-renamed/deleted rule whose old
    # name still lingers in traces is skipped.
    async with session_factory.begin() as session:
        session.add_all(
            [
                _trace(70, "forwarded", "none", matched_rules=["feishu"]),
                _trace(71, "forwarded", "none", matched_rules=["old-deleted-rule"]),
            ]
        )
    async with session_factory() as session:
        hits = await get_forward_rule_hit_counts(session, rule_names=["feishu"])

    assert set(hits.keys()) == {"feishu"}
    assert hits["feishu"]["count"] == 1


@pytest.mark.asyncio
async def test_silence_suppression_counts_scoped_to_requested_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # When scoped to a subset, only those rules are aggregated (a since-deleted
    # silence whose old id still lingers in traces is skipped).
    async with session_factory.begin() as session:
        session.add_all(
            [
                _trace(50, "skipped", "silenced", silence_id=5),
                _trace(51, "skipped", "silenced", silence_id=99),  # deleted rule
            ]
        )
    async with session_factory() as session:
        counts = await get_silence_suppression_counts(session, silence_ids=[5])

    assert set(counts.keys()) == {5}
    assert counts[5]["count"] == 1


@pytest.mark.asyncio
async def test_overview_stats_composes_volume_delivery_sources(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        session.add_all(
            [
                _trace(80, "forwarded", "none", source="volcengine"),
                _trace(81, "forwarded", "none", source="volcengine"),
                _trace(82, "skipped", "silenced", source="aliyun"),
            ]
        )
        session.add_all(
            [
                _outbox(80, "sent", idempotency_key="o80"),
                _outbox(81, "exhausted", idempotency_key="o81"),
            ]
        )
    async with session_factory() as session:
        ov = await get_overview_stats(session, "day")

    assert ov["total"] == 3
    assert ov["forwarded"] == 2
    assert ov["skipped"] == 1
    assert ov["forward_rate"] == round(2 / 3 * 100, 1)
    assert ov["skip_code_breakdown"] == {"silenced": 1}
    # Top sources by volume.
    top = {s["source"]: s["count"] for s in ov["top_sources"]}
    assert top == {"volcengine": 2, "aliyun": 1}
    # Delivery: 1 sent / 1 exhausted → 50% success.
    assert ov["delivery"]["delivered"] == 1
    assert ov["delivery"]["failed"] == 1
    assert ov["delivery"]["success_rate"] == 50.0


@pytest.mark.asyncio
async def test_list_ai_rule_disagreements_returns_only_overridden_ai_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        session.add_all(
            [
                # A fresh AI judgment a rule overrode → belongs in the queue.
                _trace(1, "forwarded", "none", route="ai", importance_override=True, source="volcengine"),
                # AI judgment with no override → excluded.
                _trace(2, "forwarded", "none", route="ai", importance_override=False),
                # Override but not a fresh AI route (rule-routed) → excluded.
                _trace(3, "forwarded", "none", route="rule_routed", importance_override=True),
            ]
        )
    async with session_factory() as session:
        result = await list_ai_rule_disagreements(session, period="week")

    assert result["count"] == 1
    assert result["items"][0]["webhook_event_id"] == 1
    assert result["items"][0]["source"] == "volcengine"
    assert result["truncated"] is False
