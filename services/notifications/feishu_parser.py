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


def _build_identity_content(analysis_result: dict[str, Any] | Mapping[str, Any], parsed: dict[str, Any]) -> str:
    labels = {
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
            lines.append(" | ".join(parts))
    return "\n".join(lines)
