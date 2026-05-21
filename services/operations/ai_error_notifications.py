"""Operational notifications for AI analysis errors."""

import hashlib
import logging
import re
from typing import Any

from adapters.plugins.feishu_card import build_ai_error_card
from core.logger import mask_url
from core.redis_keys import ai_error_alert_lock
from services.analysis.ai_policies import AIErrorNotificationPolicy
from services.notifications.factory import build_notification_channels, find_notification_channel
from services.operations.policies import FeishuNotificationPolicy
from services.webhooks.types import WebhookData

logger = logging.getLogger("webhook_service.ai_error_notifications")

_HEX_OR_UUID_RE = re.compile(r"\b[0-9a-f]{8,}(?:-[0-9a-f]{4,})*\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+\b")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(429|4\d\d|5\d\d)\b")
_ERROR_CLASS_RE = re.compile(r"\b([A-Za-z_]*(?:Error|Exception|Timeout))\b")


def _notification_dedupe_key(error_reason: str, *, is_degraded: bool) -> str:
    text = " ".join(str(error_reason or "unknown").split())
    lowered = text.lower()
    category, _, detail = lowered.partition(":")
    if category not in {"ai_error", "llm_policy_refusal"}:
        category = "ai_error"
        detail = lowered

    provider = "openrouter" if "openrouter" in lowered else "ai_provider"
    status = _STATUS_RE.search(lowered)
    error_class = _ERROR_CLASS_RE.search(text)

    normalized_detail = _URL_RE.sub("<url>", detail)
    normalized_detail = _HEX_OR_UUID_RE.sub("<id>", normalized_detail)
    normalized_detail = _NUMBER_RE.sub("<n>", normalized_detail)
    normalized_detail = normalized_detail[:200]

    parts = [
        category,
        "degraded" if is_degraded else "error",
        provider,
        status.group(1) if status else "no_status",
        error_class.group(1).lower() if error_class else "no_class",
        normalized_detail,
    ]
    digest = hashlib.md5("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"{category}:{provider}:{status.group(1) if status else 'no_status'}:{digest}"


async def send_ai_error_alert(
    webhook_data: WebhookData,
    error_reason: str,
    *,
    is_degraded: bool = False,
    policy: AIErrorNotificationPolicy | None = None,
    http_client: Any | None = None,
) -> None:
    """Send a rate-limited AI error/degradation alert to the configured operations target."""
    policy = policy or AIErrorNotificationPolicy.from_config()
    if not policy.enabled or not policy.target_url:
        return

    try:
        from core.redis_client import redis_set_nx_ex

        dedupe_key = _notification_dedupe_key(error_reason, is_degraded=is_degraded)
        error_hash = hashlib.md5(dedupe_key.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        lock_key = ai_error_alert_lock(error_hash)
        if not await redis_set_nx_ex(lock_key, "1", policy.cooldown_seconds):
            return

        channels = build_notification_channels(
            http_client=http_client,
            feishu_policy=FeishuNotificationPolicy(timeout_seconds=policy.timeout_seconds),
        )
        channel = find_notification_channel(policy.target_url, channels)
        if channel is None:
            logger.debug("[AIErrorNotify] 通知目标没有匹配渠道 target=%s", mask_url(policy.target_url))
            return
        logger.info("[AIErrorNotify] 发送 AI 错误通知 target=%s degraded=%s", mask_url(policy.target_url), is_degraded)
        success = await channel.send_card(
            policy.target_url,
            build_ai_error_card(webhook_data, error_reason, is_degraded=is_degraded),
        )
        logger.info("[AIErrorNotify] AI 错误通知完成 target=%s success=%s", mask_url(policy.target_url), success)
    except Exception as e:
        logger.error("[AIErrorNotify] 发送 AI 错误通知失败: %s", e)
