# WebhookWise: 智能 Webhook 接收与 AI 运维分析服务

一个工业级、高性能的 Webhook 智能管家。基于 FastAPI 异步架构，具备 AI 根因分析、智能告警降噪、分布式去重、冷热数据归档以及全方位可观测性。

## ✨ 核心特性

| 能力 | 说明 |
|:---|:---|
| **异步 Webhook 接收** | TaskIQ 异步任务队列，立即返回 202，后台并发处理 |
| **AI 深度分析** | LLM 自动识别重要性，输出根因定位、影响评估与修复建议 |
| **OpenClaw 深度分析** | 接入 OpenClaw 深度分析引擎，WebSocket 异步拉取结果 |
| **智能降噪去重** | Jaccard 相似度识别衍生告警，分布式去重 + 24h 时间窗口（可配置） |
| **告警风暴背压** | 同一 `alert_hash` 并发激增时 Fail-Fast + 聚合写入，防资源耗尽 |
| **转发规则引擎** | 多规则按优先级匹配，支持 Webhook / 飞书卡片 / OpenClaw 三种目标类型 |
| **转发失败重试** | 失败转发写入补偿队列，指数退避自动重试（最多 3 次） |
| **冷热数据归档** | 每日凌晨自动按重要性分级归档（high 90d / medium 30d / low 7d） |
| **运行时策略热更新** | 配置写入 DB `system_configs`，Redis Pub/Sub 广播到所有进程 |
| **全方位可观测性** | Prometheus 原生指标 + OpenTelemetry 链路追踪（可选） |

## 🏗️ 架构概览

```
                       ┌─────────────┐
  Webhook 来源  ───────▶│  FastAPI     │──── 202 Accepted
  (Prometheus,          │  /webhook    │
   Grafana, etc.)       └──────┬──────┘
                               │ TaskIQ (Redis MQ)
                               ▼
                       ┌─────────────────────────────────┐
                       │         Pipeline                │
                       │  normalize → dedup → AI analyze │
                       │  → noise reduce → forward       │
                       └────────────┬────────────────────┘
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
               PostgreSQL        Redis          外部系统
               (持久化)         (锁/缓存)     (飞书/Webhook/
                                              OpenClaw)
```

**进程模型：**
- `api` 进程：FastAPI HTTP 服务（Gunicorn 4 UvicornWorker）
- `worker` 进程：TaskIQ Worker + 定时任务调度器

## 🛠️ 技术栈

| 组件 | 技术选型 |
|:---|:---|
| Web 框架 | Python 3.12 + FastAPI + uvloop |
| 任务队列 | TaskIQ + Redis ListQueueBroker |
| 数据库 | PostgreSQL 15+ (asyncpg + SQLAlchemy 2.0) |
| 缓存/锁 | Redis 7+ |
| AI 调用 | AsyncOpenAI + Instructor (结构化输出) |
| HTTP 客户端 | httpx 单例连接池 |
| 监控 | Prometheus + prometheus-fastapi-instrumentator |
| 链路追踪 | OpenTelemetry (可选，OTLP 导出) |
| 数据迁移 | Alembic |
| 容器化 | Docker + Docker Compose (4 服务) |

## 🚀 快速开始

### Docker Compose（推荐）

```bash
# 1. 复制并填写配置
cp .env.example .env
# 至少需要填写: OPENAI_API_KEY, API_KEY

# 2. 一键启动（API + Worker + Redis + PostgreSQL）
docker-compose up -d --build

# 3. 验证
curl http://localhost:8000/health
```

数据库 Schema 迁移在容器启动时通过 `entrypoint.sh` 自动执行（`alembic upgrade head`）。

### 本地开发

```bash
pip install -r requirements.txt

# 启动 API
uvicorn main:app --reload --port 8000

# 启动 Worker（另一个终端）
python worker.py
```

### 发送测试 Webhook

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"alertname": "TestAlert", "severity": "critical", "host": "prod-01"}'
```

## 📁 目录结构

```
/
├── api/               # API 路由 (webhook, admin, analysis, forwarding)
├── core/              # 基础设施 (config, auth, redis, logger, metrics, broker)
├── services/          # 业务逻辑 (pipeline, ai_analyzer, forward, tasks, ...)
├── adapters/          # 生态适配器 (多格式归一化, 分析引擎插件)
│   └── plugins/       # 分析引擎插件 (local, openclaw, feishu_card)
├── models/            # SQLAlchemy ORM 模型
├── schemas/           # Pydantic 请求/响应 Schema
├── db/                # 数据库连接池与 session 管理
├── alembic/           # Alembic 增量 Schema 迁移
├── prompts/           # AI 提示词模板
├── templates/         # 前端 Dashboard HTML + 静态文件
├── scripts/           # 运维工具脚本
├── tests/             # pytest 测试套件
├── docs/              # 功能文档
├── main.py            # FastAPI 入口
├── worker.py          # TaskIQ Worker 入口
├── Dockerfile         # 多阶段构建 (jemalloc + 非 root)
├── docker-compose.yml # 4 服务编排
└── .env.example       # 配置模板
```

## 📡 API 端点速览

> 所有 `/api/*` 端点需要 `Authorization: Bearer <API_KEY>` Header。
> `/webhook` 端点默认无需鉴权（可通过 `REQUIRE_WEBHOOK_AUTH=true` 开启）。

### Webhook 接收
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `POST` | `/webhook` | 接收通用 Webhook（自动检测来源） |
| `POST` | `/webhook/{source}` | 接收指定来源的 Webhook |
| `GET` | `/health` | 健康检查 |
| `GET` | `/` 或 `/dashboard` | Web 管理界面 |

### 事件管理
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `GET` | `/api/webhooks` | 分页列举事件（支持 source/importance/status 过滤） |
| `GET` | `/api/webhooks/{id}` | 获取单条事件详情（含原始 payload） |

### 分析
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `POST` | `/api/deep-analyze/{webhook_id}` | 触发深度分析（local 或 openclaw 引擎） |
| `GET` | `/api/deep-analyses` | 分页列举深度分析记录 |
| `GET` | `/api/deep-analyses/{webhook_id}` | 获取某事件的所有分析记录 |
| `POST` | `/api/deep-analyses/{id}/retry` | 手动重拉 OpenClaw 分析结果 |
| `POST` | `/api/deep-analyses/{id}/forward` | 手动转发分析结果到指定 URL |
| `POST` | `/api/reanalyze/{webhook_id}` | 强制重新 AI 分析 |
| `POST` | `/api/forward/{webhook_id}` | 手动触发转发 |
| `GET` | `/api/ai-usage` | AI 用量 & 成本统计 |

### 转发规则
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `GET` | `/api/forward-rules` | 列举所有转发规则 |
| `POST` | `/api/forward-rules` | 创建转发规则 |
| `PUT` | `/api/forward-rules/{id}` | 更新转发规则 |
| `DELETE` | `/api/forward-rules/{id}` | 删除转发规则 |
| `GET` | `/api/failed-forwards` | 失败转发补偿队列 |
| `DELETE` | `/api/failed-forwards/{id}` | 删除补偿记录 |

### 运维管理
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `GET` | `/api/config` | 查看当前有效配置 |
| `GET` | `/api/config/sources` | 查看每个配置 key 的来源（db/env/default）及更新时间 |
| `POST` | `/api/config` | 热更新运行时配置（需要 `ADMIN_WRITE_KEY`） |
| `GET` | `/api/prompt` | 查看当前 AI Prompt |
| `POST` | `/api/prompt/reload` | 热重载 Prompt 文件 |
| `GET` | `/api/dead-letters` | 死信队列列表 |
| `POST` | `/api/dead-letters/{id}/replay` | 重放单条死信事件 |
| `GET` | `/api/stuck-events` | 列举僵尸事件 |
| `POST` | `/api/stuck-events/{id}/requeue` | 重新入队单条僵尸事件 |

### 监控
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `GET` | `/metrics` | Prometheus 格式指标 |

## ⚙️ 关键配置说明

优先级（低 → 高）：内置默认值 < `.env` / 环境变量 < `system_configs` 数据库表（热更新）

> 标记 `[runtime]` 的配置项支持通过 `POST /api/config` 或 Web 界面在线修改，无需重启。

### 基础设施（启动时读取，修改后需重启）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `API_KEY` | — | 管理 API 鉴权 Token（生产必须设置） |
| `ADMIN_WRITE_KEY` | — | 写操作单独 Key（为空则回退到 API_KEY） |
| `DATABASE_URL` | `postgresql://...` | PostgreSQL 连接串 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串 |
| `RUN_MODE` | `all` | `api` / `worker` / `all` |
| `ENABLE_POLLERS` | `true` | 是否启用后台定时任务 |
| `MAX_CONCURRENT_WEBHOOK_TASKS` | `30` | Webhook 后台处理最大并发数 |
| `DB_POOL_SIZE` | `20` | 数据库连接池大小 |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | SQL 语句超时（毫秒） |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_FILE` | — | 日志文件路径（为空则仅控制台输出） |
| `RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS` | `300` | 僵尸事件判定阈值（秒） |
| `WEBHOOK_SECRET` | — | HMAC-SHA256 签名校验密钥 |

### AI 分析（`[runtime]` 可热更新）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `ENABLE_AI_ANALYSIS` | `true` | 开启 AI 分析 |
| `OPENAI_API_KEY` | — | LLM 提供商 API Key |
| `OPENAI_API_URL` | OpenRouter | LLM API 基地址 |
| `OPENAI_MODEL` | `anthropic/claude-sonnet-4` | 使用的模型 |
| `AI_SYSTEM_PROMPT` | 内置 | 系统级 Prompt |
| `AI_USER_PROMPT_FILE` | `prompts/webhook_analysis_detailed.txt` | 用户 Prompt 模板文件路径 |
| `CACHE_ENABLED` | `true` | 分析结果 Redis 缓存 |
| `ANALYSIS_CACHE_TTL` | `21600` | 缓存有效期（秒，默认 6h） |

### 去重与降噪（`[runtime]` 可热更新）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `DUPLICATE_ALERT_TIME_WINDOW` | `24` | 去重时间窗口（小时） |
| `REANALYZE_AFTER_TIME_WINDOW` | `false` | 超窗后是否重新 AI 分析 |
| `FORWARD_AFTER_TIME_WINDOW` | `false` | 超窗后是否重新转发 |
| `ENABLE_ALERT_NOISE_REDUCTION` | `true` | 开启 Jaccard 降噪 |
| `NOISE_REDUCTION_WINDOW_MINUTES` | `5` | 相似度比对时间窗口（分钟） |
| `ROOT_CAUSE_MIN_CONFIDENCE` | `0.65` | 根因判定置信度阈值 |
| `SUPPRESS_DERIVED_ALERT_FORWARD` | `true` | 抑制衍生告警的转发 |

### 告警风暴背压

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `PROCESSING_LOCK_FAILFAST_THRESHOLD` | `20` | 同一 hash 在窗口内超过该数量触发 Fail-Fast |
| `PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS` | `10` | Fail-Fast 统计窗口（秒） |
| `PROCESSING_LOCK_STORM_KEEP_LATEST_N` | `200` | 风暴期每个 hash 仅保留最新 N 条记录 |

### 转发与重试（`[runtime]` 可热更新）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `ENABLE_FORWARD` | `true` | 开启自动转发 |
| `FORWARD_URL` | — | 默认转发目标 URL |
| `ENABLE_FORWARD_RETRY` | `true` | 失败转发自动重试 |
| `FORWARD_RETRY_MAX_RETRIES` | `3` | 最大重试次数 |
| `FORWARD_RETRY_INITIAL_DELAY` | `60` | 初始重试延迟（秒） |

### OpenClaw 深度分析

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `OPENCLAW_ENABLED` | `false` | 启用 OpenClaw 引擎 |
| `OPENCLAW_GATEWAY_URL` | — | OpenClaw 网关地址 |
| `OPENCLAW_GATEWAY_TOKEN` | — | 认证 Token |
| `OPENCLAW_TIMEOUT_SECONDS` | `300` | 分析超时（秒） |
| `DEEP_ANALYSIS_FEISHU_WEBHOOK` | — | 深度分析完成后推送的飞书 Webhook URL |

## 📊 Prometheus 指标

服务在 `/metrics` 暴露以下核心指标：

| 指标名 | 类型 | 说明 |
|:---|:---|:---|
| `webhook_received_total` | Counter | 接收 Webhook 总量（按 source/status） |
| `webhook_processing_status_total` | Counter | 处理状态转换次数 |
| `webhook_processing_duration_seconds` | Histogram | Pipeline 处理耗时分布 |
| `webhook_noise_reduced_total` | Counter | 降噪告警数（按 source/relation_type/suppressed） |
| `webhook_storm_suppressed_total` | Counter | 告警风暴触发抑制/聚合次数 |
| `webhook_running_tasks` | Gauge | 当前活跃处理任务数 |
| `webhook_recovery_polled_total` | Counter | Recovery 扫描处理的僵尸事件数 |
| `ai_tokens_total` | Counter | Token 消耗量（按 model/token_type） |
| `ai_cost_usd_total` | Counter | 累计 AI 成本（美元） |
| `ai_analysis_duration_seconds` | Histogram | AI 分析耗时（按 source/engine） |
| `db_queue_pending` | Gauge | 待处理事件数 |
| `forward_retry_pending` | Gauge | 转发重试队列长度 |

## 🔒 安全说明

- 所有管理 API 需 `Authorization: Bearer <API_KEY>` Header
- 写操作（配置变更、重放事件）可配置独立 `ADMIN_WRITE_KEY`
- 支持接收端 HMAC-SHA256 签名校验（`WEBHOOK_SECRET`）
- 内置熔断器（Circuit Breaker）防止下游故障雪崩
- Docker 容器以非 root 用户（appuser:1000）运行

## 📖 文档

详细功能文档见 [docs/](docs/README.md)：

- [部署说明](docs/setup/AUTO_MIGRATION.md) — 数据库迁移 & 自动化部署
- [去重时间窗口](docs/features/DUPLICATE_TIME_WINDOW.md) — 去重机制详解
- [降噪根因分析](docs/features/ALERT_NOISE_REDUCTION_ROOT_CAUSE.md) — Jaccard 算法说明
- [告警风暴背压](docs/features/ALERT_STORM_BACKPRESSURE.md) — Fail-Fast 策略
- [AI Prompt 配置](docs/features/PROMPT_CONFIG.md) — 自定义 LLM Prompt
- [配置来源追踪](docs/features/CONFIG_PROVIDER.md) — 配置优先级 & 热更新
- [故障排查](docs/troubleshooting/TROUBLESHOOTING.md) — 常见问题 & 解决方案

## 📜 许可证

MIT License
