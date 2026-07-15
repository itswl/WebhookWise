"""
Ecosystem Adapters for WebhookWise.
Handles normalization of various webhook sources into a standard format.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from adapters.declarative import register_declarative_adapters
from adapters.simple_adapters import normalize_level, register_simple_adapters
from contracts.webhook_payload import WebhookData, webhook_data_from_mapping
from core.logger import get_logger

logger = get_logger("ecosystem_adapters")

HeadersLike = Mapping[str, Any]

__all__ = [
    "HeadersLike",
    "NormalizedWebhook",
    "_header_get",
    "initialize_adapters",
    "normalize_level",
    "normalize_webhook_event",
    "register_declarative_adapters",
    "register_simple_adapters",
]


@dataclass(frozen=True)
class NormalizedWebhook:
    source: str
    data: WebhookData
    adapter: str


def _header_get(headers: HeadersLike | None, key: str) -> str | None:
    if not headers:
        return None
    value = headers.get(key)
    if value is not None:
        return str(value)
    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return None


def initialize_adapters() -> None:
    """Initialize built-in adapters during process startup."""
    from adapters.registry import registry

    before = registry.status()["normalizers"]
    register_simple_adapters()
    # Declarative YAML adapters register after the code adapters, so on a detect
    # tie the built-in code adapter wins unless a spec opts into a higher
    # priority. See adapters/declarative.py.
    register_declarative_adapters()
    if registry.status()["normalizers"] != before:
        logger.info("[Adapter] Adapter registration complete")


def normalize_webhook_event(
    data: Any,
    source: str | None,
    headers: HeadersLike | None = None,
) -> NormalizedWebhook:
    """Select an adapter based on the source or payload characteristics and output normalized data."""
    from adapters.registry import registry

    if not isinstance(data, dict):
        resolved_source = str(source or _header_get(headers, "X-Webhook-Source") or "unknown").strip().lower()
        return NormalizedWebhook(resolved_source, webhook_data_from_mapping({"raw": data}), "passthrough")

    h_src = str(_header_get(headers, "X-Webhook-Source") or "").strip().lower()
    s_hint = str(source or "").strip().lower() or h_src

    adapter_name = registry.find_adapter_by_source(s_hint) if s_hint else None
    if adapter_name is None:
        adapter_name = registry.find_adapter_by_payload(data)

    if adapter_name is None:
        return NormalizedWebhook(s_hint or "unknown", webhook_data_from_mapping(data, strict=False), "passthrough")

    normalized = registry.normalize(adapter_name, dict(data))

    placeholder_sources = {"unknown", "custom", "default", "generic"}
    source_is_alias = registry.find_adapter_by_source(s_hint) == adapter_name if s_hint else False
    final_source = s_hint if (s_hint and not source_is_alias and s_hint not in placeholder_sources) else adapter_name

    logger.info("[Adapter] Successfully matched adapter: name=%s, final_source=%s", adapter_name, final_source)
    return NormalizedWebhook(final_source, normalized, adapter_name)
