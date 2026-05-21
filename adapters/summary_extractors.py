"""
摘要字段提取

将 source-specific 的摘要字段提取逻辑从 model 层下沉到 adapter 层，
消除 WebhookEvent.to_summary_dict() 中的 if-else 膨胀。
"""

from __future__ import annotations

from typing import Any

from core.logger import get_logger
from services.webhooks.types import WebhookData

logger = get_logger("adapters.summary_extractors")

Summary = dict[str, Any]


def extract_summary_fields(source: str, parsed_data: WebhookData | None) -> Summary:
    """从 parsed_data 中提取 source 特定的摘要字段。

    返回值会被合并到 alert_info 字典中。
    若 source 没有注册提取器或 parsed_data 为空，返回空字典。
    """
    if not parsed_data:
        return {}
    if source != "mongodb":
        return {}
    try:
        return _extract_mongodb_summary(parsed_data)
    except Exception:
        logger.warning("[Summary Extractor] Failed to extract for source=%s", source, exc_info=True)
        return {}


def _extract_mongodb_summary(parsed_data: WebhookData) -> Summary:
    """MongoDB 告警摘要字段提取。"""
    monitor = parsed_data.get("监控项")
    return {
        "host": monitor.get("主机", "") if isinstance(monitor, dict) else "",
        "metric": monitor.get("监控项", "") if isinstance(monitor, dict) else "",
        "value": parsed_data.get("当前值", ""),
    }
