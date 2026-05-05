"""
tests/test_forwarding_decision.py
==================================
测试 pipeline._decide_forwarding() 转发决策逻辑。
这些是核心业务规则：哪些告警需要转发、哪些要跳过、为什么。
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import Config
from services.pipeline import _decide_forwarding


class _Event:
    """模拟 WebhookEvent 的最小对象。"""
    def __init__(self, last_notified_at=None):
        self.last_notified_at = last_notified_at


class _Noise:
    """模拟 NoiseReductionContext。"""
    def __init__(self, suppress=False, reason=""):
        self.suppress_forward = suppress
        self.reason = reason


def _make_session_scope(rules: list):
    """构造一个返回指定规则的 session_scope async context manager。"""
    mock_sess = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rules
    mock_sess.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_sess

    return _scope


NO_RULES = _make_session_scope([])


# ── 新告警（非重复、非超窗）────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_high_importance_new_alert_forwards():
    """高重要性新告警默认应该转发。"""
    with patch("services.pipeline.session_scope", NO_RULES):
        decision = await _decide_forwarding("high", False, False, None, None, "prometheus")
    assert decision.should_forward is True


@pytest.mark.asyncio
async def test_medium_importance_no_rules_does_not_forward():
    """无规则、medium 重要性不自动转发。"""
    with patch("services.pipeline.session_scope", NO_RULES):
        decision = await _decide_forwarding("medium", False, False, None, None, "prometheus")
    assert decision.should_forward is False
    assert "非高风险" in (decision.skip_reason or "")


@pytest.mark.asyncio
async def test_low_importance_no_rules_does_not_forward():
    with patch("services.pipeline.session_scope", NO_RULES):
        decision = await _decide_forwarding("low", False, False, None, None, "prometheus")
    assert decision.should_forward is False


@pytest.mark.asyncio
async def test_unknown_importance_does_not_forward():
    with patch("services.pipeline.session_scope", NO_RULES):
        decision = await _decide_forwarding("", False, False, None, None, "prometheus")
    assert decision.should_forward is False


# ── 噪声抑制优先级最高 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noise_suppression_overrides_high_importance():
    """降噪决策抑制转发时，即使是 high 重要性也不转发。"""
    noise = _Noise(suppress=True, reason="衍生告警")
    with patch("services.pipeline.session_scope", NO_RULES):
        decision = await _decide_forwarding("high", False, False, noise, None, "prometheus")
    assert decision.should_forward is False
    assert "智能降噪抑制转发" in (decision.skip_reason or "")


@pytest.mark.asyncio
async def test_noise_no_suppress_allows_forward():
    """降噪不抑制时，high 告警正常转发。"""
    noise = _Noise(suppress=False)
    with patch("services.pipeline.session_scope", NO_RULES):
        decision = await _decide_forwarding("high", False, False, noise, None, "prometheus")
    assert decision.should_forward is True


# ── 窗口内重复告警 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_in_cooldown_skips():
    """重复告警在冷却期内不转发。"""
    orig = Config.retry.NOTIFICATION_COOLDOWN_SECONDS
    Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", 300)
    try:
        event = _Event(last_notified_at=datetime.now() - timedelta(seconds=10))
        with patch("services.pipeline.session_scope", NO_RULES):
            decision = await _decide_forwarding("high", True, False, None, event, "prometheus")
        assert decision.should_forward is False
        assert "刚刚已转发" in (decision.skip_reason or "")
    finally:
        Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", orig)


@pytest.mark.asyncio
async def test_duplicate_forward_disabled_skips():
    """FORWARD_DUPLICATE_ALERTS=False 时窗口内重复不转发。"""
    orig_cooldown = Config.retry.NOTIFICATION_COOLDOWN_SECONDS
    Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", 1)
    Config.set_override("FORWARD_DUPLICATE_ALERTS", False)
    Config.set_override("ENABLE_PERIODIC_REMINDER", False)
    try:
        event = _Event(last_notified_at=None)
        with patch("services.pipeline.session_scope", NO_RULES):
            decision = await _decide_forwarding("high", True, False, None, event, "prometheus")
        assert decision.should_forward is False
        assert "跳过转发" in (decision.skip_reason or "")
    finally:
        Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)
        Config.set_override("FORWARD_DUPLICATE_ALERTS", False)
        Config.set_override("ENABLE_PERIODIC_REMINDER", False)


@pytest.mark.asyncio
async def test_duplicate_forward_enabled_forwards():
    """FORWARD_DUPLICATE_ALERTS=True 且不在冷却期，应转发。"""
    orig_cooldown = Config.retry.NOTIFICATION_COOLDOWN_SECONDS
    Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", 1)
    Config.set_override("FORWARD_DUPLICATE_ALERTS", True)
    Config.set_override("ENABLE_PERIODIC_REMINDER", False)
    try:
        event = _Event(last_notified_at=None)
        with patch("services.pipeline.session_scope", _make_session_scope([])):
            decision = await _decide_forwarding("high", True, False, None, event, "prometheus")
        assert decision.should_forward is True
    finally:
        Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)
        Config.set_override("FORWARD_DUPLICATE_ALERTS", False)
        Config.set_override("ENABLE_PERIODIC_REMINDER", False)


@pytest.mark.asyncio
async def test_duplicate_periodic_reminder_triggers_forward():
    """周期提醒间隔已到，应转发并标记 is_periodic_reminder=True。"""
    orig_cooldown = Config.retry.NOTIFICATION_COOLDOWN_SECONDS
    orig_interval = Config.retry.REMINDER_INTERVAL_HOURS
    Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", 1)
    Config.set_override("FORWARD_DUPLICATE_ALERTS", False)
    Config.set_override("ENABLE_PERIODIC_REMINDER", True)
    Config.set_override("REMINDER_INTERVAL_HOURS", 6)
    try:
        event = _Event(last_notified_at=datetime.now() - timedelta(hours=7))
        with patch("services.pipeline.session_scope", _make_session_scope([])):
            decision = await _decide_forwarding("high", True, False, None, event, "prometheus")
        assert decision.should_forward is True
        assert decision.is_periodic_reminder is True
    finally:
        Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)
        Config.set_override("FORWARD_DUPLICATE_ALERTS", False)
        Config.set_override("ENABLE_PERIODIC_REMINDER", False)
        Config.set_override("REMINDER_INTERVAL_HOURS", orig_interval)


# ── 超窗口重复告警 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_beyond_window_forward_disabled_skips():
    """FORWARD_AFTER_TIME_WINDOW=False 时超窗告警不转发。"""
    Config.set_override("FORWARD_AFTER_TIME_WINDOW", False)
    try:
        with patch("services.pipeline.session_scope", _make_session_scope([])):
            decision = await _decide_forwarding("high", False, True, None, _Event(), "prometheus")
        assert decision.should_forward is False
        assert "不转发" in (decision.skip_reason or "")
    finally:
        Config.set_override("FORWARD_AFTER_TIME_WINDOW", True)


@pytest.mark.asyncio
async def test_beyond_window_forward_enabled_and_cooldown_expired_forwards():
    """FORWARD_AFTER_TIME_WINDOW=True 且冷却期已过，超窗告警应转发。"""
    orig_cooldown = Config.retry.NOTIFICATION_COOLDOWN_SECONDS
    Config.set_override("FORWARD_AFTER_TIME_WINDOW", True)
    Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", 1)
    try:
        event = _Event(last_notified_at=datetime.now() - timedelta(seconds=60))
        with patch("services.pipeline.session_scope", _make_session_scope([])):
            decision = await _decide_forwarding("high", False, True, None, event, "prometheus")
        assert decision.should_forward is True
    finally:
        Config.set_override("FORWARD_AFTER_TIME_WINDOW", True)
        Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)


@pytest.mark.asyncio
async def test_beyond_window_in_cooldown_skips():
    """FORWARD_AFTER_TIME_WINDOW=True 但仍在冷却期，不转发。"""
    orig_cooldown = Config.retry.NOTIFICATION_COOLDOWN_SECONDS
    Config.set_override("FORWARD_AFTER_TIME_WINDOW", True)
    Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", 300)
    try:
        event = _Event(last_notified_at=datetime.now() - timedelta(seconds=10))
        with patch("services.pipeline.session_scope", _make_session_scope([])):
            decision = await _decide_forwarding("high", False, True, None, event, "prometheus")
        assert decision.should_forward is False
        assert "刚刚已转发" in (decision.skip_reason or "")
    finally:
        Config.set_override("FORWARD_AFTER_TIME_WINDOW", True)
        Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", orig_cooldown)


# ── 转发规则匹配 ──────────────────────────────────────────────────────────────


class _FakeRule:
    def __init__(self, rid, importance="", source="", target_url="https://example.com", stop=False):
        self.id = rid
        self.name = f"rule-{rid}"
        self.match_importance = importance
        self.match_source = source
        self.target_type = "webhook"
        self.target_url = target_url
        self.stop_on_match = stop

    def to_dict(self):
        return {
            "id": self.id, "name": self.name,
            "target_type": self.target_type, "target_url": self.target_url,
            "stop_on_match": self.stop_on_match,
        }


@pytest.mark.asyncio
async def test_matched_rule_enables_medium_forward():
    """有匹配规则时，即使是 medium 重要性也应转发。"""
    rule = _FakeRule(1, importance="medium")
    with patch("services.pipeline.session_scope", _make_session_scope([rule])):
        decision = await _decide_forwarding("medium", False, False, None, None, "prometheus")
    assert decision.should_forward is True
    assert len(decision.matched_rules) == 1


@pytest.mark.asyncio
async def test_stop_on_match_limits_rules():
    """stop_on_match=True 时只取第一条规则。"""
    rule1 = _FakeRule(1, importance="high", target_url="https://a.com", stop=True)
    rule2 = _FakeRule(2, importance="high", target_url="https://b.com", stop=False)
    with patch("services.pipeline.session_scope", _make_session_scope([rule1, rule2])):
        decision = await _decide_forwarding("high", False, False, None, None, "prometheus")
    assert decision.should_forward is True
    assert len(decision.matched_rules) == 1
    assert decision.matched_rules[0]["id"] == 1


@pytest.mark.asyncio
async def test_source_filter_excludes_non_matching_source():
    """规则限定 source 时，不匹配的来源不触发规则。"""
    rule = _FakeRule(1, importance="medium", source="grafana")
    with patch("services.pipeline.session_scope", _make_session_scope([rule])):
        decision = await _decide_forwarding("medium", False, False, None, None, "prometheus")
    # prometheus 来源不匹配 grafana 规则，medium 无规则命中，不转发
    assert decision.should_forward is False
