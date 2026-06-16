"""Feishu card builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from contracts.deep_analysis_report import DEEP_ANALYSIS_REPORT_SCHEMA, normalize_deep_analysis_report
from contracts.webhook_payload import JsonObject, WebhookData
from core.datetime_utils import naive_utc, parse_utc_datetime
from core.logger import mask_url
from services.notifications.feishu_parser import _build_identity_content
from services.webhooks.types import AnalysisResult

_IMPORTANCE_TEMPLATE = {"high": "red", "critical": "red", "medium": "orange", "low": "green"}
_IMPORTANCE_LABEL = {
    "high": "🔴 High",
    "critical": "🚨 Critical",
    "medium": "🟡 Medium",
    "low": "🟢 Low",
}
_CHINA_TZ = timezone(timedelta(hours=8), "UTC+8")


def _add_md_section(elements: list[JsonObject], title: str, content: object, max_len: int = 800) -> None:
    text = _truncate_section_text(content, max_len)
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


def _truncate_section_text(value: object, max_len: int) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _single_line(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _string_list(value: object) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [text for text in (_single_line(item) for item in items) if text]


def _markdown_list(value: object, *, max_items: int = 4, max_item_len: int = 180) -> str:
    lines = []
    for item in _string_list(value)[:max_items]:
        text = item if len(item) <= max_item_len else item[: max_item_len - 3] + "..."
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def _normalized_report_from_record(analysis_record: dict[str, Any]) -> dict[str, Any]:
    existing = analysis_record.get("normalized_report")
    if isinstance(existing, dict) and existing.get("schema") == DEEP_ANALYSIS_REPORT_SCHEMA:
        return existing
    return normalize_deep_analysis_report(analysis_record.get("analysis_result")).to_dict()


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
    importance_label = _IMPORTANCE_LABEL.get(importance, "🟡 Medium")

    parsed_obj = webhook_data.get("parsed_data") or webhook_data.get("body") or {}
    parsed = parsed_obj if isinstance(parsed_obj, dict) else {}
    source = webhook_data.get("source", "") or parsed.get("source", "")
    event_type = analysis_result.get("event_type") or parsed.get("event_type", "") or parsed.get("Type", "") or ""
    rule_name = parsed.get("RuleName", "") or parsed.get("alert_name", "")
    event_type_display = f"{event_type}" if event_type and rule_name else event_type or rule_name or "—"

    timestamp = _format_card_time(webhook_data.get("timestamp", ""))

    summary = analysis_result.get("summary", "")
    impact = analysis_result.get("impact_scope", "")
    prefix = "🔁 [Periodic Reminder] " if is_periodic_reminder else ""
    title = f"{prefix}📡 Webhook Event Notification"

    elements: list[JsonObject] = []

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Source**\n{source or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Importance**\n{importance_label}"}},
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**Event Type**\n{event_type_display or '—'}"},
        },
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Time**\n{timestamp or '—'}"}},
    ]
    elements.append({"tag": "div", "fields": fields})
    elements.append({"tag": "hr"})

    identity_content = _build_identity_content(analysis_result, parsed)
    if identity_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🏷️ Alert Identity**\n{identity_content}"}})
        elements.append({"tag": "hr"})

    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📝 Event Summary**\n{summary[:800]}"}})
        elements.append({"tag": "hr"})

    if impact:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🎯 Impact Scope**\n{impact[:600]}"}})
        elements.append({"tag": "hr"})

    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "(No details available)"}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": elements,
        },
    }


def build_ai_error_card(webhook_data: WebhookData, error_reason: str, *, is_degraded: bool = False) -> JsonObject:
    title = "⚠️ AI Analysis Degraded Notification" if is_degraded else "❌ AI Analysis Failed Notification"
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
                        "content": f"**Source**: {webhook_data.get('source', 'uk')}\n**Reason**: {error_reason}",
                    },
                }
            ],
        },
    }


def build_deep_analysis_card(
    analysis_record: dict[str, Any], source: str = "", webhook_event_id: int = 0
) -> JsonObject:
    report = _normalized_report_from_record(analysis_record)
    identity_value = report.get("alert_identity")
    identity: dict[str, Any] = identity_value if isinstance(identity_value, dict) else {}
    display_source = source or str(identity.get("source") or "")
    engine = analysis_record.get("engine", "uk")
    duration = analysis_record.get("duration_seconds") or 0
    confidence = report.get("confidence")
    confidence_percent = round(confidence * 100) if isinstance(confidence, (int, float)) else 0
    analysis_failed = bool(report.get("analysis_failed"))

    summary = _truncate_section_text(report.get("summary"), 900)
    root_cause = report.get("root_cause") or report.get("failure_reason")
    impact = report.get("impact")
    recommendations = report.get("recommendations")
    evidence = report.get("evidence")
    next_checks = report.get("next_checks")

    elements: list[JsonObject] = [
        {
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**Source**\n{display_source or '—'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**Alert ID**\n{webhook_event_id or '—'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**Engine**\n{engine or '—'}"}},
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**Duration**\n{_float_or_zero(duration):.1f}s"},
                },
            ],
        },
        {"tag": "hr"},
    ]

    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📝 Analysis Summary**\n{summary}"}})
        elements.append({"tag": "hr"})

    _add_md_section(elements, "🔍 Root Cause", root_cause, 1000)
    _add_md_section(elements, "💥 Impact Assessment", impact, 800)

    recommendation_md = _markdown_list(recommendations, max_items=4, max_item_len=240)
    if recommendation_md:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🛠️ Recommendations**\n{recommendation_md}"}})
        elements.append({"tag": "hr"})

    evidence_md = _markdown_list(evidence, max_items=4, max_item_len=220)
    if evidence_md:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📌 Key Evidence**\n{evidence_md}"}})
        elements.append({"tag": "hr"})

    next_checks_md = _markdown_list(next_checks, max_items=4, max_item_len=220)
    if next_checks_md:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**✅ Next Checks**\n{next_checks_md}"}})
        elements.append({"tag": "hr"})

    identity_content = _build_identity_content({"alert_identity": identity}, {})
    if identity_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**🏷️ Alert Identity**\n{identity_content}"}})
        elements.append({"tag": "hr"})

    if len(elements) == 2:
        fallback = _truncate_section_text(report.get("primary_text") or report.get("raw_text"), 1200) or "None"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📋 Analysis Content**\n{fallback}"}})
        elements.append({"tag": "hr"})

    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": (
                        f"Engine: {engine} | Confidence: {confidence_percent}% | "
                        f"Duration: {_float_or_zero(duration):.1f}s | ID: {webhook_event_id}"
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
                        f"❌ [{display_source}] Deep Analysis Failed"
                        if display_source and analysis_failed
                        else "❌ Deep Analysis Failed"
                        if analysis_failed
                        else f"🔬 [{display_source}] Deep Analysis Complete"
                        if display_source
                        else "🔬 Deep Analysis Complete"
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
    title = "🚨 Forward Retries Exhausted"
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
