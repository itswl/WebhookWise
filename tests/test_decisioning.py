"""Tests for services.webhooks.decisioning pure functions.

Zero dependencies — no DB, no async, no mocks. These are the highest-value
unit tests because the functions have no side effects and encode all
forwarding / rule-matching logic.
"""

from datetime import datetime, timedelta

from core.datetime_utils import utcnow
from core.text import split_csv_lower
from services.webhooks.decisioning import (
    ForwardDecision,
    ForwardingPolicy,
    ForwardRuleSnapshot,
    _rule_matches,
    build_final_analysis,
    decide_forwarding,
    extract_forward_match_fields,
    normalize_importance,
    select_forward_rules,
)
from services.webhooks.types import NoiseReductionContext

# ── helpers ──────────────────────────────────────────────────────────


def _make_rule(
    *,
    name: str = "test-rule",
    match_event_type: str = "",
    match_importance: str = "",
    match_source: str = "",
    match_project: str = "",
    match_region: str = "",
    match_environment: str = "",
    match_duplicate: str = "all",
    match_payload: str = "",
    target_type: str = "webhook",
    target_url: str = "http://example.com/hook",
    stop_on_match: bool = False,
    rule_id: int | None = 1,
) -> ForwardRuleSnapshot:
    return ForwardRuleSnapshot(
        id=rule_id,
        name=name,
        match_event_type=match_event_type,
        match_importance=match_importance,
        match_source=match_source,
        match_project=match_project,
        match_region=match_region,
        match_environment=match_environment,
        match_duplicate=match_duplicate,
        match_payload=match_payload,
        target_type=target_type,
        target_url=target_url,
        stop_on_match=stop_on_match,
    )


def _make_noise(
    suppress: bool = False,
    relation: str = "standalone",
    confidence: float = 0.0,
) -> NoiseReductionContext:
    return NoiseReductionContext(
        relation=relation,
        root_cause_event_id=None,
        confidence=confidence,
        suppress_forward=suppress,
        reason="" if not suppress else "test suppression",
        related_alert_count=0,
        related_alert_ids=(),
    )


def _make_policy(**overrides: object) -> ForwardingPolicy:
    defaults = {
        "notification_cooldown_seconds": 60,
        "enable_periodic_reminder": True,
        "reminder_interval_hours": 6,
    }
    defaults.update(overrides)
    return ForwardingPolicy(**defaults)  # type: ignore[arg-type]


class _Event:
    def __init__(self, last_notified_at: datetime | None = None) -> None:
        self.last_notified_at = last_notified_at


# ── normalize_importance ─────────────────────────────────────────────


class TestNormalizeImportance:
    def test_strips_and_lowercases(self) -> None:
        assert normalize_importance("  HIGH  ") == "high"

    def test_none_returns_empty(self) -> None:
        assert normalize_importance(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert normalize_importance("") == ""

    def test_splits_on_last_dot(self) -> None:
        assert normalize_importance("alert.severity.high") == "high"

    def test_no_dot_returns_as_is(self) -> None:
        assert normalize_importance("medium") == "medium"

    def test_single_char(self) -> None:
        assert normalize_importance("M") == "m"


# ── split_csv_lower ──────────────────────────────────────────────────


class TestSplitCsv:
    def test_basic_split(self) -> None:
        assert split_csv_lower("HIGH,CRITICAL") == ["high", "critical"]

    def test_strips_whitespace(self) -> None:
        assert split_csv_lower("  a , B , ") == ["a", "b"]

    def test_empty_returns_empty(self) -> None:
        assert split_csv_lower("") == []

    def test_single_value(self) -> None:
        assert split_csv_lower("high") == ["high"]


# ── _rule_matches ────────────────────────────────────────────────────


class TestRuleMatches:
    def test_empty_criteria_matches_all(self) -> None:
        rule = _make_rule()
        assert _rule_matches(rule, importance="high", source="prometheus", is_duplicate=False)
        assert _rule_matches(rule, importance="low", source="grafana", is_duplicate=True)

    def test_importance_csv_match(self) -> None:
        rule = _make_rule(match_importance="high,critical")
        assert _rule_matches(rule, importance="high", source="x", is_duplicate=False)
        assert not _rule_matches(rule, importance="medium", source="x", is_duplicate=False)

    def test_source_csv_match(self) -> None:
        rule = _make_rule(match_source="prometheus,grafana")
        assert _rule_matches(rule, importance="high", source="prometheus", is_duplicate=False)
        assert not _rule_matches(rule, importance="high", source="zabbix", is_duplicate=False)

    def test_duplicate_new_matches_only_new(self) -> None:
        rule = _make_rule(match_duplicate="new")
        assert _rule_matches(rule, importance="high", source="x", is_duplicate=False)
        assert not _rule_matches(rule, importance="high", source="x", is_duplicate=True)

    def test_duplicate_matches_only_duplicate(self) -> None:
        rule = _make_rule(match_duplicate="duplicate")
        assert _rule_matches(rule, importance="high", source="x", is_duplicate=True)
        assert not _rule_matches(rule, importance="high", source="x", is_duplicate=False)

    def test_all_matches_both_new_and_duplicate(self) -> None:
        rule = _make_rule(match_duplicate="all")
        assert _rule_matches(rule, importance="high", source="x", is_duplicate=False)
        assert _rule_matches(rule, importance="high", source="x", is_duplicate=True)

    def test_combined_filters(self) -> None:
        rule = _make_rule(match_importance="high", match_source="prometheus", match_duplicate="new")
        assert _rule_matches(rule, importance="high", source="prometheus", is_duplicate=False)
        assert not _rule_matches(rule, importance="medium", source="prometheus", is_duplicate=False)
        assert not _rule_matches(rule, importance="high", source="grafana", is_duplicate=False)

    def test_project_region_environment_filters(self) -> None:
        rule = _make_rule(match_project="eve-cn", match_region="cn-shanghai", match_environment="prod,production")
        payload = {
            "Resources": [
                {
                    "ProjectName": "eve-cn",
                    "Region": "cn-shanghai",
                    "Name": "eve-cn-prod-mongo",
                }
            ]
        }
        assert _rule_matches(rule, parsed_data=payload)
        assert not _rule_matches(_make_rule(match_project="other"), parsed_data=payload)
        assert not _rule_matches(_make_rule(match_region="cn-beijing"), parsed_data=payload)
        assert not _rule_matches(_make_rule(match_environment="dev"), parsed_data=payload)

    def test_project_environment_exclusion_filters(self) -> None:
        prod_payload = {"Resources": [{"ProjectName": "eve-cn", "Name": "eve-cn-prod-api"}]}
        dev_payload = {"Resources": [{"ProjectName": "elys-web-cn", "Name": "elys-web-cn-dev-api"}]}
        assert not _rule_matches(_make_rule(match_environment="!prod"), parsed_data=prod_payload)
        assert _rule_matches(_make_rule(match_environment="!prod"), parsed_data=dev_payload)
        assert not _rule_matches(_make_rule(match_project="!eve-cn,!cyberclone-cn"), parsed_data=prod_payload)
        assert _rule_matches(_make_rule(match_project="!eve-cn,!cyberclone-cn"), parsed_data=dev_payload)

    def test_extract_forward_match_fields_prefers_explicit_environment(self) -> None:
        fields = extract_forward_match_fields(
            {
                "Resources": [{"ProjectName": "cyberclone-cn", "Region": "cn-shanghai", "Name": "resource-dev"}],
                "labels": {"environment": "production"},
            }
        )
        assert fields == {"project": "cyberclone-cn", "region": "cn-shanghai", "environment": "prod"}


# ── select_forward_rules ─────────────────────────────────────────────


class TestSelectForwardRules:
    def test_stop_on_match_limits_results(self) -> None:
        rules = [
            _make_rule(name="a", match_importance="high", stop_on_match=True, rule_id=1),
            _make_rule(name="b", match_importance="high", rule_id=2),
        ]
        result = select_forward_rules(rules, importance="high", source="x", is_duplicate=False)
        assert len(result) == 1
        assert result[0].name == "a"

    def test_continues_without_stop(self) -> None:
        rules = [
            _make_rule(name="a", match_importance="high", rule_id=1),
            _make_rule(name="b", match_importance="high", rule_id=2),
        ]
        result = select_forward_rules(rules, importance="high", source="x", is_duplicate=False)
        assert len(result) == 2

    def test_no_matching_rules(self) -> None:
        rules = [_make_rule(match_importance="high")]
        result = select_forward_rules(rules, importance="low", source="x", is_duplicate=False)
        assert result == []

    def test_empty_rules_list(self) -> None:
        assert select_forward_rules([], importance="high", source="x", is_duplicate=False) == []


# ── decide_forwarding ────────────────────────────────────────────────


class TestDecideForwarding:
    def _decide(
        self,
        importance: str = "high",
        is_duplicate: bool = False,
        noise: NoiseReductionContext | None = None,
        original_event: _Event | None = None,
        source: str = "prometheus",
        parsed_data: dict[str, object] | None = None,
        rules: list[ForwardRuleSnapshot] | None = None,
        policy: ForwardingPolicy | None = None,
        now: datetime | None = None,
    ) -> ForwardDecision:
        return decide_forwarding(
            importance=importance,
            is_duplicate=is_duplicate,
            noise=noise or _make_noise(),
            original_event=original_event,
            source=source,
            rules=rules or [],
            policy=policy or _make_policy(),
            parsed_data=parsed_data,
            now=now or utcnow(),
        )

    def test_noise_suppress_overrides_all(self) -> None:
        noise = _make_noise(suppress=True)
        result = self._decide(importance="high", noise=noise)
        assert not result.should_forward
        assert "降噪" in (result.skip_reason or "")

    def test_high_importance_no_rules_skips(self) -> None:
        result = self._decide(importance="high")
        assert not result.should_forward

    def test_high_importance_with_matched_rule_forwards(self) -> None:
        rules = [_make_rule(match_importance="high")]
        result = self._decide(importance="high", rules=rules)
        assert result.should_forward

    def test_medium_no_rules_no_forward(self) -> None:
        result = self._decide(importance="medium")
        assert not result.should_forward

    def test_low_no_rules_no_forward(self) -> None:
        result = self._decide(importance="low")
        assert not result.should_forward

    def test_matched_rule_enables_medium_forward(self) -> None:
        rules = [_make_rule(match_importance="medium")]
        result = self._decide(importance="medium", rules=rules)
        assert result.should_forward
        assert len(result.matched_rules) == 1

    def test_match_payload_filters_rules(self) -> None:
        rules = [_make_rule(match_payload="labels.severity=critical")]
        result = self._decide(
            importance="medium",
            rules=rules,
            parsed_data={"labels": {"severity": "critical"}},
        )
        assert result.should_forward
        assert len(result.matched_rules) == 1
        rejected = self._decide(
            importance="medium",
            rules=rules,
            parsed_data={"labels": {"severity": "warning"}},
        )
        assert not rejected.should_forward

    def test_duplicate_in_cooldown_suppresses(self) -> None:
        recent = utcnow() - timedelta(seconds=10)
        result = self._decide(
            importance="high",
            is_duplicate=True,
            original_event=_Event(last_notified_at=recent),
            policy=_make_policy(notification_cooldown_seconds=60),
        )
        assert not result.should_forward

    def test_duplicate_cooldown_expired_with_matched_rules_forwards(self) -> None:
        old = utcnow() - timedelta(seconds=120)
        rules = [_make_rule(match_importance="high")]
        result = self._decide(
            importance="high",
            is_duplicate=True,
            original_event=_Event(last_notified_at=old),
            rules=rules,
            policy=_make_policy(notification_cooldown_seconds=60),
        )
        assert result.should_forward

    def test_duplicate_no_matched_rules_skips(self) -> None:
        old = utcnow() - timedelta(seconds=120)
        result = self._decide(
            importance="high",
            is_duplicate=True,
            original_event=_Event(last_notified_at=old),
            policy=_make_policy(),
        )
        assert not result.should_forward

    def test_periodic_reminder_triggers(self) -> None:
        old = utcnow() - timedelta(hours=7)
        rules = [_make_rule(match_importance="high")]
        result = self._decide(
            importance="high",
            is_duplicate=True,
            original_event=_Event(last_notified_at=old),
            rules=rules,
            policy=_make_policy(
                enable_periodic_reminder=True,
                reminder_interval_hours=6,
            ),
        )
        assert result.should_forward
        assert result.is_periodic_reminder


# ── build_final_analysis ─────────────────────────────────────────────


class TestBuildFinalAnalysis:
    def test_merges_noise_context(self) -> None:
        analysis = {"importance": "high", "summary": "test"}
        noise = _make_noise()
        result = build_final_analysis(analysis, noise)
        assert result["importance"] == "high"
        assert result["summary"] == "test"
        assert "noise_reduction" in result
        assert result["noise_reduction"]["relation"] == "standalone"

    def test_does_not_mutate_original(self) -> None:
        analysis = {"importance": "high"}
        noise = _make_noise()
        result = build_final_analysis(analysis, noise)
        assert "noise_reduction" not in analysis
        assert "noise_reduction" in result
