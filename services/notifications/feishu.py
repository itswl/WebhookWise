"""Feishu notification compatibility facade."""

from services.forwarding.circuit_breakers import build_remote_forward_dependencies, feishu_cb
from services.notifications.feishu_cards import (
    build_ai_error_card,
    build_deep_analysis_card,
    build_delivery_exhausted_card,
    build_feishu_card,
)
from services.notifications.feishu_parser import is_feishu_url
from services.notifications.feishu_transport import send_to_feishu as _send_to_feishu
from services.webhooks.types import ForwardResult

__all__ = [
    "is_feishu_url",
    "build_feishu_card",
    "build_ai_error_card",
    "build_deep_analysis_card",
    "build_delivery_exhausted_card",
    "send_to_feishu",
    "build_remote_forward_dependencies",
    "feishu_cb",
]


async def send_to_feishu(url: str, payload: dict[str, object], *, idempotency_key: str | None = None) -> ForwardResult:
    return await _send_to_feishu(
        url,
        payload,
        build_remote_forward_dependencies_fn=build_remote_forward_dependencies,
        idempotency_key=idempotency_key,
    )
