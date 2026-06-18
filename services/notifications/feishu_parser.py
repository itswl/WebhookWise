"""Shared parsing utilities for Feishu payload/card formatting."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from core.collections_utils import scalar_text_or_empty

_FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
_FEISHU_HOSTS = ("feishu.cn", "larksuite.com")


def is_feishu_url(url: str) -> bool:
    """Return whether `url` is a trusted Feishu webhook endpoint host."""
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in _FEISHU_HOSTS or any(host.endswith(suffix) for suffix in _FEISHU_HOST_SUFFIXES)


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


_IDENTITY_LABELS = {
    "project": "项目",
    "region": "区域",
    "product_namespace": "云产品",
    "service": "服务",
    "resource_name": "资源",
    "resource_id": "资源 ID",
    "rule_name": "规则",
    "metric_name": "指标",
    "severity": "级别",
    "status": "状态",
}


def extract_identity_fields(
    analysis_result: dict[str, Any] | Mapping[str, Any], parsed: dict[str, Any]
) -> list[tuple[str, str]]:
    """Return present identity items as ordered (label, value) pairs.

    Structured form so cards can render a scannable two-column grid instead of a
    dense pipe-joined line. Order follows _IDENTITY_LABELS; duplicates by
    (label, value) are dropped.
    """
    identity = analysis_result.get("alert_identity", {}) if isinstance(analysis_result, dict) else {}
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, label in _IDENTITY_LABELS.items():
        value = scalar_text_or_empty(_identity_value(identity, parsed, key))
        if not value:
            continue
        dedupe_key = (label, value)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        pairs.append((label, value))
    return pairs


def _build_identity_content(analysis_result: dict[str, Any] | Mapping[str, Any], parsed: dict[str, Any]) -> str:
    """Pipe/newline-joined identity text (kept for the deep-analysis card)."""
    pairs = extract_identity_fields(analysis_result, parsed)
    label_to_value = dict(pairs)
    identity_groups = (
        ("项目", "区域", "云产品", "服务"),
        ("资源", "资源 ID"),
        ("规则", "指标", "级别", "状态"),
    )
    lines: list[str] = []
    for group in identity_groups:
        parts = [f"{label}: {label_to_value[label]}" for label in group if label in label_to_value]
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)
