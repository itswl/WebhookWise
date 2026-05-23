from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from services.webhooks.types import AnalysisResult, ForwardResult, WebhookData

SendResult: TypeAlias = ForwardResult


@dataclass(frozen=True, slots=True)
class FormatContext:
    webhook_data: WebhookData
    analysis_result: AnalysisResult
    is_periodic_reminder: bool = False


class Channel(Protocol):
    @property
    def name(self) -> str: ...

    def supports(self, target_url: str) -> bool: ...

    def format(self, ctx: FormatContext) -> dict[str, Any]: ...

    async def send(self, url: str, payload: dict[str, Any]) -> SendResult: ...


_registry: dict[str, Channel] | None = None


def _build_registry() -> dict[str, Channel]:
    from services.channels.feishu import FeishuChannel
    from services.channels.webhook import WebhookChannel

    channels: list[Channel] = [FeishuChannel(), WebhookChannel()]
    return {channel.name: channel for channel in channels}


def get_channel_registry() -> dict[str, Channel]:
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def get_channel(name: str) -> Channel | None:
    return get_channel_registry().get(name)


def resolve_channel_name(target_type: str, target_url: str) -> str:
    registry = get_channel_registry()
    if target_type in registry and target_type not in {"", "default", "webhook"}:
        return target_type
    for channel in registry.values():
        if channel.supports(target_url):
            return channel.name
    return "webhook"


def find_channel_for_target(target_url: str) -> Channel:
    registry = get_channel_registry()
    for channel in registry.values():
        if channel.supports(target_url):
            return channel
    return registry["webhook"]
