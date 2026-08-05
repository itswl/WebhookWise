"""The delivery-split pilot channel: rendered card → hookrelay door, signed.

Pins the four promises the pilot stands on: the door never gets unsigned
bytes, the signature covers EXACTLY the bytes sent, the card ships finished
(content-blind relay), and refusal/unreachability map to the outbox retry
semantics rather than raising.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from services.forwarding.channels import _FeishuRelayChannel, resolve_channel


def _record(**overrides: Any) -> Any:
    base: dict[str, Any] = {
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

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> None: ...

    async def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> _FakeResponse:
        _FakeClient.sent.append({"url": url, "content": content, "headers": headers})
        if _FakeClient.raise_error is not None:
            raise _FakeClient.raise_error
        return _FakeClient.response


@pytest.fixture
def relay_env(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    monkeypatch.setattr(temp_config.notifications, "FORWARD_RELAY_SECRET", "door-secret")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.sent = []
    _FakeClient.response = _FakeResponse(200)
    _FakeClient.raise_error = None


def test_registry_resolves_relay_records() -> None:
    assert resolve_channel(_record()).name == "feishu_relay"
    # And never steals records belonging to other channels.
    assert resolve_channel(_record(target_type="openclaw", channel_name="openclaw")).name == "openclaw"


@pytest.mark.asyncio
async def test_signature_covers_the_exact_bytes_sent(relay_env: None) -> None:
    result = await _FeishuRelayChannel().deliver(_record())

    assert result["status"] == "success"
    sent = _FakeClient.sent[0]
    assert sent["url"] == "http://hookrelay:8100/hook/ww-notify"
    expected = hmac.new(b"door-secret", sent["content"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Hook-Signature"] == expected
    body = json.loads(sent["content"].decode())
    # The FINISHED message rides under "notification" — the relay never
    # rebuilds content, so the interactive card must arrive whole.
    assert body["notification"]["msg_type"] == "interactive"
    assert body["notification"]["card"]["header"]["title"]["content"] == "事故"


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
async def test_alert_records_render_the_real_card(relay_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.notifications import feishu

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
    assert body["notification"] == sentinel, "the relay receives WW's OWN card, not a re-rendering"


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
