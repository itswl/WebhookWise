from __future__ import annotations

import pytest

from services.analysis.ai_policies import AIErrorNotificationPolicy


class _FakeChannel:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict[str, object]]] = []

    async def send_card(self, target_url: str, card: dict[str, object]) -> bool:
        self.cards.append((target_url, card))
        return True


@pytest.mark.asyncio
async def test_ai_error_alert_cooldown_groups_volatile_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations import ai_error_notifications as notifications

    channel = _FakeChannel()
    seen_locks: set[str] = set()

    async def fake_set_nx_ex(key: str, value: str, ttl_seconds: int) -> bool:
        assert value == "1"
        assert ttl_seconds == 300
        if key in seen_locks:
            return False
        seen_locks.add(key)
        return True

    monkeypatch.setattr("core.redis_client.redis_set_nx_ex", fake_set_nx_ex)
    monkeypatch.setattr(notifications, "build_notification_channels", lambda **_: [channel])
    monkeypatch.setattr(notifications, "find_notification_channel", lambda target_url, channels: channel)

    policy = AIErrorNotificationPolicy(
        enabled=True,
        target_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        cooldown_seconds=300,
        timeout_seconds=3,
    )
    webhook_data = {"source": "volcengine", "parsed_data": {"RuleName": "OpenRouterDown"}}

    await notifications.send_ai_error_alert(
        webhook_data,
        "ai_error: OpenRouter 503 overloaded request_id=abc123456789",
        is_degraded=True,
        policy=policy,
    )
    await notifications.send_ai_error_alert(
        webhook_data,
        "ai_error: OpenRouter 503 overloaded request_id=def987654321",
        is_degraded=True,
        policy=policy,
    )

    assert len(channel.cards) == 1
    assert len(seen_locks) == 1


def test_ai_error_notification_policy_reads_runtime_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Config
    from services.analysis.ai_policies import AIErrorNotificationPolicy

    monkeypatch.setattr(Config.forwarding, "ENABLE_FORWARD", True)
    monkeypatch.setattr(Config.forwarding, "FORWARD_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/test")
    monkeypatch.setattr(Config.notifications, "AI_ERROR_NOTIFICATION_COOLDOWN_SECONDS", 123)
    monkeypatch.setattr(Config.notifications, "AI_ERROR_NOTIFICATION_TIMEOUT_SECONDS", 7)

    policy = AIErrorNotificationPolicy.from_config()

    assert policy.enabled is True
    assert policy.target_url.endswith("/test")
    assert policy.cooldown_seconds == 123
    assert policy.timeout_seconds == 7
