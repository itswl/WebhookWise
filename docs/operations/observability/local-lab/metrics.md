# Local Observability Lab Handbook: Metrics

[Back to overview](README.md)

## Viewing Business Service Metrics

Grafana -> Explore -> select the `Prometheus` datasource.

### API

```promql
sum by (http_route, http_response_status_code) (
  rate(http_server_request_duration_seconds_count{service_name="webhookwise-api"}[5m])
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

![API metrics in Prometheus](assets/api-prometheus.jpg)

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
webhook_running_tasks
or webhook_running_tasks_ratio
```

![Worker metrics in Prometheus](assets/worker-prometheus.jpg)

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

![Scheduler metrics in Prometheus](assets/scheduler-prometheus.jpg)

### Queue

```promql
queue_pending
or queue_lag
```

```promql
queue_depth
```

`queue_depth` is the Redis Stream's `XLEN`. TaskIQ runs `XACK` after consuming,
but `XACK` does not delete the stream entry, so it can grow toward
`WEBHOOK_MQ_STREAM_MAXLEN` as historical messages accumulate; to judge whether
consumption is stuck, look first at `queue_pending` and `queue_lag`.

```promql
sum by (queue_name, queue_operation, queue_status) (
  rate(queue_operations_total[5m])
)
```

![Queue metrics in Prometheus](assets/queue-prometheus.jpg)

### Database Client And Pool

These are application-side DB client/pool metrics, not Postgres server exporter metrics. The connection pool gauges are read directly from the current SQLAlchemy pool state by the OTel export callback, rather than being inferred from checkout/checkin event counts.

```promql
sum by (db_operation, db_status) (
  rate(db_sessions_total[5m])
)
```

```promql
db_pool_connections_checked_out
or db_pool_connections_max
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

These are application-side Redis client metrics, not Redis server exporter metrics.

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

![DB and Redis client metrics in Prometheus](assets/db-redis-prometheus.jpg)

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

![AI, forwarding, and domain event metrics](assets/ai-forward-events-prometheus.jpg)

## Metric Interpretation Cheat Sheet

First, remember a few suffix rules in Prometheus:

| Suffix / Type | How to read it | Common PromQL | Questions it answers |
| --- | --- | --- | --- |
| `_total` / Counter | A monotonically increasing cumulative value | `rate(x_total[5m])`, `increase(x_total[30m])` | Frequency, throughput, error counts |
| `_bucket` / Histogram | Bucketed counts | `histogram_quantile(0.95, sum by (le, ...) (rate(x_bucket[5m])))` | p95/p99 latency, request-body size distribution |
| `_sum` / Histogram | Sum of observed values | `rate(x_sum[5m]) / rate(x_count[5m])` | Average duration or average size |
| `_count` / Histogram | Number of observations | `rate(x_count[5m])` | Sample throughput |
| Gauge | A current state value | Query directly, or `max_over_time(x[30m])` | Current backlog, connection count, running tasks |

The code uses OpenTelemetry dot-notation names, for example `http.server.request.duration`. Once in Prometheus, they usually become underscore names, for example `http_server_request_duration_seconds_bucket` / `_count`. In the local Prometheus, some application metrics also appear with a `webhookwise_` prefix version; for everyday troubleshooting, prefer the unprefixed business name and try the prefixed version when you cannot find it.

The local stack pins the OTel semantic conventions schema to
`https://opentelemetry.io/schemas/1.41.0`. When upgrading the schema, first change
`OTEL_SCHEMA_URL` / `OTEL_SEMCONV_VERSION`, then update the metrics, log fields, trace
attributes, and dashboard contract tests. Histogram exemplars are enabled, and in Grafana a
latency sample with an exemplar can jump directly to the Tempo trace.

### SLO / RED / USE Quick Entry

| Goal | Recording rule | How to read it |
| --- | --- | --- |
| API success rate | `webhookwise:http_request_success_ratio_5m` | When below 0.99, first check HTTP 5xx, Loki errors, and slow Tempo traces |
| API request rate | `webhookwise:http_request_rate_5m` | Business ingress throughput; look at it together with error rate/latency |
| API p95 | `webhookwise:http_request_duration_p95_5m` | RED latency; when it rises, continue breaking it down by DB, Redis, AI, and Forwarding |
| Ingress enqueue success rate | `webhookwise:webhook_ingress_success_ratio_5m` | When below 0.99, check rate limiting, payload, and TaskIQ enqueue |
| Pipeline completion rate | `webhookwise:webhook_processing_success_ratio_5m` | When below 0.98, check pipeline steps, dead-letter, and dependency health |
| Forward success rate | `webhookwise:forward_delivery_success_ratio_5m` | When below 0.95, check target/circuit breaker/outbox |
| AI degradation rate | `webhookwise:ai_degradation_ratio_5m` | When above 0.1, check provider errors, cache, and model latency |
| DB pool utilization | `webhookwise:db_pool_utilization_ratio` | When near 1, check slow transactions, connection waits, and worker concurrency |
| Queue backlog | `webhookwise:queue_backlog` | Combine with worker task p95 and Redis latency to decide whether to scale out |
| Redis unavailable | `webhookwise:redis_unavailable_rate_5m` | Greater than 0 means the system has already entered the Redis degradation path |

### HTTP / API Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `http_server_request_duration_seconds_count` | Histogram count | `http_request_method`, `http_route`, `http_response_status_code`, `service_name` | Total number of API requests | A rise in 5xx indicates server-side errors; a rise in 4xx is usually a parameter, authentication, routing, or caller problem |
| `http_server_request_duration_seconds_bucket` | Histogram | `http_request_method`, `http_route`, `http_response_status_code` | API request duration distribution | When p95/p99 rise, continue to look at traces, DB, Redis, AI, and Forwarding metrics |
| `http_server_request_body_size_bytes_bucket` | Histogram | `http_request_method`, `http_route` | Request body size | When the Webhook payload suddenly grows, it can slow down parsing, persistence, and AI analysis |
| `http_server_active_requests` | Gauge | `service_name` | Number of HTTP requests currently being processed | A sustained rise usually indicates slow downstream, request pile-up, or insufficient process capacity |

Common reading:

```promql
sum by (http_route, http_response_status_code) (
  rate(http_server_request_duration_seconds_count{service_name="webhookwise-api"}[5m])
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

### Webhook Ingress Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `webhook_received_total` | Counter | `webhook_source`, `webhook_status` | Number of webhooks received | A spike in a particular source indicates a surge in external alerts; a rise in failure status indicates a problem in ingress validation, parsing, or enqueue |
| `webhook_ingress_payload_size_bytes_bucket` | Histogram | `webhook_source`, `webhook_outcome` | Webhook payload size distribution | Large payloads amplify parsing, DB, AI, and forwarding pressure |
| `security_checks_total` | Counter | `security_check`, `security_result` | Security check result counts | When `denied` and `failed` rise, first check the signature, token, source IP, and rate-limit configuration |

`webhook_received_total` is ingress throughput, while `http_server_request_duration_seconds_count` is HTTP-layer throughput. The two are not necessarily exactly equal, because the HTTP layer also includes ready, dashboard, static assets, and other endpoints.

### Webhook Pipeline Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `webhook_pipeline_steps_total` | Counter | `pipeline_step`, `webhook_source`, `webhook_outcome` | Execution count for each pipeline step | More `error` at a particular step means the problem is concentrated in that processing stage |
| `webhook_pipeline_step_duration_seconds_bucket` | Histogram | `pipeline_step`, `webhook_source`, `webhook_outcome` | Per-step pipeline duration | Use it to locate whether the slowness is in parsing, noise reduction, AI, persistence, forwarding, or another step |
| `webhook_processing_duration_seconds_bucket` | Histogram | `webhook_source`, `webhook_outcome` | End-to-end webhook processing duration | A rise in p95/p99 means the overall processing path has slowed down |
| `webhook_processed_total` | Counter | `webhook_status` | Webhook status-transition counts | When `error`, `failed`, or `suppressed` rise, check the cause together with logs and events |
| `webhook_running_tasks` | Gauge | Usually no business labels | Number of currently running webhook tasks | A sustained high value means the worker is busy; a high value combined with queue lag is a signal of insufficient processing capacity |

Note: the local recording rules record the raw OTel gauges under more intuitive project metric names,
and the dashboard and query scripts uniformly use recording rule names such as `webhook_running_tasks`.

### Noise Reduction / Suppression Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `webhook_noise_evaluations_total` | Counter | `webhook_source`, `webhook_relation`, `webhook_suppressed` | Number of noise-reduction evaluations | A rise usually tracks a rise in ingress alert volume |
| `webhook_noise_evaluation_duration_seconds_bucket` | Histogram | `webhook_source`, `webhook_relation`, `webhook_suppressed` | Noise-reduction evaluation duration | When it slows down, check correlation queries, caching, and rule complexity |
| `webhook_noise_evaluations_total{webhook_suppressed="true"}` | Counter query | `webhook_source`, `webhook_relation`, `webhook_suppressed` | The number suppressed by noise reduction | An abnormal rise in suppression ratio may be an alert storm, or it may be overly strict rules |
| `webhook_storm_suppressed_total` | Counter | `webhook_source` | Number of fast alert-storm suppressions | A rise means a source is producing a lot of noise in a short time, so check the upstream alert policy |

### Queue Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `queue_operations_total` | Counter | `queue_name`, `queue_operation`, `queue_status` | Number of queue operations | When `error` rises, check whether the Redis connection and stream/group are healthy |
| `queue_operation_duration_seconds_bucket` | Histogram | `queue_name`, `queue_operation`, `queue_status` | Queue operation duration | Whether the slowness is in enqueue/read/ack can be distinguished by `queue_operation` |
| `queue_depth` | Gauge | `queue_stream` | Redis Stream retained length, i.e. `XLEN` | Grows toward `WEBHOOK_MQ_STREAM_MAXLEN` as historical messages are retained; a rise on its own does not indicate a consumption backlog |
| `queue_pending` | Gauge | `queue_stream`, `queue_group` | Number of delivered but un-acked messages | A rise means the worker picked up tasks but processing or acking has not kept up |
| `queue_lag` | Gauge | `queue_stream`, `queue_group` | The lag the consumer group has not yet consumed | A sustained rise is a direct signal that the worker cannot keep up |

Common combined judgments:

| Phenomenon | Possible cause |
| --- | --- |
| `queue_depth` rises while `queue_pending` and `queue_lag` stay low | The Redis Stream is retaining historical messages; usually not a consumption blockage |
| `queue_lag` rises | The worker has not yet read new tasks; possibly insufficient consumption capacity or the worker is not reading normally |
| `queue_pending` rises | The worker has taken tasks but processing is slow, failing with retries, or acking abnormally |
| `queue_operation_duration_seconds_bucket` p95 rises | Redis is slow, the network is slow, or stream operations are blocked |

### Worker Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `worker_task_runs_total` | Counter | `worker_task_name`, `worker_task_status` | Number of worker task executions | When `error` or `failed` rise, check the Loki logs by task name |
| `worker_task_duration_seconds_bucket` | Histogram | `worker_task_name`, `worker_task_status` | Worker task duration | A high p95 means task processing is slow; continue breaking it down by DB, Redis, AI, and Forwarding |
| `webhook_dead_letter_total` | Counter | Usually no business labels | Number of dead letters that are no longer retried | This is a high-priority anomaly; you need to check the specific event and error cause |

Worker metrics mainly answer "whether tasks are being consumed, whether execution succeeds, and whether duration is stable". They are most valuable when viewed together with the Queue metrics.

### Scheduler Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `scheduler_task_runs_total` | Counter | `scheduler_task_name`, `scheduler_task_status` | Number of periodic task executions | If a task has no success for a long time or error rises, check the scheduler/worker logs |
| `scheduler_task_duration_seconds_bucket` | Histogram | `scheduler_task_name` | Periodic task duration | A longer duration means the scan range, DB query, or downstream processing has slowed down |
| `scheduler_task_lag_seconds` | Gauge | `scheduler_task_name` | The periodic task's lag relative to its expected execution time | A continuously growing lag means the task is not finishing on time or scheduling is blocked |
| `scheduler_task_last_success_unixtime_seconds` | Gauge | `scheduler_task_name` | The Unix time of the last successful execution | Use `time() - ...` to see how long it has been since the last success |

Common reading:

```promql
time() - scheduler_task_last_success_unixtime_seconds
```

The larger this value, the longer the task has gone without a successful execution. This is especially important for tasks like recovery scans and data maintenance.

### Database Client / Pool Metrics

These are application-side DB client/pool metrics, not Postgres server exporter metrics. `db_pool_connections_checked_out` and `db_pool_connections_max` come from real-time state callbacks of the SQLAlchemy pool.

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `db_sessions_total` | Counter | `db_operation`, `db_status` | DB session/transaction lifecycle counts | When `error` rises, check SQLAlchemy, connection, and transaction-rollback logs |
| `db_session_duration_seconds_bucket` | Histogram | `db_operation`, `db_status` | DB session/transaction duration | A high p95 means slow queries, long transactions, connection waits, or lock contention |
| `db_pool_connections_checked_out` | Gauge | Usually no business labels | Number of currently checked-out DB connections | Staying near the pool limit for a long time means the DB pool is under pressure |
| `db_pool_connections_max` | Gauge | Usually no business labels | DB connection pool capacity | View it together with checked_out to see whether the pool is saturated |

Note: `checked_out / max` is the actual pool utilization ratio. A high `checked_out` alone is not necessarily abnormal; look at it together with `max`, API p95, and DB session p95.

### Redis Client Metrics

These are application-side Redis client metrics, not Redis server exporter metrics.

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `redis_operations_total` | Counter | `redis_operation`, `redis_status` | Number of Redis operations | When `error` rises, check the Redis connection, timeouts, and command parameters |
| `redis_operation_duration_seconds_bucket` | Histogram | `redis_operation`, `redis_status` | Redis operation duration | When `xlen`, `xpending`, `eval`, etc. slow down, they affect the queue and rate limiting |

Redis metrics should be viewed together with the Queue. When queue lag rises and Redis p95 also rises, the bottleneck may be in Redis or how it is called; when queue lag rises but Redis is not slow, the bottleneck is more likely in the worker's business processing.

### AI Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `ai_request_duration_seconds_bucket` | Histogram | `webhook_source`, `ai_engine` | AI analysis duration | A high p95/p99 means the model call, network, or fallback has slowed down |
| `ai_request_errors_total` | Counter | `error_type` | AI provider call errors | Timeouts, rate limiting, authentication, and response-format errors show up here |
| `ai_tokens_total` | Counter | `ai_model`, `ai_token_type` | Token consumption | A spike in completion or prompt tokens directly affects cost and latency |
| `ai_cost_USD_total` | Counter | `ai_model` | Estimated AI cost | When cost is abnormal, break it down by model and source |
| `ai_cache_requests_total` | Counter | `ai_cache_operation`, `ai_cache_result` | AI cache requests and hit/miss | More misses increase the number of model calls |
| `ai_cache_operation_duration_seconds_bucket` | Histogram | `ai_cache_operation`, `ai_cache_result` | AI cache operation duration | A slow cache drags down the overall analysis |
| `ai_degradations_total` | Counter | `ai_degradation_reason` | Number of AI degradations | A rise means the main path is unstable and the system is using fallback or simplified logic |
| `ai_deep_analysis_total` | Counter | `webhook_status`, `ai_engine` | Deep-analysis task result counts | When failed rises, check the deep analysis logs and external service status |

AI metrics are usually viewed together with `webhook_processing_duration_seconds_bucket`. If overall processing is slow but AI is not, the bottleneck may be in DB, Redis, forwarding, or the queue.

### Forwarding Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `forward_delivery_total` | Counter | `forward_target_type`, `forward_status` | Number of forwarding attempts | A rise in failed means a problem with the target address, network, authentication, or payload |
| `forward_delivery_duration_seconds_bucket` | Histogram | `forward_target_type`, `forward_status` | Forwarding request duration | A high p95 means the downstream target is slow or the network is slow |
| `forward_outbox_records_total` | Counter | `forward_target_type`, `forward_status` | Outbox record lifecycle counts | Abnormal pending/failed means the async compensation path is under pressure |
| `forward_outbox_process_duration_seconds_bucket` | Histogram | `forward_target_type`, `forward_status` | Outbox processing duration | When it slows down, check the target service and DB queries |
| `forward_outbox_backlog_age_seconds` | Gauge | `forward_target_type`, `forward_status` | Age of the oldest uncompleted outbox record | A sustained rise means the async forwarding path is backing up; investigate even when request volume is low |
| `circuit_breaker_state` | Gauge | `circuit_breaker_name`, `circuit_breaker_state` | Current circuit breaker state, where the current state is 1 | open = 1 means the dependency has been protectively cut off; immediately look at downstream logs and error rates |

### Newly Added Phase Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `webhook_ingress_requests_total` | Counter | `webhook_source`, `webhook_outcome` | API ingress receive, suppress, reject, and enqueue outcomes | When rejected/error rise, first check authentication, rate limiting, body size, and TaskIQ enqueue |
| `ai_requests_total` | Counter | `webhook_source`, `ai_engine`, `ai_status` | AI / rule / cache request outcomes | openai error, cache hit, and rule success can distinguish a model failure from a deliberate degradation |
| `db_health_state` | Gauge | `db_state` | Database health state | When unhealthy=1, first check DB connections, migrations, and statement timeouts |

### Domain Events / Signals Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `observability_events_total` | Counter | OTel `event.name`, `event_name` in Prometheus | Structured business event counts | Used to confirm whether key business milestones occurred, for example task started, analysis completed, storm suppressed |
| `observability_signals_total` | Counter | OTel `signal.name`, `signal.state`, `signal_name`, `signal_state` in Prometheus | Domain state-transition counts | Good for seeing how often the system enters states such as completed, error, and suppressed |

Event and signal metrics are only good for seeing "how many times something happened". For exactly which event, request, or alert, go to Loki and query by `event.name`, `trace_id`, and `span_id`.

### Faro Frontend RUM Metrics

| Metric | Type | Meaning | Interpreting anomalies |
| --- | --- | --- | --- |
| `faro_receiver_events_total` | Counter | Number of Faro browser events, for example `session_start` | 0 usually means the Dashboard was not opened, or the SDK/collector is not connected |
| `faro_receiver_measurements_total` | Counter | Number of frontend performance measurements, for example Web Vitals, navigation, resource | 0 means browser performance data did not reach Alloy |
| `faro_receiver_exceptions_total` | Counter | Number of frontend exceptions | When it rises, go to Loki and query `{app="webhookwise-dashboard", kind="exception"} | json` |
| `faro_receiver_logs_total` | Counter | Number of frontend logs | Used to confirm whether browser logs reached Loki |
| `faro_receiver_request_duration_seconds_bucket` | Histogram | Duration of requests received by the Alloy Faro receiver | A rise means the collector processing or the network is under pressure |
| `faro_receiver_rate_limiter_requests_total` | Counter | Faro receiver rate-limited request counts | A rise means the frontend reporting volume is too high or the rate-limit configuration is too strict |

Prometheus can only show Faro receive volume and collector state. View the specific browser event content in Loki:

```logql
{app="webhookwise-dashboard"} | json
```

### Beyla Auto-instrumentation Metrics

| Metric | Type | Key labels | Meaning | Interpreting anomalies |
| --- | --- | --- | --- | --- |
| `traces_span_metrics_calls_total{source="beyla"}` | Counter | `service_name`, `span_name`, `span_kind` | Number of span calls automatically identified by Beyla eBPF | Seeing values means the eBPF auto-instrumentation path is working |
| `traces_span_metrics_duration_seconds_bucket{source="beyla"}` | Histogram | `service_name`, `span_name`, `span_kind` | Duration of HTTP/SQL/Redis spans auto-collected by Beyla | Used to supplement the application SDK metrics from a process perspective |
| `process_cpu_utilization_ratio` | Gauge | `service_name`, `cpu_mode` | Process CPU utilization | When CPU is high but requests are few, go to Pyroscope to look at hot functions |
| `process_memory_usage_bytes` | Gauge | `service_name` | Process memory usage | A continuous rise may be cache bloat or a leak; investigate together with profiles and container memory |
| `process_network_io_bytes_total` | Counter | `service_name`, direction label | Process network IO | Helps judge traffic changes when forwarding or external calls are abnormal |

Beyla is a zero-intrusion supplementary perspective. Application SDK metrics understand the business semantics better, while Beyla is closer to the real process, HTTP, SQL, and Redis calls.

### k6 Load-Testing Metrics

| Metric | Type | Meaning | Interpreting anomalies |
| --- | --- | --- | --- |
| `k6_http_reqs_total` | Counter | Total number of load-test requests | Used to confirm whether this load-test run actually reached the service |
| `k6_http_req_failed_rate` | Gauge | Request failure rate | Should be near 0 for smoke scenarios; when it rises, look at API 5xx and Loki error logs |
| `k6_http_req_duration_p95` / `k6_http_req_duration_p99` | Gauge | Request p95/p99 as observed by k6 | Represents end-to-end duration from the client perspective |
| `k6_http_req_waiting_p95` / `k6_http_req_waiting_p99` | Gauge | Time waiting for the first byte of the server response | Closest to the backend processing time; look at it first when troubleshooting slow requests |
| `k6_http_req_blocked_p95` | Gauge | Time the request is blocked on the client waiting for a connection slot, DNS, and TCP | A local abnormal rise is usually a client or connection-reuse problem |
| `k6_http_req_connecting_p95` | Gauge | TCP connection establishment duration | Usually very low locally; when it rises, look at the network or service listener |
| `k6_http_req_sending_p95` | Gauge | Time to send the request body | Rises when the payload grows or the network is slow |
| `k6_http_req_receiving_p95` | Gauge | Time to receive the response body | Rises with large responses or a slow network |
| `k6_checks_rate` | Gauge | k6 script assertion success rate | Should be near 1; a drop means the status/body checks in the script failed |
| `k6_vus` / `k6_vus_max` | Gauge | Current and maximum virtual users | Used to align load-test phases with service metric changes |
| `k6_data_sent_total` / `k6_data_received_total` | Counter | Amount of data sent and received by the load test | Helps judge payload or response-body changes |

k6 writes stale markers after it finishes, so a Grafana instant query may be empty. When querying the run you just completed, use:

```promql
max_over_time(k6_http_req_duration_p95[30m])
```

### Observability Backend Self Metrics

| Metric | Component | Meaning | Interpreting anomalies |
| --- | --- | --- | --- |
| `alloy_config_last_load_successful` | Alloy | Whether the last Alloy config load succeeded | 0 means the config load failed |
| `alloy_component_controller_running_components` | Alloy | Number of running Alloy components | An abnormal drop means some receivers/exporters did not start |
| `loki_write_dropped_entries_total` | Alloy/Loki write | Number of dropped log entries | A rise means Loki write failures, rate limiting, or a pipeline configuration problem |
| `up` | Prometheus | Whether the scrape target is available | 0 means the corresponding target failed to be scraped |
| `prometheus_tsdb_wal_writes_failed_total` | Prometheus | Number of WAL write failures | A rise means a problem with Prometheus local storage |
| `prometheus_tsdb_wal_storage_size_bytes` | Prometheus | WAL space usage | When it keeps growing, check disk and sample volume |
