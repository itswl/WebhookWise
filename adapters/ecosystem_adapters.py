from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

from core.config import Config
from core.http_client import get_http_client
from core.utils import feishu_cb

logger = logging.getLogger('webhook_service.ecosystem_adapters')

WebhookData = dict[str, Any]
HeadersLike = Mapping[str, Any]


@dataclass(frozen=True)
class NormalizedWebhook:
    source: str
    data: WebhookData
    adapter: str


def _header_get(headers: HeadersLike | None, key: str) -> str | None:
    if not headers:
        return None

    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return None


def _normalize_source(source: str | None) -> str:
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


def _pick_first_string(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_tag_value(tags: Any, key: str) -> str | None:
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


def _is_feishu_card(data: WebhookData) -> bool:
    """检测飞书卡片格式（火山引擎日志服务告警等）"""
    if not isinstance(data, dict):
        return False
    return data.get('msg_type') == 'interactive' and isinstance(data.get('card'), dict)


def _normalize_feishu_card(data: WebhookData) -> WebhookData:
    """
    解析飞书卡片格式，提取关键字段。
    典型来源：火山引擎日志服务告警通知
    """
    card = data.get('card', {})
    header = card.get('header', {})
    elements = card.get('elements', [])

    # 从 header 提取标题
    header_title = ''
    if isinstance(header, dict):
        title_obj = header.get('title', {})
        if isinstance(title_obj, dict):
            header_title = str(title_obj.get('content', '') or '').strip()
        elif isinstance(title_obj, str):
            header_title = title_obj.strip()

    # 从 elements[0].content 解析关键信息
    content_text = ''
    for elem in elements:
        if isinstance(elem, dict) and elem.get('tag') == 'markdown':
            content_text = str(elem.get('content', '') or '').strip()
            break

    # 解析 content 中的键值对
    alert_strategy = ''
    log_topic = ''
    alert_level = ''
    first_trigger_time = ''
    trigger_condition = ''
    query_result = ''

    lines = content_text.split('\n')
    for line in lines:
        line = line.strip()
        if '告警策略：' in line:
            alert_strategy = line.split('告警策略：', 1)[1].strip()
        elif '告警日志主题：' in line:
            log_topic = line.split('告警日志主题：', 1)[1].strip()
        elif '告警级别：' in line:
            alert_level = line.split('告警级别：', 1)[1].strip()
        elif '首次触发时间：' in line:
            first_trigger_time = line.split('首次触发时间：', 1)[1].strip()
        elif '触发条件：' in line:
            trigger_condition = line.split('触发条件：', 1)[1].strip()
        elif '当前查询结果：' in line:
            query_result = line.split('当前查询结果：', 1)[1].strip()

    normalized = dict(data)
    normalized.update({
        'Type': 'FeishuCard',
        'RuleName': alert_strategy or header_title or 'feishu_alert',
        'alert_name': alert_strategy or header_title or 'feishu_alert',
        'Level': _normalize_level(alert_level),
        'MetricName': log_topic or 'feishu_log_alert',
        'event': 'alert',
        'event_type': 'feishu_card_alert',
        'alert_id': alert_strategy,
    })

    if first_trigger_time:
        normalized['first_trigger_time'] = first_trigger_time
    if trigger_condition:
        normalized['trigger_condition'] = trigger_condition
    if query_result:
        normalized['query_result'] = query_result
    if log_topic:
        normalized['Resources'] = [{'InstanceId': log_topic}]
    if content_text:
        normalized['summary'] = content_text

    return normalized


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
    'feishu_card': ({'feishu_card', 'volcengine_log'}, _is_feishu_card, _normalize_feishu_card),
}


def _find_adapter_by_source(source: str) -> str | None:
    for name, (aliases, _detector, _normalizer) in ADAPTERS.items():
        if source in aliases:
            return name
    return None


def _find_adapter_by_payload(data: WebhookData) -> str | None:
    for name, (_aliases, detector, _normalizer) in ADAPTERS.items():
        if detector(data):
            return name
    return None


def normalize_webhook_event(
    data: Any,
    source: str | None,
    headers: HeadersLike | None = None,
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
        logger.info(f"[Adapter] 未能匹配特定适配器，使用透传模式: source={final_source}")
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

    logger.info(f"[Adapter] 成功匹配适配器: name={adapter_name}, final_source={final_source}")
    return NormalizedWebhook(final_source, normalized, adapter_name)


# ========== 飞书深度分析通知 ==========

def _truncate_text(text: str, max_len: int) -> str:
    """截断文本，超长时添加省略号"""
    if not text:
        return ''
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + '...'


def _format_recommendations(recs: Any, max_items: int = 5, max_item_len: int = 200) -> str:
    """格式化修复建议列表，兼容字符串数组和对象数组"""
    if not recs:
        return '无'
    
    if not isinstance(recs, (list, tuple)):
        return _truncate_text(str(recs), max_item_len)
    
    lines = []
    for i, rec in enumerate(recs[:max_items], 1):
        if isinstance(rec, dict):
            priority = rec.get('priority', '')
            action = rec.get('action', str(rec))
            action = _truncate_text(action, max_item_len)
            if priority:
                lines.append(f"{i}. **{priority}**: {action}")
            else:
                lines.append(f"{i}. {action}")
        else:
            lines.append(f"{i}. {_truncate_text(str(rec), max_item_len)}")
    
    if len(recs) > max_items:
        lines.append(f"... 还有 {len(recs) - max_items} 条建议")
    
    return '\n'.join(lines) if lines else '无'


async def send_feishu_deep_analysis(
    webhook_url: str,
    analysis_record: dict,
    source: str = '',
    webhook_event_id: int = 0
) -> bool:
    """
    发送深度分析结果到飞书
    
    Args:
        webhook_url: 飞书 webhook URL
        analysis_record: 深度分析记录，包含 analysis_result, engine, duration_seconds 等
        source: 告警来源
        webhook_event_id: 关联的 webhook 事件 ID
    
    Returns:
        bool: 是否发送成功
    """
    if not webhook_url:
        return False
    
    result = analysis_record.get('analysis_result', {})
    if not isinstance(result, dict):
        result = {}
    
    engine = analysis_record.get('engine', 'unknown')
    duration = analysis_record.get('duration_seconds', 0)
    confidence = result.get('confidence', 0)
    if isinstance(confidence, (int, float)):
        confidence = round(confidence * 100)
    
    # 提取分析结果字段（排除内部字段）
    root_cause = _truncate_text(result.get('root_cause', '无'), 500)
    impact = _truncate_text(result.get('impact', '无'), 500)
    recommendations = _format_recommendations(result.get('recommendations', []))
    
    # 构建标题
    title = '🔬 深度分析完成'
    if source:
        title = f'🔬 [{source}] 深度分析完成'
    
    # 构建飞书消息卡片
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue"
            },
            "elements": [
                # 根因分析
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**🔍 根因分析：**\n{root_cause}"}
                },
                {"tag": "hr"},
                # 影响范围
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**💥 影响范围：**\n{impact}"}
                },
                {"tag": "hr"},
                # 修复建议
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**✅ 修复建议：**\n{recommendations}"}
                },
                {"tag": "hr"},
                # 元信息
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"引擎: {engine} | 置信度: {confidence}% | 耗时: {duration:.1f}s | ID: {webhook_event_id}"}
                    ]
                }
            ]
        }
    }
    
    resp = feishu_cb.call(requests.post, webhook_url, json=card, timeout=Config.FEISHU_WEBHOOK_TIMEOUT)

    if resp is None:
        logger.warning(f"飞书深度分析通知被熔断拦截: webhook_event_id={webhook_event_id}")
        return False

    try:
        if resp.status_code == 200:
            logger.info(f"飞书深度分析通知发送成功: webhook_event_id={webhook_event_id}")
            return True
        else:
            logger.warning(f"飞书深度分析通知发送失败: status={resp.status_code}, response={resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"飞书深度分析通知异常: {e}")
        return False
