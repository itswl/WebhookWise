"""HTTP polling for OpenClaw final analysis results."""

import logging
import time
from typing import Any

import httpx

from services.analysis.openclaw_poll_policy import OpenClawPollPolicy
from services.webhooks.types import WebhookData

logger = logging.getLogger("webhook_service.openclaw_http")


def _describe_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return repr(exc)


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
    headers = {**policy.http_auth_headers(trace_id), "Connection": "close"}
    timeout = httpx.Timeout(
        connect=policy.http_connect_timeout,
        read=policy.http_poll_timeout,
        write=policy.http_connect_timeout,
        pool=policy.http_connect_timeout,
    )
    last_error = None
    transport_error = False

    for attempt in range(retry_count):
        started = time.monotonic()
        try:
            url = f"{base_url}/sessions/{session_key}/final"
            logger.debug("HTTP /final 请求 (尝试 %s/%s): %s", attempt + 1, retry_count, url)

            response = await http_client.get(url, headers=headers, timeout=timeout)
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if response.status_code == 404:
                last_error = "Session not found"
                logger.warning("Session 未找到 (尝试 %d/%d elapsed=%sms)", attempt + 1, retry_count, elapsed_ms)
                continue

            if response.status_code in (202, 204):
                last_error = "分析进行中"
                logger.debug("分析进行中 (尝试 %s/%s elapsed=%sms)", attempt + 1, retry_count, elapsed_ms)
                continue

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    "HTTP /final 返回非 200 status=%s attempt=%s/%s elapsed=%sms",
                    response.status_code,
                    attempt + 1,
                    retry_count,
                    elapsed_ms,
                )
                continue

            try:
                raw = response.json()
            except ValueError:
                last_error = "Invalid JSON response"
                logger.warning(
                    "HTTP /final 返回无效 JSON (尝试 %s/%s elapsed=%sms)", attempt + 1, retry_count, elapsed_ms
                )
                continue
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
                result: WebhookData = {"status": "completed", "text": text, "msg_count": msg_count}
                if is_final is True:
                    result["is_final"] = True
                return result

            if is_final is False or not is_final:
                last_error = "分析进行中"
                continue

            last_error = "No text content"
        except httpx.ReadTimeout as e:
            last_error = f"ReadTimeout after {policy.http_poll_timeout:g}s"
            logger.info(
                "HTTP /final 等待超时，按 pending 处理 attempt=%s/%s timeout=%ss error_type=%s error=%s",
                attempt + 1,
                retry_count,
                policy.http_poll_timeout,
                type(e).__name__,
                _describe_exception(e),
            )
            return {"status": "pending", "error": last_error}
        except httpx.TimeoutException as e:
            transport_error = True
            last_error = _describe_exception(e)
            logger.warning(
                "HTTP 轮询超时 attempt=%s/%s error_type=%s error=%s",
                attempt + 1,
                retry_count,
                type(e).__name__,
                last_error,
            )
        except Exception as e:
            transport_error = True
            last_error = _describe_exception(e)
            logger.warning(
                "HTTP 轮询异常 attempt=%s/%s error_type=%s error=%s",
                attempt + 1,
                retry_count,
                type(e).__name__,
                last_error,
            )

    if last_error == "分析进行中":
        return {"status": "pending"}
    if transport_error:
        return {"status": "error", "error": last_error or "HTTP transport error", "retryable": True}
    return {"status": "error", "error": last_error}
