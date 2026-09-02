"""Digest delivery through the outbox: window alignment, grouping, one send per group.

The per-alert outbox row is kept — it IS the record — but it carries its group
and waits for the window; the first row claimed delivers for its due siblings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from services.forwarding import outbox_records
from services.forwarding.outbox_records import (
    deferred_digest_kicks,
    digest_key_for,
    digest_window_start,
    digest_window_start_from_key,
)
from services.forwarding.policies import ForwardDeliveryPolicy
from services.forwarding.types import ForwardRuleSnapshot
from services.webhooks.types import ANALYSIS_DIGEST, ForwardOutboxStatus

FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/demo-token"
DINGTALK_URL = "https://oapi.dingtalk.com/robot/send?access_token=demo"
WEBHOOK_URL = "https://example.test/hook"
DEPOSIT = "示例充值超限告警"


@pytest.fixture
def session_factory(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return db_app_context_session_factory


def _policy() -> ForwardDeliveryPolicy:
    return ForwardDeliveryPolicy(
        timeout_seconds=10,
        max_attempts=3,
        retry_initial_delay=30,
        retry_max_delay=300,
        retry_backoff_multiplier=2.0,
        stale_processing_threshold_seconds=60,
        max_delivery_age_seconds=0,
    )


def _snapshot(
    rule_id: int, *, target_type: str = "webhook", target_url: str = "", name: str = ""
) -> ForwardRuleSnapshot:
    return ForwardRuleSnapshot(
        id=rule_id,
        name=name or f"rule-{rule_id}",
        match_event_type="",
        match_importance="",
        match_source="",
        match_duplicate="all",
        match_payload="",
        target_type=target_type,
        target_url=target_url,
        stop_on_match=False,
    )


def _digested_analysis(importance: str = "medium") -> dict[str, Any]:
    # Route "ai", not "rule_excluded": the exclusion would (correctly) drop a
    # deep-analysis target on its own, and these tests are about the digest.
    return {
        "importance": importance,
        "summary": "当前值 920.00 超过阈值 500",
        "_route_type": "ai",
        ANALYSIS_DIGEST: {"window_minutes": 60, "rule": "digest: deposits hourly"},
    }


# ── window alignment and the key ─────────────────────────────────────────────


def test_the_window_is_floored_from_the_alert_time_aligned_to_utc_midnight() -> None:
    at = datetime(2026, 9, 2, 10, 37, 12)

    assert digest_window_start(at, 60) == datetime(2026, 9, 2, 10, 0)
    assert digest_window_start(at, 1440) == datetime(2026, 9, 2, 0, 0)
    # 90-minute slots start at 00:00 UTC: 09:00, 10:30, 12:00 …
    assert digest_window_start(at, 90) == datetime(2026, 9, 2, 10, 30)
    assert digest_window_start(datetime(2026, 9, 2, 10, 0), 60) == datetime(2026, 9, 2, 10, 0)


def test_two_alerts_in_one_window_share_a_key_and_the_key_names_its_window() -> None:
    first = digest_key_for(
        forward_rule_id=7, target_type="webhook", window_start=digest_window_start(datetime(2026, 9, 2, 10, 3), 60)
    )
    second = digest_key_for(
        forward_rule_id=7, target_type="webhook", window_start=digest_window_start(datetime(2026, 9, 2, 10, 58), 60)
    )
    later = digest_key_for(
        forward_rule_id=7, target_type="webhook", window_start=digest_window_start(datetime(2026, 9, 2, 11, 0), 60)
    )
    other_rule = digest_key_for(
        forward_rule_id=8, target_type="webhook", window_start=digest_window_start(datetime(2026, 9, 2, 10, 3), 60)
    )

    assert first == second == "7:webhook:2026-09-02T10:00"
    assert later != first
    assert other_rule != first
    assert digest_window_start_from_key(first) == datetime(2026, 9, 2, 10, 0)
    assert digest_window_start_from_key("garbage") is None
    assert digest_window_start_from_key(None) is None


# ── record creation ──────────────────────────────────────────────────────────


async def _create(
    session_factory: async_sessionmaker[AsyncSession],
    rules: list[ForwardRuleSnapshot],
    *,
    analysis: dict[str, Any],
    is_periodic_reminder: bool = False,
    timestamp: str = "2026-09-02T02:37:12Z",
    webhook_id: int = 1,
) -> list[int]:
    async with session_factory.begin() as session:
        return await outbox_records.create_outbox_records(
            session,
            rules,
            webhook_id=webhook_id,
            orig_id=None,
            forward_data={"source": "grafana", "timestamp": timestamp, "parsed_data": {"RuleName": DEPOSIT}},
            analysis_result=analysis,  # type: ignore[arg-type]
            formatted_payload=None,
            event_type="webhook_forward",
            is_periodic_reminder=is_periodic_reminder,
            policy=_policy(),
            log_tag="test",
        )


@pytest.mark.asyncio
async def test_chat_records_wait_for_the_window_while_machine_targets_stay_immediate(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from models import ForwardOutbox

    monkeypatch.setattr(outbox_records, "inbound_actions_for", AsyncMock(return_value=set()))
    rules = [
        _snapshot(1, target_url=FEISHU_URL, name="chat"),
        _snapshot(2, target_url=DINGTALK_URL, name="dingtalk"),
        _snapshot(3, target_type="deep_analysis", name="investigator"),
        _snapshot(4, target_url=WEBHOOK_URL, name="pipeline"),
        _snapshot(5, target_type="feishu_app", target_url="feishu-app://oc_demo", name="app"),
    ]
    before = utcnow()

    ids = await _create(session_factory, rules, analysis=_digested_analysis())

    async with session_factory() as session:
        rows = {row.rule_name: row for row in (await session.execute(select(ForwardOutbox))).scalars().all()}
    assert len(ids) == 5 and set(rows) == {"chat", "dingtalk", "investigator", "pipeline", "app"}

    window_end = datetime(2026, 9, 2, 3, 0)
    for name, rule_id in (("chat", 1), ("dingtalk", 2), ("app", 5)):
        row = rows[name]
        assert row.digest_key == f"{rule_id}:{row.target_type}:2026-09-02T02:00", name
        assert row.digest_window_end == window_end
        assert row.next_attempt_at == window_end
        assert row.status == ForwardOutboxStatus.PENDING
    for name in ("investigator", "pipeline"):
        assert rows[name].digest_key is None, name
        assert rows[name].digest_window_end is None
        assert rows[name].next_attempt_at is not None and rows[name].next_attempt_at <= utcnow()
        assert rows[name].next_attempt_at >= before - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_an_undigested_alert_is_filed_exactly_as_before(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import ForwardOutbox

    ids = await _create(
        session_factory, [_snapshot(1, target_url=FEISHU_URL)], analysis={"importance": "high", "summary": "s"}
    )

    async with session_factory() as session:
        row = await session.get(ForwardOutbox, ids[0])
    assert row is not None and row.digest_key is None and row.digest_window_end is None
    assert row.next_attempt_at is not None and row.next_attempt_at <= utcnow()


@pytest.mark.asyncio
async def test_periodic_reminders_are_not_raised_for_a_digested_rule(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The digest already says "still firing" every window; a reminder card
    would repeat it at exactly the cadence the rule removed. The webhook copy
    is not a chat and keeps its reminder."""
    from models import ForwardOutbox

    ids = await _create(
        session_factory,
        [_snapshot(1, target_url=FEISHU_URL, name="chat"), _snapshot(2, target_url=WEBHOOK_URL, name="pipeline")],
        analysis=_digested_analysis(),
        is_periodic_reminder=True,
    )

    async with session_factory() as session:
        rows = (await session.execute(select(ForwardOutbox))).scalars().all()
    assert len(ids) == 1 and [row.rule_name for row in rows] == ["pipeline"]
    assert rows[0].is_periodic_reminder is True and rows[0].digest_key is None


@pytest.mark.asyncio
async def test_only_the_row_that_opens_a_group_earns_a_delayed_kick(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One kick per group is enough: the row it wakes claims every due sibling.
    Every deferred row is still reported, so none is kicked immediately."""
    first = await _create(session_factory, [_snapshot(1, target_url=FEISHU_URL)], analysis=_digested_analysis())
    # A second alert of the same rule, later in the same window, reaching the
    # chat rule AND a webhook rule.
    second = await _create(
        session_factory,
        [_snapshot(1, target_url=FEISHU_URL), _snapshot(2, target_url=WEBHOOK_URL)],
        analysis=_digested_analysis(),
        timestamp="2026-09-02T02:50:00Z",
        webhook_id=2,
    )
    assert len(first) == 1 and len(second) == 2
    chat_row, webhook_row = second

    now = datetime(2026, 9, 2, 2, 55)
    async with session_factory() as session:
        opener = await deferred_digest_kicks(session, first, now=now)
        follower = await deferred_digest_kicks(session, second, now=now)
        after_close = await deferred_digest_kicks(session, first, now=datetime(2026, 9, 2, 3, 0))

    assert opener.ids == frozenset(first)
    assert opener.kicks == ((first[0], datetime(2026, 9, 2, 3, 0)),)
    # The second alert's chat row is deferred but does not open the group, so
    # it earns no kick of its own; its webhook row is not deferred at all.
    assert follower.ids == frozenset({chat_row})
    assert webhook_row not in follower.ids
    assert follower.kicks == ()
    # A row already due needs no delayed kick — the scan or an immediate kick takes it.
    assert after_close.ids == frozenset() and after_close.kicks == ()


def test_a_deferred_kick_lands_after_the_row_is_due_never_before() -> None:
    from services.webhooks.pipeline_stages import _seconds_until

    assert _seconds_until(utcnow() + timedelta(seconds=30)) in (31, 32)
    assert _seconds_until(utcnow() - timedelta(seconds=30)) == 1


# ── delivery: one send per group ─────────────────────────────────────────────


async def _insert_group(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int,
    target_url: str = FEISHU_URL,
    key: str = "7:webhook:2026-09-02T02:00",
    due: datetime | None = None,
) -> list[int]:
    from models import ForwardOutbox

    now = utcnow()
    due_at = due or (now - timedelta(minutes=1))
    ids: list[int] = []
    async with session_factory.begin() as session:
        for index in range(count):
            record = ForwardOutbox(
                idempotency_key=f"forward:{index}:{now.timestamp()}",
                webhook_event_id=100 + index,
                forward_rule_id=7,
                rule_name="chat",
                target_type="webhook",
                target_url=target_url,
                channel_name="webhook",
                event_type="webhook_forward",
                status=ForwardOutboxStatus.PENDING,
                attempts=0,
                max_attempts=3,
                next_attempt_at=due_at,
                digest_key=key,
                digest_window_end=datetime(2026, 9, 2, 3, 0),
                forward_data={
                    "source": "grafana",
                    "timestamp": f"2026-09-02T02:{10 + index:02d}:00Z",
                    "parsed_data": {"RuleName": DEPOSIT, "status": "firing"},
                },
                analysis_result={
                    "importance": "medium",
                    "summary": f"第 {index + 1} 条",
                    ANALYSIS_DIGEST: {"window_minutes": 60, "rule": "d"},
                },
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            await session.flush()
            ids.append(int(record.id))
    return ids


@pytest.mark.asyncio
async def test_three_siblings_become_one_card_and_all_end_sent(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from models import ForwardOutbox
    from services.forwarding.outbox import process_forward_outbox_by_id
    from services.notifications import feishu

    sends: list[dict[str, Any]] = []

    async def fake_send(url: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        sends.append(payload)
        return {"status": "success", "status_code": 200}

    monkeypatch.setattr(feishu, "send_to_feishu", fake_send)
    ids = await _insert_group(session_factory, count=3)

    await process_forward_outbox_by_id(ids[1])  # any row of the group may be the one kicked

    assert len(sends) == 1, "the channel is called ONCE for the group"
    card = sends[0]
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == f"📦 汇总通知 · {DEPOSIT}"
    body = "\n".join(
        str(element.get("text", {}).get("content", ""))
        for element in card["card"]["elements"]
        if element.get("tag") == "div"
    )
    assert "共 3 条" in body
    assert "第 1 条" in body and "第 2 条" in body and "第 3 条" in body

    async with session_factory() as session:
        rows = (await session.execute(select(ForwardOutbox).order_by(ForwardOutbox.id))).scalars().all()
    assert [row.status for row in rows] == [ForwardOutboxStatus.SENT] * 3
    assert all(row.attempts == 1 and row.sent_at is not None for row in rows)


@pytest.mark.asyncio
async def test_a_sibling_not_yet_due_is_left_for_the_next_window(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from models import ForwardOutbox
    from services.forwarding.outbox import process_forward_outbox_by_id
    from services.notifications import feishu

    sends: list[dict[str, Any]] = []

    async def fake_send(url: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        sends.append(payload)
        return {"status": "success"}

    monkeypatch.setattr(feishu, "send_to_feishu", fake_send)
    due = await _insert_group(session_factory, count=2)
    # Same group, but parked into the future (a sibling returned after a failed
    # attempt): the claim must respect next_attempt_at, not just the key.
    later = await _insert_group(session_factory, count=1, due=utcnow() + timedelta(hours=1))

    await process_forward_outbox_by_id(due[0])

    assert len(sends) == 1
    async with session_factory() as session:
        statuses = {row.id: row.status for row in (await session.execute(select(ForwardOutbox))).scalars().all()}
    assert statuses[due[0]] == statuses[due[1]] == ForwardOutboxStatus.SENT
    assert statuses[later[0]] == ForwardOutboxStatus.PENDING


@pytest.mark.asyncio
async def test_a_failed_group_retries_as_a_group(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leader follows the normal retry policy; the siblings are parked on the
    leader's next attempt so the same kick claims the whole group again."""
    from models import ForwardOutbox
    from services.forwarding import outbox
    from services.notifications import feishu

    async def failing_send(url: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {"status": "failed", "message": "feishu 500", "retryable": True}

    monkeypatch.setattr(feishu, "send_to_feishu", failing_send)
    monkeypatch.setattr(outbox, "schedule_forward_outbox_retry", AsyncMock())
    ids = await _insert_group(session_factory, count=3)

    await outbox.process_forward_outbox_by_id(ids[0])

    async with session_factory() as session:
        rows = {row.id: row for row in (await session.execute(select(ForwardOutbox))).scalars().all()}
    leader, siblings = rows[ids[0]], [rows[ids[1]], rows[ids[2]]]
    assert leader.status == ForwardOutboxStatus.RETRYING
    assert leader.next_attempt_at is not None and leader.next_attempt_at > utcnow()
    for sibling in siblings:
        assert sibling.status == ForwardOutboxStatus.PENDING
        assert sibling.next_attempt_at == leader.next_attempt_at
        assert sibling.attempts == 1
        assert "feishu 500" in str(sibling.last_error)


@pytest.mark.asyncio
async def test_a_permanently_failed_group_exhausts_together(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One dead webhook, one exhausted notice — not one per alert."""
    from models import ForwardOutbox
    from services.forwarding import outbox
    from services.notifications import feishu
    from services.operations import self_notify

    async def dead_send(url: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {"status": "failed", "message": "webhook removed", "retryable": False}

    monkeypatch.setattr(feishu, "send_to_feishu", dead_send)
    monkeypatch.setattr(self_notify, "notify_delivery_exhausted", AsyncMock())
    notices = AsyncMock(return_value={"status": "skipped", "outbox_ids": []})
    monkeypatch.setattr(outbox, "enqueue_forward_notification", notices)
    ids = await _insert_group(session_factory, count=3)

    await outbox.process_forward_outbox_by_id(ids[0])

    async with session_factory() as session:
        rows = (await session.execute(select(ForwardOutbox).order_by(ForwardOutbox.id))).scalars().all()
    assert [row.status for row in rows] == [ForwardOutboxStatus.EXHAUSTED] * 3
    assert all(row.next_attempt_at is None for row in rows)
    assert notices.await_count == 1
