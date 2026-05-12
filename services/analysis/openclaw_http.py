"""HTTP polling for OpenClaw final analysis results."""

import logging
from typing import Any

from services.analysis.openclaw_poll_policy import OpenClawPollPolicy
from services.webhooks.types import WebhookData

logger = logging.getLogger("webhook_service.openclaw_http")


async def poll_openclaw_final(
    session_key: str,
    *,
    policy: OpenClawPollPolicy,
    http_client: Any,
    trace_id: str | None = None,
    retry_count: int = 3,
) -> WebhookData:
    """
    Fetch final OpenClaw text over HTTP.

    Returns:
        - {"status": "completed", "text": "...", "msg_count": N}
        - {"status": "pending"}
        - {"status": "error", "error": "..."}
    """
    base_url = policy.http_api_url.rstrip("/")
    headers = policy.http_auth_headers(trace_id)
    last_error = None
    transport_error = False

    for attempt in range(retry_count):
        try:
            url = f"{base_url}/sessions/{session_key}/final"
            logger.debug("HTTP /final 请求 (尝试 %s/%s): %s", attempt + 1, retry_count, url)

            response = await http_client.get(url, headers=headers, timeout=policy.http_poll_timeout)

            if response.status_code == 404:
                last_error = "Session not found"
                logger.warning("Session 未找到 (尝试 %d/%d)", attempt + 1, retry_count)
                continue

            if response.status_code in (202, 204):
                last_error = "分析进行中"
                logger.debug("分析进行中 (尝试 %s/%s)", attempt + 1, retry_count)
                continue

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue

            raw = response.json()
            if not isinstance(raw, dict):
                last_error = "Invalid JSON response"
                continue

            data: WebhookData = raw
            is_final = data.get("isFinal")
            is_processing = data.get("isProcessing", False)
            text = data.get("text", "")
            msg_count = int(data.get("messageCount", 0) or 0)

            if is_processing is True:
                last_error = "分析进行中"
                continue

            if text and is_final is not False:
                return {"status": "completed", "text": text, "msg_count": msg_count}

            if is_final is False or not is_final:
                last_error = "分析进行中"
                continue

            last_error = "No text content"
        except Exception as e:
            transport_error = True
            last_error = str(e)
            logger.warning("HTTP 轮询异常: %s", e)

    if last_error == "分析进行中":
        return {"status": "pending"}
    if transport_error:
        return {"status": "error", "error": last_error or "HTTP transport error", "retryable": True}
    return {"status": "error", "error": last_error}
