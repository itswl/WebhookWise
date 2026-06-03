"""Feishu card builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from contracts.webhook_payload import JsonObject, WebhookData
from core.datetime_utils import naive_utc, parse_utc_datetime
from core.logger import mask_url
from services.notifications.feishu_parser import (
    _build_identity_content,
    _deep_analysis_view,
    _markdown_bullets,
    _section_text,
)
from services.webhooks.types import OPENCLAW_TEXT, AnalysisResult

_IMPORTANCE_TEMPLATE = {"high": "red", "critical": "red", "medium": "orange", "low": "green"}
_IMPORTANCE_LABEL = {
    "high": "🔴 高",
    "critical": "🚨 紧急",
    "medium": "🟡 中",
    "low": "🟢 低",
}
_CHINA_TZ = timezone(timedelta(hours=8), "UTC+8")


def _add_md_section(elements: list[JsonObject], title: str, content: object, max_len: int = 800) -> None:
    text = _section_text(content, max_len)
    if not text:
        return
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{text}"}})
    elements.append({"tag": "hr"})


def _float_or_zero(value: object) -> float:
    if isinstance(value, (int, float, str, bytes)):
        try:
            return float(value or 0)
        except ValueError:
            return 0.0
    return 0.0


def _format_card_time(value: object) -> str:
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = naive_utc(value)
    elif isinstance(value, str):
        parsed = parse_utc_datetime(value)
        if parsed is None:
            return value
    else:
        return str(value) if value else ""
    return parsed.replace(tzinfo=UTC).astimezone(_CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def build_feishu_card(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    *,
    is_periodic_reminder: bool = False,
) -> JsonObject:
    importance = str(analysis_result.get("importance", "medium")).strip().lower()
    if "." in importance:
        importance = importance.rsplit(".", 1)[-1]
    template = _IMPORTANCE_TEMPLATE.get(importance, "orange")
    importance_label = _IMPORTANCE_LABEL.get(importance, "🟡 中")

    parsed_obj = webhook_data.get("parsed_data") or webhook_data.get("body") or {}
    parsed = parsed_obj if isinstance(parsed_obj, dict) else {}
    source = webhook_data.get("source", "") or parsed.get("source", "")
    event_type = analysis_result.get("event_type") or parsed.get("event_type", "") or parsed.get("Type", "") or ""
    rule_name = parsed.get("RuleName", "") or parsed.get("alert_name", "")
    event_type_display = f"{event_type}" if event_type and rule_name else event_type or rule_name or "—"

    timestamp = _format_card_time(webhook_data.get("timestamp", ""))

    summary = analysis_result.get("summary", "")
    impact = analysis_result.get("impact_scope", "")
    prefix = "🔁 [周期提醒] " if is_periodic_reminder else ""
    title = f"{prefix}📡 Webhook 事件通知"

    elements: list[JsonObject] = []

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**来源**\n{source or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**重要性**\n{importance_label}"}},
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**事件类型**\n{event_type_display or '—'}"},
        },
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**时间**\n{timestamp or '—'}"}},
    ]
    elements.append({"tag": "div", "fields": fields})
    elements.append({"tag": "hr"})

    identity_content = _build_identity_content(analysis_result, parsed)
    if identity_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🏷️ 告警定位**\n{identity_content}"}})
        elements.append({"tag": "hr"})

    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📝 事件摘要**\n{summary[:800]}"}})
        elements.append({"tag": "hr"})

    if impact:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🎯 影响范围**\n{impact[:600]}"}})
        elements.append({"tag": "hr"})

    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "（暂无详情）"}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": elements,
        },
    }


def build_ai_error_card(webhook_data: WebhookData, error_reason: str, *, is_degraded: bool = False) -> JsonObject:
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
) -> JsonObject:
    result = analysis_record.get("analysis_result", {})
    result = result if isinstance(result, dict) else {}
    view = _deep_analysis_view(result)
    # 兼容 OpenClaw 文本字段
    if OPENCLAW_TEXT in result and OPENCLAW_TEXT not in view:
        text = result.get(OPENCLAW_TEXT)
        if text:
            view = dict(view)
            view[OPENCLAW_TEXT] = text

    engine = analysis_record.get("engine", "uk")
    duration = analysis_record.get("duration_seconds") or 0
    confidence = view.get("confidence", 0)
    confidence_percent = round(confidence * 100) if isinstance(confidence, (int, float)) else 0
    analysis_failed = bool(view.get("analysis_failed"))

    summary = _section_text(view.get("summary") or view.get("conclusion"), 900)
    root_cause = view.get("root_cause") or view.get("reason") or view.get("analysis") or view.get("failure_reason")
    impact = view.get("impact") or view.get("impact_scope")
    recommendations = view.get("recommendations") or view.get("actions") or view.get("next_steps") or view.get("solution")
    evidence = view.get("evidence") or view.get("supports") or view.get("observations")

    elements: list[JsonObject] = [
        {
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**来源**\n{source or '—'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**告警 ID**\n{webhook_event_id or '—'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**引擎**\n{engine or '—'}"}},
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**耗时**\n{_float_or_zero(duration):.1f}s"},
                },
            ],
        },
        {"tag": "hr"},
    ]

    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📝 分析摘要**\n{summary}"}})
        elements.append({"tag": "hr"})

    _add_md_section(elements, "🔍 根因定位", root_cause, 1000)
    _add_md_section(elements, "💥 影响评估", impact, 800)

    recommendation_md = _markdown_bullets(recommendations, max_items=4, max_item_len=240)
    if recommendation_md:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🛠️ 修复建议**\n{recommendation_md}"}})
        elements.append({"tag": "hr"})

    evidence_md = _markdown_bullets(evidence, max_items=4, max_item_len=220)
    if evidence_md:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📌 关键证据**\n{evidence_md}"}})
        elements.append({"tag": "hr"})

    identity_content = _build_identity_content(view, {})
    if identity_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🏷️ 告警定位**\n{identity_content}"}})
        elements.append({"tag": "hr"})

    if len(elements) == 2:
        fallback = _section_text(view or result, 1200) or "无"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📋 分析内容**\n{fallback}"}})
        elements.append({"tag": "hr"})

    elements.append(
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
        }
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": (
                        f"❌ [{source}] 深度分析失败"
                        if source and analysis_failed
                        else "❌ 深度分析失败"
                        if analysis_failed
                        else f"🔬 [{source}] 深度分析完成"
                        if source
                        else "🔬 深度分析完成"
                    ),
                },
                "template": "red" if analysis_failed else "blue",
            },
            "elements": elements,
        },
    }


def build_delivery_exhausted_card(outbox: Any) -> JsonObject:
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
