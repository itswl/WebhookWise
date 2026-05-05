# 故障排查指南

## 🔍 快速诊断

### 健康检查

```bash
curl http://localhost:8000/health
```

正常响应：
```json
{"status": "ok", "db": "ok", "timestamp": "..."}
```

如果 `db` 为 `error`，说明数据库连接失败，检查 `DATABASE_URL` 配置。

### 查看日志

```bash
# Docker 模式
docker logs webhookwise-api -f
docker logs webhookwise-worker -f

# 本地模式
tail -f logs/webhook.log
```

日志为 JSON 结构化格式，每条日志含 `trace_id`、`event_id`（若有上下文）。

---

## ❗ 常见问题

### 1. Webhook 接收后无分析结果

**症状：** POST `/webhook` 返回 202，但事件一直处于 `received` 状态。

**排查步骤：**

1. 确认 Worker 进程是否在运行：
   ```bash
   docker ps  # 检查 webhook-worker 是否 Up
   ```

2. 检查 Worker 日志是否有报错：
   ```bash
   docker logs webhookwise-worker --tail 50
   ```

3. 检查 Redis 连接：
   ```bash
   redis-cli -u $REDIS_URL ping  # 应返回 PONG
   ```

4. 确认任务是否入队（TaskIQ 使用 Redis List）：
   ```bash
   redis-cli llen webhook:queue
   ```

5. 如果是事件卡在 `analyzing` 状态超过 5 分钟，Recovery Poller 会自动重拾。也可手动触发：
   ```bash
   curl -X POST http://localhost:8000/api/stuck-events/requeue-all \
     -H "Authorization: Bearer $API_KEY"
   ```

---

### 2. AI 分析未执行（事件重要性为空或走规则降级）

**症状：** 事件完成处理但 `ai_analysis` 为规则分析结果，日志中出现 "AI 分析降级"。

**排查步骤：**

1. 检查 `ENABLE_AI_ANALYSIS` 和 `OPENAI_API_KEY` 是否已配置：
   ```bash
   curl http://localhost:8000/api/config \
     -H "Authorization: Bearer $API_KEY" | jq '.data.OPENAI_API_KEY'
   ```

2. 检查 AI API 连通性（Worker 日志会有 HTTP 错误详情）。

3. 如果使用 OpenRouter，确认 `OPENAI_API_URL` 正确（默认 `https://openrouter.ai/api/v1`）。

4. 检查 `ENABLE_AI_DEGRADATION` 是否为 `true`（开启时 AI 失败会静默降级到规则分析）。

---

### 3. 配置修改后不生效

**症状：** 修改了 `.env` 或通过 `POST /api/config` 提交了新配置，但行为未变化。

**说明：**
- `.env` 中的配置**只在服务启动时读取**，修改后必须重启容器。
- `POST /api/config` 写入数据库，通过 Redis Pub/Sub 广播，**无需重启**，但生效有秒级延迟。

**排查：**
```bash
# 查看某个 key 当前来源（db/env/default）及最后更新时间
curl http://localhost:8000/api/config/sources \
  -H "Authorization: Bearer $API_KEY" | jq '.data.OPENAI_MODEL'
```

如果来源是 `db` 但值仍为旧值，检查 Redis Pub/Sub 是否正常：
```bash
redis-cli subscribe config:updates  # 应该能收到广播消息
```

---

### 4. 深度分析结果一直处于「分析中」

**症状：** OpenClaw 深度分析记录状态为 `pending`，长时间未完成。

**排查步骤：**

1. 检查 `OPENCLAW_ENABLED` 是否为 `true`，`OPENCLAW_GATEWAY_URL` 是否可访问。

2. 通过手动重拉确认 OpenClaw 是否已有结果：
   ```bash
   curl -X POST http://localhost:8000/api/deep-analyses/{id}/retry \
     -H "Authorization: Bearer $API_KEY"
   ```

3. 如果返回超时错误（已超过 `OPENCLAW_TIMEOUT_SECONDS`），说明分析超时，需重新发起：
   ```bash
   curl -X POST http://localhost:8000/api/deep-analyze/{webhook_id} \
     -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"engine": "openclaw"}'
   ```

4. 如果 OpenClaw 服务不可用，可改用本地引擎：
   ```bash
   curl -X POST http://localhost:8000/api/deep-analyze/{webhook_id} \
     -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"engine": "local"}'
   ```

---

### 5. 深度分析完成后飞书未收到通知

**症状：** 手动重拉成功，但飞书群未收到消息。

**排查步骤：**

1. 确认 `DEEP_ANALYSIS_FEISHU_WEBHOOK` 已配置：
   ```bash
   curl http://localhost:8000/api/config \
     -H "Authorization: Bearer $API_KEY" | jq '.data.DEEP_ANALYSIS_FEISHU_WEBHOOK'
   ```

2. 手动测试飞书 Webhook 连通性：
   ```bash
   curl -X POST "$DEEP_ANALYSIS_FEISHU_WEBHOOK" \
     -H "Content-Type: application/json" \
     -d '{"msg_type": "text", "content": {"text": "test"}}'
   ```

3. 检查 Worker 日志中是否有 `飞书深度分析通知` 相关日志（INFO 或 WARNING 级别）。

4. 飞书 Webhook 熔断器（Circuit Breaker）在连续 5 次失败后会打开 30 秒，期间通知被静默丢弃。等待自动恢复或重启服务。

---

### 6. 转发失败，事件未推送到目标系统

**症状：** 事件处理完成，但目标系统未收到通知。

**排查步骤：**

1. 查看失败转发队列：
   ```bash
   curl http://localhost:8000/api/failed-forwards \
     -H "Authorization: Bearer $API_KEY"
   ```

2. 检查转发规则是否正确配置（重要性匹配、目标 URL 非空）：
   ```bash
   curl http://localhost:8000/api/forward-rules \
     -H "Authorization: Bearer $API_KEY"
   ```

3. 手动触发转发：
   ```bash
   curl -X POST http://localhost:8000/api/forward/{webhook_id} \
     -H "Authorization: Bearer $API_KEY"
   ```

4. 检查 Worker 日志中是否有转发相关的 HTTP 错误。

---

### 7. 告警被标记为重复但预期不是

**症状：** 新告警被归入已有告警的重复项，`is_duplicate=true`。

**排查：**

1. 检查 `DUPLICATE_ALERT_TIME_WINDOW`（默认 24 小时）。如需缩短去重窗口：
   ```bash
   curl -X POST http://localhost:8000/api/config \
     -H "Authorization: Bearer $API_KEY" \
     -H "X-Admin-Key: $ADMIN_WRITE_KEY" \
     -H "Content-Type: application/json" \
     -d '{"key": "DUPLICATE_ALERT_TIME_WINDOW", "value": "1"}'
   ```

2. 告警 hash 由 `source + alertname + 关键字段` 生成。如果两条告警 hash 相同但内容有差异，说明关键区分字段未被包含在 hash 计算中。查看 `models/webhook.py` 中的 `generate_hash` 方法。

3. 对于已被错误标记的事件，可强制重新分析：
   ```bash
   curl -X POST http://localhost:8000/api/reanalyze/{webhook_id} \
     -H "Authorization: Bearer $API_KEY"
   ```

---

### 8. 数据库语句超时（`statement timeout`）

**症状：** 日志出现 `canceling statement due to statement timeout` 或 `asyncpg.exceptions.QueryCanceledError`。

**原因：** SQL 查询或行锁等待超过了 `DB_STATEMENT_TIMEOUT_MS`（默认 30000ms）。

**处理：**

- 如果是偶发的行锁超时，Recovery Poller 会在 5 分钟内自动重试。
- 如果频繁出现，可适当增大超时：`.env` 中调整 `DB_STATEMENT_TIMEOUT_MS=60000`，然后重启服务。
- 检查是否有长事务未提交（通过 PostgreSQL `pg_stat_activity` 视图排查）。

---

### 9. 服务内存持续增长

**症状：** 容器内存用量随时间缓慢上升。

**排查：**

1. 检查 `db_queue_pending` 指标（`/metrics`）是否持续增长，说明有事件积压未处理。

2. 检查 Redis 内存：
   ```bash
   redis-cli info memory | grep used_memory_human
   ```

3. 检查主表 `webhook_events` 行数：如果很大，说明数据归档未正常执行。手动触发归档：
   ```bash
   curl -X POST http://localhost:8000/api/maintenance/run \
     -H "Authorization: Bearer $API_KEY" \
     -H "X-Admin-Key: $ADMIN_WRITE_KEY"
   ```

4. Docker 内存问题：`docker-compose.yml` 中 API 服务默认限制 1GB，Worker 512MB。可按需调整。

---

### 10. `POST /api/config` 返回 403

**症状：** 写配置时提示无权限。

**原因：** 配置写入需要单独的 `ADMIN_WRITE_KEY`（若未设置，则复用 `API_KEY`）。

**修复：** 请求时添加 Header：
```bash
curl -X POST http://localhost:8000/api/config \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Admin-Key: $ADMIN_WRITE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key": "LOG_LEVEL", "value": "DEBUG"}'
```

---

## 🪲 开启 DEBUG 日志

排查 AI 分析内容或 Webhook 解析问题时，开启 DEBUG 级别：

```bash
# 热更新，无需重启
curl -X POST http://localhost:8000/api/config \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Admin-Key: $ADMIN_WRITE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key": "LOG_LEVEL", "value": "DEBUG"}'
```

排查完成后记得改回 `INFO`。

---

## 📞 寻求帮助

提供以下信息有助于快速定位问题：
1. 相关的 `trace_id`（从日志中获取）
2. 对应事件的 `event_id`
3. Worker 和 API 日志中的完整错误堆栈
4. `GET /api/config/sources` 的返回结果（确认配置来源）
