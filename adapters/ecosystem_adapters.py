"""
Ecosystem Adapters for WebhookWise.
Handles normalization of various webhook sources into a standard format.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from adapters.normalized import AlertIdentity, with_alert_identity
from services.webhooks.types import WebhookData

logger = logging.getLogger("webhook_service.ecosystem_adapters")

HeadersLike = Mapping[str, Any]
_initialized = False


@dataclass(frozen=True)
class NormalizedWebhook:
    source: str
    data: WebhookData
    adapter: str


# ── 基础工具函数 ──────────────────────────────────────────────────────────────


def _header_get(headers: HeadersLike | None, key: str) -> str | None:
    if not headers:
        return None
    value = headers.get(key)
    if value is not None:
        return str(value)
    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return None


def _normalize_level(value: Any) -> str:
    text = str(value or "").strip().lower()
    high = {
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
    medium = {"warning", "warn", "p1", "medium", "moderate", "acknowledged", "警告"}
    low = {"info", "ok", "resolved", "normal", "low", "notice", "恢复", "已恢复", "正常"}

    if text in high:
        return "critical"
    if text in medium:
        return "warning"
    if text in low:
        return "info"

    if any(k in text for k in high):
        return "critical"
    if any(k in text for k in medium):
        return "warning"
    if any(k in text for k in ("resolved", "ok", "normal", "low", "info")):
        return "info"
    return "warning"


_T = TypeVar("_T")


def _pick_first(*values: _T | None) -> _T | None:
    for v in values:
        if v is not None and str(v).strip():
            return v
    return None


def _pick_label(labels: Mapping[str, Any], *keys: str) -> Any | None:
    lower_map = {str(k).lower(): v for k, v in labels.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value is not None and str(value).strip():
            return value
    return None


def _extract_tag(tags: object, key: str) -> str | None:
    if not isinstance(tags, list):
        return None
    prefix = f"{key}:"
    for t in tags:
        if isinstance(t, str) and t.startswith(prefix):
            return t[len(prefix) :].strip()
    return None


def _safe_resource_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _pick_first_resource(resources: list[dict[str, Any]]) -> str | None:
    if not resources:
        return None
    first = resources[0]
    resource = _pick_first(first.get("InstanceId"), first.get("ResourceID"), first.get("Id"), first.get("id"))
    if resource:
        return str(resource)
    dimensions = first.get("Dimensions")
    if not isinstance(dimensions, list):
        return None
    important = {"Node", "ResourceID", "Instance", "InstanceId", "Host", "Pod", "Container"}
    for item in dimensions:
        if isinstance(item, dict) and item.get("Name") in important and item.get("Value"):
            return str(item["Value"])
    return None


# ── 统一适配器实现 (由插件发现机制调用) ──────────────────────────────────────────────


def register_simple_adapters() -> None:
    """注册轻量级适配器，直接实现在此文件中以减少文件碎片。"""
    from adapters.registry import registry

    # 幂等保护：已注册过就跳过
    if registry.find_adapter_by_source("volcengine") is not None:
        return

    # 1. 火山引擎 (Volcengine CloudMonitor)
    @registry.register_detector("volcengine")
    def _detect_volc(data: WebhookData) -> bool:
        return str(data.get("Namespace", "")).startswith("VCM_") and bool(data.get("Resources"))

    @registry.register("volcengine", aliases={"volc", "vcm", "cloudmonitor", "volcengine_cloudmonitor"})
    def _norm_volc(data: WebhookData) -> WebhookData:
        resources = _safe_resource_list(data.get("Resources"))
        resource = _pick_first_resource(resources)
        name = _pick_first(data.get("RuleName"), data.get("AlertName"), data.get("MetricName"), data.get("Type"))
        return with_alert_identity(
            dict(data),
            AlertIdentity(
                source="volcengine",
                name=str(name) if name else None,
                resource=resource,
                fingerprint=_pick_first(data.get("alert_id"), data.get("AlertId"), data.get("ID")),
                severity=_normalize_level(data.get("Level") or data.get("Severity")),
            ),
        )

    # 2. Grafana
    @registry.register_detector("grafana")
    def _detect_grafana(data: WebhookData) -> bool:
        return any(k in data for k in ("ruleName", "dashboardId")) and any(k in data for k in ("state", "status"))

    @registry.register("grafana", aliases={"grafana"})
    def _norm_grafana(data: WebhookData) -> WebhookData:
        rule = _pick_first(data.get("ruleName"), data.get("title"), "grafana_alert")
        state = _pick_first(data.get("state"), data.get("status"))
        res = dict(data)
        res.update({"Type": "GrafanaAlert", "RuleName": rule, "Level": _normalize_level(state), "event": "alert"})
        if "message" in data:
            res["summary"] = data["message"]
        if "ruleId" in data or "panelId" in data:
            res["Resources"] = [{"InstanceId": str(_pick_first(data.get("ruleId"), data.get("panelId")))}]
        return with_alert_identity(
            res,
            AlertIdentity(
                source="grafana",
                name=str(rule) if rule else None,
                resource=str(_pick_first(data.get("ruleId"), data.get("panelId"), data.get("dashboardId")) or ""),
                severity=_normalize_level(state),
            ),
        )

    # 3. Prometheus / Alertmanager
    @registry.register_detector("prometheus")
    def _detect_prom(data: WebhookData) -> bool:
        return isinstance(data.get("alerts"), list) and len(data["alerts"]) > 0

    @registry.register("prometheus", aliases={"prometheus", "alertmanager"})
    def _norm_prom(data: WebhookData) -> WebhookData:
        first = data.get("alerts", [{}])[0]
        labels = first.get("labels", {})
        labels = labels if isinstance(labels, Mapping) else {}
        annotations = first.get("annotations", {})
        annotations = annotations if isinstance(annotations, Mapping) else {}
        name = _pick_first(
            _pick_label(labels, "alertname", "alert_name", "internal_label_alertname"),
            data.get("alertingRuleName"),
            "prometheus_alert",
        )
        res = dict(data)
        res.update(
            {
                "Type": "PrometheusAlert",
                "RuleName": name,
                "Level": _normalize_level(_pick_label(labels, "severity", "internal_label_alert_level")),
                "event": "alert",
            }
        )
        summary = _pick_first(annotations.get("summary"), annotations.get("description"))
        if summary:
            res["summary"] = summary
        instance = _pick_first(
            _pick_label(labels, "instance", "pod", "host", "node", "container", "deployment"),
            _pick_label(labels, "resource", "resource_id", "internal_label_resource"),
        )
        namespace = _pick_first(_pick_label(labels, "namespace", "internal_label_namespace", "kubernetes_namespace"))
        service = _pick_first(
            _pick_label(
                labels,
                "service",
                "internal_label_service",
                "app",
                "app_kubernetes_io_name",
                "app.kubernetes.io/name",
                "k8s_app",
                "job",
            )
        )
        if instance:
            res["Resources"] = [{"InstanceId": instance}]
        return with_alert_identity(
            res,
            AlertIdentity(
                source="prometheus",
                name=str(name) if name else None,
                resource=str(instance or service or namespace or ""),
                service=str(service) if service else None,
                fingerprint=_pick_first(
                    first.get("fingerprint"),
                    _pick_label(labels, "fingerprint", "internal_label_alert_id", "alert_id", "rule_id"),
                ),
                severity=_normalize_level(_pick_label(labels, "severity", "internal_label_alert_level")),
            ),
        )

    # 4. Datadog
    @registry.register_detector("datadog")
    def _detect_datadog(data: WebhookData) -> bool:
        return sum(1 for k in ("alert_type", "event_type", "query") if k in data) >= 2

    @registry.register("datadog", aliases={"datadog"})
    def _norm_datadog(data: WebhookData) -> WebhookData:
        tags = data.get("tags", [])
        title = _pick_first(data.get("alert_name"), data.get("title"), "datadog_alert")
        level = _pick_first(data.get("alert_type"), data.get("priority"))
        res = dict(data)
        res.update({"Type": "DatadogAlert", "RuleName": title, "Level": _normalize_level(level), "event": "alert"})
        host = _pick_first(data.get("host"), _extract_tag(tags, "host"), _extract_tag(tags, "instance"))
        if host:
            res["Resources"] = [{"InstanceId": host}]
        if "text" in data or "body" in data:
            res["summary"] = _pick_first(data.get("text"), data.get("body"))
        return with_alert_identity(
            res,
            AlertIdentity(
                source="datadog",
                name=str(title) if title else None,
                resource=str(host) if host else None,
                service=_extract_tag(tags, "service"),
                fingerprint=_pick_first(data.get("id"), data.get("event_id")),
                severity=_normalize_level(level),
            ),
        )

    # 5. PagerDuty
    @registry.register_detector("pagerduty")
    def _detect_pagerduty(data: WebhookData) -> bool:
        return "incident" in data or (isinstance(data.get("event"), dict) and "event_type" in data["event"])

    @registry.register("pagerduty", aliases={"pagerduty"})
    def _norm_pagerduty(data: WebhookData) -> WebhookData:
        inc = data.get("incident", {})
        evt = data.get("event", {})
        alert_id = inc.get("id") or evt.get("data", {}).get("id")
        service = inc.get("service", {}).get("summary") or evt.get("data", {}).get("service", {}).get("summary")
        title = _pick_first(
            inc.get("title"), evt.get("data", {}).get("title"), data.get("description"), "pagerduty_incident"
        )
        res = dict(data)
        res.update(
            {
                "Type": "PagerDutyEvent",
                "RuleName": title,
                "Level": _normalize_level(inc.get("urgency") or evt.get("event_type")),
                "event": evt.get("event_type", "alert"),
                "alert_id": alert_id,
                "service": service,
            }
        )
        return with_alert_identity(
            res,
            AlertIdentity(
                source="pagerduty",
                name=str(title) if title else None,
                service=str(service) if service else None,
                fingerprint=str(alert_id) if alert_id else None,
                severity=_normalize_level(inc.get("urgency") or evt.get("event_type")),
            ),
        )


def initialize_adapters() -> None:
    """Initialize built-in and plugin adapters during process startup."""
    global _initialized
    if _initialized:
        return

    from adapters.registry import registry

    register_simple_adapters()
    registry.auto_discover()
    _initialized = True
    logger.info("[Adapter] 适配器注册完成")


def normalize_webhook_event(
    data: Any,
    source: str | None,
    headers: HeadersLike | None = None,
) -> NormalizedWebhook:
    """根据 source 或 payload 特征选择适配器，并输出标准化数据。"""
    from adapters.registry import registry

    if not isinstance(data, dict):
        resolved_source = str(source or _header_get(headers, "X-Webhook-Source") or "unknown").strip().lower()
        return NormalizedWebhook(resolved_source, {"raw": data}, "passthrough")

    h_src = str(_header_get(headers, "X-Webhook-Source") or "").strip().lower()
    s_hint = str(source or "").strip().lower() or h_src

    # 1. 匹配适配器
    adapter_name = registry.find_adapter_by_source(s_hint) if s_hint else None
    if adapter_name is None:
        adapter_name = registry.find_adapter_by_payload(data)

    # 2. 透传逻辑
    if adapter_name is None:
        return NormalizedWebhook(s_hint or "unknown", dict(data), "passthrough")

    # 3. 归一化
    normalized = registry.normalize(adapter_name, dict(data))

    # 决定最终来源名称
    placeholder_sources = {"unknown", "custom", "default", "generic"}
    source_is_alias = registry.find_adapter_by_source(s_hint) == adapter_name if s_hint else False
    final_source = s_hint if (s_hint and not source_is_alias and s_hint not in placeholder_sources) else adapter_name

    logger.info("[Adapter] 成功匹配适配器: name=%s, final_source=%s", adapter_name, final_source)
    return NormalizedWebhook(final_source, normalized, adapter_name)
