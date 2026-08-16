"""System notifications ask the rules first, and fall back to configuration."""

from typing import Any

import pytest

from services.forwarding.types import ForwardRuleSnapshot
from services.notifications.routing import resolve_notification_target


def _rule(**overrides: Any) -> ForwardRuleSnapshot:
    values: dict[str, Any] = {
        "id": 7,
        "name": "incident room",
        "match_event_type": "incident_created",
        "match_importance": "",
        "match_source": "",
        "match_duplicate": "all",
        "match_payload": "",
        "target_type": "feishu",
        "target_url": "https://open.feishu.cn/hook/rule",
        "target_name": "",
        "stop_on_match": False,
    }
    values.update(overrides)
    return ForwardRuleSnapshot(**values)


def _patch_rules(monkeypatch: pytest.MonkeyPatch, loader: Any) -> None:
    monkeypatch.setattr("services.forwarding.rules.get_cached_forward_rules", loader)


@pytest.mark.asyncio
async def test_a_rule_claims_the_event_and_is_recorded_as_the_decider(monkeypatch) -> None:
    async def rules(session: Any = None) -> list[ForwardRuleSnapshot]:
        return [_rule()]

    _patch_rules(monkeypatch, rules)

    target = await resolve_notification_target(
        "incident_created", fallback_url="https://configured", fallback_name="incident-notification"
    )

    assert target.url == "https://open.feishu.cn/hook/rule"
    # The point of the change: a delivery that can be attributed, and therefore
    # seen failing in the rules UI instead of dying inside a config value.
    assert target.from_rule and target.rule_id == 7
    assert target.rule_name == "incident room"


@pytest.mark.asyncio
async def test_without_a_matching_rule_nothing_moves(monkeypatch) -> None:
    """The compatible half: today's cascade still decides until rules exist."""

    async def rules(session: Any = None) -> list[ForwardRuleSnapshot]:
        return [_rule(match_event_type="sla_breached")]

    _patch_rules(monkeypatch, rules)

    target = await resolve_notification_target(
        "incident_created", fallback_url="https://configured", fallback_name="incident-notification"
    )

    assert target.url == "https://configured"
    assert not target.from_rule


@pytest.mark.asyncio
async def test_a_rule_pointing_at_a_gateway_is_not_a_notification_target(monkeypatch) -> None:
    async def rules(session: Any = None) -> list[ForwardRuleSnapshot]:
        return [_rule(target_type="deep_analysis", target_url="")]

    _patch_rules(monkeypatch, rules)

    target = await resolve_notification_target(
        "incident_created", fallback_url="https://configured", fallback_name="incident-notification"
    )

    assert target.url == "https://configured"


@pytest.mark.asyncio
async def test_a_broken_rules_lookup_never_silences_a_notification(monkeypatch) -> None:
    """The failure this whole area exists to end: a card that goes nowhere quietly."""

    async def boom(session: Any = None) -> list[ForwardRuleSnapshot]:
        raise RuntimeError("cache down")

    _patch_rules(monkeypatch, boom)

    target = await resolve_notification_target(
        "incident_created", fallback_url="https://configured", fallback_name="incident-notification"
    )

    assert target.url == "https://configured"
