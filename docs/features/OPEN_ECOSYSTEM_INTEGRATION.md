# 开源生态集成

## 概述

项目现已支持主流监控生态的 webhook 自动适配：

- Prometheus Alertmanager
- Grafana Alerting
- PagerDuty
- Datadog

核心能力：

- 支持显式来源路由：`POST /webhook/<source>`
- 支持自动识别来源：`POST /webhook` + payload 检测
- 统一映射为内部标准字段（`RuleName` / `Level` / `Resources` 等）

实现代码：

- 适配器：`ecosystem_adapters.py`
- 接入点：`core/app.py::_parse_webhook_request`

## 路由

### 通用入口（自动识别）

```bash
POST /webhook
```

### 指定来源入口

```bash
POST /webhook/prometheus
POST /webhook/grafana
POST /webhook/pagerduty
POST /webhook/datadog
```

## 字段映射说明

### Prometheus

- `labels.alertname` -> `RuleName`
- `labels.severity` -> `Level`
- `labels.instance|pod|service|host` -> `Resources[0].InstanceId`
- `annotations.summary|description` -> `summary`

### Grafana

- `ruleName|title` -> `RuleName`
- `state|status` -> `Level`
- `ruleId|dashboardId|panelId` -> `Resources[0].InstanceId`

### PagerDuty

- `event.data.title|incident.title` -> `RuleName`
- `event.event_type|urgency` -> `Level`
- `event.data.id|incident.id` -> `alert_id` / `Resources[0].InstanceId`
- `service.summary` -> `service`

### Datadog

- `alert_name|title` -> `RuleName`
- `alert_type|event_type|priority` -> `Level`
- `host|tags(host:xxx)` -> `Resources[0].InstanceId`
- `metric|query` -> `MetricName`

## 适配行为

- 命中适配器后会在日志打印：

```text
生态适配命中: adapter=<name>, source=<resolved_source>
```

- 未命中时保持透传（`passthrough`），不改变原 payload。

## 快速验证

### Prometheus

```bash
curl -X POST http://localhost:8000/webhook/prometheus \
  -H "Content-Type: application/json" \
  -d '{
    "status": "firing",
    "alerts": [{
      "labels": {
        "alertname": "HighCPU",
        "severity": "critical",
        "instance": "node-1"
      },
      "annotations": {
        "summary": "CPU > 90%"
      }
    }]
  }'
```

### Grafana

```bash
curl -X POST http://localhost:8000/webhook/grafana \
  -H "Content-Type: application/json" \
  -d '{
    "ruleName": "API Error Rate",
    "state": "alerting",
    "title": "Error Rate Alert",
    "dashboardId": "db-001"
  }'
```

### PagerDuty

```bash
curl -X POST http://localhost:8000/webhook/pagerduty \
  -H "Content-Type: application/json" \
  -d '{
    "event": {
      "event_type": "incident.triggered",
      "data": {
        "id": "PDI123",
        "title": "Database Down",
        "service": {"summary": "order-service"}
      }
    }
  }'
```

### Datadog

```bash
curl -X POST http://localhost:8000/webhook/datadog \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Memory usage high",
    "alert_type": "error",
    "query": "avg(last_5m):avg:system.mem.pct_usable{*} < 0.1",
    "tags": ["host:web-01", "service:web"]
  }'
```

## 测试

```bash
PYTHONPATH=. pytest -q tests/test_ecosystem_adapters.py
```

