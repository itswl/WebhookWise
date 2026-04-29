"""请求解析与响应构建模块，从 pipeline.py 提取。"""

import hashlib
from datetime import datetime

import orjson
from fastapi.responses import JSONResponse

from adapters.ecosystem_adapters import normalize_webhook_event
from api import (
    InvalidJsonError,
    WebhookRequestContext,
    _ok,
)
from core.logger import logger
from core.webhook_security import ensure_webhook_auth


def _parse_webhook_request(
    client_ip: str, headers: dict, payload: dict, raw_body: bytes, source: str | None
) -> WebhookRequestContext:
    requested_source = source or headers.get("x-webhook-source", "unknown")

    logger.info(f"[Webhook] 收到请求: IP={client_ip}, Source={requested_source}")
    try:
        raw_hash = hashlib.sha256(raw_body).hexdigest() if raw_body else None
    except Exception:
        raw_hash = None
    logger.debug(f"[Webhook] 原始载荷: size={len(raw_body) if raw_body else 0}, sha256={raw_hash}")

    ensure_webhook_auth(headers, raw_body)

    if not payload and raw_body:
        try:
            payload = orjson.loads(raw_body)
        except Exception:
            raise InvalidJsonError() from None

    data = payload

    normalized = normalize_webhook_event(data, requested_source)
    parsed_data = normalized.data
    requested_source = normalized.source
    webhook_full_data = {
        "body": data,
        "headers": headers,
        "query": {},
        "parsed_data": parsed_data,
        "source": requested_source,
    }

    return WebhookRequestContext(
        client_ip=client_ip,
        source=requested_source,
        payload=raw_body,
        parsed_data=parsed_data,
        webhook_full_data=webhook_full_data,
        headers=headers,
    )


def _build_webhook_response(
    webhook_id: int | str,
    analysis_result: dict,
    forward_result: dict,
    is_dup: bool,
    original_id: int | None,
    beyond_window: bool,
    is_within_window: bool,
) -> JSONResponse:
    is_degraded = analysis_result.get("_degraded", False)
    degraded_reason = analysis_result.get("_degraded_reason")
    clean_analysis = {k: v for k, v in analysis_result.items() if not k.startswith("_")}

    return _ok(
        status=200,
        message="Webhook processed successfully",
        timestamp=datetime.now().isoformat(),
        webhook_id=webhook_id,
        ai_analysis=clean_analysis,
        ai_degraded=is_degraded,
        ai_degraded_reason=degraded_reason if is_degraded else None,
        forward_status=forward_result.get("status", "unknown"),
        is_duplicate=is_dup,
        duplicate_of=original_id if is_dup else None,
        beyond_time_window=beyond_window,
        is_within_window=is_within_window,
    )
