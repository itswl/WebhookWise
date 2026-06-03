# 本地可观测实验手册：观测后端、RUM、Beyla 与 k6

[返回总览](README.md)

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

![Prometheus targets](../../../assets/observability-local-lab/prometheus-targets.jpg)

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

![Faro events in Loki](../../../assets/observability-local-lab/faro-loki.jpg)

如果 `faro_receiver_*` 一直是 0，先检查：

- 是否打开过 `http://localhost:8000`
- 浏览器是否能访问 `https://unpkg.com/@grafana/faro-web-sdk...`
- Alloy 是否暴露了 `12347`
- `http://localhost:12345/graph` 中是否有 `faro.receiver "dashboard"`

## 看 Beyla 自动采集

Beyla 是 API 容器旁边的 eBPF sidecar。它没有单独 UI，数据进 Prometheus 和 Tempo。

确认 Beyla 容器：

```bash
docker compose -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml ps beyla
docker compose -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml logs --tail=80 beyla
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

![Beyla metrics in Prometheus](../../../assets/observability-local-lab/beyla-prometheus.jpg)

Tempo 里也可以搜索：

```text
service.name = webhookwise-api
```

Docker Desktop 上可能看到类似 `bpffs` 的 warning。它表示 pinned map / log enricher / profile correlation 等增强能力受限，基础 HTTP / SQL / Redis 指标和 trace 仍然可以工作。

## 跑 k6 压测

运行仓库内置 smoke 压测：

```bash
docker compose -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml --profile load run --rm k6
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

![k6 metrics in Prometheus](../../../assets/observability-local-lab/k6-prometheus.jpg)

同时可以从 API 侧验证 webhook 入口确实收到流量：

```promql
sum by (http_route, http_response_status_code) (
  increase(http_server_request_duration_seconds_count{
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
docker compose -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml down
```

如果只是重跑压测，不需要重启整套栈。
