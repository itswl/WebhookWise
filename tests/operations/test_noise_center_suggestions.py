"""What the noise centre may and may not propose, and the digest it can apply."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import DecisionTrace, ForwardRule, InboundRule, NoiseReductionAction, WebhookEvent
from services.operations import runtime_settings as rt
from services.operations.noise_center import (
    _digest_expected_reduction,
    apply_noise_suggestion,
    get_noise_center,
    undo_noise_action,
)

_ALERT_RULE = "Example deposit threshold"


@pytest.fixture
def session(db_session):
    return db_session


def _forward_rule(
    name: str,
    *,
    target_type: str = "feishu",
    match_event_type: str = "",
) -> ForwardRule:
    return ForwardRule(
        name=name,
        enabled=True,
        target_type=target_type,
        target_url="https://example.com/hook",
        match_source="grafana",
        match_event_type=match_event_type,
        match_duplicate="all",
    )


async def _seed(session: AsyncSession, *, alerts: int = 24, duplicates: int = 14) -> dict[str, ForwardRule]:
    """One repetitive alert rule, and four forward rules with different standing."""
    now = utcnow()
    rules = {
        "tunable": _forward_rule("Primary operations channel"),
        "system_events": _forward_rule("Incident notifications", match_event_type="incident_created,incident_resolved"),
        "relay_target": _forward_rule("Comparison feed", target_type="feishu_relay"),
        "shadow_name": _forward_rule("shadow: hookstack relay"),
    }
    session.add_all(list(rules.values()))
    await session.flush()
    for index in range(alerts):
        event = WebhookEvent(
            source="grafana",
            timestamp=now - timedelta(minutes=index * 5),
            parsed_data={"RuleName": _ALERT_RULE, "status": "firing"},
            is_duplicate=index < duplicates,
        )
        session.add(event)
        await session.flush()
        session.add(
            DecisionTrace(
                webhook_event_id=event.id,
                created_at=event.timestamp,
                source="grafana",
                alert_name=_ALERT_RULE,
                outcome="forwarded",
                skip_code="none",
                matched_rules=[rules["tunable"].name],
            )
        )
    await session.commit()
    return rules


def _by_kind(payload: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in payload["suggestions"] if item["kind"] == kind]


@pytest.mark.asyncio
async def test_system_notification_and_shadow_rules_are_never_tuned(session: AsyncSession) -> None:
    """Measured on production 2026-09-02: three identical "notify on new alerts
    only" suggestions, one against the shadow relay and one against a rule whose
    event types are incident_created,incident_resolved."""
    rules = await _seed(session)

    payload = await get_noise_center(session, window_days=7)

    tuned = {item["scope"]["rule_id"] for item in _by_kind(payload, "duplicate_filter")}
    assert tuned == {int(rules["tunable"].id)}
    assert int(rules["system_events"].id) not in tuned
    assert int(rules["relay_target"].id) not in tuned
    assert int(rules["shadow_name"].id) not in tuned


@pytest.mark.asyncio
async def test_a_repetitive_alert_rule_is_proposed_as_an_hourly_digest(session: AsyncSession) -> None:
    await _seed(session)

    payload = await get_noise_center(session, window_days=7)

    digest = _by_kind(payload, "digest")
    assert len(digest) == 1
    suggestion = digest[0]
    assert suggestion["scope"]["rule_name"] == _ALERT_RULE
    assert suggestion["scope"]["window_minutes"] == 60
    assert suggestion["scope"]["duplicate_rate"] == pytest.approx(58.3)
    assert suggestion["risk"] == "low"
    assert suggestion["action_available"] is True
    assert 0 < suggestion["confidence"] <= 1


@pytest.mark.asyncio
async def test_a_quiet_or_non_repeating_rule_is_left_alone(session: AsyncSession) -> None:
    # Above the repeat share but far below NOISE_DIGEST_MIN_ALERTS.
    await _seed(session, alerts=8, duplicates=6)

    payload = await get_noise_center(session, window_days=7)

    assert _by_kind(payload, "digest") == []


def test_expected_reduction_counts_one_card_per_window_the_rule_fired_in() -> None:
    # 200 alerts spread over 24 hours it actually fired in -> 24 cards remain.
    assert _digest_expected_reduction(200, firing_windows=24) == 176
    # The same 200 alerts inside a single window collapse to one card.
    assert _digest_expected_reduction(200, firing_windows=1) == 199
    # A rule that fires once per window saves nothing, and says so.
    assert _digest_expected_reduction(10, firing_windows=10) == 0
    # Pricing the QUIET windows too (the first shape of this) drove every real
    # candidate negative: 93 alerts a week against 168 hourly windows.
    assert _digest_expected_reduction(93, firing_windows=40) == 53
    # Never negative, whatever a caller passes.
    assert _digest_expected_reduction(3, firing_windows=99) == 0


@pytest.mark.asyncio
async def test_applying_a_digest_writes_one_inbound_rule_and_is_idempotent(session: AsyncSession) -> None:
    await _seed(session)
    payload = await get_noise_center(session, window_days=7)
    suggestion = _by_kind(payload, "digest")[0]

    applied = await apply_noise_suggestion(
        session,
        suggestion_id=str(suggestion["id"]),
        window_days=7,
        actor="alice",
    )
    assert applied["changed"] is True

    inbound = (await session.execute(select(InboundRule))).scalars().all()
    assert len(inbound) == 1
    rule = inbound[0]
    assert rule.action == "digest"
    assert rule.action_value == "60"
    assert rule.match_rule_name == _ALERT_RULE
    assert rule.name == f"digest 60m: {_ALERT_RULE}"
    assert rule.enabled is True

    # Already batched: the suggestion is not offered a second time.
    payload = await get_noise_center(session, window_days=7)
    assert _by_kind(payload, "digest") == []

    repeated = await apply_noise_suggestion(
        session,
        suggestion_id=str(suggestion["id"]),
        window_days=7,
        actor="alice",
    )
    assert repeated["changed"] is False
    assert repeated["reason"] == "already_applied"
    assert len((await session.execute(select(InboundRule))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_undoing_a_digest_disables_the_rule_and_re_applying_reuses_it(session: AsyncSession) -> None:
    await _seed(session)
    payload = await get_noise_center(session, window_days=7)
    suggestion = _by_kind(payload, "digest")[0]
    applied = await apply_noise_suggestion(
        session,
        suggestion_id=str(suggestion["id"]),
        window_days=7,
        actor="alice",
    )
    action_id = int(applied["action"]["id"])
    rule_id = int(applied["action"]["resource_id"])
    assert applied["action"]["action_type"] == "digest"
    assert applied["action"]["resource_type"] == "inbound_rule"

    undone = await undo_noise_action(session, action_id=action_id, actor="alice")
    assert undone["changed"] is True
    rule = await session.get(InboundRule, rule_id)
    assert rule is not None
    assert rule.enabled is False

    payload = await get_noise_center(session, window_days=7)
    reapplied = await apply_noise_suggestion(
        session,
        suggestion_id=str(_by_kind(payload, "digest")[0]["id"]),
        window_days=7,
        actor="alice",
    )
    assert reapplied["changed"] is True
    assert int(reapplied["action"]["resource_id"]) == rule_id
    assert len((await session.execute(select(InboundRule))).scalars().all()) == 1
    assert len((await session.execute(select(NoiseReductionAction))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_a_synthetic_source_is_absent_from_the_tables_and_the_suggestions(session: AsyncSession) -> None:
    await _seed(session)
    now = utcnow()
    for index in range(30):
        event = WebhookEvent(
            source="rotation-probe",
            timestamp=now - timedelta(minutes=index),
            parsed_data={"RuleName": "Credential rotation probe", "status": "firing"},
            is_duplicate=index > 0,
        )
        session.add(event)
        await session.flush()
        session.add(
            DecisionTrace(
                webhook_event_id=event.id,
                created_at=event.timestamp,
                source="rotation-probe",
                alert_name="Credential rotation probe",
                outcome="forwarded",
                skip_code="none",
                matched_rules=[],
            )
        )
    await session.commit()

    rt._swap_snapshot({"SYNTHETIC_SOURCES": "Rotation-Probe"})
    try:
        payload = await get_noise_center(session, window_days=7)
    finally:
        rt._reset_snapshot_for_tests()

    assert [row["source"] for row in payload["sources"]] == ["grafana"]
    assert all(row["name"] != "Credential rotation probe" for row in payload["noisy_rules"])
    assert payload["summary"]["total"] == 24
    assert all(item["scope"].get("rule_name") != "Credential rotation probe" for item in payload["suggestions"])
