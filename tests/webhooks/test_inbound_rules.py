"""Inbound rules: the forwarding matcher, pointed at what an alert costs."""

from unittest.mock import AsyncMock

import pytest

from services.webhooks.decisioning import InboundRuleSnapshot, matching_inbound_actions
from services.webhooks.inbound_rules import SKIP_AI, SKIP_DEEP_ANALYSIS, validate

TOPUP = {
    "RuleName": "示例充值超限告警",
    "Level": "info",
    "status": "firing",
    "commonLabels": {"alertname": "示例充值超限告警", "env": "prod"},
}


def _rule(**overrides: object) -> InboundRuleSnapshot:
    values: dict[str, object] = {"id": 1, "name": "no ai for top-ups", "action": SKIP_AI}
    values.update(overrides)
    return InboundRuleSnapshot(**values)  # type: ignore[arg-type]


def test_a_rule_name_list_matches_the_way_an_operator_thinks() -> None:
    rules = [_rule(match_rule_name="示例充值超限告警,示例提现超限告警")]

    assert matching_inbound_actions(rules, parsed_data=TOPUP, rule_name="示例充值超限告警") == {SKIP_AI}
    assert matching_inbound_actions(rules, parsed_data=TOPUP, rule_name="DatasourceNoData") == set()


def test_the_shared_matcher_brings_source_and_payload_with_it() -> None:
    """The point of reusing _rule_matches: criteria a name list cannot express."""
    rules = [_rule(match_source="grafana", match_payload="alertname=示例充值超限告警")]

    assert matching_inbound_actions(rules, parsed_data=TOPUP, source="grafana") == {SKIP_AI}
    # Same alert, different sender: not this rule's business.
    assert matching_inbound_actions(rules, parsed_data=TOPUP, source="prometheus") == set()


def test_actions_accumulate_rather_than_stopping_at_the_first() -> None:
    """Two independent decisions, possibly reached by two different rules."""
    rules = [
        _rule(id=1, match_rule_name="示例充值超限告警", action=SKIP_AI),
        _rule(id=2, match_source="grafana", action=SKIP_DEEP_ANALYSIS),
    ]

    actions = matching_inbound_actions(rules, parsed_data=TOPUP, source="grafana", rule_name="示例充值超限告警")

    assert actions == {SKIP_AI, SKIP_DEEP_ANALYSIS}


def test_a_skip_ai_rule_may_not_filter_on_something_it_cannot_know() -> None:
    """Importance is decided after this rule runs, so such a rule never matches.

    Refused on write rather than left to look configured and do nothing — the
    same silent-failure shape as a mistyped exclusion name.
    """
    problem = validate({"name": "x", "action": SKIP_AI, "match_importance": "high"})
    assert problem is not None and "never match" in problem

    # The same criterion is legitimate for the post-judgement action.
    assert validate({"name": "x", "action": SKIP_DEEP_ANALYSIS, "match_importance": "high"}) is None


def test_a_rule_with_no_criteria_is_refused() -> None:
    problem = validate({"name": "everything", "action": SKIP_AI})
    assert problem is not None and "every alert" in problem


def test_unknown_actions_are_refused() -> None:
    assert validate({"name": "x", "action": "drop", "match_source": "grafana"}) is not None


@pytest.mark.asyncio
async def test_a_rules_outage_does_not_stop_analysis(monkeypatch) -> None:
    """Fails open: the alternative is losing analysis because a cache is sad."""
    from services.webhooks import inbound_rules

    monkeypatch.setattr(inbound_rules._cache, "get", AsyncMock(side_effect=RuntimeError("redis down")))

    assert await inbound_rules.inbound_actions_for(parsed_data=TOPUP, rule_name="示例充值超限告警") == set()
