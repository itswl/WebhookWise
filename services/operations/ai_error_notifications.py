"""Operational notifications for AI analysis errors."""

import hashlib
import re
from typing import Any

from core.logger import get_logger
from core.redis_health import ai_error_alert_lock
from services.analysis.analysis_policies import AIErrorNotificationPolicy
from services.channels.feishu import build_ai_error_card
from services.forwarding.outbox import resolve_and_forward
from services.webhooks.types import WebhookData

logger = get_logger("ai_error_notifications")

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

        event_type = "ai_degraded" if is_degraded else "ai_error"
        logger.info("[AIErrorNotify] 发送 AI 错误通知 degraded=%s", is_degraded)
        result = await resolve_and_forward(
            event_type=event_type,
            formatted_payload=build_ai_error_card(webhook_data, error_reason, is_degraded=is_degraded),
            wait=False,
        )
        logger.info("[AIErrorNotify] AI 错误通知已入队 outbox_ids=%s", result.get("outbox_ids"))
    except Exception as e:
        logger.error("[AIErrorNotify] 发送 AI 错误通知失败: %s", e)
