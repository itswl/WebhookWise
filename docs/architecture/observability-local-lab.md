# 本地可观测实验手册

这份手册记录一次完整的本地可观测验证流程：启动 Grafana Alloy / Prometheus / Loki / Tempo / Pyroscope / Beyla，触发 Faro 前端 RUM，运行 k6 压测，然后在 Grafana 里找到对应数据。

## 数据流

```text
API / Worker / Scheduler
  -> OpenTelemetry SDK
  -> Alloy
      -> Prometheus: metrics
      -> Loki: logs
      -> Tempo: traces

Dashboard browser
  -> Grafana Faro Web SDK
  -> Alloy faro.receiver
      -> Loki: browser events and measurements

webhook-service container
  -> Beyla eBPF auto-instrumentation
  -> Alloy
      -> Prometheus: span/process/service graph metrics
      -> Tempo: auto traces

k6
  -> Prometheus remote write
```

## 启动本地栈

在仓库根目录运行：

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
```

确认服务状态：

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml ps
curl -fsS http://localhost:8000/ready
curl -fsS http://localhost:9090/-/ready
curl -fsS http://localhost:3100/ready
curl -fsS http://localhost:3200/ready
curl -fsS http://localhost:12345/-/ready
```

常用入口：

- Grafana: `http://localhost:3000`, local default is `admin/admin`
- Prometheus: `http://localhost:9090`
- Loki API: `http://localhost:3100`
- Tempo: `http://localhost:3200`
- Pyroscope: `http://localhost:4040`
- Alloy graph: `http://localhost:12345/graph`
- Faro collector endpoint: `http://localhost:12347/collect`

`http://localhost:3100` 返回 404 是正常的。Loki 没有根路径 UI，内容要通过 Grafana Explore 或 Loki API 查询。

## 服务覆盖总览

当前本地栈的服务清单来自：

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml config --services
```

| 服务 | 作用 | 健康入口 | 指标入口 | 日志入口 | Trace / Profile |
| --- | --- | --- | --- | --- | --- |
| `webhook-service` / `webhook-receiver` | HTTP API、Dashboard、webhook 入队 | `http://localhost:8000/ready` | Prometheus: `service_name="webhookwise-api"` | Loki: `{service_name="webhookwise-api"}` 或 `{service_name="webhookwise"}` | Tempo: `service.name=webhookwise-api`; Pyroscope: `webhookwise-api` |
| `worker` | 异步处理 webhook、AI、转发、重试 | Docker healthcheck | Prometheus: `service_name="webhookwise-worker"`、`worker_*`、`queue_*` | Loki: `{service_name="webhookwise-worker"}` 或 `{service_name="webhookwise"}` | Tempo: `service.name=webhookwise-worker`; Pyroscope: `webhookwise-worker` |
| `scheduler` | 周期任务、恢复扫描、轮询 | Docker healthcheck | Prometheus: `service_name="webhookwise-scheduler"`、`scheduler_*` | Loki: `{service_name="webhookwise-scheduler"}` 或 `{service_name="webhookwise"}` | Tempo: `service.name=webhookwise-scheduler`; Pyroscope: `webhookwise-scheduler` |
| `migrate` | Alembic 迁移一次性任务 | container exit code | 无，`OTEL_ENABLED=false` | `docker logs webhook-migrate` | 无 |
| `postgres` | 本地数据库 | Docker healthcheck / `pg_isready` | 目前只有应用侧 DB client/pool 指标 | `docker logs webhook-postgres` | 应用 DB spans 和 Beyla SQL spans |
| `redis` | taskiq broker / stream / cache | Docker healthcheck / `redis-cli ping` | 目前只有应用侧 Redis client 指标 | `docker logs webhook-redis` | 应用 Redis spans 和 Beyla Redis spans |
| `grafana` | 查询与 Dashboard UI | `http://localhost:3000/api/health` | Grafana 自身未被 Prometheus scrape | `docker logs webhooks-grafana-1` | 无 |
| `prometheus` | 指标存储和查询 | `http://localhost:9090/-/ready` | `http://localhost:9090/metrics`，本地主要 scrape Alloy | `docker logs webhooks-prometheus-1` | 无 |
| `loki` | 日志存储和查询 | `http://localhost:3100/ready` | Loki 自身未被 Prometheus scrape | `docker logs webhooks-loki-1` | 无 |
| `tempo` | Trace 存储和查询 | `http://localhost:3200/ready` | Tempo 自身未被 Prometheus scrape | `docker logs webhooks-tempo-1` | Grafana Tempo Explore |
| `pyroscope` | Profile 存储和查询 | `http://localhost:4040` | Pyroscope 自身未被 Prometheus scrape | `docker logs webhooks-pyroscope-1` | Grafana Profiles / Pyroscope UI |
| `alloy` | OTLP/Faro 接收、日志 tail、转发 | `http://localhost:12345/-/ready` | Prometheus scrape `alloy:12345` | `docker logs webhooks-alloy-1` | Alloy graph |
| `beyla` | eBPF 自动采集 API 容器 | container running | Prometheus: `source="beyla"`、`traces_*`、`process_*` | `docker logs webhooks-beyla-1` | Tempo: auto traces |
| `k6` | 一次性压测任务 | run exit code | Prometheus: `k6_*` remote write | run output | 无 |
| Dashboard browser / Faro | 前端 RUM | 打开 `http://localhost:8000` | Prometheus: `faro_receiver_*` | Loki: `{app="webhookwise-dashboard"}` | 可转成 frontend traces，取决于 Faro SDK 上报内容 |

注意：Postgres、Redis、Grafana、Loki、Tempo、Pyroscope 当前没有各自的 Prometheus exporter/scrape job。它们在本地手册里通过健康检查、Docker logs、应用侧 client metrics、Beyla 自动 spans 和后端 API 验证。若要生产级实例指标，可补 `postgres_exporter`、`redis_exporter` 以及 Grafana/Loki/Tempo/Pyroscope 自身 metrics scrape。

`scheduler_*` 指标描述的是周期任务执行结果。当前 taskiq 执行侧可能把这些指标打在 `service_name="webhookwise-worker"` 下；排查 scheduler 时同时看 scheduler 容器健康、scheduler 日志、worker 侧 `scheduler_*` 指标。

服务级 Prometheus 总览：

![Service metrics overview](../assets/observability-local-lab/services-prometheus.jpg)

## 统一排查路径

遇到问题时按这条链路走：

1. 服务是否活着：`docker compose ... ps` 和各 `/ready`。
2. 请求是否进 API：Prometheus 查 `http_server_requests_total{service_name="webhookwise-api"}`。
3. 是否入队：查 `queue_operations_total`、`queue_depth_ratio`、`queue_pending_ratio`。
4. worker 是否处理：查 `worker_task_runs_total`、`webhook_processed_total`、`webhook_processing_duration_seconds_bucket`。
5. DB/Redis 是否慢或失败：查 `db_sessions_total`、`redis_operations_total`、对应 duration bucket。
6. 是否转发/AI 异常：查 `forward_*`、`ai_*`、Loki 里按 `trace_id` / `event_name` 搜。
7. 需要链路细节：Tempo 按 `service.name`、`trace_id` 或 Grafana 的 trace/log 跳转。
8. CPU/内存疑点：Pyroscope profiles 或 Beyla `process_*` 指标。

## 看 Alloy 管线

打开 `http://localhost:12345/graph`。这里看的是采集管线拓扑，不是业务数据本身。

重点看几条边：

- `otelcol.receiver.otlp "default"` -> processors -> Prometheus / Loki / Tempo exporters
- `faro.receiver "dashboard"` -> `loki.process "faro"` -> `loki.write "local"`
- `loki.source.file "webhook_logs"` -> `loki.process "webhook_logs"` -> Loki

![Alloy graph](../assets/observability-local-lab/alloy-graph.jpg)

## 看业务服务指标

Grafana -> Explore -> datasource 选择 `Prometheus`。

### API

```promql
sum by (http_route, http_status_code) (
  rate(http_server_requests_total{service_name="webhookwise-api"}[5m])
)
```

```promql
histogram_quantile(
  0.95,
  sum by (le, http_route) (
    rate(http_server_request_duration_seconds_bucket{service_name="webhookwise-api"}[5m])
  )
)
```

```promql
sum by (webhook_source, webhook_status) (
  increase(webhook_received_total[30m])
)
```

```promql
sum by (security_check, security_result) (
  increase(security_checks_total[30m])
)
```

![API metrics in Prometheus](../assets/observability-local-lab/api-prometheus.jpg)

### Worker

```promql
sum by (worker_task_name, worker_task_status) (
  rate(worker_task_runs_total[5m])
)
```

```promql
histogram_quantile(
  0.95,
  sum by (le, worker_task_name) (
    rate(worker_task_duration_seconds_bucket[5m])
  )
)
```

```promql
webhook_running_tasks_ratio
```

![Worker metrics in Prometheus](../assets/observability-local-lab/worker-prometheus.jpg)

### Scheduler

```promql
sum by (scheduler_task_name, scheduler_task_status) (
  increase(scheduler_task_runs_total[30m])
)
```

```promql
time() - scheduler_task_last_success_unixtime_seconds
```

```promql
scheduler_task_lag_seconds
```

![Scheduler metrics in Prometheus](../assets/observability-local-lab/scheduler-prometheus.jpg)

### Queue

```promql
queue_depth_ratio
or queue_pending_ratio
or queue_lag_ratio
```

```promql
sum by (queue_name, queue_operation, queue_status) (
  rate(queue_operations_total[5m])
)
```

![Queue metrics in Prometheus](../assets/observability-local-lab/queue-prometheus.jpg)

### Database Client And Pool

这些是应用侧 DB client/pool 指标，不是 Postgres server exporter 指标。

```promql
sum by (db_operation, db_status) (
  rate(db_sessions_total[5m])
)
```

```promql
db_pool_connections_checked_out_ratio
or db_pool_connections_max_ratio
```

```promql
histogram_quantile(
  0.95,
  sum by (le, db_operation) (
    rate(db_session_duration_seconds_bucket[5m])
  )
)
```

### Redis Client

这些是应用侧 Redis client 指标，不是 Redis server exporter 指标。

```promql
sum by (redis_operation, redis_status) (
  rate(redis_operations_total[5m])
)
```

```promql
histogram_quantile(
  0.95,
  sum by (le, redis_operation) (
    rate(redis_operation_duration_seconds_bucket[5m])
  )
)
```

![DB and Redis client metrics in Prometheus](../assets/observability-local-lab/db-redis-prometheus.jpg)

### AI / Forwarding / Domain Events

```promql
histogram_quantile(
  0.95,
  sum by (le, webhook_source, ai_engine) (
    rate(ai_request_duration_seconds_bucket[5m])
  )
)
```

```promql
sum by (ai_model, ai_token_type) (
  increase(ai_tokens_total[1h])
)
```

```promql
sum by (forward_target_type, forward_status) (
  increase(forward_delivery_total[30m])
)
```

```promql
sum by (event_name) (
  increase(observability_events_total[30m])
)
```

![AI, forwarding, and domain event metrics](../assets/observability-local-lab/ai-forward-events-prometheus.jpg)

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
{service_name="webhookwise", level="ERROR"}
```

按结构化事件筛选：

```logql
{service_name="webhookwise"} | json | event_name != ""
```

日志里通常能看到 `trace_id` / `span_id`，可以用这些字段跳到 Tempo 查链路。

![Service logs in Loki](../assets/observability-local-lab/service-logs-loki.jpg)

基础设施容器日志目前没有统一采进 Loki，用 Docker logs 看：

```bash
docker logs --tail=100 webhooks-alloy-1
docker logs --tail=100 webhooks-prometheus-1
docker logs --tail=100 webhooks-loki-1
docker logs --tail=100 webhooks-tempo-1
docker logs --tail=100 webhooks-pyroscope-1
docker logs --tail=100 webhooks-grafana-1
docker logs --tail=100 webhook-postgres
docker logs --tail=100 webhook-redis
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

Tempo API 也可快速确认数据：

```bash
curl -fsS 'http://localhost:3200/api/search?tags=service.name%3Dwebhookwise-api&limit=5'
```

## 看 Profile

Pyroscope 直接入口：

```text
http://localhost:4040
```

Grafana 里也可以用 Profiles / Pyroscope datasource。当前本地栈为三个 Python 进程打开 profile：

- `webhookwise-api`
- `webhookwise-worker`
- `webhookwise-scheduler`

优先在以下场景看 profile：

- API p95/p99 变高，但 DB/Redis/AI 没明显慢调用。
- worker 队列积压，同时 CPU 明显升高。
- scheduler 任务 duration 变长，但日志里没有错误。

![Pyroscope flamegraph](../assets/observability-local-lab/pyroscope-ui.jpg)

## 看可观测后端自身

### Alloy

```promql
alloy_config_last_load_successful
alloy_component_controller_running_components
loki_write_dropped_entries_total
faro_receiver_rate_limiter_requests_total
```

Alloy 自身 metrics 页面：

```text
http://localhost:12345/metrics
```

### Prometheus

```promql
up
prometheus_tsdb_wal_writes_failed_total
prometheus_tsdb_wal_storage_size_bytes
```

Prometheus targets:

```text
http://localhost:9090/targets
```

![Prometheus targets](../assets/observability-local-lab/prometheus-targets.jpg)

### Loki / Tempo / Pyroscope / Grafana

这些组件当前主要通过健康接口和 Docker logs 验证：

```bash
curl -fsS http://localhost:3100/ready
curl -fsS http://localhost:3200/ready
curl -fsS http://localhost:3000/api/health
curl -fsS http://localhost:4040
```

如果要把它们自身的 runtime metrics 纳入同一个 Prometheus，需要在 `deploy/observability/prometheus.yml` 增加 scrape job，并确认各组件 metrics endpoint 和网络地址。

## 看 Faro 前端 RUM

Faro 是前端浏览器数据。它只会在打开业务 Dashboard 后上报：

```text
http://localhost:8000
```

本地页面会加载 `templates/static/js/faro.js`，默认把数据发到：

```text
http://localhost:12347/collect
```

Faro 接收量在 Prometheus 里看：

```promql
faro_receiver_events_total
or faro_receiver_measurements_total
or faro_receiver_exceptions_total
or faro_receiver_logs_total
```

Faro 具体事件在 Loki 里看：

```logql
{app="webhookwise-dashboard"} | json
```

分类型查看：

```logql
{app="webhookwise-dashboard", kind="event"} | json
{app="webhookwise-dashboard", kind="measurement"} | json
```

截图里可以看到 `session_start`、`faro.performance.navigation`、`faro.performance.resource` 以及 Web Vitals measurement。

![Faro events in Loki](../assets/observability-local-lab/faro-loki.jpg)

如果 `faro_receiver_*` 一直是 0，先检查：

- 是否打开过 `http://localhost:8000`
- 浏览器是否能访问 `https://unpkg.com/@grafana/faro-web-sdk...`
- Alloy 是否暴露了 `12347`
- `http://localhost:12345/graph` 中是否有 `faro.receiver "dashboard"`

## 看 Beyla 自动采集

Beyla 是 API 容器旁边的 eBPF sidecar。它没有单独 UI，数据进 Prometheus 和 Tempo。

确认 Beyla 容器：

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml ps beyla
docker logs --tail=80 webhooks-beyla-1
```

Prometheus 里看 Beyla span metrics：

```promql
sum by (span_name, span_kind) (
  traces_span_metrics_calls_total{
    source="beyla",
    service_name="webhookwise-api"
  }
)
```

看 p95：

```promql
histogram_quantile(
  0.95,
  sum by (le, span_name) (
    rate(traces_span_metrics_duration_seconds_bucket{
      source="beyla",
      service_name="webhookwise-api"
    }[5m])
  )
)
```

看进程资源：

```promql
sum by (cpu_mode) (
  process_cpu_utilization_ratio{service_name="webhookwise-api"}
)
```

![Beyla metrics in Prometheus](../assets/observability-local-lab/beyla-prometheus.jpg)

Tempo 里也可以搜索：

```text
service.name = webhookwise-api
```

Docker Desktop 上可能看到类似 `bpffs` 的 warning。它表示 pinned map / log enricher / profile correlation 等增强能力受限，基础 HTTP / SQL / Redis 指标和 trace 仍然可以工作。

## 跑 k6 压测

运行仓库内置 smoke 压测：

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile load run --rm k6
```

默认脚本是 `tests/k6/webhook_smoke.js`：

- 10s ramp to 2 VUs
- 30s ramp to 6 VUs
- 10s ramp down to 0
- 请求 `POST /webhook/k6`
- 输出到 Prometheus remote write

一次健康结果示例：

```text
http_reqs: 117
http_req_failed: 0.00%
http_req_duration p95: 14.26ms
http_req_duration p99: 17.6ms
checks: 100%
```

k6 指标在 Prometheus 里看。k6 结束后会写 stale markers，所以 instant query 可能为空。查询刚跑完的一轮时用 `max_over_time(...[30m])`。

```promql
max_over_time(k6_http_reqs_total[30m])
```

```promql
max_over_time(k6_http_req_failed_rate[30m])
```

```promql
max_over_time(k6_http_req_duration_p95[30m])
max_over_time(k6_http_req_duration_p99[30m])
```

```promql
max_over_time(k6_checks_rate[30m])
```

![k6 metrics in Prometheus](../assets/observability-local-lab/k6-prometheus.jpg)

同时可以从 API 侧验证 webhook 入口确实收到流量：

```promql
sum by (http_route, http_status_code) (
  increase(http_server_requests_total{
    service_name="webhookwise-api",
    http_route="/webhook/{source}"
  }[30m])
)
```

## 常见现象

| 现象 | 解释 | 处理 |
| --- | --- | --- |
| `http://localhost:3100` 是 404 | Loki 没有根路径 UI | 用 Grafana Explore 或 `/ready` / API |
| k6 instant query 为空 | k6 run 结束后写 stale markers | 用 `max_over_time(...[30m])` |
| Faro 计数为 0 | 没有打开业务前端或 SDK 未加载 | 打开 `http://localhost:8000` 并检查 browser console/network |
| Beyla 没有 UI | Beyla 是采集 sidecar | 在 Prometheus / Tempo 看 `source="beyla"` |
| Alloy graph 有线但 Grafana 没数据 | 可能还没产生业务事件 | 打开 Dashboard、调用 API 或跑 k6 |

## 清理

停止本地栈：

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml down
```

如果只是重跑压测，不需要重启整套栈。
