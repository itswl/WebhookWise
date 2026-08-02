"""Out-of-band self-notification: the last-resort operator alert.

The normal signal for "your notifications are failing" is itself a
notification — the outbox_exhausted card rides the same rules/outbox/channel
stack that just proved broken, so a full channel outage is silent exactly when
it matters most. This module posts one minimal text message DIRECTLY to a
dedicated operator webhook (SELF_NOTIFY_WEBHOOK_URL), bypassing rules, outbox,
retries, and card formatting entirely.

Deliberately best-effort: a single POST with a short timeout, never raises
into the caller, and rate-limited (Redis gate, in-process fallback when Redis
is down) so a failure storm produces one message per window plus a suppressed
count in the next one.
"""

from __future__ import annotations

import time

from core.logger import get_logger

logger = get_logger("operations.self_notify")

_GATE_KEY = "webhookwise:selfnotify:delivery:gate"
_SUPPRESSED_KEY = "webhookwise:selfnotify:delivery:suppressed"
_POST_TIMEOUT_SECONDS = 5.0
_SEND_ERRORS = (Exception,)  # last-resort path: log anything, never propagate

# Fallback gate when Redis is unavailable: per-process, monotonic seconds.
_last_sent_monotonic: float | None = None


def _reset_gate_for_tests() -> None:
    global _last_sent_monotonic
    _last_sent_monotonic = None


async def _acquire_gate(interval_seconds: int) -> tuple[bool, int]:
    """One send per interval. Returns (allowed, suppressed_since_last_send).

    Uses a Redis SET NX EX gate shared across processes; while the gate is
    held, callers increment a suppressed counter that is folded into the next
    allowed message. If Redis is down the whole point of this module is at its
    most relevant, so fail open to a per-process monotonic gate instead of
    staying silent.
    """
    global _last_sent_monotonic
    from core import redis_client

    try:
        acquired = await redis_client.redis_set_nx_ex(_GATE_KEY, "1", interval_seconds)
        if not acquired:
            await redis_client.redis_incr_with_expire(_SUPPRESSED_KEY, interval_seconds * 2)
            return False, 0
        suppressed_raw = await redis_client.redis_get_str(_SUPPRESSED_KEY)
        await redis_client.redis_delete(_SUPPRESSED_KEY)
        _last_sent_monotonic = time.monotonic()
        return True, int(suppressed_raw or 0)
    except _SEND_ERRORS as e:  # noqa: BLE001 - degrade to the in-process gate
        now = time.monotonic()
        if _last_sent_monotonic is not None and (now - _last_sent_monotonic) < interval_seconds:
            return False, 0
        logger.warning("[SelfNotify] Redis gate unavailable (%s); using in-process rate limit", e)
        _last_sent_monotonic = now
        return True, 0


def _build_text(*, target_type: str, error: str, outbox_id: int, event_id: int | None, suppressed: int) -> str:
    lines = [
        "⚠️ WebhookWise 投递兜底通知",
        f"转发重试已耗尽:target={target_type} outbox_id={outbox_id}"
        + (f" event_id={event_id}" if event_id is not None else ""),
        f"错误:{error[:300]}",
    ]
    if suppressed > 0:
        lines.append(f"(本窗口内另有 {suppressed} 次终态失败被合并)")
    lines.append("请检查通道配置/网络;修复后可在仪表盘 Forwards 页重新入队。")
    return "\n".join(lines)


async def notify_delivery_exhausted(
    *,
    target_type: str,
    error: str,
    outbox_id: int,
    event_id: int | None = None,
) -> bool:
    """Post the out-of-band failure notice. Returns True only when sent."""
    from core.app_context import get_config_manager
    from services.operations import runtime_settings as rt

    try:
        cfg = get_config_manager().notifications
        url = str(cfg.SELF_NOTIFY_WEBHOOK_URL or "").strip()
        if not url:
            return False
        interval_minutes = max(
            1, rt.override_or("SELF_NOTIFY_MIN_INTERVAL_MINUTES", int(cfg.SELF_NOTIFY_MIN_INTERVAL_MINUTES))
        )
        allowed, suppressed = await _acquire_gate(interval_minutes * 60)
        if not allowed:
            return False

        text = _build_text(
            target_type=target_type, error=error, outbox_id=outbox_id, event_id=event_id, suppressed=suppressed
        )
        if cfg.SELF_NOTIFY_KIND == "feishu":
            payload: dict[str, object] = {"msg_type": "text", "content": {"text": text}}
        else:
            payload = {
                "source": "webhookwise",
                "type": "delivery_exhausted",
                "target_type": target_type,
                "outbox_id": outbox_id,
                "event_id": event_id,
                "suppressed": suppressed,
                "text": text,
            }

        from core.http_client import get_http_client

        response = await get_http_client().post(url, json=payload, timeout=_POST_TIMEOUT_SECONDS)
        if response.status_code >= 300:
            logger.warning(
                "[SelfNotify] Fallback webhook returned status=%s body=%s",
                response.status_code,
                response.text[:200],
            )
            return False
        logger.info(
            "[SelfNotify] Delivery-exhausted notice sent target=%s outbox_id=%s suppressed=%s",
            target_type,
            outbox_id,
            suppressed,
        )
        return True
    except _SEND_ERRORS as e:  # noqa: BLE001 - never let the fallback hurt the caller
        logger.warning("[SelfNotify] Fallback notification failed (best-effort, not retried): %s", e)
        return False
