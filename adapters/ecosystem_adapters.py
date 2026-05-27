"""
Ecosystem Adapters for WebhookWise.
Handles normalization of various webhook sources into a standard format.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from adapters.normalized import AlertIdentity, with_alert_identity
from core.logger import get_logger
from services.webhooks.types import JsonObject, WebhookData, webhook_data_from_mapping

logger = get_logger("ecosystem_adapters")

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


def normalize_level(value: Any) -> str:
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


# ── 统一适配器实现 ──────────────────────────────────────────────────────────────


def register_simple_adapters() -> None:
    """注册轻量级适配器，直接实现在此文件中以减少文件碎片。"""
    from adapters.registry import registry

    # 幂等保护：已注册过就跳过
    if registry.find_adapter_by_source("volcengine") is not None:
        return

    # 1. 火山引擎 (Volcengine CloudMonitor)
    @registry.register_detector("volcengine")
    def _detect_volc(data: JsonObject) -> bool:
        return str(data.get("Namespace", "")).startswith("VCM_") and bool(data.get("Resources"))

    @registry.register("volcengine", aliases={"volc", "vcm", "cloudmonitor", "volcengine_cloudmonitor"})
    def _norm_volc(data: JsonObject) -> WebhookData:
        resources = _safe_resource_list(data.get("Resources"))
        resource = _pick_first_resource(resources)
        name = _pick_first(data.get("RuleName"), data.get("AlertName"), data.get("MetricName"), data.get("Type"))
        return with_alert_identity(
            dict(data),
            AlertIdentity(
                source="volcengine",
                name=str(name) if name else None,
                resource=resource,
                fingerprint=str(fingerprint) if (fingerprint := _pick_first(data.get("alert_id"), data.get("AlertId"), data.get("ID"))) else None,
                severity=normalize_level(data.get("Level") or data.get("Severity")),
            ),
        )

    # 2. Grafana
    @registry.register_detector("grafana")
    def _detect_grafana(data: JsonObject) -> bool:
        return any(k in data for k in ("ruleName", "dashboardId")) and any(k in data for k in ("state", "status"))

    @registry.register("grafana", aliases={"grafana"})
    def _norm_grafana(data: JsonObject) -> WebhookData:
        rule = _pick_first(data.get("ruleName"), data.get("title"), "grafana_alert")
        state = _pick_first(data.get("state"), data.get("status"))
        res = dict(data)
        res.update({"Type": "GrafanaAlert", "RuleName": rule, "Level": normalize_level(state), "event": "alert"})
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
                severity=normalize_level(state),
            ),
        )

    # 3. Prometheus / Alertmanager
    @registry.register_detector("prometheus")
    def _detect_prom(data: JsonObject) -> bool:
        return isinstance(data.get("alerts"), list) and len(data["alerts"]) > 0

    @registry.register("prometheus", aliases={"prometheus", "alertmanager"})
    def _norm_prom(data: JsonObject) -> WebhookData:
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
                "Level": normalize_level(_pick_label(labels, "severity", "internal_label_alert_level")),
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
                severity=normalize_level(_pick_label(labels, "severity", "internal_label_alert_level")),
            ),
        )

    # 4. Datadog
    @registry.register_detector("datadog")
    def _detect_datadog(data: JsonObject) -> bool:
        return sum(1 for k in ("alert_type", "event_type", "query") if k in data) >= 2

    @registry.register("datadog", aliases={"datadog"})
    def _norm_datadog(data: JsonObject) -> WebhookData:
        tags = data.get("tags", [])
        title = _pick_first(data.get("alert_name"), data.get("title"), "datadog_alert")
        level = _pick_first(data.get("alert_type"), data.get("priority"))
        res = dict(data)
        res.update({"Type": "DatadogAlert", "RuleName": title, "Level": normalize_level(level), "event": "alert"})
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
                fingerprint=str(fingerprint) if (fingerprint := _pick_first(data.get("id"), data.get("event_id"))) else None,
                severity=normalize_level(level),
            ),
        )

    # 5. PagerDuty
    @registry.register_detector("pagerduty")
    def _detect_pagerduty(data: JsonObject) -> bool:
        return "incident" in data or (isinstance(data.get("event"), dict) and "event_type" in data["event"])

    @registry.register("pagerduty", aliases={"pagerduty"})
    def _norm_pagerduty(data: JsonObject) -> WebhookData:
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
                "Level": normalize_level(inc.get("urgency") or evt.get("event_type")),
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
                severity=normalize_level(inc.get("urgency") or evt.get("event_type")),
            ),
        )

    # 6. 飞书卡片（火山引擎日志服务等）
    @registry.register_detector("feishu_card")
    def _detect_feishu_card(data: JsonObject) -> bool:
        return data.get("msg_type") == "interactive" and isinstance(data.get("card"), dict)

    @registry.register("feishu_card", aliases={"feishu_card", "volcengine_log"})
    def _norm_feishu_card(data: JsonObject) -> WebhookData:
        card = data.get("card", {})
        card = card if isinstance(card, Mapping) else {}
        header = card.get("header", {})
        elements = card.get("elements", [])

        header_title = ""
        if isinstance(header, Mapping):
            title_obj = header.get("title", {})
            if isinstance(title_obj, Mapping):
                header_title = str(title_obj.get("content", "") or "").strip()
            elif isinstance(title_obj, str):
                header_title = title_obj.strip()

        content_text = ""
        if isinstance(elements, list):
            for elem in elements:
                if isinstance(elem, Mapping) and elem.get("tag") == "markdown":
                    content_text = str(elem.get("content", "") or "").strip()
                    break

        alert_strategy = ""
        log_topic = ""
        alert_level = ""
        first_trigger_time = ""
        trigger_condition = ""
        query_result = ""

        for line in content_text.split("\n"):
            line = line.strip()
            if "告警策略：" in line:
                alert_strategy = line.split("告警策略：", 1)[1].strip()
            elif "告警日志主题：" in line:
                log_topic = line.split("告警日志主题：", 1)[1].strip()
            elif "告警级别：" in line:
                alert_level = line.split("告警级别：", 1)[1].strip()
            elif "首次触发时间：" in line:
                first_trigger_time = line.split("首次触发时间：", 1)[1].strip()
            elif "触发条件：" in line:
                trigger_condition = line.split("触发条件：", 1)[1].strip()
            elif "当前查询结果：" in line:
                query_result = line.split("当前查询结果：", 1)[1].strip()

        normalized = dict(data)
        normalized.update(
            {
                "Type": "FeishuCard",
                "RuleName": alert_strategy or header_title or "feishu_alert",
                "alert_name": alert_strategy or header_title or "feishu_alert",
                "Level": normalize_level(alert_level),
                "MetricName": log_topic or "feishu_log_alert",
                "event": "alert",
                "event_type": "feishu_card_alert",
                "alert_id": alert_strategy,
            }
        )

        if first_trigger_time:
            normalized["first_trigger_time"] = first_trigger_time
        if trigger_condition:
            normalized["trigger_condition"] = trigger_condition
        if query_result:
            normalized["query_result"] = query_result
        if log_topic:
            normalized["Resources"] = [{"InstanceId": log_topic}]
        if content_text:
            normalized["summary"] = content_text

        return webhook_data_from_mapping(normalized, strict=False)


def initialize_adapters() -> None:
    """Initialize built-in adapters during process startup."""
    global _initialized
    if _initialized:
        return

    register_simple_adapters()
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
        return NormalizedWebhook(resolved_source, webhook_data_from_mapping({"raw": data}), "passthrough")

    h_src = str(_header_get(headers, "X-Webhook-Source") or "").strip().lower()
    s_hint = str(source or "").strip().lower() or h_src

    # 1. 匹配适配器
    adapter_name = registry.find_adapter_by_source(s_hint) if s_hint else None
    if adapter_name is None:
        adapter_name = registry.find_adapter_by_payload(data)

    # 2. 透传逻辑
    if adapter_name is None:
        return NormalizedWebhook(s_hint or "unknown", webhook_data_from_mapping(data, strict=False), "passthrough")

    # 3. 归一化
    normalized = registry.normalize(adapter_name, dict(data))

    # 决定最终来源名称
    placeholder_sources = {"unknown", "custom", "default", "generic"}
    source_is_alias = registry.find_adapter_by_source(s_hint) == adapter_name if s_hint else False
    final_source = s_hint if (s_hint and not source_is_alias and s_hint not in placeholder_sources) else adapter_name

    logger.info("[Adapter] 成功匹配适配器: name=%s, final_source=%s", adapter_name, final_source)
    return NormalizedWebhook(final_source, normalized, adapter_name)
