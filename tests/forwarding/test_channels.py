"""Tests for the ForwardChannel strategy registry."""

from __future__ import annotations

from types import SimpleNamespace

from services.forwarding.channels import resolve_channel


def _record(**kw: object) -> object:
    base = {
        "channel_name": None,
        "target_type": None,
        "target_url": "",
        "forward_data": None,
        "analysis_result": None,
        "formatted_payload": None,
        "is_periodic_reminder": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_openclaw_record_resolves_to_openclaw_channel() -> None:
    ch = resolve_channel(_record(target_type="openclaw"))
    assert ch.name == "openclaw"


def test_feishu_url_resolves_to_feishu_channel() -> None:
    ch = resolve_channel(_record(target_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc"))
    assert ch.name == "feishu"


def test_generic_url_resolves_to_webhook_channel() -> None:
    ch = resolve_channel(_record(target_url="https://example.com/hook"))
    assert ch.name == "webhook"


def test_openclaw_takes_precedence_over_feishu_url() -> None:
    # An openclaw record should never be treated as feishu even if its URL looked feishu-ish.
    ch = resolve_channel(_record(target_type="openclaw", target_url="https://open.feishu.cn/x"))
    assert ch.name == "openclaw"


def test_openclaw_followup_only_on_pending_result() -> None:
    ch = resolve_channel(_record(target_type="openclaw"))
    assert (
        ch.needs_followup_on_success(_record(target_type="openclaw"), {"status": "pending", "_pending": True}) is True
    )
    assert ch.needs_followup_on_success(_record(target_type="openclaw"), {"status": "success"}) is False


def test_http_channels_never_need_followup() -> None:
    feishu_ch = resolve_channel(_record(target_url="https://open.feishu.cn/x"))
    webhook_ch = resolve_channel(_record(target_url="https://example.com/hook"))
    assert feishu_ch.needs_followup_on_success(_record(), {"status": "success"}) is False
    assert webhook_ch.needs_followup_on_success(_record(), {"status": "success"}) is False
