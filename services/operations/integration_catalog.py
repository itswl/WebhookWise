"""Curated integration catalog and guided forwarding-rule setup."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.url_security import UnsafeTargetUrlError, validate_outbound_url
from schemas.operations import IntegrationSetupRequest, IntegrationTestRequest
from services.forwarding.rules import create_forward_rule
from services.operations.audit_logger import add_audit

_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "feishu",
        "name": "Feishu bot",
        "description": "Send formatted alert cards to a Feishu group bot.",
        "icon": "💬",
        "target_type": "feishu",
        "requires_url": True,
        "url_hint": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
        "recommended_for": ["operations", "incident_response"],
    },
    {
        "id": "generic_webhook",
        "name": "Generic webhook",
        "description": "Deliver normalized alert and analysis JSON to an HTTP endpoint.",
        "icon": "🔗",
        "target_type": "webhook",
        "requires_url": True,
        "url_hint": "https://example.com/webhooks/alerts",
        "recommended_for": ["automation", "custom_integrations"],
    },
    {
        "id": "openclaw",
        "name": "OpenClaw analysis",
        "description": "Route selected alerts into the configured OpenClaw deep-analysis channel.",
        "icon": "🧠",
        "target_type": "openclaw",
        "requires_url": False,
        "url_hint": "Uses the server OpenClaw configuration",
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
    if target_type == "openclaw":
        from core.app_context import get_config_manager

        enabled = bool(get_config_manager().openclaw.OPENCLAW_ENABLED)
        return {
            "healthy": enabled,
            "status": "configuration_managed" if enabled else "disabled",
            "message": (
                "OpenClaw uses the active server-side channel configuration"
                if enabled
                else "OpenClaw is disabled in the server configuration"
            ),
        }
    target_url = await _validated_url(payload.target_url)
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
    target_url = "" if target_type == "openclaw" else await _validated_url(payload.target_url)
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


async def _validated_url(value: str) -> str:
    if not value.strip():
        raise UnsafeTargetUrlError("Target URL cannot be empty")
    return await validate_outbound_url(value)
