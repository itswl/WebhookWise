"""Curated integration catalog and guided forwarding-rule setup."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from schemas.operations import IntegrationSetupRequest, IntegrationTestRequest
from services.forwarding.rules import create_forward_rule
from services.forwarding.target_validation import validated_target_url
from services.operations.audit_logger import add_audit

# `sprite` names an icon in the dashboard's sprite sheet — emoji icons were
# retired from the dashboard, and a server-supplied emoji would smuggle one
# straight past that decision.
_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "feishu",
        "name": "Feishu bot",
        "description": "Send formatted alert cards to a Feishu group bot.",
        "sprite": "message",
        "target_type": "feishu",
        "requires_url": True,
        "url_hint": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
        "recommended_for": ["operations", "incident_response"],
    },
    {
        "id": "dingtalk",
        "name": "DingTalk bot",
        "description": "Send markdown alert messages to a DingTalk group robot.",
        "sprite": "bell",
        "target_type": "dingtalk",
        "requires_url": True,
        "url_hint": "https://oapi.dingtalk.com/robot/send?access_token=...",
        "recommended_for": ["operations", "incident_response"],
    },
    {
        "id": "wecom",
        "name": "WeCom bot",
        "description": "Send markdown alert messages to a WeCom (企业微信) group bot.",
        "sprite": "inbox",
        "target_type": "wecom",
        "requires_url": True,
        "url_hint": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...",
        "recommended_for": ["operations", "incident_response"],
    },
    {
        "id": "generic_webhook",
        "name": "Generic webhook",
        "description": "Deliver normalized alert and analysis JSON to an HTTP endpoint.",
        "sprite": "link",
        "target_type": "webhook",
        "requires_url": True,
        "url_hint": "https://example.com/webhooks/alerts",
        "recommended_for": ["automation", "custom_integrations"],
    },
    {
        "id": "feishu_relay",
        "name": "Feishu relay",
        "description": "Hand analysis results to a hookrelay front door (HMAC-signed); the relay owns rendering and downstream delivery.",
        "sprite": "radio",
        "target_type": "feishu_relay",
        "requires_url": True,
        "url_hint": "http://hookrelay:8100/hook/...",
        "recommended_for": ["operations"],
    },
    {
        "id": "deep_analysis",
        "name": "Deep analysis",
        "description": "Route selected alerts into the configured deep-analysis gateway.",
        "sprite": "lightbulb",
        "target_type": "deep_analysis",
        "requires_url": False,
        "url_hint": "Uses the server deep-analysis configuration",
        "recommended_for": ["deep_analysis"],
    },
)


def integration_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in _CATALOG]


def _template(template_id: str) -> dict[str, Any]:
    for item in _CATALOG:
        if item["id"] == template_id:
            return item
    raise ValueError("Unknown integration template")


async def test_integration(payload: IntegrationTestRequest) -> dict[str, Any]:
    template = _template(payload.template_id)
    target_type = str(template["target_type"])
    if target_type == "deep_analysis":
        from core.app_context import get_config_manager

        enabled = bool(get_config_manager().deep_analysis.DEEP_ANALYSIS_ENABLED)
        return {
            "healthy": enabled,
            "status": "configuration_managed" if enabled else "disabled",
            "message": (
                "Deep analysis uses the active server-side channel configuration"
                if enabled
                else "Deep analysis is disabled in the server configuration"
            ),
        }
    target_url = await validated_target_url(target_type, payload.target_url)
    from services.forwarding.remote import send_forward_rule_test

    result = await send_forward_rule_test(
        rule_name=payload.name,
        target_url=target_url,
        target_type=target_type,
    )
    return {
        "healthy": result.get("status") == "success",
        "status": result.get("status"),
        "message": result.get("message") or "Test message delivered",
    }


async def install_integration(session: AsyncSession, payload: IntegrationSetupRequest) -> dict[str, Any]:
    template = _template(payload.template_id)
    target_type = str(template["target_type"])
    # deep_analysis is configuration-managed: the rule never carries a URL.
    target_url = "" if target_type == "deep_analysis" else await validated_target_url(target_type, payload.target_url)
    if payload.enabled:
        probe = await test_integration(
            IntegrationTestRequest(template_id=payload.template_id, name=payload.name, target_url=target_url)
        )
        if not probe["healthy"]:
            raise RuntimeError("The integration target test failed; no enabled rule was created")

    rule = await create_forward_rule(
        session=session,
        name=payload.name,
        target_type=target_type,
        enabled=payload.enabled,
        priority=payload.priority,
        match_importance=payload.importance,
        match_source=payload.source,
        match_project=payload.project,
        match_environment=payload.environment,
        target_url=target_url,
        target_name=payload.target_name,
        stop_on_match=False,
    )
    add_audit(
        session,
        "forward_rule",
        rule.id,
        rule.name,
        "created",
        f"Integration installed from catalog: {payload.template_id}",
    )
    await session.commit()
    return {
        "rule_id": rule.id,
        "name": rule.name,
        "template_id": payload.template_id,
        "target_type": target_type,
        "enabled": rule.enabled,
    }
