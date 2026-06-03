# 本地可观测实验手册

这份手册记录一次完整的本地可观测验证流程：启动 Grafana Alloy / Prometheus / Loki / Tempo / Pyroscope / Beyla，触发 Faro 前端 RUM，运行 k6 压测，然后在 Grafana 里找到对应数据。

## 分册导航

- 启动、服务覆盖和统一排查路径：本文
- [业务指标和指标速查](metrics.md)
- [日志、Trace、Smoke 与告警](logs-traces.md)
- [Profile 分析](profiling.md)
- [观测后端、Faro、Beyla 与 k6](backends-rum-load.md)

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

在仓库根目录先启动业务栈，再启动观测栈：

```bash
docker compose up -d --build
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml up -d --build
```

如果要让 API、Worker、Scheduler 上报到本地观测栈，`.env` 中至少需要开启 `OTEL_ENABLED=true`、`OTEL_LOGS_ENABLED=true`，并设置 `OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4318`。启用 Pyroscope 时同时设置 `PYROSCOPE_ENABLED=true`、`PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040`，修改后重启业务容器。

确认服务状态：

```bash
docker compose ps
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml ps
curl -fsS http://localhost:8000/ready
curl -fsS http://localhost:9090/-/ready
curl -fsS http://localhost:3100/ready
curl -fsS http://localhost:3200/ready
curl -fsS http://localhost:12345/-/ready
```

常用入口：

- Grafana: `http://localhost:3000`, local default is `admin/admin`
- Grafana AIOps dashboard: `http://localhost:3000/d/webhook-wise-aiops/webhookwise-aiops-e5a4a7-e79b98`
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
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml config --services
```

下表中的业务服务日志使用 `docker compose logs <service>`；观测服务日志使用 `docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml logs <service>`。

| 服务 | 作用 | 健康入口 | 指标入口 | 日志入口 | Trace / Profile |
| --- | --- | --- | --- | --- | --- |
| `webhook-service` / `webhook-receiver` | HTTP API、Dashboard、webhook 入队 | `http://localhost:8000/ready` | Prometheus: `service_name="webhookwise-api"` | Loki: `{service_name="webhookwise-api"}` | Tempo: `service.name=webhookwise-api`; Pyroscope: `webhookwise-api` |
| `worker` | 异步处理 webhook、AI、转发、重试 | Docker healthcheck | Prometheus: `service_name="webhookwise-worker"`、`worker_*`、`queue_*` | Loki: `{service_name="webhookwise-worker"}` | Tempo: `service.name=webhookwise-worker`; Pyroscope: `webhookwise-worker` |
| `scheduler` | 周期任务、恢复扫描、轮询 | Docker healthcheck | Prometheus: `service_name="webhookwise-scheduler"`、`scheduler_*` | Loki: `{service_name="webhookwise-scheduler"}` | Tempo: `service.name=webhookwise-scheduler`; Pyroscope: `webhookwise-scheduler` |
| `migrate` | Alembic 迁移一次性任务 | container exit code | 无，`OTEL_ENABLED=false` | `docker compose ... logs migrate` | 无 |
| `postgres` | 本地数据库 | Docker healthcheck / `pg_isready` | 目前只有应用侧 DB client/pool 指标 | `docker compose ... logs postgres` | 应用 DB spans 和 Beyla SQL spans |
| `redis` | taskiq broker / stream / cache | Docker healthcheck / `redis-cli ping` | 目前只有应用侧 Redis client 指标 | `docker compose ... logs redis` | 应用 Redis spans 和 Beyla Redis spans |
| `grafana` | 查询与 Dashboard UI | `http://localhost:3000/api/health` | Grafana 自身未被 Prometheus scrape | `docker compose ... logs grafana` | 无 |
| `prometheus` | 指标存储和查询 | `http://localhost:9090/-/ready` | `http://localhost:9090/metrics`，本地主要 scrape Alloy | `docker compose ... logs prometheus` | 无 |
| `loki` | 日志存储和查询 | `http://localhost:3100/ready` | Loki 自身未被 Prometheus scrape | `docker compose ... logs loki` | 无 |
| `tempo` | Trace 存储和查询 | `http://localhost:3200/ready` | Tempo 自身未被 Prometheus scrape | `docker compose ... logs tempo` | Grafana Tempo Explore |
| `pyroscope` | Profile 存储和查询 | `http://localhost:4040` | Pyroscope 自身未被 Prometheus scrape | `docker compose ... logs pyroscope` | Grafana Profiles / Pyroscope UI |
| `alloy` | OTLP/Faro 接收、信号转发 | `http://localhost:12345/-/ready` | Prometheus scrape `alloy:12345` | `docker compose ... logs alloy` | Alloy graph |
| `beyla` | eBPF 自动采集 API 容器 | container running | Prometheus: `source="beyla"`、`traces_*`、`process_*` | `docker compose ... logs beyla` | Tempo: auto traces |
| `k6` | 一次性压测任务 | run exit code | Prometheus: `k6_*` remote write | run output | 无 |
| Dashboard browser / Faro | 前端 RUM | 打开 `http://localhost:8000` | Prometheus: `faro_receiver_*` | Loki: `{app="webhookwise-dashboard"}` | 可转成 frontend traces，取决于 Faro SDK 上报内容 |

注意：Postgres、Redis、Grafana、Loki、Tempo、Pyroscope 当前没有各自的 Prometheus exporter/scrape job。它们在本地手册里通过健康检查、Compose service logs、应用侧 client metrics、Beyla 自动 spans 和后端 API 验证。若要生产级实例指标，可补 `postgres_exporter`、`redis_exporter` 以及 Grafana/Loki/Tempo/Pyroscope 自身 metrics scrape。

`scheduler_*` 指标描述的是周期任务执行结果。当前 taskiq 执行侧可能把这些指标打在 `service_name="webhookwise-worker"` 下；排查 scheduler 时同时看 scheduler 容器健康、scheduler 日志、worker 侧 `scheduler_*` 指标。

服务级 Prometheus 总览：

![Service metrics overview](../../../assets/observability-local-lab/services-prometheus.jpg)

Dashboard 面板覆盖范围、No data 语义和维护 checklist 见 [dashboards.md](../dashboards.md)。

## 统一排查路径

遇到问题时按这条链路走：

1. 服务是否活着：`docker compose ... ps` 和各 `/ready`。
2. 请求是否进 API：Prometheus 查 `http_server_request_duration_seconds_count{service_name="webhookwise-api"}`。
3. 是否入队和消费：查 `queue_operations_total`、`queue_pending`、`queue_lag`；`queue_depth` 只表示 Redis Stream 保留长度。
4. worker 是否处理：查 `worker_task_runs_total`、`webhook_processed_total`、`webhook_processing_duration_seconds_bucket`。
5. DB/Redis 是否慢或失败：查 `db_sessions_total`、`redis_operations_total`、对应 duration bucket。
6. 是否转发/AI 异常：查 `webhook_forward_decisions_total`、`forward_*`、`ai_requests_total`、`ai_*`、Loki 里按 `trace_id` / `event.name` 搜。
7. 需要链路细节：Tempo 按 `service.name`、`trace_id` 或 Grafana 的 trace/log 跳转。
8. CPU/内存疑点：Pyroscope profiles 或 Beyla `process_*` 指标。

## 看 Alloy 管线

打开 `http://localhost:12345/graph`。这里看的是采集管线拓扑，不是业务数据本身。

重点看几条边：

- `otelcol.receiver.otlp "default"` -> processors -> Prometheus / Loki / Tempo exporters
- `faro.receiver "dashboard"` -> `loki.process "faro"` -> `loki.write "local"`
- 应用日志也走 `otelcol.receiver.otlp "default"`，不再通过文件 tail 进入 Loki

![Alloy graph](../../../assets/observability-local-lab/alloy-graph.jpg)
