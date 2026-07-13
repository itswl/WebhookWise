"""Generic remote webhook forwarding."""

import time
from typing import Any, cast

import httpx

from contracts.webhook_payload import JsonObject, WebhookData
from core.circuit_breaker import CircuitBreakerOpenException
from core.logger import get_logger, mask_url
from core.observability.metrics import FORWARD_DELIVERY_DURATION_SECONDS, FORWARD_DELIVERY_TOTAL
from core.url_security import UnsafeTargetUrlError
from services.forwarding.circuit_breakers import RemoteForwardDependencies, build_remote_forward_dependencies
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import AnalysisResult, ForwardResult

logger = get_logger("forwarding.remote")

_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429}
_DISABLE_RULE_HTTP_STATUS_CODES = {401, 403, 404, 410}
_DISABLE_RULE_FEISHU_ERROR_CODES = {"19001"}


def _feishu_business_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""

    code = body.get("StatusCode", body.get("code"))
    if code in (None, "", 0, "0"):
        return ""
    message = body.get("StatusMessage") or body.get("msg") or body.get("message") or "unknown error"
    return f"feishu business error code={code}: {message}"


def _feishu_business_failure(response: httpx.Response) -> ForwardResult | None:
    """Classify a Feishu business error without leaking the webhook URL.

    Feishu returns HTTP 200 for many business failures. Invalid webhook tokens
    are permanent and must not consume the retry budget. Unknown business
    errors remain retryable because a payload-specific failure must not disable
    an otherwise healthy integration.
    """
    message = _feishu_business_error(response)
    if not message:
        return None
    try:
        body = response.json()
    except ValueError:
        body = {}
    raw_code = body.get("StatusCode", body.get("code")) if isinstance(body, dict) else None
    error_code = str(raw_code or "unknown")
    return {
        "status": "failed",
        "status_code": response.status_code,
        "message": message,
        "error_code": error_code,
        "retryable": error_code not in _DISABLE_RULE_FEISHU_ERROR_CODES,
        "disable_rule": error_code in _DISABLE_RULE_FEISHU_ERROR_CODES,
    }


async def send_forward_rule_test(*, rule_name: str, target_url: str, target_type: str | None) -> ForwardResult:
    """Deliver a synthetic test message for a forward rule, bypassing the outbox.

    Owns the channel decision (feishu vs generic webhook) and payload building so
    the API layer does not perform external delivery directly. The test path is
    intentionally direct (not via the idempotent outbox) so each test really
    sends.
    """
    from services.notifications.feishu import build_feishu_card, is_feishu_url, send_to_feishu

    test_webhook: WebhookData = {"source": "test", "parsed_data": {"test": True, "rule_name": rule_name}}
    test_analysis: AnalysisResult = {"summary": f"Test rule: {rule_name}", "importance": "low", "event_type": "test"}

    if is_feishu_url(target_url):
        payload: JsonObject = build_feishu_card(test_webhook, test_analysis)
        return await send_to_feishu(target_url, payload)
    return await post_json_to_remote(
        target_url,
        {"webhook": test_webhook, "analysis": test_analysis},
        target_type_label=target_type or "webhook",
    )


async def post_json_to_remote(
    target_url: str,
    payload: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
    policy: ForwardDeliveryPolicy | None = None,
    validate_target: bool = True,
    dependencies: RemoteForwardDependencies | None = None,
    target_type_label: str = "raw_json",
    idempotency_key: str | None = None,
) -> ForwardResult:
    """Post an already-built JSON payload to a remote webhook target.

    Forwarding is at-least-once (stale-recovery and retries can re-deliver the
    same outbox row), so when an ``idempotency_key`` is supplied it is sent as an
    ``Idempotency-Key`` request header. The key is stable across redeliveries of
    the same outbox row, letting a downstream that honours it collapse
    duplicates; one that ignores it is unaffected.
    """
    started = time.perf_counter()
    status = "unknown"
    policy = policy or ForwardDeliveryPolicy.from_config()
    dependencies = dependencies or build_remote_forward_dependencies()
    if http_client is not None:
        dependencies = RemoteForwardDependencies(
            http_client=http_client,
            circuit_breaker=dependencies.circuit_breaker,
            validate_url=dependencies.validate_url,
        )
    url = target_url
    if validate_target:
        try:
            url = await dependencies.validate_url(url)
        except UnsafeTargetUrlError as e:
            logger.warning("[Forward] Target URL security validation failed target=%s error=%s", mask_url(url), e)
            status = "invalid_target"
            FORWARD_DELIVERY_TOTAL.labels(target_type_label, status).inc()
            FORWARD_DELIVERY_DURATION_SECONDS.labels(target_type_label, status).observe(time.perf_counter() - started)
            return {
                "status": "invalid_target",
                "message": str(e),
                "error_code": "unsafe_target",
                "retryable": False,
                "disable_rule": True,
            }

    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None

    async def _do_post() -> httpx.Response:
        final_url = await dependencies.validate_url(url) if validate_target else url
        logger.info("[Forward] Starting raw-json forward target=%s", mask_url(final_url))
        resp = cast(
            httpx.Response,
            await dependencies.http_client.post(
                final_url, json=payload, timeout=policy.timeout_seconds, headers=headers
            ),
        )
        resp.raise_for_status()
        return resp

    try:
        response = await dependencies.circuit_breaker.call_async(_do_post)
        if target_type_label == "feishu":
            business_failure = _feishu_business_failure(response)
            if business_failure:
                logger.warning(
                    "[Forward] Feishu business response failed target=%s error=%s",
                    mask_url(url),
                    business_failure.get("message"),
                )
                status = "failed"
                return business_failure
        logger.info(
            "[Forward] raw-json forward completed target=%s status_code=%s", mask_url(url), response.status_code
        )
        status = "success"
        return {"status": "success", "status_code": response.status_code}
    except UnsafeTargetUrlError as e:
        logger.warning(
            "[Forward] Target URL security validation failed before sending target=%s error=%s", mask_url(url), e
        )
        status = "invalid_target"
        return {
            "status": "invalid_target",
            "message": str(e),
            "error_code": "unsafe_target",
            "retryable": False,
            "disable_rule": True,
        }
    except CircuitBreakerOpenException:
        logger.warning("[Forward] Circuit breaker is open, forward intercepted target=%s", mask_url(url))
        status = "circuit_broken"
        return {
            "status": "circuit_broken",
            "message": "circuit breaker is open",
            "error_code": "circuit_open",
            "retryable": True,
        }
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        retryable = status_code in _RETRYABLE_HTTP_STATUS_CODES or status_code >= 500
        logger.warning(
            "[Forward] Remote returned HTTP error target=%s status_code=%s retryable=%s",
            mask_url(url),
            status_code,
            retryable,
        )
        status = "failed"
        return {
            "status": "failed",
            "status_code": status_code,
            "message": f"remote returned HTTP {status_code}",
            "error_code": f"http_{status_code}",
            "retryable": retryable,
            "disable_rule": status_code in _DISABLE_RULE_HTTP_STATUS_CODES,
        }
    except (httpx.RequestError, OSError, TimeoutError, ValueError) as e:
        logger.error(
            "[Forward] raw-json forward failed target=%s error_type=%s error=%s", mask_url(url), type(e).__name__, e
        )
        status = "failed"
        return {"status": "failed", "message": str(e), "retryable": True}
    finally:
        FORWARD_DELIVERY_TOTAL.labels(target_type_label, status).inc()
        FORWARD_DELIVERY_DURATION_SECONDS.labels(target_type_label, status).observe(time.perf_counter() - started)
