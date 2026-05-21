"""OpenClaw forwarding and trigger payload construction."""

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx

from core.circuit_breaker import CircuitBreakerOpenException
from core.logger import logger, mask_url
from core.observability.metrics import FORWARD_DELIVERY_DURATION_SECONDS, FORWARD_DELIVERY_TOTAL
from services.analysis.ai_prompt import DEEP_ANALYSIS_PROMPT_KIND, get_prompt_source, load_deep_analysis_prompt_template
from services.forwarding.dependencies import OpenClawForwardDependencies, build_openclaw_forward_dependencies
from services.forwarding.policies import OpenClawTriggerPolicy
from services.webhooks.types import ForwardResult, WebhookData

_JSON_UTF8_CONTENT_TYPE = "application/json; charset=utf-8"


async def forward_to_openclaw(
    webhook_data: WebhookData,
    analysis_result: dict[str, Any],
    *,
    policy: OpenClawTriggerPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
    dependencies: OpenClawForwardDependencies | None = None,
) -> ForwardResult:
    """推送任务到 OpenClaw 进行深度分析。"""
    started = time.perf_counter()
    status = "unknown"
    policy = policy or OpenClawTriggerPolicy.from_config()
    dependencies = dependencies or build_openclaw_forward_dependencies()
    if http_client is not None:
        dependencies = OpenClawForwardDependencies(
            http_client=http_client,
            circuit_breaker=dependencies.circuit_breaker,
        )
    if not policy.enabled:
        logger.debug("[Forward] OpenClaw 未启用，跳过深度分析")
        status = "disabled"
        FORWARD_DELIVERY_TOTAL.labels("openclaw", status).inc()
        FORWARD_DELIVERY_DURATION_SECONDS.labels("openclaw", status).observe(time.perf_counter() - started)
        return {"status": "disabled"}

    async def _do_request() -> dict[str, Any]:
        from services.analysis.ai_analyzer import analyze_webhook_with_ai

        result = await analyze_with_openclaw(webhook_data, policy=policy, dependencies=dependencies)
        if result.get("_degraded"):
            logger.warning("[Forward] OpenClaw 降级，回退本地 AI: %s", result.get("_degraded_reason"))
            local_data = {
                "source": webhook_data.get("source", "unknown"),
                "headers": webhook_data.get("headers", {}),
                "parsed_data": webhook_data.get("parsed_data", {}),
            }
            return await analyze_webhook_with_ai(local_data)
        return result

    try:
        res = await dependencies.circuit_breaker.call_async(_do_request)
        status = str(res.get("status") or ("pending" if res.get("_pending") else "success"))
        return res
    except CircuitBreakerOpenException:
        status = "circuit_broken"
        return {"status": "circuit_broken"}
    except Exception as e:
        logger.error("OpenClaw 转发异常: %s", e)
        status = "error"
        return {"status": "error", "message": str(e)}
    finally:
        FORWARD_DELIVERY_TOTAL.labels("openclaw", status).inc()
        FORWARD_DELIVERY_DURATION_SECONDS.labels("openclaw", status).observe(time.perf_counter() - started)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _extract_openclaw_overview(source: str, alert_data: dict[str, Any]) -> dict[str, Any]:
    first_alert: dict[str, Any] = {}
    alerts = alert_data.get("alerts")
    if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
        first_alert = alerts[0]

    labels = _dict_or_empty(first_alert.get("labels"))
    annotations = _dict_or_empty(first_alert.get("annotations"))
    overview: dict[str, Any] = {
        "source": source,
        "type": alert_data.get("Type"),
        "rule_name": alert_data.get("RuleName") or labels.get("alertname") or alert_data.get("alertingRuleName"),
        "level": alert_data.get("Level") or labels.get("severity") or labels.get("internal_label_alert_level"),
        "summary": alert_data.get("summary") or annotations.get("summary") or annotations.get("description"),
    }
    if labels:
        overview["labels"] = labels
    if annotations:
        overview["annotations"] = annotations
    if first_alert:
        overview["prometheus_alert"] = {
            "status": first_alert.get("status"),
            "startsAt": first_alert.get("startsAt"),
            "endsAt": first_alert.get("endsAt"),
            "generatorURL": first_alert.get("generatorURL"),
            "fingerprint": first_alert.get("fingerprint") or labels.get("internal_label_alert_id"),
        }
    return {k: v for k, v in overview.items() if v not in (None, "", {}, [])}


def _build_openclaw_prompt_payload(source: str, alert_data: dict[str, Any]) -> dict[str, Any]:
    overview = _extract_openclaw_overview(source, alert_data)
    return {"overview": overview, "payload": alert_data}


async def analyze_with_openclaw(
    webhook_data: WebhookData,
    user_question: str = "",
    thinking_level: str = "high",
    *,
    policy: OpenClawTriggerPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
    dependencies: OpenClawForwardDependencies | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """通过 OpenClaw Agent 进行深度分析（非阻塞触发，立即返回）"""
    from core.observability.tracing import get_current_trace_id

    policy = policy or OpenClawTriggerPolicy.from_config()
    dependencies = dependencies or build_openclaw_forward_dependencies()
    if http_client is not None:
        dependencies = OpenClawForwardDependencies(
            http_client=http_client,
            circuit_breaker=dependencies.circuit_breaker,
        )
    if not policy.enabled:
        logger.warning("[OpenClaw] 未启用，跳过深度分析")
        return {"_degraded": True, "_degraded_reason": "OpenClaw 未启用"}

    alert_data = webhook_data.get("parsed_data", {})
    source = webhook_data.get("source", "unknown")
    if not isinstance(alert_data, dict):
        alert_data = {"raw": alert_data}
    from services.webhooks.payload_sanitizer import sanitize_for_ai_async

    alert_data = await sanitize_for_ai_async(alert_data, strip_configured_keys=False, truncate=False)
    prompt_payload = _build_openclaw_prompt_payload(str(source), alert_data)
    template = await load_deep_analysis_prompt_template()

    overview_json = json.dumps(prompt_payload.get("overview", {}), ensure_ascii=False, separators=(",", ":"))
    payload_json = json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"))
    message = (
        f"{template}\n\n"
        "## 当前告警关键字段（优先使用）\n"
        f"告警来源: {source}\n"
        "```json\n"
        f"{overview_json}\n"
        "```\n\n"
        "## 当前告警数据\n"
        "下面的 payload 仅做敏感字段脱敏，不做大小裁剪；若网关或模型显示层发生截断，请基于上方关键字段继续排查，不要要求用户重新粘贴。\n"
        "```json\n"
        f"{payload_json}\n"
        "```"
    )
    if user_question:
        message += f"\n\n## 用户补充问题\n{user_question}"
    logger.info(
        "[OpenClaw] 深度分析 prompt 已加载 source=%s bytes=%s",
        get_prompt_source(DEEP_ANALYSIS_PROMPT_KIND),
        len(template.encode("utf-8")),
    )

    session_key = f"hook:deep-analysis:{source}:{uuid.uuid4()}"
    payload = {
        "message": message,
        "name": "deep-analysis",
        "sessionKey": session_key,
        "wakeMode": "now",
        "deliver": False,
        "thinking": thinking_level,
        "timeoutSeconds": policy.timeout_seconds,
    }

    platform = policy.platform
    hooks_token = policy.hooks_token
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    connect_timeout = policy.connect_timeout

    if platform == "hermes":
        import hashlib
        import hmac as hmac_mod

        target_url = f"{policy.gateway_url}/webhooks/agent"
        signature = hmac_mod.new(hooks_token.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        headers = {"Content-Type": _JSON_UTF8_CONTENT_TYPE, "X-Webhook-Signature": signature}
    else:
        target_url = f"{policy.gateway_url}/hooks/agent"
        headers = {"Authorization": f"Bearer {hooks_token}", "Content-Type": _JSON_UTF8_CONTENT_TYPE}
    kwargs: dict[str, Any] = {"content": payload_bytes}

    trace_id = get_current_trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    if not hooks_token:
        logger.warning("[%s] OpenClaw token 为空，将按当前配置继续发起请求", platform.upper())
    logger.info(
        "[%s] 正在发起分析请求: target=%s session_key=%s payload_bytes=%s trace_id=%s",
        platform.upper(),
        mask_url(target_url),
        session_key,
        len(payload_bytes),
        trace_id or "-",
    )

    max_retries = policy.max_retries
    last_error = None
    response: httpx.Response | None = None

    for attempt in range(max_retries):
        try:
            response = cast(
                httpx.Response,
                await dependencies.circuit_breaker.call_async(
                    dependencies.http_client.post,
                    target_url,
                    headers=headers,
                    timeout=httpx.Timeout(60.0, connect=connect_timeout),
                    **kwargs,
                ),
            )
            response.raise_for_status()
            break
        except CircuitBreakerOpenException as e:
            last_error = str(e)
            logger.warning("[%s] 请求被熔断器拦截 target=%s error=%s", platform.upper(), mask_url(target_url), e)
            if policy.enable_degradation:
                return {"_degraded": True, "_degraded_reason": f"{platform.capitalize()} 请求失败: {last_error}"}
            raise
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "[%s] 请求异常 target=%s attempt=%d/%d error_type=%s error=%s",
                platform.upper(),
                mask_url(target_url),
                attempt + 1,
                max_retries,
                type(e).__name__,
                e,
            )
            if attempt < max_retries - 1:
                import asyncio

                await (sleep or asyncio.sleep)(policy.retry_sleep_seconds)
    else:
        logger.error("[%s] 请求失败，已重试 %d 次: %s", platform.upper(), max_retries, last_error)
        if policy.enable_degradation:
            return {"_degraded": True, "_degraded_reason": f"{platform.capitalize()} 请求失败: {last_error}"}
        raise Exception(f"{platform.capitalize()} 请求失败: {last_error}")

    if response is None:
        raise RuntimeError(f"{platform.capitalize()} 请求失败: empty response")

    try:
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("OpenClaw response is not a JSON object")
        result: dict[str, Any] = raw
        if platform == "hermes":
            run_id = result.get("delivery_id") or result.get("runId")
            session_key = run_id if run_id else session_key
        else:
            run_id = result.get("runId")
        logger.info(
            "[%s] 成功触发深度分析 run_id=%s session_key=%s status_code=%s",
            platform.upper(),
            run_id,
            session_key,
            response.status_code,
        )
        return {"_pending": True, "_openclaw_run_id": run_id, "_openclaw_session_key": session_key}
    except Exception as e:
        logger.error("[OpenClaw] 响应解析失败 status_code=%s error=%s", response.status_code, e)
        if policy.enable_degradation:
            return {"_degraded": True, "_degraded_reason": f"响应解析失败: {e!s}"}
        raise
