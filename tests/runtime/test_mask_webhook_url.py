"""Tests for webhook-URL secret masking (dashboard display safety)."""

from __future__ import annotations

from core.sensitive_data import mask_webhook_url


def test_masks_feishu_hook_token_keeps_version_and_tail() -> None:
    masked = mask_webhook_url(
        "https://open.feishu.cn/open-apis/bot/v2/hook/cf37791b-73eb-4bcc-b571-b824cce0f805"
    )
    assert masked == "https://open.feishu.cn/open-apis/bot/v2/hook/****f805"
    # The full token must not survive.
    assert "cf37791b" not in masked
    # Structural segments (the API version) are preserved for recognizability.
    assert "/bot/v2/hook/" in masked


def test_masks_query_access_token_without_encoding_noise() -> None:
    masked = mask_webhook_url("https://oapi.dingtalk.com/robot/send?access_token=abcdef123456")
    assert masked == "https://oapi.dingtalk.com/robot/send?access_token=****3456"
    assert "abcdef123456" not in masked
    assert "%2A" not in masked  # '*' not percent-encoded


def test_masks_generic_webhook_path_token() -> None:
    masked = mask_webhook_url("https://example.com/webhook/sometoken99")
    assert masked == "https://example.com/webhook/****en99"


def test_leaves_secretless_url_recognizable() -> None:
    url = "https://generic.example.com/notify"
    assert mask_webhook_url(url) == url


def test_short_token_fully_masked() -> None:
    assert mask_webhook_url("https://x.com/hook/abc") == "https://x.com/hook/****"


def test_handles_empty_and_none() -> None:
    assert mask_webhook_url("") == ""
    assert mask_webhook_url(None) is None
