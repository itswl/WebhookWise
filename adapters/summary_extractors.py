"""
摘要提取注册表

将 source-specific 的摘要字段提取逻辑从 model 层下沉到 adapter 层，
消除 WebhookEvent.to_summary_dict() 中的 if-else 膨胀。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# 注册表: source -> extractor function
_SUMMARY_EXTRACTORS: dict[str, Callable[[dict], dict[str, Any]]] = {}


def register_summary_extractor(source: str):
    """装饰器：注册 source 对应的摘要提取器。"""

    def decorator(func: Callable[[dict], dict[str, Any]]):
        _SUMMARY_EXTRACTORS[source] = func
        logger.debug(f"[Summary Extractor] Registered extractor for: {source}")
        return func

    return decorator


def extract_summary_fields(source: str, parsed_data: dict | None) -> dict[str, Any]:
    """从 parsed_data 中提取 source 特定的摘要字段。

    返回值会被合并到 alert_info 字典中。
    若 source 没有注册提取器或 parsed_data 为空，返回空字典。
    """
    if not parsed_data or source not in _SUMMARY_EXTRACTORS:
        return {}
    try:
        return _SUMMARY_EXTRACTORS[source](parsed_data)
    except Exception:
        logger.warning(f"[Summary Extractor] Failed to extract for source={source}", exc_info=True)
        return {}


# ── 内置提取器 ────────────────────────────────────────────────────────────────


@register_summary_extractor("mongodb")
def _extract_mongodb_summary(parsed_data: dict) -> dict[str, Any]:
    """MongoDB 告警摘要字段提取。"""
    monitor = parsed_data.get("监控项")
    return {
        "host": monitor.get("主机", "") if isinstance(monitor, dict) else "",
        "metric": monitor.get("监控项", "") if isinstance(monitor, dict) else "",
        "value": parsed_data.get("当前值", ""),
    }
