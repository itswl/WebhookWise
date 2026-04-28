from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.config import Config
from core.http_client import get_http_client
from core.utils import feishu_cb

logger = logging.getLogger("webhook_service.ecosystem_adapters")

WebhookData = dict[str, Any]
HeadersLike = Mapping[str, Any]


@dataclass(frozen=True)
class NormalizedWebhook:
    source: str
    data: WebhookData
    adapter: str


def _header_get(headers: HeadersLike | None, key: str) -> str | None:
    if not headers:
        return None

    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return None


def _normalize_source(source: str | None) -> str:
    return str(source or "").strip().lower()


def _normalize_level(value: Any) -> str:
    text = str(value or "").strip().lower()

    high_keywords = {
        "critical",
        "error",
        "fatal",
        "p0",
        "sev1",
        "severe",
        "high",
        "urgent",
        "alerting",
        "firing",
        "triggered",
        "严重",
        "紧急",
    }
    medium_keywords = {"warning", "warn", "p1", "medium", "moderate", "acknowledged", "警告"}
    low_keywords = {"info", "ok", "resolved", "normal", "low", "notice", "恢复", "已恢复", "正常"}

    if text in high_keywords:
        return "critical"
    if text in medium_keywords:
        return "warning"
    if text in low_keywords:
        return "info"

    if any(keyword in text for keyword in ("critical", "fatal", "error", "p0", "sev1", "high", "urgent")):
        return "critical"
    if any(keyword in text for keyword in ("warning", "warn", "p1", "medium", "moderate")):
        return "warning"
    if any(keyword in text for keyword in ("resolved", "ok", "normal", "low", "info")):
        return "info"

    return "warning"


def _pick_first_string(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_tag_value(tags: Any, key: str) -> str | None:
    if not isinstance(tags, list):
        return None

    prefix = f"{key}:"
    for tag in tags:
        if not isinstance(tag, str):
            continue
        if tag.startswith(prefix):
            value = tag[len(prefix) :].strip()
            if value:
                return value
    return None


def normalize_webhook_event(
    data: Any,
    source: str | None,
    headers: HeadersLike | None = None,
) -> NormalizedWebhook:
    """根据 source 或 payload 特征选择适配器，并输出标准化数据。"""
    from adapters.registry import registry

    registry.auto_discover()  # 幂等，确保插件已加载

    if not isinstance(data, dict):
        resolved_source = (
            _normalize_source(source) or _normalize_source(_header_get(headers, "X-Webhook-Source")) or "unknown"
        )
        return NormalizedWebhook(resolved_source, {"raw": data}, "passthrough")

    header_source = _normalize_source(_header_get(headers, "X-Webhook-Source"))
    source_hint = _normalize_source(source) or header_source

    # 1. 优先按 source / X-Webhook-Source 别名匹配
    adapter_name = None
    if source_hint:
        adapter_name = registry.find_adapter_by_source(source_hint)

    # 2. 回退通过负载特征探测
    if adapter_name is None:
        adapter_name = registry.find_adapter_by_payload(data)

    # 3. 均未命中则透传
    if adapter_name is None:
        final_source = source_hint or "unknown"
        logger.info(f"[Adapter] 未能匹配特定适配器，使用透传模式: source={final_source}")
        return NormalizedWebhook(final_source, dict(data), "passthrough")

    # 4. 调用归一化
    normalized = registry.normalize(adapter_name, dict(data))

    # 显式 source 不是生态来源时保留（避免覆盖业务自定义来源）
    # 但 unknown/custom/default 等占位来源在命中适配器后应切换为生态来源
    placeholder_sources = {"unknown", "custom", "default", "generic"}
    source_is_alias = registry.find_adapter_by_source(source_hint) == adapter_name if source_hint else False
    if source_hint and not source_is_alias and source_hint not in placeholder_sources:
        final_source = source_hint
    else:
        final_source = adapter_name

    logger.info(f"[Adapter] 成功匹配适配器: name={adapter_name}, final_source={final_source}")
    return NormalizedWebhook(final_source, normalized, adapter_name)


# ========== 飞书深度分析通知 ==========


def _truncate_text(text: str, max_len: int) -> str:
    """截断文本，超长时添加省略号"""
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _format_recommendations(recs: Any, max_items: int = 5, max_item_len: int = 200) -> str:
    """格式化修复建议列表，兼容字符串数组和对象数组"""
    if not recs:
        return "无"

    if not isinstance(recs, (list, tuple)):
        return _truncate_text(str(recs), max_item_len)

    lines = []
    for i, rec in enumerate(recs[:max_items], 1):
        if isinstance(rec, dict):
            priority = rec.get("priority", "")
            action = rec.get("action", str(rec))
            action = _truncate_text(action, max_item_len)
            if priority:
                lines.append(f"{i}. **{priority}**: {action}")
            else:
                lines.append(f"{i}. {action}")
        else:
            lines.append(f"{i}. {_truncate_text(str(rec), max_item_len)}")

    if len(recs) > max_items:
        lines.append(f"... 还有 {len(recs) - max_items} 条建议")

    return "\n".join(lines) if lines else "无"


async def send_feishu_deep_analysis(
    webhook_url: str, analysis_record: dict, source: str = "", webhook_event_id: int = 0
) -> bool:
    """
    发送深度分析结果到飞书

    Args:
        webhook_url: 飞书 webhook URL
        analysis_record: 深度分析记录，包含 analysis_result, engine, duration_seconds 等
        source: 告警来源
        webhook_event_id: 关联的 webhook 事件 ID

    Returns:
        bool: 是否发送成功
    """
    if not webhook_url:
        return False

    result = analysis_record.get("analysis_result", {})
    if not isinstance(result, dict):
        result = {}

    engine = analysis_record.get("engine", "unknown")
    duration = analysis_record.get("duration_seconds", 0)
    confidence = result.get("confidence", 0)
    if isinstance(confidence, (int, float)):
        confidence = round(confidence * 100)

    # 提取分析结果字段（排除内部字段）
    root_cause = _truncate_text(result.get("root_cause", "无"), 500)
    impact = _truncate_text(result.get("impact", "无"), 500)
    recommendations = _format_recommendations(result.get("recommendations", []))

    # 构建标题
    title = "🔬 深度分析完成"
    if source:
        title = f"🔬 [{source}] 深度分析完成"

    # 构建飞书消息卡片
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": [
                # 根因分析
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**🔍 根因分析：**\n{root_cause}"}},
                {"tag": "hr"},
                # 影响范围
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**💥 影响范围：**\n{impact}"}},
                {"tag": "hr"},
                # 修复建议
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**✅ 修复建议：**\n{recommendations}"}},
                {"tag": "hr"},
                # 元信息
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"引擎: {engine} | 置信度: {confidence}% | 耗时: {duration:.1f}s | ID: {webhook_event_id}",
                        }
                    ],
                },
            ],
        },
    }

    client = get_http_client()
    resp = await feishu_cb.call_async(client.post, webhook_url, json=card, timeout=Config.FEISHU_WEBHOOK_TIMEOUT)

    if resp is None:
        logger.warning(f"飞书深度分析通知被熔断拦截: webhook_event_id={webhook_event_id}")
        return False

    try:
        if resp.status_code == 200:
            logger.info(f"飞书深度分析通知发送成功: webhook_event_id={webhook_event_id}")
            return True
        else:
            logger.warning(f"飞书深度分析通知发送失败: status={resp.status_code}, response={resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"飞书深度分析通知异常: {e}")
        return False
