from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from core.app_context import get_config_manager
from core.datetime_utils import naive_utc, parse_utc_datetime
from core.logger import mask_url
from services.forwarding.circuit_breakers import RemoteForwardDependencies, build_remote_forward_dependencies, feishu_cb
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import AnalysisResult, ForwardResult, WebhookData

_FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
_FEISHU_HOSTS = ("feishu.cn", "larksuite.com")
_CHINA_TZ = timezone(timedelta(hours=8), "UTC+8")

_IMPORTANCE_TEMPLATE = {"high": "red", "critical": "red", "medium": "orange", "low": "green"}
_IMPORTANCE_LABEL = {
    "high": "🔴 高",
    "critical": "🚨 紧急",
    "medium": "🟡 中",
    "low": "🟢 低",
}


def _truncate_text(text: object, max_len: int) -> str:
    if not text:
        return ""
    normalized = str(text)
    return normalized if len(normalized) <= max_len else normalized[: max_len - 3] + "..."


def _float_or_zero(value: object) -> float:
    if isinstance(value, (int, float, str, bytes)):
        try:
            return float(value or 0)
        except ValueError:
            return 0.0
    return 0.0


def _format_card_time(value: object) -> str:
    if isinstance(value, datetime):
        parsed = naive_utc(value)
    elif isinstance(value, str):
        parsed = parse_utc_datetime(value)
        if parsed is None:
            return value
    else:
        return str(value) if value else ""
    return parsed.replace(tzinfo=UTC).astimezone(_CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def is_feishu_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host in _FEISHU_HOSTS or any(host.endswith(suffix) for suffix in _FEISHU_HOST_SUFFIXES)


def build_feishu_card(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    *,
    is_periodic_reminder: bool = False,
) -> WebhookData:
    importance = str(analysis_result.get("importance", "medium")).strip().lower()
    if "." in importance:
        importance = importance.rsplit(".", 1)[-1]
    template = _IMPORTANCE_TEMPLATE.get(importance, "orange")
    importance_label = _IMPORTANCE_LABEL.get(importance, "🟡 中")

    parsed = webhook_data.get("parsed_data") or webhook_data.get("body") or {}
    source = webhook_data.get("source", "") or parsed.get("source", "")
    event_type = parsed.get("event_type", "") or parsed.get("Type", "") or ""
    rule_name = parsed.get("RuleName", "") or parsed.get("alert_name", "")
    event_type_display = f"{event_type}" if event_type and rule_name else event_type or rule_name or "—"

    timestamp = _format_card_time(webhook_data.get("timestamp", ""))

    summary = analysis_result.get("summary", "")
    impact = analysis_result.get("impact_scope", "")
    actions = analysis_result.get("actions") or []
    if isinstance(actions, list):
        suggestion = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(actions) if a) if actions else ""
    else:
        suggestion = str(actions)

    prefix = "🔁 [周期提醒] " if is_periodic_reminder else ""
    title = f"{prefix}📡 Webhook 事件通知"

    elements: list[WebhookData] = []

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**来源**\n{source or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**重要性**\n{importance_label}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**事件类型**\n{event_type_display or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**时间**\n{timestamp or '—'}"}},
    ]
    elements.append({"tag": "div", "fields": fields})
    elements.append({"tag": "hr"})

    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📝 事件摘要**\n{summary[:800]}"}})
        elements.append({"tag": "hr"})

    if impact:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🎯 影响范围**\n{impact[:600]}"}})
        elements.append({"tag": "hr"})

    if suggestion:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**✅ 建议操作**\n{suggestion[:800]}"}})

    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "（暂无详情）"}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": elements,
        },
    }


def build_ai_error_card(webhook_data: WebhookData, error_reason: str, *, is_degraded: bool = False) -> WebhookData:
    title = "⚠️ AI 分析降级通知" if is_degraded else "❌ AI 分析失败通知"
    template = "orange" if is_degraded else "red"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**来源**: {webhook_data.get('source', 'uk')}\n**原因**: {error_reason}",
                    },
                }
            ],
        },
    }


def build_deep_analysis_card(
    analysis_record: dict[str, Any], source: str = "", webhook_event_id: int = 0
) -> WebhookData:
    result = analysis_record.get("analysis_result", {})
    result = result if isinstance(result, dict) else {}
    engine = analysis_record.get("engine", "uk")
    duration = analysis_record.get("duration_seconds") or 0
    confidence = result.get("confidence", 0)
    confidence_percent = round(confidence * 100) if isinstance(confidence, (int, float)) else 0

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🔬 [{source}] 深度分析完成" if source else "🔬 深度分析完成",
                },
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**🔍 根因分析：**\n{_truncate_text(result.get('root_cause', '无'), 500)}",
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**💥 影响范围：**\n{_truncate_text(result.get('impact', '无'), 500)}",
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": (
                                f"引擎: {engine} | 置信度: {confidence_percent}% | "
                                f"耗时: {_float_or_zero(duration):.1f}s | ID: {webhook_event_id}"
                            ),
                        }
                    ],
                },
            ],
        },
    }


def build_delivery_exhausted_card(outbox: Any) -> WebhookData:
    outbox_id = getattr(outbox, "id", None)
    event_id = getattr(outbox, "webhook_event_id", None)
    target_type = getattr(outbox, "target_type", "") or getattr(outbox, "channel_name", "")
    target_url = getattr(outbox, "target_url", "") or ""
    attempts = getattr(outbox, "attempts", 0)
    max_attempts = getattr(outbox, "max_attempts", 0)
    last_error = str(getattr(outbox, "last_error", "") or "")[:500]
    title = "🚨 转发重试耗尽"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**Outbox ID**\n{outbox_id or '—'}"},
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**Webhook ID**\n{event_id or '—'}"},
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**Target Type**\n{target_type or '—'}"},
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**Attempts**\n{attempts}/{max_attempts}",
                            },
                        },
                    ],
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**Target**\n{mask_url(target_url) if target_url else '—'}"},
                },
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**Last Error**\n{last_error or '—'}"}},
            ],
        },
    }


async def send_to_feishu(url: str, payload: dict[str, Any]) -> ForwardResult:
    from dataclasses import replace

    from services.forwarding.remote import post_json_to_remote

    timeout_seconds = int(get_config_manager().notifications.FEISHU_WEBHOOK_TIMEOUT)
    policy = replace(ForwardDeliveryPolicy.from_config(), timeout_seconds=timeout_seconds)
    base_dependencies = build_remote_forward_dependencies()
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
    )
