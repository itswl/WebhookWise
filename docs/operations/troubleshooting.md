# Troubleshooting Guide

## 🔍 Quick Diagnosis

### Health check

```bash
curl http://localhost:8000/ready
```

Normal response:
```json
{"success": true, "data": {"status": "ready", "database": "ok", "redis": "ok", "queue": "redis_stream"}}
```

If the HTTP status is `503`, use `data.database` / `data.redis` to identify the failing dependency; `queue` is always `redis_stream`.

### View logs

```bash
# Docker mode
docker compose logs webhook-service -f
docker compose logs worker -f

# Local mode: view the stdout of the terminal that started uvicorn/gunicorn/taskiq
```

For everyday troubleshooting, just run `docker compose ...` from the repository root; the root `compose.yaml` only manages the business containers such as PostgreSQL, Redis, API, Worker, and Scheduler. The observability stack uses the separate project `webhookwise-observability`, which you specify explicitly when you need to view containers such as Grafana, Prometheus, Loki, Tempo, and Alloy.

Logs are written to stdout; when the observability stack is enabled, they enter Loki through OTLP logs. Each log contains `trace_id`, `span_id`, `request.id`, and `webhook.event_id` (when context is available).

---

## ❗ Common Issues

### 1. No analysis result after a webhook is received

**Symptom:** POST `/v1/webhook` returns 200, but the final event cannot be found by `request_id`.

**Troubleshooting steps:**

1. First confirm the API readiness status:
   ```bash
   curl http://localhost:8000/ready
   ```

2. Confirm whether the Worker process is running:
   ```bash
   docker compose ps worker
   ```

3. Check whether there are errors in the Worker logs:
   ```bash
   docker compose logs worker --tail 50
   ```

4. Check the Redis connection:
   ```bash
   docker compose exec redis redis-cli ping  # should return PONG
   ```

5. Confirm whether the task was enqueued (TaskIQ uses Redis Stream):
   ```bash
   docker compose exec redis redis-cli xinfo stream webhook:queue
   ```

6. If processing failed and already went to dead-letter, you can replay it by the original raw payload:
   ```bash
   curl http://localhost:8000/v1/admin/dead-letters \
     -H "Authorization: Bearer $API_KEY"

   curl -X POST http://localhost:8000/v1/admin/dead-letters/{event_id}/replay \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

---

### 2. AI analysis did not run (event importance is empty or it falls back to rules)

**Symptom:** The event finished processing but `ai_analysis` is a rule-based analysis result, and the logs show that AI analysis was degraded.

**Troubleshooting steps:**

1. Check whether `ENABLE_AI_ANALYSIS` and `OPENAI_API_KEY` are configured in the process environment.
   ```bash
   docker compose exec webhook-service sh -lc 'printf "ENABLE_AI_ANALYSIS=%s\nOPENAI_API_KEY=%s\n" "$ENABLE_AI_ANALYSIS" "${OPENAI_API_KEY:+configured}"'
   ```

2. Check AI API connectivity (the Worker logs will have HTTP error details).

3. If you use OpenRouter, confirm that `OPENAI_API_URL` is correct (default `https://openrouter.ai/api/v1`).

4. Check whether `ENABLE_AI_DEGRADATION` is `true` (when enabled, AI failures silently fall back to rule-based analysis).

---

### 3. Configuration changes do not take effect

**Symptom:** You changed `.env`, environment variables, ConfigMap, or Secret, but the behavior did not change.

**Explanation:**
- All application configuration is read only at service startup; after changes you must restart the local process or perform a rolling release of the container.
- The application no longer reads configuration from the database and does not provide an online configuration read/write entry point.

**Troubleshooting:**
```bash
docker compose config
docker compose exec webhook-service env | sort
```

---

### 4. Deep analysis result stays in "analyzing"

**Symptom:** The OpenClaw deep analysis record stays in the `pending` state and does not complete for a long time.

**Troubleshooting steps:**

1. Check whether `OPENCLAW_ENABLED` is `true` and whether `OPENCLAW_GATEWAY_URL` is reachable.

2. Confirm whether OpenClaw already has a result by manually re-fetching:
   ```bash
   curl -X POST http://localhost:8000/v1/deep-analyses/{id}/retry \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

3. If it returns a timeout error (already exceeded `OPENCLAW_TIMEOUT_SECONDS`), the analysis timed out and you need to re-initiate it:
   ```bash
   curl -X POST http://localhost:8000/v1/deep-analyze/{webhook_id} \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY" \
     -H "Content-Type: application/json" \
     -d '{"engine": "openclaw"}'
   ```

4. The current manual deep-analysis entry point only accepts `auto` / `openclaw`. When OpenClaw is unavailable, the endpoint falls back to local AI per configuration or returns `No engine available`; do not pass `engine: "local"`.

---

### 5. Feishu did not receive a notification after deep analysis completed

**Symptom:** The manual re-fetch succeeded, but the Feishu group did not receive a message.

**Troubleshooting steps:**

1. Confirm that `DEEP_ANALYSIS_FEISHU_WEBHOOK` is configured:
   ```bash
   docker compose exec webhook-service sh -lc 'test -n "$DEEP_ANALYSIS_FEISHU_WEBHOOK" && echo configured'
   ```

2. Manually test Feishu Webhook connectivity:
   ```bash
   curl -X POST "$DEEP_ANALYSIS_FEISHU_WEBHOOK" \
     -H "Content-Type: application/json" \
     -d '{"msg_type": "text", "content": {"text": "test"}}'
   ```

3. Check the Worker logs for logs related to the Feishu deep-analysis notification (INFO or WARNING level).

4. The Feishu Webhook circuit breaker opens for a while after consecutive failures. Notifications now go into the outbox first; a failed delivery records the failure reason and retries per policy. Checking the outbox status and the Worker logs shows whether it is a circuit-breaker trip, URL safety check, HTTP error, or a Feishu business error code.

---

### 6. Forwarding failed and the event was not pushed to the target system

**Symptom:** The event finished processing, but the target system did not receive a notification.

**Troubleshooting steps:**

1. Check whether the forwarding rules are configured correctly (importance match, non-empty target URL):
   ```bash
   curl http://localhost:8000/v1/forward-rules \
     -H "Authorization: Bearer $API_KEY"
   ```

2. Manually trigger forwarding (write operations require `ADMIN_WRITE_KEY`):
   ```bash
   curl -X POST http://localhost:8000/v1/forward/{webhook_id} \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

3. Check the Worker logs for `ForwardOutbox` or forwarding-related HTTP errors.

4. If an outbox record enters `expired`, it has exceeded `FORWARD_MAX_DELIVERY_AGE_SECONDS`, and the system stops automatic delivery to avoid mistakenly sending stale alerts. After confirming it still needs to be sent, use the manual forwarding endpoint to resend the current event.

---

### 7. An alert is marked as a duplicate when it is not expected to be

**Symptom:** A new alert is grouped as a duplicate of an existing alert, `is_duplicate=true`.

**Troubleshooting:**

1. Check `DUPLICATE_ALERT_TIME_WINDOW` (default 24 hours). To shorten the deduplication window, change the configuration file and then restart or perform a rolling release:
   ```bash
   DUPLICATE_ALERT_TIME_WINDOW=1
   ```

2. The alert hash is preferentially generated from the `_alert_identity` produced by the adapter. If two alerts have the same hash but differ in content, first check whether the corresponding adapter wrote the key identity fields into `_alert_identity`; when an unknown source lacks an identity, it falls back to the full payload hash and logs a warning.

3. For an event that was mismarked, you can force re-analysis:
   ```bash
   curl -X POST http://localhost:8000/v1/reanalyze/{webhook_id} \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

---

### 8. Database statement timeout (`statement timeout`)

**Symptom:** The logs show `canceling statement due to statement timeout` or `asyncpg.exceptions.QueryCanceledError`.

**Cause:** A SQL query or row-lock wait exceeded `DB_STATEMENT_TIMEOUT_MS` (default 30000ms).

**What to do:**

- If it is an occasional row-lock timeout, TaskIQ retries and background re-scans will automatically redeliver.
- If it happens frequently, you can increase the timeout appropriately: set `DB_STATEMENT_TIMEOUT_MS=60000` in `.env` and then restart the service.
- Check whether there is a long, uncommitted transaction (investigate via the PostgreSQL `pg_stat_activity` view).

---

### 9. Service memory keeps growing

**Symptom:** Container memory usage rises slowly over time.

**Troubleshooting:**

1. In Grafana/a Prometheus-compatible backend, check whether `queue.pending`, `queue.lag`, and `webhook.running_tasks` keep growing. `queue.depth` is the Redis Stream retained length, and growth on its own does not indicate a consumption backlog.

2. Check Redis memory:
   ```bash
   docker compose exec redis redis-cli info memory | grep used_memory_human
   ```

3. Check the row count of the main table `webhook_events`: if it is very large, it means expired-data cleanup is not running normally. Cleanup is performed by the `scheduled_data_maintenance` periodic task; first check the scheduler/worker logs and the `scheduler.task.*` metrics.

4. Docker memory problems: in `deploy/compose/docker-compose.yml`, the API service defaults to a 1GB limit and the Worker to 512MB. Adjust as needed.

---

## 🪲 Enabling DEBUG Logging

When troubleshooting AI analysis content or Webhook parsing problems, enable the DEBUG level:

```bash
# Change the configuration file, then restart or perform a rolling release
LOG_LEVEL=DEBUG
```

Remember to change it back to `INFO` after troubleshooting.

---

## 📞 Getting Help

Providing the following information helps locate the problem quickly:
1. The relevant `trace_id` (obtained from the logs)
2. The `event_id` of the corresponding event
3. The full error stack from the Worker and API logs
4. The environment variables of the relevant containers and the time of the last rollout/restart
