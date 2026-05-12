"""Generic remote webhook forwarding."""

from typing import Any, cast

import httpx

from adapters.notification_targets import is_feishu_url
from core.circuit_breaker import CircuitBreakerOpenException
from core.logger import logger
from core.url_security import UnsafeTargetUrlError
from services.forwarding.dependencies import RemoteForwardDependencies, build_remote_forward_dependencies
from services.forwarding.policies import RemoteForwardPolicy
from services.webhooks.types import AnalysisResult, ForwardResult, WebhookData


async def forward_to_remote(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    target_url: str | None = None,
    is_periodic_reminder: bool = False,
    http_client: httpx.AsyncClient | None = None,
    policy: RemoteForwardPolicy | None = None,
    dependencies: RemoteForwardDependencies | None = None,
) -> ForwardResult:
    """转发分析结果到远程 Webhook URL (支持飞书卡片自动格式化)。"""
    policy = policy or RemoteForwardPolicy.from_config()
    dependencies = dependencies or build_remote_forward_dependencies()
    if http_client is not None:
        dependencies = RemoteForwardDependencies(
            http_client=http_client,
            circuit_breaker=dependencies.circuit_breaker,
            validate_url=dependencies.validate_url,
        )
    url = target_url or policy.forward_url
    if not url:
        logger.debug("[Forward] 无转发 URL，跳过")
        return {"status": "skipped", "reason": "no_forward_url"}
    try:
        url = await dependencies.validate_url(url)
    except UnsafeTargetUrlError as e:
        logger.warning("[Forward] 目标 URL 安全校验失败 url=%s error=%s", url, e)
        return {"status": "invalid_target", "message": str(e)}

    is_feishu = is_feishu_url(url)
    if is_feishu:
        from adapters.plugins.feishu_card import build_feishu_card

        payload = build_feishu_card(webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder)
    else:
        payload = {"webhook": webhook_data, "analysis": analysis_result, "is_periodic_reminder": is_periodic_reminder}

    async def _do_post() -> httpx.Response:
        logger.debug("[Forward] POST %s is_feishu=%s periodic=%s", url, is_feishu, is_periodic_reminder)
        resp = cast(
            httpx.Response, await dependencies.http_client.post(url, json=payload, timeout=policy.timeout_seconds)
        )
        resp.raise_for_status()
        return resp

    try:
        response = await dependencies.circuit_breaker.call_async(_do_post)
        resp_payload: dict[str, Any] = {}
        if response.content:
            try:
                raw_json = response.json()
                resp_payload = raw_json if isinstance(raw_json, dict) else {"_raw": raw_json}
            except ValueError:
                resp_payload = {"_raw": response.text[:1000]}
        return {
            "status": "success",
            "status_code": response.status_code,
            "response": resp_payload,
        }
    except CircuitBreakerOpenException:
        logger.warning("[Forward] 熔断器已开启，转发被拦截 url=%s", url)
        return {"status": "circuit_broken", "message": "熔断器已开启"}
    except Exception as e:
        logger.error("[Forward] 转发失败 url=%s error=%s", url, e)
        return {"status": "failed", "message": str(e)}


async def post_json_to_remote(
    target_url: str,
    payload: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
    policy: RemoteForwardPolicy | None = None,
    validate_target: bool = True,
    dependencies: RemoteForwardDependencies | None = None,
) -> ForwardResult:
    """Post an already-built JSON payload to a remote webhook target."""
    policy = policy or RemoteForwardPolicy.from_config()
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
            logger.warning("[Forward] 目标 URL 安全校验失败 url=%s error=%s", url, e)
            return {"status": "invalid_target", "message": str(e)}

    async def _do_post() -> httpx.Response:
        logger.debug("[Forward] POST raw-json %s", url)
        resp = cast(
            httpx.Response, await dependencies.http_client.post(url, json=payload, timeout=policy.timeout_seconds)
        )
        resp.raise_for_status()
        return resp

    try:
        response = await dependencies.circuit_breaker.call_async(_do_post)
        return {"status": "success", "status_code": response.status_code}
    except CircuitBreakerOpenException:
        logger.warning("[Forward] 熔断器已开启，转发被拦截 url=%s", url)
        return {"status": "circuit_broken", "message": "熔断器已开启"}
    except Exception as e:
        logger.error("[Forward] 转发失败 url=%s error=%s", url, e)
        return {"status": "failed", "message": str(e)}
