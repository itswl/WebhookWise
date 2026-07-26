"""Feishu custom-app transport for interactive incident cards."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreakerOpenException
from core.http_client import get_http_client
from core.logger import get_logger
from services.forwarding.circuit_breakers import feishu_cb
from services.webhooks.types import ForwardResult

logger = get_logger("notifications.feishu_app")

_FEISHU_API_BASE = "https://open.feishu.cn"
_TENANT_ACCESS_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_PATH = "/open-apis/im/v1/messages"
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_token_lock = asyncio.Lock()
_cached_app_id = ""
_cached_token: str | None = None
_cached_until = 0.0


def _business_code(payload: object) -> int:
    if not isinstance(payload, dict):
        return -1
    try:
        return int(payload.get("code", -1))
    except (TypeError, ValueError):
        return -1


async def _tenant_access_token(client: httpx.AsyncClient) -> str:
    global _cached_app_id, _cached_token, _cached_until

    config = get_config_manager().notifications
    app_id = config.FEISHU_APP_ID.strip()
    app_secret = config.FEISHU_APP_SECRET.strip()
    if not app_id or not app_secret:
        raise ValueError("Feishu app credentials are not configured")
    now = time.monotonic()
    if _cached_app_id == app_id and _cached_token and now < _cached_until:
        return _cached_token
    async with _token_lock:
        now = time.monotonic()
        if _cached_app_id == app_id and _cached_token and now < _cached_until:
            return _cached_token
        response = await client.post(
            f"{_FEISHU_API_BASE}{_TENANT_ACCESS_PATH}",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=config.FEISHU_WEBHOOK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or _business_code(payload) != 0:
            raise ValueError("Feishu tenant token request was rejected")
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise ValueError("Feishu tenant token response did not contain a token")
        try:
            expires_in = max(60, int(payload.get("expire", 7200)))
        except (TypeError, ValueError):
            expires_in = 7200
        _cached_app_id = app_id
        _cached_token = token
        _cached_until = time.monotonic() + max(30, expires_in - 300)
        return token


async def send_to_feishu_app(
    chat_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ForwardResult:
    """Send an interactive card through a Feishu custom app."""
    config = get_config_manager().notifications
    receive_id = chat_id.strip()
    if not receive_id:
        return {
            "status": "failed",
            "message": "Feishu incident chat id is not configured",
            "error_code": "missing_chat_id",
            "retryable": False,
        }
    client = http_client or get_http_client()

    async def _send() -> httpx.Response:
        token = await _tenant_access_token(client)
        card = payload.get("card") if isinstance(payload.get("card"), dict) else payload
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = await client.post(
            f"{_FEISHU_API_BASE}{_MESSAGE_PATH}",
            params={"receive_id_type": "chat_id"},
            headers=headers,
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
            },
            timeout=config.FEISHU_WEBHOOK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        if _business_code(body) != 0:
            code = _business_code(body)
            raise ValueError(f"Feishu message API rejected the request (code={code})")
        return response

    try:
        response = await feishu_cb.call_async(_send)
        return {"status": "success", "status_code": response.status_code}
    except CircuitBreakerOpenException:
        return {
            "status": "circuit_broken",
            "message": "Feishu circuit breaker is open",
            "error_code": "circuit_open",
            "retryable": True,
        }
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        logger.warning("[FeishuApp] Message API returned HTTP %s", status_code)
        return {
            "status": "failed",
            "status_code": status_code,
            "message": f"Feishu message API returned HTTP {status_code}",
            "error_code": f"http_{status_code}",
            "retryable": status_code in _RETRYABLE_STATUS_CODES,
        }
    except (httpx.RequestError, OSError, TimeoutError) as error:
        logger.warning("[FeishuApp] Message delivery failed: %s", type(error).__name__)
        return {
            "status": "failed",
            "message": "Feishu message delivery failed",
            "error_code": "transport_error",
            "retryable": True,
        }
    except (TypeError, ValueError) as error:
        logger.warning("[FeishuApp] Message delivery was rejected: %s", error)
        return {
            "status": "failed",
            "message": str(error),
            "error_code": "feishu_business_error",
            "retryable": False,
        }
