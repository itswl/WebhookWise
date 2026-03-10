from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

WebhookData = dict[str, Any]
HeadersLike = Mapping[str, Any]


@dataclass(frozen=True)
class NormalizedWebhook:
    source: str
    data: WebhookData
    adapter: str


def _header_get(headers: Optional[HeadersLike], key: str) -> Optional[str]:
    if not headers:
        return None

    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return None


def _normalize_source(source: Optional[str]) -> str:
    return str(source or '').strip().lower()


def _normalize_level(value: Any) -> str:
    text = str(value or '').strip().lower()

    high_keywords = {
        'critical', 'error', 'fatal', 'p0', 'sev1', 'severe', 'high', 'urgent',
        'alerting', 'firing', 'triggered', '严重', '紧急'
    }
    medium_keywords = {
        'warning', 'warn', 'p1', 'medium', 'moderate', 'acknowledged', '警告'
    }
    low_keywords = {
        'info', 'ok', 'resolved', 'normal', 'low', 'notice', '恢复', '已恢复', '正常'
    }

    if text in high_keywords:
        return 'critical'
    if text in medium_keywords:
        return 'warning'
    if text in low_keywords:
        return 'info'

    if any(keyword in text for keyword in ('critical', 'fatal', 'error', 'p0', 'sev1', 'high', 'urgent')):
        return 'critical'
    if any(keyword in text for keyword in ('warning', 'warn', 'p1', 'medium', 'moderate')):
        return 'warning'
    if any(keyword in text for keyword in ('resolved', 'ok', 'normal', 'low', 'info')):
        return 'info'

    return 'warning'


def _pick_first_string(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_tag_value(tags: Any, key: str) -> Optional[str]:
    if not isinstance(tags, list):
        return None

    prefix = f'{key}:'
    for tag in tags:
        if not isinstance(tag, str):
            continue
        if tag.startswith(prefix):
            value = tag[len(prefix):].strip()
            if value:
                return value
    return None


def _is_prometheus(data: WebhookData) -> bool:
    if not isinstance(data, dict):
        return False
    alerts = data.get('alerts')
    return isinstance(alerts, list) and len(alerts) > 0


def _is_grafana(data: WebhookData) -> bool:
    if not isinstance(data, dict):
        return False
    return any(k in data for k in ('ruleName', 'dashboardId', 'panelId')) and any(
        k in data for k in ('state', 'status', 'title')
    )


def _is_pagerduty(data: WebhookData) -> bool:
    if not isinstance(data, dict):
        return False

    event = data.get('event')
    if isinstance(event, dict) and event.get('event_type'):
        return True

    return 'incident' in data and any(k in data for k in ('messages', 'event'))


def _is_datadog(data: WebhookData) -> bool:
    if not isinstance(data, dict):
        return False

    keys = ('alert_type', 'event_type', 'event_type_text', 'query', 'title')
    present = sum(1 for key in keys if key in data)
    return present >= 2 or ('tags' in data and ('alert_type' in data or 'title' in data))


def _normalize_prometheus(data: WebhookData) -> WebhookData:
    first_alert = data.get('alerts', [{}])[0] if data.get('alerts') else {}
    labels = first_alert.get('labels', {}) if isinstance(first_alert, dict) else {}
    annotations = first_alert.get('annotations', {}) if isinstance(first_alert, dict) else {}

    alert_name = _pick_first_string(
        labels.get('alertname') if isinstance(labels, dict) else None,
        data.get('alertingRuleName'),
        data.get('groupLabels', {}).get('alertname') if isinstance(data.get('groupLabels'), dict) else None,
        'prometheus_alert'
    )

    level = _normalize_level(
        labels.get('severity') if isinstance(labels, dict) else None
    )

    instance = _pick_first_string(
        labels.get('instance') if isinstance(labels, dict) else None,
        labels.get('pod') if isinstance(labels, dict) else None,
        labels.get('service') if isinstance(labels, dict) else None,
        labels.get('host') if isinstance(labels, dict) else None
    )

    normalized = dict(data)
    normalized.update({
        'Type': 'PrometheusAlert',
        'RuleName': alert_name,
        'alert_name': alert_name,
        'Level': level,
        'MetricName': _pick_first_string(
            labels.get('__name__') if isinstance(labels, dict) else None,
            'prometheus_alert'
        ),
        'event': 'alert',
    })

    summary = _pick_first_string(
        annotations.get('summary') if isinstance(annotations, dict) else None,
        annotations.get('description') if isinstance(annotations, dict) else None
    )
    if summary:
        normalized['summary'] = summary

    if instance:
        normalized['Resources'] = [{'InstanceId': instance}]

    return normalized


def _normalize_grafana(data: WebhookData) -> WebhookData:
    rule_name = _pick_first_string(data.get('ruleName'), data.get('title'), 'grafana_alert')
    state = _pick_first_string(data.get('state'), data.get('status'))

    level = _normalize_level(state)
    summary = _pick_first_string(data.get('message'), data.get('title'), data.get('ruleUrl'))
    resource_id = _pick_first_string(data.get('ruleId'), data.get('dashboardId'), data.get('panelId'))

    normalized = dict(data)
    normalized.update({
        'Type': 'GrafanaAlert',
        'RuleName': rule_name,
        'alert_name': rule_name,
        'Level': level,
        'MetricName': _pick_first_string(data.get('evalMatches'), rule_name),
        'event': 'alert',
        'event_type': _pick_first_string(state, 'alert')
    })

    if summary:
        normalized['summary'] = summary

    if resource_id:
        normalized['Resources'] = [{'InstanceId': str(resource_id)}]

    return normalized


def _normalize_pagerduty(data: WebhookData) -> WebhookData:
    event_obj = data.get('event') if isinstance(data.get('event'), dict) else {}
    incident = data.get('incident') if isinstance(data.get('incident'), dict) else {}
    event_data = event_obj.get('data') if isinstance(event_obj.get('data'), dict) else {}

    title = _pick_first_string(
        incident.get('title'),
        event_data.get('title'),
        data.get('description'),
        'pagerduty_incident'
    )

    event_type = _pick_first_string(event_obj.get('event_type'), data.get('event_type'), 'incident.triggered')
    urgency = _pick_first_string(
        incident.get('urgency'),
        event_data.get('urgency'),
        event_type
    )

    service = None
    incident_service = incident.get('service')
    if isinstance(incident_service, dict):
        service = _pick_first_string(incident_service.get('summary'), incident_service.get('id'))

    if not service:
        event_service = event_data.get('service') if isinstance(event_data.get('service'), dict) else {}
        service = _pick_first_string(event_service.get('summary'), event_service.get('id'))

    incident_id = _pick_first_string(
        incident.get('id'),
        event_data.get('id'),
        data.get('incident_id')
    )

    normalized = dict(data)
    normalized.update({
        'Type': 'PagerDutyEvent',
        'RuleName': title,
        'alert_name': title,
        'Level': _normalize_level(urgency),
        'MetricName': _pick_first_string(event_type, 'pagerduty_incident'),
        'event': event_type,
        'event_type': event_type,
        'alert_id': incident_id,
    })

    if service:
        normalized['service'] = service

    if incident_id:
        normalized['Resources'] = [{'InstanceId': incident_id}]

    return normalized


def _normalize_datadog(data: WebhookData) -> WebhookData:
    tags = data.get('tags')
    title = _pick_first_string(data.get('alert_name'), data.get('title'), 'datadog_alert')
    alert_type = _pick_first_string(
        data.get('alert_type'),
        data.get('event_type'),
        data.get('event_type_text'),
        data.get('priority')
    )

    host = _pick_first_string(
        data.get('host'),
        _extract_tag_value(tags, 'host'),
        _extract_tag_value(tags, 'instance')
    )
    service = _pick_first_string(data.get('service'), _extract_tag_value(tags, 'service'))

    normalized = dict(data)
    normalized.update({
        'Type': 'DatadogAlert',
        'RuleName': title,
        'alert_name': title,
        'Level': _normalize_level(alert_type),
        'MetricName': _pick_first_string(data.get('metric'), data.get('query'), 'datadog_alert'),
        'event': 'alert',
        'event_type': _pick_first_string(alert_type, 'alert'),
        'alert_id': _pick_first_string(data.get('id'), data.get('alert_id')),
    })

    if service:
        normalized['service'] = service

    if host:
        normalized['Resources'] = [{'InstanceId': host}]

    text = _pick_first_string(data.get('text'), data.get('body'))
    if text:
        normalized['summary'] = text

    return normalized


ADAPTERS: dict[str, tuple[set[str], Callable[[WebhookData], bool], Callable[[WebhookData], WebhookData]]] = {
    'prometheus': ({'prometheus', 'alertmanager'}, _is_prometheus, _normalize_prometheus),
    'grafana': ({'grafana'}, _is_grafana, _normalize_grafana),
    'pagerduty': ({'pagerduty'}, _is_pagerduty, _normalize_pagerduty),
    'datadog': ({'datadog'}, _is_datadog, _normalize_datadog),
}


def _find_adapter_by_source(source: str) -> Optional[str]:
    for name, (aliases, _detector, _normalizer) in ADAPTERS.items():
        if source in aliases:
            return name
    return None


def _find_adapter_by_payload(data: WebhookData) -> Optional[str]:
    for name, (_aliases, detector, _normalizer) in ADAPTERS.items():
        if detector(data):
            return name
    return None


def normalize_webhook_event(
    data: Any,
    source: Optional[str],
    headers: Optional[HeadersLike] = None,
) -> NormalizedWebhook:
    """根据 source 或 payload 特征选择适配器，并输出标准化数据。"""
    if not isinstance(data, dict):
        resolved_source = _normalize_source(source) or _normalize_source(_header_get(headers, 'X-Webhook-Source')) or 'unknown'
        return NormalizedWebhook(resolved_source, {'raw': data}, 'passthrough')

    header_source = _normalize_source(_header_get(headers, 'X-Webhook-Source'))
    source_hint = _normalize_source(source) or header_source

    adapter_name = _find_adapter_by_source(source_hint)
    if adapter_name is None:
        adapter_name = _find_adapter_by_payload(data)

    if adapter_name is None:
        final_source = source_hint or 'unknown'
        return NormalizedWebhook(final_source, dict(data), 'passthrough')

    aliases, _detector, normalizer = ADAPTERS[adapter_name]
    normalized = normalizer(dict(data))

    # 显式 source 不是生态来源时保留（避免覆盖业务自定义来源）
    # 但 unknown/custom/default 等占位来源在命中适配器后应切换为生态来源
    placeholder_sources = {'unknown', 'custom', 'default', 'generic'}
    if source_hint and source_hint not in aliases and source_hint not in placeholder_sources:
        final_source = source_hint
    else:
        final_source = adapter_name

    return NormalizedWebhook(final_source, normalized, adapter_name)
