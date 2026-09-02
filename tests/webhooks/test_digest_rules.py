"""The digest verb: a noisy alert rule can be batched, not just capped.

Two business-threshold rules were 56% of a week's production volume (187 of
331 alerts), each firing a card of its own. A cap changes what the card says;
this changes how many cards there are. These are the properties that make the
rule safe to write once and forget.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.webhooks.decisioning import InboundRuleSnapshot, matching_inbound_digest_window
from services.webhooks.inbound_rules import normalize_action_value, validate
from services.webhooks.types import (
    ANALYSIS_DIGEST,
    CAP_IMPORTANCE,
    DIGEST,
    INBOUND_ACTIONS,
    INBOUND_ACTIONS_WITH_VALUE,
    SKIP_AI,
    AnalysisResult,
    analysis_digest_window,
    mark_analysis_digest,
    parse_digest_window_minutes,
)

DEPOSIT = "示例充值超限告警"


def _rule(**kw: object) -> InboundRuleSnapshot:
    base: dict[str, object] = {
        "id": 1,
        "name": "digest: deposits hourly",
        "action": DIGEST,
        "action_value": "60",
        "priority": 10,
        "match_rule_name": DEPOSIT,
    }
    base.update(kw)
    return InboundRuleSnapshot(**base)  # type: ignore[arg-type]


def test_the_verb_is_enumerated_wherever_the_others_are() -> None:
    assert DIGEST in INBOUND_ACTIONS
    assert DIGEST in INBOUND_ACTIONS_WITH_VALUE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", 60),  # empty means the default, not an error
        ("  ", 60),
        (None, 60),
        ("5", 5),
        ("1440", 1440),
        ("90", 90),
        ("4", None),
        ("1441", None),
        ("60.5", None),
        ("-60", None),
        ("hourly", None),
    ],
)
def test_a_window_is_a_whole_number_of_minutes_inside_the_bounds(value: object, expected: int | None) -> None:
    assert parse_digest_window_minutes(value) == expected


@pytest.mark.parametrize(
    ("action", "value", "expected_ok"),
    [
        (DIGEST, "", True),
        (DIGEST, "60", True),
        (DIGEST, "5", True),
        (DIGEST, "1440", True),
        (DIGEST, "4", False),
        (DIGEST, "1441", False),
        (DIGEST, "an hour", False),
        # The other verbs are untouched by the new one.
        (CAP_IMPORTANCE, "medium", True),
        (CAP_IMPORTANCE, "", False),
        (SKIP_AI, "", True),
        (SKIP_AI, "60", False),
    ],
)
def test_the_write_path_validates_the_window(action: str, value: str, expected_ok: bool) -> None:
    problem = validate({"name": "r", "action": action, "action_value": value, "match_rule_name": DEPOSIT})

    assert (problem is None) is expected_ok, problem


def test_an_empty_digest_window_is_stored_as_the_default() -> None:
    """A row reading action_value="" would look like "no window" to anyone
    inspecting the table while the matcher quietly used 60. Write it down."""
    assert normalize_action_value(DIGEST, "") == "60"
    assert normalize_action_value(DIGEST, " 90 ") == "90"
    # Other verbs keep the existing lower-casing and nothing more.
    assert normalize_action_value(CAP_IMPORTANCE, "MEDIUM") == "medium"
    assert normalize_action_value(SKIP_AI, "") == ""


def test_a_digest_rule_may_filter_on_importance() -> None:
    """Unlike skip_ai, the digest is decided AFTER judgement (on the capped
    importance), so "digest the mediums" is a rule that can match."""
    assert validate({"name": "r", "action": DIGEST, "match_rule_name": DEPOSIT, "match_importance": "medium"}) is None


def test_the_window_matches_on_the_alert_rule_name() -> None:
    rules = [_rule()]

    assert matching_inbound_digest_window(rules, rule_name=DEPOSIT) == (60, "digest: deposits hourly")
    assert matching_inbound_digest_window(rules, rule_name="DatasourceNoData") == (0, "")


def test_an_empty_stored_value_means_the_default_window() -> None:
    assert matching_inbound_digest_window([_rule(action_value="")], rule_name=DEPOSIT) == (
        60,
        "digest: deposits hourly",
    )


def test_a_stored_value_the_parser_rejects_is_ignored_not_guessed() -> None:
    """A row predating validation must deliver per alert, never "never"."""
    assert matching_inbound_digest_window([_rule(action_value="hourly")], rule_name=DEPOSIT) == (0, "")


def test_priority_decides_when_two_windows_match() -> None:
    rules = [
        _rule(id=1, name="daily", action_value="1440", priority=99),
        _rule(id=2, name="hourly", action_value="60", priority=1),
    ]

    assert matching_inbound_digest_window(rules, rule_name=DEPOSIT) == (1440, "daily")


def test_other_verbs_are_not_mistaken_for_a_window() -> None:
    rules = [_rule(action=CAP_IMPORTANCE, action_value="medium"), _rule(id=2, action=SKIP_AI, action_value="")]

    assert matching_inbound_digest_window(rules, rule_name=DEPOSIT) == (0, "")


def test_the_marker_names_the_window_and_the_rule() -> None:
    analysis = AnalysisResult(importance="medium", summary="a deposit over the threshold")

    marked = mark_analysis_digest(analysis, window_minutes=60, rule_name="digest: deposits hourly")

    assert marked[ANALYSIS_DIGEST] == {"window_minutes": 60, "rule": "digest: deposits hourly"}
    assert analysis_digest_window(marked) == (60, "digest: deposits hourly")
    assert analysis_digest_window({"importance": "high"}) == (0, "")
    assert analysis_digest_window({ANALYSIS_DIGEST: {"window_minutes": "60"}}) == (0, "")
    assert analysis_digest_window(None) == (0, "")


@pytest.mark.asyncio
async def test_the_pipeline_marks_a_reused_analysis_against_the_rules_as_they_are_now() -> None:
    """Same converging layer as the cap: a reused analysis lifted from an earlier
    event must be decided for THIS alert, and an inherited marker must not
    survive a rule that no longer matches."""
    from services.webhooks import pipeline_stages

    reused = AnalysisResult(importance="high", summary="lifted from an earlier event")
    reused["_route_type"] = "rechain"
    reused[ANALYSIS_DIGEST] = {"window_minutes": 1440, "rule": "stale"}

    ctx = SimpleNamespace(event_id=1, req_ctx=SimpleNamespace(parsed_data={"RuleName": DEPOSIT}, source="grafana"))

    with (
        patch.object(
            pipeline_stages, "_resolve_noise_context", new=AsyncMock(return_value=(reused, object(), object()))
        ),
        patch(
            "services.webhooks.inbound_rules.inbound_importance_cap_for",
            new=AsyncMock(return_value=("medium", "cap: deposits")),
        ),
        patch(
            "services.webhooks.inbound_rules.inbound_digest_window_for",
            new=AsyncMock(return_value=(60, "digest: deposits hourly")),
        ) as digest_lookup,
    ):
        analysis, _, _ = await pipeline_stages.resolve_noise_context(ctx, object())  # type: ignore[arg-type]

    assert analysis[ANALYSIS_DIGEST] == {"window_minutes": 60, "rule": "digest: deposits hourly"}
    # Matched on the CAPPED importance: "digest the mediums" sees what the card will say.
    assert digest_lookup.await_args is not None
    assert digest_lookup.await_args.kwargs["importance"] == "medium"
    assert digest_lookup.await_args.kwargs["rule_name"] == DEPOSIT


@pytest.mark.asyncio
async def test_the_pipeline_clears_an_inherited_marker_when_no_rule_matches() -> None:
    from services.webhooks import pipeline_stages

    reused = AnalysisResult(importance="low", summary="x")
    reused[ANALYSIS_DIGEST] = {"window_minutes": 60, "rule": "deleted since"}
    ctx = SimpleNamespace(event_id=1, req_ctx=SimpleNamespace(parsed_data={"RuleName": DEPOSIT}, source="grafana"))

    with (
        patch.object(
            pipeline_stages, "_resolve_noise_context", new=AsyncMock(return_value=(reused, object(), object()))
        ),
        patch("services.webhooks.inbound_rules.inbound_importance_cap_for", new=AsyncMock(return_value=("", ""))),
        patch("services.webhooks.inbound_rules.inbound_digest_window_for", new=AsyncMock(return_value=(0, ""))),
    ):
        analysis, _, _ = await pipeline_stages.resolve_noise_context(ctx, object())  # type: ignore[arg-type]

    assert ANALYSIS_DIGEST not in analysis


@pytest.mark.asyncio
async def test_the_api_refuses_a_window_outside_the_bounds_and_stores_the_default(db_session, monkeypatch) -> None:
    """The API is where an operator meets the bounds; the stored row is where
    the next operator reads them."""
    import json

    from api.v1 import inbound_rules as api
    from services.webhooks import inbound_rules as store

    monkeypatch.setattr(store, "publish_inbound_rules_invalidation", AsyncMock())

    refused = await api.create_rule(
        {"name": "digest deposits", "action": DIGEST, "action_value": "3", "match_rule_name": DEPOSIT},
        session=db_session,
    )
    assert getattr(refused, "status_code", None) == 400
    assert "between 5 and 1440" in json.loads(refused.body)["error"]  # type: ignore[union-attr]

    created = await api.create_rule(
        {"name": "digest deposits", "action": DIGEST, "action_value": "", "match_rule_name": DEPOSIT},
        session=db_session,
    )
    assert isinstance(created, dict) and created["success"] is True
    assert created["data"]["action_value"] == "60"

    listed = await api.list_inbound_rules(limit=50, session=db_session)
    assert DIGEST in listed["actions"]
    assert DIGEST in listed["actions_with_value"]

    # A partial update that switches the verb is validated as the rule WILL be.
    updated = await api.update_rule(created["data"]["id"], {"action_value": "90"}, session=db_session)
    assert isinstance(updated, dict) and updated["data"]["action_value"] == "90"
    still_refused = await api.update_rule(created["data"]["id"], {"action_value": "2000"}, session=db_session)
    assert getattr(still_refused, "status_code", None) == 400
