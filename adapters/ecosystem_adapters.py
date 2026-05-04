from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.config import Config
from core.http_client import get_http_client
from core.circuit_breaker import feishu_cb

logger = logging.getLogger("webhook_service.ecosystem_adapters")

WebhookData = dict[str, Any]
HeadersLike = Mapping[str, Any]


@dataclass(frozen=True)
class NormalizedWebhook:
    source: str
    data: WebhookData
    adapter: str


# ── 基础工具函数 ──────────────────────────────────────────────────────────────


def _header_get(headers: HeadersLike | None, key: str) -> str | None:
    if not headers: return None
    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target: return str(v)
    return None


def _normalize_level(value: Any) -> str:
    text = str(value or "").strip().lower()
    high = {"critical", "error", "fatal", "p0", "sev1", "severe", "high", "urgent", "alerting", "firing", "严重", "紧急"}
    medium = {"warning", "warn", "p1", "medium", "moderate", "警告"}
    low = {"info", "ok", "resolved", "normal", "low", "notice", "恢复", "已恢复", "正常"}

    if text in high: return "critical"
    if text in medium: return "warning"
    if text in low: return "info"
    
    if any(k in text for k in high): return "critical"
    if any(k in text for k in medium): return "warning"
    return "warning"


def _pick_first(*values: Any) -> Any:
    for v in values:
        if v is not None and str(v).strip(): return v
    return None


def _extract_tag(tags: Any, key: str) -> str | None:
    if not isinstance(tags, list): return None
    prefix = f"{key}:"
    for t in tags:
        if isinstance(t, str) and t.startswith(prefix):
            return t[len(prefix):].strip()
    return None


# ── 统一适配器实现 (由插件发现机制调用) ──────────────────────────────────────────────


def register_simple_adapters():
    """注册轻量级适配器，直接实现在此文件中以减少文件碎片。"""
    from adapters.registry import registry

    # 1. 火山引擎 (Volcengine CloudMonitor)
    @registry.register_detector("volcengine")
    def _detect_volc(data: dict) -> bool:
        return str(data.get("Namespace", "")).startswith("VCM_") and bool(data.get("Resources"))

    @registry.register("volcengine", aliases={"volc", "vcm", "cloudmonitor"})
    def _norm_volc(data: dict) -> dict: return dict(data)

    # 2. Grafana
    @registry.register_detector("grafana")
    def _detect_grafana(data: dict) -> bool:
        return any(k in data for k in ("ruleName", "dashboardId")) and any(k in data for k in ("state", "status"))

    @registry.register("grafana", aliases={"grafana"})
    def _norm_grafana(data: dict) -> dict:
        rule = _pick_first(data.get("ruleName"), data.get("title"), "grafana_alert")
        state = _pick_first(data.get("state"), data.get("status"))
        res = dict(data)
        res.update({"Type": "GrafanaAlert", "RuleName": rule, "Level": _normalize_level(state), "event": "alert"})
        if "message" in data: res["summary"] = data["message"]
        if "ruleId" in data or "panelId" in data: res["Resources"] = [{"InstanceId": str(_pick_first(data.get("ruleId"), data.get("panelId")))}]
        return res

    # 3. Prometheus / Alertmanager
    @registry.register_detector("prometheus")
    def _detect_prom(data: dict) -> bool:
        return isinstance(data.get("alerts"), list) and len(data["alerts"]) > 0

    @registry.register("prometheus", aliases={"prometheus", "alertmanager"})
    def _norm_prom(data: dict) -> dict:
        first = data.get("alerts", [{}])[0]
        labels = first.get("labels", {})
        annotations = first.get("annotations", {})
        name = _pick_first(labels.get("alertname"), data.get("alertingRuleName"), "prometheus_alert")
        res = dict(data)
        res.update({"Type": "PrometheusAlert", "RuleName": name, "Level": _normalize_level(labels.get("severity")), "event": "alert"})
        summary = _pick_first(annotations.get("summary"), annotations.get("description"))
        if summary: res["summary"] = summary
        instance = _pick_first(labels.get("instance"), labels.get("pod"), labels.get("host"))
        if instance: res["Resources"] = [{"InstanceId": instance}]
        return res

    # 4. Datadog
    @registry.register_detector("datadog")
    def _detect_datadog(data: dict) -> bool:
        return sum(1 for k in ("alert_type", "event_type", "query") if k in data) >= 2

    @registry.register("datadog", aliases={"datadog"})
    def _norm_datadog(data: dict) -> dict:
        tags = data.get("tags", [])
        title = _pick_first(data.get("alert_name"), data.get("title"), "datadog_alert")
        level = _pick_first(data.get("alert_type"), data.get("priority"))
        res = dict(data)
        res.update({"Type": "DatadogAlert", "RuleName": title, "Level": _normalize_level(level), "event": "alert"})
        host = _pick_first(data.get("host"), _extract_tag(tags, "host"), _extract_tag(tags, "instance"))
        if host: res["Resources"] = [{"InstanceId": host}]
        if "text" in data or "body" in data: res["summary"] = _pick_first(data.get("text"), data.get("body"))
        return res

    # 5. PagerDuty
    @registry.register_detector("pagerduty")
    def _detect_pagerduty(data: dict) -> bool:
        return "incident" in data or (isinstance(data.get("event"), dict) and "event_type" in data["event"])

    @registry.register("pagerduty", aliases={"pagerduty"})
    def _norm_pagerduty(data: dict) -> dict:
        inc = data.get("incident", {})
        evt = data.get("event", {})
        title = _pick_first(inc.get("title"), evt.get("data", {}).get("title"), data.get("description"), "pagerduty_incident")
        res = dict(data)
        res.update({"Type": "PagerDutyEvent", "RuleName": title, "Level": _normalize_level(inc.get("urgency") or evt.get("event_type")), "event": evt.get("event_type", "alert")})
        return res


def normalize_webhook_event(
    data: Any,
    source: str | None,
    headers: HeadersLike | None = None,
) -> NormalizedWebhook:
    """根据 source 或 payload 特征选择适配器，并输出标准化数据。"""
    from adapters.registry import registry

    # 注册内置简单适配器
    register_simple_adapters()
    # 发现外部插件适配器
    registry.auto_discover()

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

    logger.info(f"[Adapter] 成功匹配适配器: name={adapter_name}, final_source={final_source}")
    return NormalizedWebhook(final_source, normalized, adapter_name)


# ========== 飞书深度分析通知 (保持原样，略作格式精简) ==========


def _truncate_text(text: str, max_len: int) -> str:
    if not text: return ""
    text = str(text)
    return text if len(text) <= max_len else text[:max_len - 3] + "..."


async def send_feishu_deep_analysis(webhook_url: str, analysis_record: dict, source: str = "", webhook_event_id: int = 0) -> bool:
    if not webhook_url: return False
    res = analysis_record.get("analysis_result", {})
    engine, duration = analysis_record.get("engine", "uk"), analysis_record.get("duration_seconds", 0)
    conf = round(res.get("confidence", 0) * 100) if isinstance(res.get("confidence"), (int, float)) else 0

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"🔬 [{'[' + source + '] ' if source else ''}深度分析完成"}, "template": "blue"},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**🔍 根因分析：**\n{_truncate_text(res.get('root_cause', '无'), 500)}"}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**💥 影响范围：**\n{_truncate_text(res.get('impact', '无'), 500)}"}},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"引擎: {engine} | 置信度: {conf}% | 耗时: {duration:.1f}s | ID: {webhook_event_id}"}]},
            ],
        },
    }
    client = get_http_client()
    resp = await feishu_cb.call_async(client.post, webhook_url, json=card, timeout=Config.ai.FEISHU_WEBHOOK_TIMEOUT)
    return resp is not None and resp.status_code == 200
