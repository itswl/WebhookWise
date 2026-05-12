"""Compatibility facade for forwarding APIs.

Implementation lives in focused modules:
- rules.py for forwarding rules
- failed_records.py for failed-forward audit records
- remote.py for generic webhook delivery
- openclaw.py for OpenClaw triggers
"""

from typing import Any

import httpx

from core.circuit_breaker import forward_cb, openclaw_cb
from core.http_client import get_http_client
from core.url_security import validate_outbound_url
from services.forwarding.failed_records import (
    cleanup_old_success_records,
    delete_failed_forward,
    get_failed_forward_stats,
    get_failed_forwards,
    manual_retry_reset,
    record_failed_forward,
)
from services.forwarding.openclaw import (
    _build_openclaw_prompt_payload,
)
from services.forwarding.policies import (
    ForwardOutboxPolicy,
    ForwardRetryPolicy,
    OpenClawTriggerPolicy,
    RemoteForwardPolicy,
)
from services.forwarding.rules import (
    create_forward_rule,
    delete_forward_rule,
    get_forward_rule,
    get_forward_rules,
    update_forward_rule,
)
from services.webhooks.types import AnalysisResult, ForwardResult, WebhookData


async def forward_to_remote(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    target_url: str | None = None,
    is_periodic_reminder: bool = False,
    http_client: httpx.AsyncClient | None = None,
    policy: RemoteForwardPolicy | None = None,
) -> ForwardResult:
    """Compatibility wrapper that preserves historical monkeypatch points."""
    from services.forwarding import remote

    remote.get_http_client = get_http_client  # type: ignore[attr-defined]
    remote.validate_outbound_url = validate_outbound_url  # type: ignore[attr-defined]
    remote.forward_cb = forward_cb  # type: ignore[attr-defined]
    return await remote.forward_to_remote(
        webhook_data=webhook_data,
        analysis_result=analysis_result,
        target_url=target_url,
        is_periodic_reminder=is_periodic_reminder,
        http_client=http_client,
        policy=policy,
    )


async def post_json_to_remote(
    target_url: str,
    payload: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
    policy: RemoteForwardPolicy | None = None,
    validate_target: bool = True,
) -> ForwardResult:
    """Compatibility wrapper for raw JSON forwarding."""
    from services.forwarding import remote

    remote.get_http_client = get_http_client  # type: ignore[attr-defined]
    remote.validate_outbound_url = validate_outbound_url  # type: ignore[attr-defined]
    remote.forward_cb = forward_cb  # type: ignore[attr-defined]
    return await remote.post_json_to_remote(
        target_url,
        payload,
        http_client=http_client,
        policy=policy,
        validate_target=validate_target,
    )


async def forward_to_openclaw(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    *,
    policy: OpenClawTriggerPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ForwardResult:
    """Compatibility wrapper that preserves historical monkeypatch points."""
    from services.forwarding import openclaw

    openclaw.openclaw_cb = openclaw_cb  # type: ignore[attr-defined]
    openclaw.get_http_client = get_http_client  # type: ignore[attr-defined]
    return await openclaw.forward_to_openclaw(
        webhook_data,
        analysis_result,
        policy=policy,
        http_client=http_client,
    )


async def analyze_with_openclaw(
    webhook_data: WebhookData,
    user_question: str = "",
    thinking_level: str = "high",
    *,
    policy: OpenClawTriggerPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
    sleep: Any | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper that preserves historical monkeypatch points."""
    from services.forwarding import openclaw

    openclaw.openclaw_cb = openclaw_cb  # type: ignore[attr-defined]
    openclaw.get_http_client = get_http_client  # type: ignore[attr-defined]
    return await openclaw.analyze_with_openclaw(
        webhook_data,
        user_question=user_question,
        thinking_level=thinking_level,
        policy=policy,
        http_client=http_client,
        sleep=sleep,
    )


__all__ = [
    "ForwardRetryPolicy",
    "ForwardOutboxPolicy",
    "OpenClawTriggerPolicy",
    "RemoteForwardPolicy",
    "_build_openclaw_prompt_payload",
    "analyze_with_openclaw",
    "cleanup_old_success_records",
    "create_forward_rule",
    "delete_failed_forward",
    "delete_forward_rule",
    "forward_cb",
    "forward_to_openclaw",
    "forward_to_remote",
    "get_failed_forward_stats",
    "get_failed_forwards",
    "get_forward_rule",
    "get_forward_rules",
    "get_http_client",
    "manual_retry_reset",
    "openclaw_cb",
    "post_json_to_remote",
    "record_failed_forward",
    "update_forward_rule",
    "validate_outbound_url",
]
