# 故障排查指南

## 🔍 快速诊断

### 健康检查

```bash
curl http://localhost:8000/ready
```

正常响应：
```json
{"success": true, "data": {"status": "ready", "database": "ok", "redis": "ok", "queue": "redis_stream"}}
```

如果 HTTP 状态为 `503`，按 `data.database` / `data.redis` 判断失败依赖；`queue` 固定为 `redis_stream`。

### 查看日志

```bash
# Docker 模式
docker compose logs webhook-service -f
docker compose logs worker -f

# 本地模式：查看启动 uvicorn/gunicorn/taskiq 的终端 stdout
```

日志输出到 stdout；启用观测栈时通过 OTLP logs 进入 Loki。每条日志含 `trace_id`、`span_id`、`request.id`、`webhook.event_id`（若有上下文）。

---

## ❗ 常见问题

### 1. Webhook 接收后无分析结果

**症状：** POST `/webhook` 返回 202，但按 `request_id` 查不到最终事件。

**排查步骤：**

1. 先确认 API 就绪状态：
   ```bash
   curl http://localhost:8000/ready
   ```

2. 确认 Worker 进程是否在运行：
   ```bash
   docker compose ps worker
   ```

3. 检查 Worker 日志是否有报错：
   ```bash
   docker compose logs worker --tail 50
   ```

4. 检查 Redis 连接：
   ```bash
   docker compose -f docker-compose.infra.yml exec redis redis-cli ping  # 应返回 PONG
   ```

5. 确认任务是否入队（TaskIQ 使用 Redis Stream）：
   ```bash
   docker compose -f docker-compose.infra.yml exec redis redis-cli xinfo stream webhook:queue
   ```

6. 如果处理失败已进入 dead-letter，可按原 raw payload 重放：
   ```bash
   curl http://localhost:8000/api/admin/dead-letters \
     -H "Authorization: Bearer $API_KEY"

   curl -X POST http://localhost:8000/api/admin/dead-letters/{event_id}/replay \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

---

### 2. AI 分析未执行（事件重要性为空或走规则降级）

**症状：** 事件完成处理但 `ai_analysis` 为规则分析结果，日志中出现 "AI 分析降级"。

**排查步骤：**

1. 检查进程环境中的 `ENABLE_AI_ANALYSIS` 和 `OPENAI_API_KEY` 是否已配置。
   ```bash
   docker compose exec webhook-service sh -lc 'printf "ENABLE_AI_ANALYSIS=%s\nOPENAI_API_KEY=%s\n" "$ENABLE_AI_ANALYSIS" "${OPENAI_API_KEY:+configured}"'
   ```

2. 检查 AI API 连通性（Worker 日志会有 HTTP 错误详情）。

3. 如果使用 OpenRouter，确认 `OPENAI_API_URL` 正确（默认 `https://openrouter.ai/api/v1`）。

4. 检查 `ENABLE_AI_DEGRADATION` 是否为 `true`（开启时 AI 失败会静默降级到规则分析）。

---

### 3. 配置修改后不生效

**症状：** 修改了 `.env`、环境变量、ConfigMap 或 Secret，但行为未变化。

**说明：**
- 所有应用配置都只在服务启动时读取，修改后必须重启本地进程或滚动发布容器。
- 应用不再从数据库读取配置，也不提供在线配置读写入口。

**排查：**
```bash
docker compose config
docker compose exec webhook-service env | sort
```

---

### 4. 深度分析结果一直处于「分析中」

**症状：** OpenClaw 深度分析记录状态为 `pending`，长时间未完成。

**排查步骤：**

1. 检查 `OPENCLAW_ENABLED` 是否为 `true`，`OPENCLAW_GATEWAY_URL` 是否可访问。

2. 通过手动重拉确认 OpenClaw 是否已有结果：
   ```bash
   curl -X POST http://localhost:8000/api/deep-analyses/{id}/retry \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

3. 如果返回超时错误（已超过 `OPENCLAW_TIMEOUT_SECONDS`），说明分析超时，需重新发起：
   ```bash
   curl -X POST http://localhost:8000/api/deep-analyze/{webhook_id} \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY" \
     -H "Content-Type: application/json" \
     -d '{"engine": "openclaw"}'
   ```

4. 当前手动深度分析入口只接受 `auto` / `openclaw`。OpenClaw 不可用时，接口会按配置降级到本地 AI 或返回 `No engine available`；不要传 `engine: "local"`。

---

### 5. 深度分析完成后飞书未收到通知

**症状：** 手动重拉成功，但飞书群未收到消息。

**排查步骤：**

1. 确认 `DEEP_ANALYSIS_FEISHU_WEBHOOK` 已配置：
   ```bash
   docker compose exec webhook-service sh -lc 'test -n "$DEEP_ANALYSIS_FEISHU_WEBHOOK" && echo configured'
   ```

2. 手动测试飞书 Webhook 连通性：
   ```bash
   curl -X POST "$DEEP_ANALYSIS_FEISHU_WEBHOOK" \
     -H "Content-Type: application/json" \
     -d '{"msg_type": "text", "content": {"text": "test"}}'
   ```

3. 检查 Worker 日志中是否有 `飞书深度分析通知` 相关日志（INFO 或 WARNING 级别）。

4. 飞书 Webhook 熔断器（Circuit Breaker）在连续失败后会打开一段时间。当前通知会先进入 outbox，投递失败会记录失败原因并按策略重试；检查 outbox 状态和 Worker 日志能看到是否是熔断、URL 安全校验、HTTP 错误或飞书业务错误码。

---

### 6. 转发失败，事件未推送到目标系统

**症状：** 事件处理完成，但目标系统未收到通知。

**排查步骤：**

1. 检查转发规则是否正确配置（重要性匹配、目标 URL 非空）：
   ```bash
   curl http://localhost:8000/api/forward-rules \
     -H "Authorization: Bearer $API_KEY"
   ```

2. 手动触发转发（写操作需要 `ADMIN_WRITE_KEY`）：
   ```bash
   curl -X POST http://localhost:8000/api/forward/{webhook_id} \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

3. 检查 Worker 日志中是否有 `ForwardOutbox` 或转发相关的 HTTP 错误。

4. 如果 outbox 记录进入 `expired`，表示已超过 `FORWARD_MAX_DELIVERY_AGE_SECONDS`，系统为避免过期告警误发而停止自动投递。确认仍需发送后，使用手动转发接口重新发送当前事件。

---

### 7. 告警被标记为重复但预期不是

**症状：** 新告警被归入已有告警的重复项，`is_duplicate=true`。

**排查：**

1. 检查 `DUPLICATE_ALERT_TIME_WINDOW`（默认 24 小时）。如需缩短去重窗口，修改配置文件后重启或滚动发布：
   ```bash
   DUPLICATE_ALERT_TIME_WINDOW=1
   ```

2. 告警 hash 优先由 adapter 产出的 `_alert_identity` 生成。如果两条告警 hash 相同但内容有差异，优先检查对应 adapter 是否把关键身份字段写进了 `_alert_identity`；未知来源缺少 identity 时会退回完整 payload hash 并记录 warning。

3. 对于已被错误标记的事件，可强制重新分析：
   ```bash
   curl -X POST http://localhost:8000/api/reanalyze/{webhook_id} \
     -H "Authorization: Bearer $ADMIN_WRITE_KEY"
   ```

---

### 8. 数据库语句超时（`statement timeout`）

**症状：** 日志出现 `canceling statement due to statement timeout` 或 `asyncpg.exceptions.QueryCanceledError`。

**原因：** SQL 查询或行锁等待超过了 `DB_STATEMENT_TIMEOUT_MS`（默认 30000ms）。

**处理：**

- 如果是偶发的行锁超时，TaskIQ 重试和后台补扫会自动重新投递。
- 如果频繁出现，可适当增大超时：`.env` 中调整 `DB_STATEMENT_TIMEOUT_MS=60000`，然后重启服务。
- 检查是否有长事务未提交（通过 PostgreSQL `pg_stat_activity` 视图排查）。

---

### 9. 服务内存持续增长

**症状：** 容器内存用量随时间缓慢上升。

**排查：**

1. 在 Grafana/Prometheus-compatible backend 中检查 `queue.pending`、`queue.lag`、`webhook.running_tasks` 是否持续增长。`queue.depth` 是 Redis Stream 保留长度，单独增长不代表消费积压。

2. 检查 Redis 内存：
   ```bash
   docker compose -f docker-compose.infra.yml exec redis redis-cli info memory | grep used_memory_human
   ```

3. 检查主表 `webhook_events` 行数：如果很大，说明过期数据清理未正常执行。清理由 `scheduled_data_maintenance` 周期任务执行，优先检查 scheduler/worker 日志和 `scheduler.task.*` 指标。

4. Docker 内存问题：`docker-compose.yml` 中 API 服务默认限制 1GB，Worker 512MB。可按需调整。

---

## 🪲 开启 DEBUG 日志

排查 AI 分析内容或 Webhook 解析问题时，开启 DEBUG 级别：

```bash
# 修改配置文件后重启或滚动发布
LOG_LEVEL=DEBUG
```

排查完成后记得改回 `INFO`。

---

## 📞 寻求帮助

提供以下信息有助于快速定位问题：
1. 相关的 `trace_id`（从日志中获取）
2. 对应事件的 `event_id`
3. Worker 和 API 日志中的完整错误堆栈
4. 相关容器的环境变量和最近一次滚动/重启时间
