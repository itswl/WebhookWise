from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from core.app_context import get_config_manager
from core.datetime_utils import naive_utc, parse_utc_datetime
from core.logger import mask_url
from services.forwarding.circuit_breakers import RemoteForwardDependencies, build_remote_forward_dependencies, feishu_cb
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import AnalysisResult, ForwardResult, JsonObject, WebhookData

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
_IDENTITY_LABELS = (
    ("project", "项目"),
    ("region", "区域"),
    ("product_namespace", "云产品"),
    ("service", "服务"),
    ("resource_name", "资源"),
    ("resource_id", "资源ID"),
    ("rule_name", "规则"),
    ("metric_name", "指标"),
    ("severity", "级别"),
    ("status", "状态"),
)
_IDENTITY_GROUPS = (
    ("project", "region", "product_namespace", "service"),
    ("resource_name", "resource_id"),
    ("rule_name", "metric_name", "severity", "status"),
)
_DEEP_TEXT_FIELD_CANDIDATES = (
    "summary",
    "description",
    "finding",
    "observation",
    "action",
    "reason",
    "message",
    "content",
    "text",
    "root_cause",
    "impact",
    "impact_scope",
    "error",
    "failure_reason",
    "name",
    "title",
)


def _truncate_text(text: object, max_len: int) -> str:
    if not text:
        return ""
    normalized = str(text)
    return normalized if len(normalized) <= max_len else normalized[: max_len - 3] + "..."


def _single_line(text: object) -> str:
    return " ".join(str(text or "").split()).strip()


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped, flags=re.IGNORECASE)
    return stripped.strip()


def _sanitize_loose_json(text: str) -> str:
    return re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)


def _extract_json_block(text: str) -> str:
    start = -1
    for idx, char in enumerate(text):
        if char in "{[":
            start = idx
            break
    if start < 0:
        return ""

    pairs = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return text[start : idx + 1]
    return ""


def _parse_json_like_text(value: object) -> JsonObject | list[Any] | None:
    if not isinstance(value, str):
        return None
    stripped = _strip_json_fence(value)
    if not stripped:
        return None
    json_block = _extract_json_block(stripped)
    candidates = [stripped, _sanitize_loose_json(stripped), json_block, _sanitize_loose_json(json_block)]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _display_deep_value(value: object, *, separator: str = "\n", max_depth: int = 3, _depth: int = 0) -> str:
    if value in (None, ""):
        return ""
    parsed = _parse_json_like_text(value)
    if parsed is not None:
        return _display_deep_value(parsed, separator=separator, max_depth=max_depth, _depth=_depth + 1)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return separator.join(
            item
            for item in (
                _display_deep_value(v, separator=separator, max_depth=max_depth, _depth=_depth + 1) for v in value
            )
            if item
        )
    if isinstance(value, dict):
        if _depth < max_depth:
            for key in _DEEP_TEXT_FIELD_CANDIDATES:
                text = _display_deep_value(
                    value.get(key), separator=separator, max_depth=max_depth, _depth=_depth + 1
                )
                if text:
                    return text
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def _deep_analysis_view(result: object) -> JsonObject:
    data = dict(result) if isinstance(result, dict) else {}
    parsed = (
        _parse_json_like_text(data.get("summary"))
        or _parse_json_like_text(data.get("root_cause"))
        or _parse_json_like_text(data.get("_openclaw_text"))
        or _parse_json_like_text(data.get("analysis"))
        or _parse_json_like_text(data.get("details"))
    )
    if isinstance(parsed, dict):
        return {**data, **parsed}
    return data


def _section_text(value: object, max_len: int) -> str:
    return _truncate_text(_display_deep_value(value, separator="\n"), max_len)


def _list_lines(value: object, *, max_items: int, max_item_len: int) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    lines: list[str] = []
    for item in items[:max_items]:
        text = _display_deep_value(item, separator=" ｜ ")
        if text:
            lines.append(_truncate_text(_single_line(text), max_item_len))
    return lines


def _markdown_bullets(value: object, *, max_items: int = 4, max_item_len: int = 180) -> str:
    lines = _list_lines(value, max_items=max_items, max_item_len=max_item_len)
    return "\n".join(f"- {line}" for line in lines)


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


def _identity_value(identity: dict[str, Any], parsed: dict[str, Any], key: str) -> object:
    if key in identity and identity[key]:
        return identity[key]
    if key == "project":
        resources = parsed.get("Resources")
        if isinstance(resources, list) and resources and isinstance(resources[0], dict):
            return resources[0].get("ProjectName") or parsed.get("Project")
        return parsed.get("Project")
    if key == "region":
        resources = parsed.get("Resources")
        if isinstance(resources, list) and resources and isinstance(resources[0], dict):
            return resources[0].get("Region")
    if key == "resource_name":
        resources = parsed.get("Resources")
        if isinstance(resources, list) and resources and isinstance(resources[0], dict):
            return resources[0].get("Name") or resources[0].get("InstanceId")
    if key == "resource_id":
        resources = parsed.get("Resources")
        if isinstance(resources, list) and resources and isinstance(resources[0], dict):
            return resources[0].get("Id") or resources[0].get("InstanceId")
    if key == "rule_name":
        return parsed.get("RuleName") or parsed.get("alert_name")
    if key == "metric_name":
        resources = parsed.get("Resources")
        if isinstance(resources, list) and resources and isinstance(resources[0], dict):
            metrics = resources[0].get("Metrics")
            if isinstance(metrics, list) and metrics and isinstance(metrics[0], dict):
                return metrics[0].get("Name")
        return parsed.get("MetricName")
    if key == "severity":
        return parsed.get("Level") or parsed.get("Severity")
    if key == "status":
        return parsed.get("status")
    return None


def _identity_text(value: object) -> str:
    if not value:
        return ""
    return " ".join(str(value).splitlines()).strip()


def _build_identity_content(analysis_result: AnalysisResult, parsed: dict[str, Any]) -> str:
    identity_raw = analysis_result.get("alert_identity")
    identity = dict(identity_raw) if isinstance(identity_raw, dict) else {}
    labels = dict(_IDENTITY_LABELS)
    values: dict[str, str] = {}
    seen_values: set[tuple[str, str]] = set()
    for key, label in _IDENTITY_LABELS:
        raw = _identity_value(identity, parsed, key)
        value = _identity_text(raw)
        if not value:
            continue
        dedupe_key = (label, value)
        if dedupe_key in seen_values:
            continue
        seen_values.add(dedupe_key)
        values[key] = value

    lines: list[str] = []
    for group in _IDENTITY_GROUPS:
        parts = [f"{labels[key]}: {values[key]}" for key in group if key in values]
        if parts:
            lines.append(" ｜ ".join(parts))
    return "\n".join(lines)


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
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**事件类型**\n{event_type_display or '—'}"}},
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
