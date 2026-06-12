"""Feishu transport/HTTP execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from core.app_context import get_config_manager
from services.forwarding.circuit_breakers import RemoteForwardDependencies, build_remote_forward_dependencies, feishu_cb
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import ForwardResult


async def send_to_feishu(
    url: str,
    payload: dict[str, Any],
    *,
    build_remote_forward_dependencies_fn: Callable[[], RemoteForwardDependencies] = build_remote_forward_dependencies,
    idempotency_key: str | None = None,
) -> ForwardResult:
    from services.forwarding.remote import post_json_to_remote

    timeout_seconds = int(get_config_manager().notifications.FEISHU_WEBHOOK_TIMEOUT)
    policy = replace(ForwardDeliveryPolicy.from_config(), timeout_seconds=timeout_seconds)
    base_dependencies = build_remote_forward_dependencies_fn()
    dependencies = RemoteForwardDependencies(
        http_client=base_dependencies.http_client,
        circuit_breaker=feishu_cb,
        validate_url=base_dependencies.validate_url,
    )
    return await post_json_to_remote(
        url,
        payload,
        policy=policy,
        validate_target=True,
        dependencies=dependencies,
        target_type_label="feishu",
        idempotency_key=idempotency_key,
    )
