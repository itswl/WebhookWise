# 本地可观测实验手册：日志、Trace、Smoke 与告警

[返回总览](README.md)

## 看日志

Grafana -> Explore -> datasource 选择 `Loki`。

全部应用聚合日志：

```logql
{service_name="webhookwise"}
```

按服务看：

```logql
{service_name="webhookwise-api"}
{service_name="webhookwise-worker"}
{service_name="webhookwise-scheduler"}
```

按级别筛选：

```logql
{service_name="webhookwise-api", severity="error"}
```

约定：结构化字段 `severity` 固定使用小写 `trace/debug/info/warn/error/fatal`，便于 Loki 查询和告警；日志内容里同时保留 `severity_text`，值为大写 `TRACE/DEBUG/INFO/WARN/ERROR/FATAL`，便于 Grafana line format 或滚动日志里快速扫级别。

应用日志通过 OTLP logs 进入 Alloy。Alloy 会把 `severity`、`severity_text`、`event.name`、`signal.name`、`signal.state`、`webhook.source`、`webhook.status` 放进 Loki label；Loki 侧会使用安全化后的 label 名（例如 `event_name`、`webhook_source`）。`trace_id` / `span_id` 只作为日志字段和 derived field 跳转线索，不作为 label，避免高基数压垮索引。

按结构化事件筛选：

```logql
{service_name="webhookwise-api", event_name!=""}
```

日志里通常能看到 `trace_id` / `span_id`，可以用这些字段跳到 Tempo 查链路。

![Service logs in Loki](../../../assets/observability-local-lab/service-logs-loki.jpg)

基础设施容器日志目前没有统一采进 Loki，用 Compose service logs 看：

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 alloy
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 prometheus
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 loki
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 tempo
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 pyroscope
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 grafana
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 postgres
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=100 redis
```

## 看 Trace

Grafana -> Explore -> datasource 选择 `Tempo`。

常用搜索：

```text
service.name = webhookwise-api
service.name = webhookwise-worker
service.name = webhookwise-scheduler
```

如果日志中有 `trace_id`，可以在 Tempo 里直接按 trace id 打开。Grafana datasource 已配置 `tracesToLogsV2` 和 `tracesToProfiles`，可从 trace 跳到 Loki 日志和 Pyroscope profile。

从 Loki 反跳 Tempo：Loki datasource 已配置 derived field，会从 JSON 日志的
`trace_id` 里提取 32 位 trace id，点击 `View Trace` 直接打开 Tempo。

从 Tempo 跳 Loki：Tempo datasource 的 `tracesToLogsV2` 会把
`service.name -> service_name`、`webhook.source -> webhook_source`、
`webhook.status -> webhook_status` 映射到 Loki label，并启用 trace id 过滤。

Tempo API 也可快速确认数据：

```bash
curl -fsS 'http://localhost:3200/api/search?tags=service.name%3Dwebhookwise-api&limit=5'
```

## Smoke 与告警

改完可观测配置后，优先跑一条端到端 smoke：

```bash
python scripts/observability/webhookwise_observe.py smoke
```

它会检查健康状态，打一条 `observability-smoke` webhook，然后确认 Prometheus、
Loki、Tempo 和 Prometheus alert rules 都有基本响应。线上只查询不造流量：

```bash
python scripts/observability/webhookwise_observe.py smoke --skip-webhook
```

本地 Prometheus 会加载 `deploy/observability/alerts.yml`。这份规则包含：

- API / ingress / processing / forward 的 SLO recording rules 和 5m+1h / 30m+6h burn-rate 告警
- API 5xx 比例
- webhook dead letter
- queue pending / lag 积压
- Redis Stream retained depth 持续增长
- DB pool 接近容量
- AI 错误和高延迟
- Alloy exporter queue 堵塞
- Loki 写入丢弃
- Alloy 配置加载失败

同一个规则文件也提供 recording rules，把容易误解的 `_ratio` gauge 名称记录成
更直接的名字，例如 `queue_pending`、`queue_lag`、`queue_depth`、
`webhook_events_active`、`db_pool_connections_checked_out`。
排障时可以用 `python scripts/observability/webhookwise_observe.py runbook <alert_name>`
自动收集告警状态、相关 SLO/RED/USE 查询、错误日志、Trace 和 Profile 链接。
