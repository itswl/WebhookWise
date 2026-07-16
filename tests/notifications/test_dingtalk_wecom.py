"""DingTalk / WeCom bot channels: URL detection, payload shape, registry routing."""

from __future__ import annotations

from typing import cast

from contracts.webhook_payload import WebhookData
from models import ForwardOutbox
from services.forwarding.channels import resolve_channel
from services.notifications.dingtalk import build_dingtalk_markdown, is_dingtalk_url
from services.notifications.wecom import build_wecom_markdown, is_wecom_url
from services.webhooks.types import AnalysisResult

_DINGTALK_URL = "https://oapi.dingtalk.com/robot/send?access_token=abc123"
_WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"
_FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/xyz"


def test_url_detection_is_strict() -> None:
    assert is_dingtalk_url(_DINGTALK_URL)
    assert is_wecom_url(_WECOM_URL)
    # Wrong host / scheme / path never match; the two bots never cross-match.
    assert not is_dingtalk_url(_WECOM_URL)
    assert not is_wecom_url(_DINGTALK_URL)
    assert not is_dingtalk_url(_FEISHU_URL)
    assert not is_dingtalk_url("http://oapi.dingtalk.com/robot/send?access_token=abc")
    assert not is_wecom_url("https://qyapi.weixin.qq.com/other/path")
    assert not is_dingtalk_url("https://evil.example/oapi.dingtalk.com/robot/send")
    assert not is_dingtalk_url("")


def _inputs() -> tuple[WebhookData, AnalysisResult]:
    webhook_data = cast(
        WebhookData,
        {
            "source": "volcengine",
            "timestamp": "2026-07-16T08:00:00Z",
            "parsed_data": {"RuleName": "gpu-mem-high"},
        },
    )
    analysis = cast(
        AnalysisResult,
        {"importance": "high", "summary": "GPU 显存超过 95%", "impact_scope": "渲染任务暂停", "event_type": "alert"},
    )
    return webhook_data, analysis


def test_dingtalk_markdown_payload_shape() -> None:
    webhook_data, analysis = _inputs()
    payload = build_dingtalk_markdown(webhook_data, analysis)
    assert payload["msgtype"] == "markdown"
    md = payload["markdown"]
    assert md["title"] == "📡 告警通知"
    assert "GPU 显存超过 95%" in md["text"]
    assert "gpu-mem-high" in md["text"]
    assert "🔴 高" in md["text"]


def test_wecom_markdown_payload_shape_and_reminder_prefix() -> None:
    webhook_data, analysis = _inputs()
    payload = build_wecom_markdown(webhook_data, analysis, is_periodic_reminder=True)
    assert payload["msgtype"] == "markdown"
    content = payload["markdown"]["content"]
    assert content.startswith("### 🔁 [周期提醒] 📡 告警通知")
    assert "影响范围" in content
    assert len(content) <= 3500


def _record(url: str) -> ForwardOutbox:
    return ForwardOutbox(
        idempotency_key=f"k-{url[:20]}",
        target_type="webhook",
        target_url=url,
        forward_data={"source": "volcengine", "parsed_data": {"RuleName": "r"}},
        analysis_result={"importance": "low", "summary": "s"},
    )


def test_registry_routes_bot_urls_to_their_channels() -> None:
    assert resolve_channel(_record(_DINGTALK_URL)).name == "dingtalk"
    assert resolve_channel(_record(_WECOM_URL)).name == "wecom"
    assert resolve_channel(_record(_FEISHU_URL)).name == "feishu"
    assert resolve_channel(_record("https://example.com/hook")).name == "webhook"


def test_bot_channels_build_native_payload_not_envelope() -> None:
    from services.forwarding.channels import _build_bot_markdown_payload

    record = _record(_DINGTALK_URL)
    payload = _build_bot_markdown_payload(record, build_dingtalk_markdown)
    assert payload["msgtype"] == "markdown"

    # Without forward_data/analysis the builder falls back to the generic path
    # instead of crashing (system records never target bot URLs today).
    bare = ForwardOutbox(idempotency_key="bare", target_type="webhook", target_url=_DINGTALK_URL)
    assert _build_bot_markdown_payload(bare, build_dingtalk_markdown) == {}
