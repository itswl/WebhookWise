"""MongoDB-specific alert summary fields for API projections."""

from __future__ import annotations

from typing import Any

from services.webhooks.types import WebhookData

Summary = dict[str, Any]


def mongodb_summary_fields(parsed_data: WebhookData | None) -> Summary:
    if not parsed_data:
        return {}
    monitor = parsed_data.get("监控项")
    return {
        "host": monitor.get("主机", "") if isinstance(monitor, dict) else "",
        "metric": monitor.get("监控项", "") if isinstance(monitor, dict) else "",
        "value": parsed_data.get("当前值", ""),
    }
