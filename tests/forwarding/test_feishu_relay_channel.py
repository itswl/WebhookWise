"""The delivery-split pilot channel: rendered card → hookrelay door, signed.

Pins the four promises the pilot stands on: the door never gets unsigned
bytes, the signature covers EXACTLY the bytes sent, the card ships finished
(content-blind relay), and refusal/unreachability map to the outbox retry
semantics rather than raising.
"""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from services.forwarding.channels import _FeishuRelayChannel, resolve_channel


def _record(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "id": 42,
        "webhook_event_id": 7,
        "rule_name": "所有告警通知",
        "channel_name": None,
        "target_type": "feishu_relay",
        "target_url": "http://hookrelay:8100/hook/ww-notify",
        "formatted_payload": {"msg_type": "interactive", "card": {"header": {"title": {"content": "事故"}}}},
        "forward_data": None,
        "analysis_result": None,
        "is_periodic_reminder": False,
        "idempotency_key": "k1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "{}"):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    sent: list[dict[str, Any]] = []
    response: _FakeResponse = _FakeResponse(200)
    raise_error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    async def post(
        self, url: str, *, content: bytes, headers: dict[str, str], timeout: float | None = None
    ) -> _FakeResponse:
        _FakeClient.sent.append({"url": url, "content": content, "headers": headers, "timeout": timeout})
        if _FakeClient.raise_error is not None:
            raise _FakeClient.raise_error
        return _FakeClient.response


@pytest.fixture
def relay_env(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    import core.http_client as core_http_client

    monkeypatch.setattr(temp_config.notifications, "FORWARD_RELAY_SECRET", "door-secret")
    monkeypatch.setattr(temp_config.notifications, "FORWARD_RELAY_MODE", "processed")
    # The channel now shares the internal-hop client instead of constructing a
    # throwaway httpx.AsyncClient per delivery; the fake stands in for it.
    monkeypatch.setattr(core_http_client, "get_deep_analysis_client", lambda: _FakeClient())
    _FakeClient.sent = []
    _FakeClient.response = _FakeResponse(200)
    _FakeClient.raise_error = None


def test_registry_resolves_relay_records() -> None:
    assert resolve_channel(_record()).name == "feishu_relay"
    # And never steals records belonging to other channels.
    assert resolve_channel(_record(target_type="deep_analysis", channel_name="deep_analysis")).name == "deep_analysis"


@pytest.mark.asyncio
async def test_signature_covers_the_exact_bytes_sent(relay_env: None) -> None:
    result = await _FeishuRelayChannel().deliver(_record())

    assert result["status"] == "success"
    sent = _FakeClient.sent[0]
    assert sent["url"] == "http://hookrelay:8100/hook/ww-notify"
    # Timestamped: the door verifies "{ts}.{body}" within a freshness window,
    # so a captured delivery cannot be replayed into the group later.
    stamp = sent["headers"]["X-Hook-Timestamp"]
    expected = hmac.new(b"door-secret", stamp.encode() + b"." + sent["content"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Hook-Signature"] == expected
    assert abs(int(stamp) - int(time.time())) < 60, "the stamp must be now, not a constant"
    # At-least-once made safe for the receiver.
    assert sent["headers"]["X-Hook-Idempotency-Key"].startswith("outbox-")
    body = json.loads(sent["content"].decode())
    # The RESULT, not a rendering of it: this service no longer decides what a
    # Feishu card looks like — the relay does, per downstream.
    assert "notification" not in body, "processed mode must not ship a rendered card"
    assert set(body) == {"meta", "analysis", "identity", "links"}
    assert body["meta"]["event_id"] == 7 and body["meta"]["outbox_id"] == 42


@pytest.mark.asyncio
async def test_unconfigured_secret_refuses_to_send(
    relay_env: None, monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    monkeypatch.setattr(temp_config.notifications, "FORWARD_RELAY_SECRET", "")
    result = await _FeishuRelayChannel().deliver(_record())
    assert result["status"] == "failed" and result["retryable"] is False
    assert _FakeClient.sent == [], "no unsigned bytes may ever leave for the door"


@pytest.mark.asyncio
async def test_relay_5xx_is_retryable_4xx_is_not(relay_env: None) -> None:
    _FakeClient.response = _FakeResponse(503, "busy")
    result = await _FeishuRelayChannel().deliver(_record())
    assert result["status"] == "failed" and result["retryable"] is True

    _FakeClient.response = _FakeResponse(401, "bad signature")
    result = await _FeishuRelayChannel().deliver(_record())
    assert result["status"] == "failed" and result["retryable"] is False


@pytest.mark.asyncio
async def test_unreachable_relay_is_retryable(relay_env: None) -> None:
    _FakeClient.raise_error = httpx.ConnectError("down")
    result = await _FeishuRelayChannel().deliver(_record())
    assert result["status"] == "failed" and result["retryable"] is True


@pytest.mark.asyncio
async def test_processed_mode_sends_judgement_not_presentation(relay_env: None) -> None:
    """What crosses the wire is what this service DECIDED: the analysis, the
    identity as data, the links as data. No colours, no markdown, no card
    schema — those are the pipe's business now."""
    record = _record(
        formatted_payload=None,
        forward_data={
            "source": "grafana",
            "timestamp": "2026-08-07 10:32:50",
            "parsed_data": {"RuleName": "示例充值超限告警", "project": "demo-alarm", "environment": "prod"},
        },
        analysis_result={
            "summary": "9 分钟内 3 次大额充值",
            "importance": "high",
            "event_type": "business",
            "impact_scope": "未观察到服务影响",
        },
    )
    result = await _FeishuRelayChannel().deliver(record)

    assert result["status"] == "success"
    body = json.loads(_FakeClient.sent[0]["content"].decode())
    assert body["analysis"]["summary"] == "9 分钟内 3 次大额充值"
    assert body["analysis"]["impact_scope"] == "未观察到服务影响"
    # Identity as DATA, not a pre-rendered breadcrumb: separators and order are
    # formatting, and formatting moved to the pipe.
    assert body["identity"] == {"project": "demo-alarm", "environment": "prod", "rule": "示例充值超限告警"}
    assert body["meta"]["is_recovery"] is False and body["meta"]["timestamp"] == "2026-08-07 10:32:50"
    assert "card" not in json.dumps(body), "no presentation crossed the wire"


async def test_card_mode_remains_available_as_the_rollback(
    relay_env: None, monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    """Until the relay's rendering is proven in the group, flipping one env var
    puts this service back in charge of the card."""
    from services.notifications import feishu

    monkeypatch.setattr(temp_config.notifications, "FORWARD_RELAY_MODE", "card")
    sentinel = {"msg_type": "interactive", "card": {"built": "by-ww"}}
    monkeypatch.setattr(feishu, "build_feishu_card", lambda *a, **k: dict(sentinel))

    record = _record(
        formatted_payload=None,
        forward_data={"source": "grafana", "parsed_data": {"RuleName": "充值"}},
        analysis_result={"summary": "s", "importance": "high"},
    )
    result = await _FeishuRelayChannel().deliver(record)

    assert result["status"] == "success"
    body = json.loads(_FakeClient.sent[0]["content"].decode())
    assert body["notification"] == sentinel, "the relay receives WW's OWN card, untouched"


@pytest.mark.asyncio
async def test_rule_validation_accepts_private_relay_urls() -> None:
    """The public-IP rule guard must not reject the relay door: it is private
    BY DESIGN (guarded by admin-write + HMAC), unlike user-supplied forward
    targets. Shape is still enforced."""
    from api.v1.forwarding import _validated_target_url
    from core.url_security import UnsafeTargetUrlError

    assert await _validated_target_url("feishu_relay", "http://hookrelay:8100/hook/ww-notify") == (
        "http://hookrelay:8100/hook/ww-notify"
    )
    with pytest.raises(UnsafeTargetUrlError):
        await _validated_target_url("feishu_relay", "ftp://hookrelay:8100/x")


@pytest.mark.asyncio
async def test_rule_validation_accepts_feishu_app_chat_targets() -> None:
    """feishu-app:// is not a URL; the generic validator rejected the scheme
    outright, so the feishu_app channel could never be targeted by a rule
    saved through the API despite being a fully implemented channel."""
    from core.url_security import UnsafeTargetUrlError
    from services.forwarding.target_validation import validated_target_url

    assert await validated_target_url("feishu_app", "feishu-app://oc_incidents") == "feishu-app://oc_incidents"
    with pytest.raises(UnsafeTargetUrlError):
        await validated_target_url("feishu_app", "feishu-app://")
    with pytest.raises(UnsafeTargetUrlError):
        await validated_target_url("feishu_app", "https://example.com/not-a-chat")


@pytest.mark.asyncio
async def test_rule_test_button_uses_the_real_channel(relay_env: None) -> None:
    """The test button must exercise the SAME path a real delivery takes.

    It used to fall through to the generic webhook sender, which posts raw
    JSON at the relay door with no signature — the door answers 401 and the
    button reports a delivery failure for a rule that in fact delivers fine.
    A test that lies about a healthy rule is worse than no test.
    """
    from services.forwarding.remote import send_forward_rule_test

    result = await send_forward_rule_test(
        rule_name="所有告警通知",
        target_url="http://hookrelay:8100/hook/ww-notify",
        target_type="feishu_relay",
    )

    assert result["status"] == "success"
    sent = _FakeClient.sent[0]
    assert sent["url"] == "http://hookrelay:8100/hook/ww-notify"
    # Signed and timestamped exactly like a real delivery.
    assert sent["headers"]["X-Hook-Signature"] and sent["headers"]["X-Hook-Timestamp"]
    body = json.loads(sent["content"].decode())
    assert body["analysis"]["summary"], "the test exercises the real processed envelope"


@pytest.mark.asyncio
async def test_meta_carries_identity_and_echoes_the_correlation_id(relay_env: None) -> None:
    """The relay stamps X-Request-Id on the way in; echoing it back in meta is
    what links the outbound half of a round trip to the inbound one."""
    record = _record(
        formatted_payload={"msg_type": "text"},
        forward_data={
            "source": "grafana",
            "parsed_data": {"RuleName": "示例充值超限告警"},
            "headers": {"x-request-id": "hr-86"},
        },
        analysis_result=None,
    )
    result = await _FeishuRelayChannel().deliver(record)

    assert result["status"] == "success"
    meta = json.loads(_FakeClient.sent[0]["content"].decode())["meta"]
    assert meta["alert_name"] == "示例充值超限告警", "the ledger must be searchable by alert"
    assert meta["source"] == "grafana" and meta["rule_name"] == "所有告警通知"
    assert meta["correlation_id"] == "hr-86", "the round trip is linkable"
