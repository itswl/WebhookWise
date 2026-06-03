# Docker E2E

这一层测试验证核心业务链路，而不是单个函数：

```text
HTTP /v1/webhook/prometheus
  -> migrate job applies Alembic
  -> PostgreSQL quick receive
  -> Redis / TaskIQ
  -> Worker pipeline
  -> Scheduler process starts
  -> fake OpenAI structured AI analysis
  -> Redis analysis cache and noise reduction path enabled
  -> Feishu interactive card
  -> fake Feishu HTTP server
```

运行：

```bash
tests/e2e/run_webhook_to_feishu.sh
```

脚本会启动一次性 Docker Compose 环境：

- `postgres`: 干净 PostgreSQL 15
- `migrate`: 一次性 Alembic 迁移任务
- `redis`: 真 Redis 7
- `webhook-service`: API 容器
- `worker`: TaskIQ Worker 容器
- `scheduler`: TaskIQ Scheduler 容器
- `fake-openai`: 本地 OpenAI-compatible chat completions server
- `fake-feishu`: 本地 HTTP server，记录收到的 webhook payload

通过条件：

- API `/ready` 可用；
- migrate 任务成功退出；
- scheduler 容器保持 running；
- webhook 请求返回 `200`;
- worker 从 Redis 消费并完成处理；
- fake OpenAI 收到 `/v1/chat/completions` 请求；
- fake Feishu 收到 `msg_type=interactive` 的卡片 payload。

失败时脚本会打印 `docker compose ps` 和最近容器日志，并自动执行：

```bash
docker compose -f tests/e2e/docker-compose.yml down -v --remove-orphans
```

这条测试依赖 Docker，运行时间明显长于普通 `pytest`。默认 CI 可以不跑；改动 Alembic、TaskIQ、Redis、pipeline 或转发逻辑时必须跑。
