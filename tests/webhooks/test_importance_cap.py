"""An operator-set ceiling on an alert rule's severity.

Measured over 80 production investigations, WebhookWise files 90% of alerts
`high` and the investigator that read them agrees on a quarter. The worst
offender is a business-signal rule whose money keywords force `high`, and it
cannot be fixed by editing keywords (the sibling withdrawal rule genuinely IS
high a third of the time) nor by `importance_overrides` (which keys on
alert_hash, and these alerts carry a user id in their identity — 25 distinct
hashes over 25 reports).

So the ceiling is per alert RULE, and these are the properties that make it safe.
"""

from __future__ import annotations

import pytest

from services.webhooks.decisioning import InboundRuleSnapshot, matching_inbound_importance_cap
from services.webhooks.types import (
    ANALYSIS_IMPORTANCE_CAP,
    CAP_IMPORTANCE,
    SKIP_AI,
    AnalysisResult,
    apply_importance_cap,
)


def _analysis(importance: str) -> AnalysisResult:
    return AnalysisResult(importance=importance, summary="a deposit over the threshold")


def _rule(**kw: object) -> InboundRuleSnapshot:
    base: dict[str, object] = {
        "id": 1,
        "name": "cap deposits",
        "action": CAP_IMPORTANCE,
        "action_value": "medium",
        "priority": 10,
        "match_rule_name": "示例充值超限告警",
    }
    base.update(kw)
    return InboundRuleSnapshot(**base)  # type: ignore[arg-type]


def test_a_cap_lowers_a_judgement_above_it() -> None:
    result = apply_importance_cap(_analysis("high"), cap="medium", rule_name="cap deposits")

    assert result["importance"] == "medium"
    # The trace matters as much as the value: an importance nobody can attribute
    # is how you end up arguing with a model that never said it.
    assert result[ANALYSIS_IMPORTANCE_CAP] == {
        "capped_to": "medium",
        "judged": "high",
        "rule": "cap deposits",
    }


def test_a_cap_is_a_ceiling_not_a_floor() -> None:
    """The load-bearing property. If a cap could RAISE severity, setting one
    would be unsafe to forget about: an alert the judgement called `low` would
    start paging at `medium` forever."""
    result = apply_importance_cap(_analysis("low"), cap="medium", rule_name="cap deposits")

    assert result["importance"] == "low"
    assert ANALYSIS_IMPORTANCE_CAP not in result


def test_an_unrecognised_severity_is_capped_rather_than_trusted() -> None:
    """A value this system does not know must not slip ABOVE the ceiling an
    operator set — the safe reading of an unknown is the most severe one."""
    result = apply_importance_cap(_analysis("catastrophic"), cap="medium", rule_name="r")

    assert result["importance"] == "medium"


def test_a_meaningless_cap_changes_nothing() -> None:
    for cap in ("", "urgent", "HIGH-ish"):
        assert apply_importance_cap(_analysis("high"), cap=cap, rule_name="r")["importance"] == "high"


def test_the_cap_matches_on_the_alert_rule_name() -> None:
    rules = [_rule()]

    cap, name = matching_inbound_importance_cap(rules, rule_name="示例充值超限告警")
    assert (cap, name) == ("medium", "cap deposits")

    # The sibling rule must be untouched — it is genuinely high a third of the
    # time, which is the whole reason this is per-rule and not a keyword edit.
    assert matching_inbound_importance_cap(rules, rule_name="示例提现超限告警") == ("", "")


def test_priority_decides_when_two_caps_match() -> None:
    """Unlike the action SET, a cap carries a value, so two matching rules would
    otherwise disagree and the winner would depend on iteration order."""
    rules = [
        _rule(id=1, name="low cap", action_value="low", priority=99),
        _rule(id=2, name="medium cap", action_value="medium", priority=1),
    ]

    assert matching_inbound_importance_cap(rules, rule_name="示例充值超限告警") == ("low", "low cap")


def test_other_verbs_are_not_mistaken_for_a_cap() -> None:
    rules = [_rule(action=SKIP_AI, action_value="")]

    assert matching_inbound_importance_cap(rules, rule_name="示例充值超限告警") == ("", "")


def test_a_cap_rule_with_no_value_is_ignored_rather_than_guessed() -> None:
    """The write path refuses to save one, but a row predating that validation
    must not be interpreted as "cap at whatever"."""
    rules = [_rule(action_value="")]

    assert matching_inbound_importance_cap(rules, rule_name="示例充值超限告警") == ("", "")


@pytest.mark.parametrize(
    ("action", "value", "expected_ok"),
    [
        (CAP_IMPORTANCE, "medium", True),
        (CAP_IMPORTANCE, "", False),
        (CAP_IMPORTANCE, "urgent", False),
        (SKIP_AI, "medium", False),
        (SKIP_AI, "", True),
    ],
)
def test_the_write_path_refuses_a_verb_and_value_that_disagree(action: str, value: str, expected_ok: bool) -> None:
    """A rule storing a value its verb never reads looks configured and does
    something else."""
    from services.webhooks.inbound_rules import validate

    problem = validate(
        {"name": "r", "action": action, "action_value": value, "match_rule_name": "示例充值超限告警"}
    )

    assert (problem is None) is expected_ok, problem
