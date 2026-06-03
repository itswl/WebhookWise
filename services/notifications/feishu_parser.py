"""Shared parsing utilities for Feishu payload/card formatting."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from contracts.webhook_payload import JsonObject
from core.collections_utils import scalar_text_or_empty
from core.json import extract_balanced_json_text
from services.webhooks.types import OPENCLAW_TEXT

_FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
_FEISHU_HOSTS = ("feishu.cn", "larksuite.com")

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


def is_feishu_url(url: str) -> bool:
    """Return whether `url` is a trusted Feishu webhook endpoint host."""
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in _FEISHU_HOSTS or any(host.endswith(suffix) for suffix in _FEISHU_HOST_SUFFIXES)


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped, flags=re.IGNORECASE)
    return stripped.strip()


def _sanitize_loose_json(text: str) -> str:
    return re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", text)


def _parse_json_like_text(value: object) -> JsonObject | list[Any] | None:
    if not isinstance(value, str):
        return None
    stripped = _strip_json_fence(value)
    if not stripped:
        return None
    json_block = extract_balanced_json_text(stripped, allow_arrays=True) or ""
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
                _display_deep_value(v, separator=separator, max_depth=max_depth, _depth=_depth + 1)
                for v in value
            )
            if item
        )
    if isinstance(value, dict):
        if _depth < max_depth:
            for key in _DEEP_TEXT_FIELD_CANDIDATES:
                text = _display_deep_value(value.get(key), separator=separator, max_depth=max_depth, _depth=_depth + 1)
                if text:
                    return text
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def _section_text(value: object, max_len: int) -> str:
    return _truncate_text(_display_deep_value(value, separator="\n"), max_len)


def _truncate_text(text: object, max_len: int) -> str:
    if not text:
        return ""
    normalized = str(text)
    return normalized if len(normalized) <= max_len else normalized[: max_len - 3] + "..."


def _single_line(text: object) -> str:
    return " ".join(str(text or "").split()).strip()


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


def _deep_analysis_view(result: object) -> JsonObject:
    data = dict(result) if isinstance(result, dict) else {}
    parsed = (
        _parse_json_like_text(data.get("summary"))
        or _parse_json_like_text(data.get("root_cause"))
        or _parse_json_like_text(data.get(OPENCLAW_TEXT))
        or _parse_json_like_text(data.get("analysis"))
        or _parse_json_like_text(data.get("details"))
    )
    if isinstance(parsed, dict):
        return {**data, **parsed}
    return data


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


def _build_identity_content(analysis_result: dict[str, Any] | Mapping[str, Any], parsed: dict[str, Any]) -> str:
    labels = {
        "project": "项目",
        "region": "区域",
        "product_namespace": "云产品",
        "service": "服务",
        "resource_name": "资源",
        "resource_id": "资源ID",
        "rule_name": "规则",
        "metric_name": "指标",
        "severity": "级别",
        "status": "状态",
    }
    values: dict[str, str] = {}
    seen_values: set[tuple[str, str]] = set()
    for key, label in labels.items():
        raw = _identity_value(analysis_result.get("alert_identity", {}) if isinstance(analysis_result, dict) else {}, parsed, key)
        value = scalar_text_or_empty(raw)
        if not value:
            continue
        dedupe_key = (label, value)
        if dedupe_key in seen_values:
            continue
        seen_values.add(dedupe_key)
        values[key] = value

    identity_groups = (("project", "region", "product_namespace", "service"), ("resource_name", "resource_id"), ("rule_name", "metric_name", "severity", "status"))
    lines: list[str] = []
    for group in identity_groups:
        parts = [f"{labels[key]}: {values[key]}" for key in group if key in values]
        if parts:
            lines.append(" ｜ ".join(parts))
    return "\n".join(lines)
