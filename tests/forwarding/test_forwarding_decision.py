"""
tests/forwarding/test_forwarding_decision.py
=============================================
Tests the forwarding decision logic in forwarding_stage.resolve_forward_decision().
These are core business rules: which alerts get forwarded, which are skipped, and why.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest

from core.app_context import get_default_app_context
from core.datetime_utils import utcnow
from services.webhooks.forwarding_stage import resolve_forward_decision


class _Event:
    """Minimal object that simulates a WebhookEvent."""

    def __init__(self, last_notified_at=None):
        self.last_notified_at = last_notified_at


class _Noise:
    """Simulates a NoiseReductionContext."""

    def __init__(self, suppress=False, reason=""):
        self.suppress_forward = suppress
        self.reason = reason


def _make_rules_loader(rules: list):
    async def _loader(**_: object):
        return rules

    return _loader


NO_RULES = _make_rules_loader([])


def _make_silences_loader(silences: list):
    async def _loader(**_: object):
        return silences

    return _loader


NO_SILENCES = _make_silences_loader([])


@pytest.fixture(autouse=True)
def _restore_static_config(temp_config):
    yield


@pytest.fixture(autouse=True)
def _no_silences_by_default():
    """Default the active-silences load to empty so these rule tests stay pure.

    Silences are an additive suppressor; tests that exercise silence behavior
    patch get_cached_active_silences explicitly.
    """
    with patch("services.webhooks.forwarding_stage.get_cached_active_silences", NO_SILENCES):
        yield


def _set_config(key: str, value: object) -> None:
    from core.config.manager import get_config_keys

    context = get_default_app_context()
    assert context is not None
    config = context.config
    config_info = get_config_keys()[key]
    setattr(getattr(config, config_info["sub"]), key, value)


# ── New alerts (not duplicates, not outside the window) ─────────────────────────


@pytest.mark.asyncio
async def test_high_importance_with_matched_rule_forwards():
    """High importance plus a matching rule should forward."""
    rule = _FakeRule(1, importance="high")
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])):
        decision = await resolve_forward_decision("high", False, None, None, "prometheus")
    assert decision.should_forward is True


@pytest.mark.asyncio
async def test_medium_importance_no_rules_does_not_forward():
    """No rules and medium importance does not auto-forward."""
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", NO_RULES):
        decision = await resolve_forward_decision("medium", False, None, None, "prometheus")
    assert decision.should_forward is False
    assert decision.skip_code == "no_match"
    assert "No matching forwarding rule" in (decision.skip_reason or "")


@pytest.mark.asyncio
async def test_low_importance_no_rules_does_not_forward():
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", NO_RULES):
        decision = await resolve_forward_decision("low", False, None, None, "prometheus")
    assert decision.should_forward is False


@pytest.mark.asyncio
async def test_unknown_importance_does_not_forward():
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", NO_RULES):
        decision = await resolve_forward_decision("", False, None, None, "prometheus")
    assert decision.should_forward is False


# ── Noise suppression has the highest priority ──────────────────────────────────


@pytest.mark.asyncio
async def test_noise_suppression_overrides_high_importance():
    """When the noise-reduction decision suppresses forwarding, even high importance is not forwarded."""
    noise = _Noise(suppress=True, reason="衍生告警")
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", NO_RULES):
        decision = await resolve_forward_decision("high", False, noise, None, "prometheus")
    assert decision.should_forward is False
    assert decision.skip_code == "noise_suppressed"
    assert "Smart noise reduction suppressed forwarding" in (decision.skip_reason or "")


@pytest.mark.asyncio
async def test_noise_no_suppress_allows_forward():
    """When noise reduction does not suppress, a high alert plus a matching rule forwards normally."""
    noise = _Noise(suppress=False)
    rule = _FakeRule(1, importance="high")
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])):
        decision = await resolve_forward_decision("high", False, noise, None, "prometheus")
    assert decision.should_forward is True


# ── Duplicate alerts within the window ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_in_cooldown_skips():
    """A duplicate alert is not forwarded while within the cooldown period."""
    context = get_default_app_context()
    assert context is not None
    orig = context.config.retry.NOTIFICATION_COOLDOWN_SECONDS
    _set_config("NOTIFICATION_COOLDOWN_SECONDS", 300)
    try:
        event = _Event(last_notified_at=utcnow() - timedelta(seconds=10))
        with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", NO_RULES):
            decision = await resolve_forward_decision("high", True, None, event, "prometheus")
        assert decision.should_forward is False
        assert decision.skip_code == "cooldown"
        assert "Just notified, in cooldown" in (decision.skip_reason or "")
    finally:
        _set_config("NOTIFICATION_COOLDOWN_SECONDS", orig)


@pytest.mark.asyncio
async def test_duplicate_no_matched_rules_skips():
    """A duplicate alert with no matching rule is not forwarded."""
    context = get_default_app_context()
    assert context is not None
    orig_cooldown = context.config.retry.NOTIFICATION_COOLDOWN_SECONDS
    _set_config("NOTIFICATION_COOLDOWN_SECONDS", 1)
    _set_config("ENABLE_PERIODIC_REMINDER", False)
    try:
        event = _Event(last_notified_at=None)
        with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", NO_RULES):
            decision = await resolve_forward_decision("high", True, None, event, "prometheus")
        assert decision.should_forward is False
        assert "Duplicate alert: no matching forwarding rule" in (decision.skip_reason or "")
    finally:
        _set_config("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)
        _set_config("ENABLE_PERIODIC_REMINDER", False)


@pytest.mark.asyncio
async def test_duplicate_with_matched_rules_forwards():
    """When a rule matches and it is not within the cooldown period, a duplicate alert should forward."""
    context = get_default_app_context()
    assert context is not None
    orig_cooldown = context.config.retry.NOTIFICATION_COOLDOWN_SECONDS
    _set_config("NOTIFICATION_COOLDOWN_SECONDS", 1)
    _set_config("ENABLE_PERIODIC_REMINDER", False)
    rule = _FakeRule(1, importance="high")
    try:
        event = _Event(last_notified_at=None)
        with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])):
            decision = await resolve_forward_decision("high", True, None, event, "prometheus")
        assert decision.should_forward is True
    finally:
        _set_config("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)
        _set_config("ENABLE_PERIODIC_REMINDER", False)


@pytest.mark.asyncio
async def test_duplicate_periodic_reminder_triggers_forward():
    """When the periodic-reminder interval has elapsed, it should forward and set is_periodic_reminder=True."""
    context = get_default_app_context()
    assert context is not None
    orig_cooldown = context.config.retry.NOTIFICATION_COOLDOWN_SECONDS
    orig_interval = context.config.retry.REMINDER_INTERVAL_HOURS
    _set_config("NOTIFICATION_COOLDOWN_SECONDS", 1)
    _set_config("ENABLE_PERIODIC_REMINDER", True)
    _set_config("REMINDER_INTERVAL_HOURS", 6)
    rule = _FakeRule(1, importance="high")
    try:
        event = _Event(last_notified_at=utcnow() - timedelta(hours=7))
        with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])):
            decision = await resolve_forward_decision("high", True, None, event, "prometheus")
        assert decision.should_forward is True
        assert decision.is_periodic_reminder is True
    finally:
        _set_config("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)
        _set_config("ENABLE_PERIODIC_REMINDER", False)
        _set_config("REMINDER_INTERVAL_HOURS", orig_interval)


# ── Forwarding rule matching ────────────────────────────────────────────────────


class _FakeRule:
    def __init__(self, rid, importance="", source="", target_url="https://example.com", stop=False, duplicate="all"):
        self.id = rid
        self.name = f"rule-{rid}"
        self.match_event_type = ""
        self.match_importance = importance
        self.match_source = source
        self.match_duplicate = duplicate
        self.match_project = ""
        self.match_region = ""
        self.match_environment = ""
        self.match_payload = ""
        self.target_type = "webhook"
        self.target_url = target_url
        self.stop_on_match = stop

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "target_type": self.target_type,
            "target_url": self.target_url,
            "stop_on_match": self.stop_on_match,
        }


@pytest.mark.asyncio
async def test_matched_rule_enables_medium_forward():
    """When a rule matches, even medium importance should forward."""
    rule = _FakeRule(1, importance="medium")
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])):
        decision = await resolve_forward_decision("medium", False, None, None, "prometheus")
    assert decision.should_forward is True
    assert len(decision.matched_rules) == 1


@pytest.mark.asyncio
async def test_stop_on_match_limits_rules():
    """With stop_on_match=True, only the first rule is taken."""
    rule1 = _FakeRule(1, importance="high", target_url="https://a.com", stop=True)
    rule2 = _FakeRule(2, importance="high", target_url="https://b.com", stop=False)
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule1, rule2])):
        decision = await resolve_forward_decision("high", False, None, None, "prometheus")
    assert decision.should_forward is True
    assert len(decision.matched_rules) == 1
    assert decision.matched_rules[0].id == 1


@pytest.mark.asyncio
async def test_source_filter_excludes_non_matching_source():
    """When a rule restricts the source, a non-matching source does not trigger the rule."""
    rule = _FakeRule(1, importance="medium", source="grafana")
    with patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])):
        decision = await resolve_forward_decision("medium", False, None, None, "prometheus")
    # The prometheus source does not match the grafana rule; with no rule hit for medium, it does not forward
    assert decision.should_forward is False


# ── Manual silence ──────────────────────────────────────────────────────────────


class _FakeSilence:
    """Mirrors SilenceSnapshot's match attributes."""

    def __init__(self, sid=1, source="", importance="", event_type="", project="", region="", environment="", payload=""):
        self.id = sid
        self.match_source = source
        self.match_importance = importance
        self.match_event_type = event_type
        self.match_project = project
        self.match_region = region
        self.match_environment = environment
        self.match_payload = payload
        self.comment = ""


@pytest.mark.asyncio
async def test_silence_matching_source_suppresses_forward():
    """An active silence on the source mutes an otherwise-forwarded alert."""
    rule = _FakeRule(1, importance="high")
    silence = _FakeSilence(source="prometheus")
    with (
        patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])),
        patch("services.webhooks.forwarding_stage.get_cached_active_silences", _make_silences_loader([silence])),
    ):
        decision = await resolve_forward_decision("high", False, None, None, "prometheus")
    assert decision.should_forward is False
    assert decision.skip_code == "silenced"
    assert "Silenced" in (decision.skip_reason or "")


@pytest.mark.asyncio
async def test_silence_non_matching_source_does_not_suppress():
    """A silence scoped to another source leaves the alert forwarded."""
    rule = _FakeRule(1, importance="high")
    silence = _FakeSilence(source="grafana")
    with (
        patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])),
        patch("services.webhooks.forwarding_stage.get_cached_active_silences", _make_silences_loader([silence])),
    ):
        decision = await resolve_forward_decision("high", False, None, None, "prometheus")
    assert decision.should_forward is True


@pytest.mark.asyncio
async def test_silence_applies_to_duplicate_occurrences():
    """A silence mutes duplicate occurrences too (match_duplicate is fixed to 'all')."""
    rule = _FakeRule(1, importance="high")
    silence = _FakeSilence(source="prometheus")
    event = _Event(last_notified_at=utcnow() - timedelta(hours=7))
    with (
        patch("services.webhooks.forwarding_stage.get_cached_forward_rules", _make_rules_loader([rule])),
        patch("services.webhooks.forwarding_stage.get_cached_active_silences", _make_silences_loader([silence])),
    ):
        decision = await resolve_forward_decision("high", True, None, event, "prometheus")
    assert decision.should_forward is False
    assert decision.skip_code == "silenced"
