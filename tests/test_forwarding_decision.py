from datetime import datetime, timedelta

from core.config import Config
from core.runtime_config import _KEY_TO_SUBCONFIG
from services.pipeline_forward import decide_forwarding


class _Event:
    def __init__(self, last_notified_at=None, duplicate_count=1):
        self.last_notified_at = last_notified_at
        self.duplicate_count = duplicate_count


def _set_config(**kwargs):
    originals = {}
    for k in kwargs:
        sub_name = _KEY_TO_SUBCONFIG.get(k)
        if sub_name:
            originals[k] = getattr(getattr(Config, sub_name), k)
    for k, v in kwargs.items():
        sub_name = _KEY_TO_SUBCONFIG.get(k)
        if sub_name:
            setattr(getattr(Config, sub_name), k, v)
    return originals


def _restore_config(originals):
    for k, v in originals.items():
        sub_name = _KEY_TO_SUBCONFIG.get(k)
        if sub_name:
            setattr(getattr(Config, sub_name), k, v)


async def test_non_high_never_forwarded():
    decision = await decide_forwarding("low", False, False, None, None, None)
    assert decision.should_forward is False
    assert "非高风险事件不自动转发" in (decision.skip_reason or "")


async def test_beyond_window_respects_forward_switch():
    originals = _set_config(FORWARD_AFTER_TIME_WINDOW=False)
    try:
        decision = await decide_forwarding("high", False, True, None, _Event(), 1)
        assert decision.should_forward is False
        assert "配置跳过转发" in (decision.skip_reason or "")
    finally:
        _restore_config(originals)


async def test_beyond_window_recently_notified_skips():
    originals = _set_config(FORWARD_AFTER_TIME_WINDOW=True, NOTIFICATION_COOLDOWN_SECONDS=60)
    try:
        event = _Event(last_notified_at=datetime.now() - timedelta(seconds=10))
        decision = await decide_forwarding("high", False, True, None, event, 1)
        assert decision.should_forward is False
        assert "刚刚已转发" in (decision.skip_reason or "")
    finally:
        _restore_config(originals)


async def test_duplicate_periodic_reminder_triggers_forward():
    originals = _set_config(
        ENABLE_PERIODIC_REMINDER=True,
        REMINDER_INTERVAL_HOURS=6,
        FORWARD_DUPLICATE_ALERTS=False,
        NOTIFICATION_COOLDOWN_SECONDS=1,
    )
    try:
        event = _Event(last_notified_at=datetime.now() - timedelta(hours=7), duplicate_count=5)
        decision = await decide_forwarding("high", True, False, None, event, 1)
        assert decision.should_forward is True
        assert decision.is_periodic_reminder is True
    finally:
        _restore_config(originals)


async def test_duplicate_no_periodic_and_disabled_duplicate_forward_skips():
    originals = _set_config(ENABLE_PERIODIC_REMINDER=False, FORWARD_DUPLICATE_ALERTS=False)
    try:
        event = _Event(last_notified_at=None)
        decision = await decide_forwarding("high", True, False, None, event, 1)
        assert decision.should_forward is False
        assert "配置跳过转发" in (decision.skip_reason or "")
    finally:
        _restore_config(originals)


async def test_duplicate_no_periodic_and_enabled_duplicate_forward_forwards():
    originals = _set_config(ENABLE_PERIODIC_REMINDER=False, FORWARD_DUPLICATE_ALERTS=True)
    try:
        event = _Event(last_notified_at=None)
        decision = await decide_forwarding("high", True, False, None, event, 1)
        assert decision.should_forward is True
        assert decision.is_periodic_reminder is False
    finally:
        _restore_config(originals)


async def test_noise_reduction_can_suppress_forwarding():
    class _Noise:
        suppress_forward = True
        reason = "关联到根因告警#100，抑制衍生通知"

    decision = await decide_forwarding("high", False, False, _Noise(), None, None)
    assert decision.should_forward is False
    assert "智能降噪抑制转发" in (decision.skip_reason or "")
