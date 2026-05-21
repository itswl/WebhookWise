# WebhookWise: 智能 Webhook 接收与 AI 运维分析服务

一个面向生产运维场景的 Webhook 智能管家。基于 FastAPI 异步架构，具备 AI 根因分析、智能告警降噪、缓存/数据库去重、冷热数据归档以及可观测性能力。

## 📚 项目入口

- API 文档：运行服务后打开 `http://localhost:8000/docs`，离线导出见 [docs/api/README.md](docs/api/README.md)
- 贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 架构边界：[docs/architecture/boundaries.md](docs/architecture/boundaries.md)
- Kubernetes 清单：[deploy/k8s/README.md](deploy/k8s/README.md)

## ✨ 核心特性

| 能力 | 说明 |
|:---|:---|
| **异步 Webhook 接收** | API 只负责接收并投递 TaskIQ/Redis Stream，立即返回 202 |
| **AI 深度分析** | LLM 自动识别重要性，输出根因定位、影响评估与修复建议 |
| **OpenClaw 深度分析** | 接入 OpenClaw 深度分析引擎，通过 TaskIQ 动态延迟任务拉取结果 |
| **智能降噪去重** | Adapter 范式化告警 identity，混合相似度/可选 embedding 识别衍生告警，缓存/数据库复用 + 24h 时间窗口（可配置） |
| **告警风暴背压** | 同一 `alert_hash` Redis 分布式 single-flight + 短窗口 Fail-Fast，防资源耗尽 |
| **转发规则引擎** | 多规则按优先级匹配，支持 Webhook / 飞书卡片 / OpenClaw 三种目标类型 |
| **事务性转发 Outbox** | 处理结果与转发意图同事务落库，Worker 异步消费，避免 DB 状态与 HTTP 副作用脱节 |
| **转发失败重试** | Outbox 投递失败后指数退避重试；超过最大投递年龄会标记 `expired`，避免旧告警误发 |
| **冷热数据归档** | 每日凌晨自动按重要性分级归档（high 90d / medium 30d / low 7d） |
| **运行时策略热更新** | 配置写入 DB `system_configs`，Redis Pub/Sub 广播到所有进程 |
| **全方位可观测性** | OpenTelemetry SDK + OTLP 统一输出 metrics/traces/logs |

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
               (持久化)        (队列/缓存)    (飞书/Webhook/
                                              OpenClaw)
```

**进程模型：**
- `migrate` 一次性任务：等待 PostgreSQL 就绪并执行 `alembic upgrade head`
- `api` 进程：FastAPI HTTP 服务（Gunicorn 4 UvicornWorker）
- `worker` 进程：TaskIQ Worker，消费异步业务任务和定时任务
- `scheduler` 进程：TaskIQ Scheduler，只负责周期性投递任务，不执行业务逻辑
- `RUN_MODE=all`：通过 `supervisord` 在同一个容器内同时拉起 `api` / `worker` / `scheduler`，适合单机小部署或演示；需要横向扩容 Worker 时仍推荐独立进程/独立容器。

**异步职责边界：**
- TaskIQ：基于 Redis Stream 的异步任务投递与 Worker 消费
- Scheduler：周期性投递 recovery、metrics、数据维护等任务
- TaskIQ 动态调度：按事件投递 Webhook 处理重试、Forward Outbox 重试和 OpenClaw 结果拉取
- Forward Outbox：Webhook 处理事务内只写入待发送意图，由 Worker 执行真实 HTTP/OpenClaw 转发
- PostgreSQL：Webhook 事实存储、失败转发/死信/重试状态等可审计状态
- Redis：TaskIQ 队列、短窗口风暴计数、缓存、运行时配置广播

**部署边界：**
- 小规模部署使用 `RUN_MODE=all` 把 API、Worker、Scheduler 放进同一个应用容器。
- 默认生产拓扑仍是独立 API、Worker、Scheduler 容器，便于横向扩容 Worker。
- 两种拓扑共享同一套 Redis/TaskIQ/Outbox/分布式锁语义；部署形态不改变业务执行流。

## 🛠️ 技术栈

| 组件 | 技术选型 |
|:---|:---|
| Web 框架 | Python 3.12 + FastAPI + uvloop |
| 任务队列 | TaskIQ + Redis Stream Broker |
| 数据库 | PostgreSQL 15+ (asyncpg + SQLAlchemy 2.0) |
| 队列/缓存 | Redis 7+ |
| AI 调用 | AsyncOpenAI + Instructor (结构化输出) |
| HTTP 客户端 | httpx 单例连接池 |
| 可观测性 | OpenTelemetry SDK + OTLP -> OpenTelemetry Collector |
| 展示/告警 | Grafana + Prometheus-compatible metrics backend / Tempo / Loki |
| 数据迁移 | Alembic |
| 容器化 | Docker + Docker Compose；默认多容器拓扑，可选 supervisor all-in-one 应用容器 |

## Kubernetes 部署

`deploy/k8s/` 提供基础清单：API、Worker、Scheduler、迁移 Job、Redis、PostgreSQL、ConfigMap、Secret 示例与 ServiceAccount。先根据 [deploy/k8s/README.md](deploy/k8s/README.md) 填写真实 Secret，再执行 `kubectl apply -k deploy/k8s`。应用镜像必须使用 release tag 或 digest，避免使用 `latest`。

## 🚀 快速开始

### Docker Compose（推荐）

```bash
# 1. 复制并填写配置
cp .env.example .env
# 至少需要替换: API_KEY, ADMIN_WRITE_KEY, WEBHOOK_SECRET；需要 AI 分析时再填写 OPENAI_API_KEY

# 2. 一键启动（Migrate + API + Worker + Scheduler + Redis + PostgreSQL）
docker-compose up -d --build

# 3. 验证
curl http://localhost:8000/health
```

数据库 Schema 迁移由 Compose 中的一次性 `migrate` 服务执行（`alembic upgrade head`）。API、Worker 和 Scheduler 只在迁移成功后启动，`entrypoint.sh` 只负责按 `RUN_MODE` 分发进程。

### Supervisor all-in-one 模式（小团队/演示）

默认 Compose 是独立 API、Worker、Scheduler 容器。如果想在单机小部署中减少应用容器数量，可以用 supervisor 在一个应用容器里同时拉起三类进程：

```bash
docker compose -f docker-compose.yml -f docker-compose.supervisor.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.supervisor.yml exec webhook-service supervisorctl -c /app/supervisord.conf status
```

`RUN_MODE=all` 时容器 PID 1 是 `supervisord`，它会管理三个子进程：

- `api`：Gunicorn + UvicornWorker，监听 `:8000`
- `worker`：`taskiq worker services.operations.taskiq_wiring:broker`
- `scheduler`：`taskiq scheduler services.operations.taskiq_wiring:scheduler`

健康检查会同时校验 supervisor 中三个 program 均为 `RUNNING`，并探测 API `/ready`。

All-in-one 只改变进程拓扑，不改变业务语义：Webhook 仍进入 Redis Stream，Worker 仍通过 TaskIQ 消费，Outbox、重试、缓存、分布式锁仍使用同一套实现。

默认三容器模式下，API、Worker、Scheduler 都只通过 OTLP 把 metrics/traces/logs 发给 Grafana Alloy；应用不暴露 `/metrics`，也不直接绑定 Prometheus client。需要本地观测栈时，可叠加 `docker-compose.observability.yml` 启动 Alloy + Prometheus + Tempo + Loki + Grafana + Pyroscope，并附带 Beyla 自动观测、Faro 前端遥测和 k6 压测入口。

### 本地可观测性学习栈

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
```

启动后常用入口：

| 组件 | 地址 | 用途 |
|:---|:---|:---|
| Grafana | http://localhost:3000 | 统一看 metrics/traces/logs/profiles |
| Alloy | http://localhost:12345 | 查看采集管线与组件状态 |
| Faro receiver | http://localhost:12347/collect | Dashboard 前端遥测入口 |
| Prometheus | http://localhost:9090 | 应用、Beyla、k6 指标 |
| Loki | http://localhost:3100 | OTLP 日志、文件日志、Faro 日志 |
| Tempo | http://localhost:3200 | 应用 OTel trace + Beyla/Faro trace |
| Pyroscope | http://localhost:4040 | Python 持续 profiling |

运行一次 k6 学习压测：

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile load run --rm k6
```

Beyla 通过 eBPF 观测 `webhook-service` 容器，需要 Linux eBPF 能力和 privileged 容器；在 Docker Desktop 上如果内核能力不足，其他 OTel/Faro/Loki/k6 链路仍可继续使用。

### 本地开发

```bash
pip install -r requirements.lock

# 启动 API
uvicorn main:app --reload --port 8000

# 启动 Worker（另一个终端）
taskiq worker core.taskiq_broker:broker services.operations.tasks
```

### 发送测试 Webhook

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"alertname": "TestAlert", "severity": "critical", "host": "prod-01"}'
```

## ✅ 测试与验证

项目目前分三层验证：

| 层级 | 命令 | 覆盖内容 |
|:---|:---|:---|
| 静态检查 | `ruff check .` / `mypy .` | 代码风格、类型边界 |
| 单元 + 进程内集成 | `pytest` | 纯函数、核心服务、FastAPI 路由到 pipeline 的进程内链路 |
| Docker E2E | `tests/e2e/run_webhook_to_feishu.sh` | 真 PostgreSQL、真 Redis、API 容器、TaskIQ Worker、TaskIQ Scheduler、fake Feishu HTTP server |

常规本地/CI 快速验证：

```bash
ruff check .
mypy .
pytest
```

核心链路 E2E 验证：

```bash
tests/e2e/run_webhook_to_feishu.sh
```

这条 E2E 会自动：

1. 用 `tests/e2e/docker-compose.yml` 启动一次性环境；
2. 从干净 PostgreSQL 执行 `alembic upgrade head`；
3. 等待一次性 `migrate` 任务完成后，启动 API、Redis、TaskIQ Worker、TaskIQ Scheduler 和 fake Feishu；
4. 向 `/webhook/prometheus` 发送真实 HTTP 请求；
5. 等待 Worker 从 Redis 消费任务；
6. 断言 fake Feishu 收到飞书 `interactive` card。

脚本退出时会自动 `docker compose down -v --remove-orphans` 清理容器和数据卷。失败时会打印相关容器最近日志，优先看 `webhook-service`、`worker` 和 `scheduler`。

> Docker E2E 比普通 pytest 慢，默认不放进快速 CI。发版前、改动迁移/队列/转发链路时应手动跑一遍。

## 📁 目录结构

```
/
├── api/               # API 路由 (webhook, admin, analysis, forwarding)
├── core/              # 基础设施 (config, auth, redis, logger, metrics, broker)
├── services/          # 业务逻辑，按能力分包
│   ├── webhooks/      # 接收、持久化、查询、主处理 Pipeline
│   ├── forwarding/    # 转发规则、外部投递、失败转发补偿
│   ├── analysis/      # AI 分析、降噪、OpenClaw 集成
│   ├── operations/    # TaskIQ 任务、调度入口、恢复/指标/维护任务
│   └── runtime_config/# 运行时配置热更新服务
├── adapters/          # 生态适配器 (多格式归一化)
│   └── plugins/       # 生态适配器插件 (feishu_card)
├── models/            # SQLAlchemy ORM 模型
├── schemas/           # Pydantic 请求/响应 Schema
├── db/                # 数据库连接池与 session 管理
├── alembic/           # Alembic 增量 Schema 迁移
├── prompts/           # AI 提示词模板
├── templates/         # 前端 Dashboard HTML + 静态文件
├── scripts/           # 运维工具脚本
├── tests/             # pytest 测试套件 + Docker E2E
├── docs/              # 功能文档
├── main.py            # FastAPI 入口
├── worker.py          # TaskIQ Worker 入口
├── Dockerfile         # 多阶段构建 (jemalloc + 非 root)
├── docker-compose.yml # 6 服务编排（含 migrate job）
└── .env.example       # 配置模板
```

详细边界约束见 [docs/architecture/boundaries.md](docs/architecture/boundaries.md)。简要规则：

- `api/` 只做 HTTP 绑定，业务编排放在 `services/*`。
- `adapters/` 负责外部载荷归一化或插件注册，不承载业务决策。
- `core/` 只放跨切面运行时胶水；长出业务分支时迁回对应领域包。
- 默认部署是 API、Worker、Scheduler 分进程/分容器；`docker-compose.supervisor.yml` 仅是单机/演示 override。

## 📡 API 端点速览

> 所有 `/api/*` 端点需要 `Authorization: Bearer <API_KEY>` Header；会修改状态、触发 AI/OpenClaw 或发起外部转发的写接口需要 `Authorization: Bearer <ADMIN_WRITE_KEY>`（未配置时回退到 `API_KEY`）。
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
| `POST` | `/api/deep-analyze/{webhook_id}` | 触发 OpenClaw 深度分析（写权限） |
| `GET` | `/api/deep-analyses` | 分页列举深度分析记录 |
| `GET` | `/api/deep-analyses/{webhook_id}` | 获取某事件的所有分析记录 |
| `POST` | `/api/deep-analyses/{id}/retry` | 手动重拉 OpenClaw 分析结果（写权限） |
| `POST` | `/api/deep-analyses/{id}/forward` | 手动转发分析结果到指定 URL（写权限） |
| `POST` | `/api/reanalyze/{webhook_id}` | 强制重新 AI 分析（写权限） |
| `POST` | `/api/forward/{webhook_id}` | 手动触发转发（写权限） |
| `GET` | `/api/ai-usage` | AI 用量 & 成本统计 |

### 转发规则
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `GET` | `/api/forward-rules` | 列举所有转发规则 |
| `POST` | `/api/forward-rules` | 创建转发规则（写权限） |
| `PUT` | `/api/forward-rules/{id}` | 更新转发规则（写权限） |
| `DELETE` | `/api/forward-rules/{id}` | 删除转发规则（写权限） |
| `GET` | `/api/failed-forwards` | 失败转发审计记录 |
| `POST` | `/api/failed-forwards/{id}/retry` | 重置失败转发重试（写权限） |
| `DELETE` | `/api/failed-forwards/{id}` | 删除补偿记录（写权限） |

### 运维管理
| 方法 | 路径 | 说明 |
|:---|:---|:---|
| `GET` | `/api/config` | 查看当前有效配置 |
| `GET` | `/api/config/sources` | 查看每个配置 key 的来源（db/env/default）及更新时间 |
| `POST` | `/api/config` | 热更新运行时配置（写权限） |
| `GET` | `/api/prompt?kind=user\|deep_analysis` | 查看当前 AI Prompt 或深度分析 Prompt |
| `POST` | `/api/prompt/reload?kind=user\|deep_analysis` | 热重载 Prompt 文件（写权限） |
| `GET` | `/api/admin/dead-letters` | 死信队列列表 |
| `POST` | `/api/admin/dead-letters/{id}/replay` | 重放单条死信事件（写权限） |
| `GET` | `/api/admin/stuck-events` | 列举僵尸事件 |
| `POST` | `/api/admin/stuck-events/{id}/requeue` | 重新入队单条僵尸事件（写权限） |

### 可观测性

应用不提供 HTTP 指标端点。Metrics、traces、logs 统一通过 OTLP 发送到 OpenTelemetry Collector，Collector 再转发到 Prometheus-compatible backend、Tempo 和 Loki。

## ⚙️ 关键配置说明

优先级（低 → 高）：内置默认值 < `.env` / 环境变量 < `system_configs` 数据库表（仅启用运行时配置后参与热更新）

> 标记 `[runtime]` 的业务策略项支持通过 `POST /api/config` 或 Web 界面在线修改，无需重启。连接串、模型基地址、Token/API Key 默认要求通过环境变量或 ConfigMap 修改并滚动重启，避免多进程配置短暂不一致。

### 基础设施（启动时读取，修改后需重启）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `API_KEY` | — | 管理 API 鉴权 Token（生产必须设置） |
| `ADMIN_WRITE_KEY` | — | 写操作单独 Key（为空则回退到 API_KEY） |
| `REQUIRE_WEBHOOK_AUTH` | `true` | 生产环境默认要求 Webhook 签名或 Token 鉴权 |
| `ALLOW_UNAUTHENTICATED_WEBHOOK` | `false` | 显式允许生产环境公开接收 Webhook（不推荐） |
| `WEBHOOK_RATE_LIMIT_PER_MINUTE` | `600` | 按客户端 IP 限流；设为 `0` 表示关闭 |
| `DATABASE_URL` | `postgresql://...` | PostgreSQL 连接串 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串 |
| `RUN_MODE` | `api` | `api` / `worker` / `scheduler` / `all`；`all` 为 supervisor all-in-one 拓扑 |
| `ENABLE_RUNTIME_CONFIG` | `false`（生产） | 启用 DB/Redis 运行时业务策略配置 |
| `ALLOW_RUNTIME_CONNECTION_CONFIG` | `false` | 允许连接/密钥类配置热更新；生产不建议开启 |
| `API_WORKERS` | `4` | `RUN_MODE=all` 时 API Gunicorn worker 数 |
| `DB_POOL_SIZE` | `5` | 单进程数据库连接池大小 |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | SQL 语句超时（毫秒） |
| `LOG_LEVEL` | `INFO` | 项目业务日志级别（`webhook_service`、`config`、`db`、`models` 等） |
| `THIRD_PARTY_LOG_LEVEL` | `WARNING` | 第三方/框架日志级别（TaskIQ、httpx、uvicorn、gunicorn 等） |
| `LOG_FILE` | — | 日志文件路径（为空则仅控制台输出） |
| `RECOVERY_SCAN_INTERVAL_SECONDS` | `300` | recovery-only DB 兜底扫描间隔；正常路径走 Redis/TaskIQ |
| `MAX_CONCURRENT_WEBHOOK_TASKS` | `30` | 所有 Worker 全局 Webhook 处理并发上限（Redis 分布式令牌） |
| `WEBHOOK_TASK_SLOT_LEASE_SECONDS` | `1800` | 全局并发令牌租约秒数，长任务会自动续期 |
| `RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS` | `300` | 僵尸事件判定阈值（秒） |
| `WEBHOOK_SECRET` | — | HMAC-SHA256 签名校验密钥 |

### AI 分析

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `ENABLE_AI_ANALYSIS` | `true` | `[runtime]` 开启 AI 分析 |
| `OPENAI_API_KEY` | — | LLM 提供商 API Key；默认需重启生效 |
| `OPENAI_API_URL` | OpenRouter | LLM API 基地址；默认需重启生效 |
| `OPENAI_MODEL` | `anthropic/claude-sonnet-4` | `[runtime]` 使用的模型 |
| `AI_SYSTEM_PROMPT` | 内置 | `[runtime]` 系统级 Prompt |
| `AI_USER_PROMPT_FILE` | `prompts/webhook_analysis_detailed.txt` | 用户 Prompt 模板文件路径 |
| `AI_USER_PROMPT` | — | `[runtime]` 用户 Prompt 内联覆盖，优先级高于文件 |
| `DEEP_ANALYSIS_PROMPT_FILE` | `prompts/deep_analysis.txt` | OpenClaw 深度分析 Prompt 模板文件路径 |
| `DEEP_ANALYSIS_PROMPT` | — | `[runtime]` 深度分析 Prompt 内联覆盖，优先级高于文件 |
| `CACHE_ENABLED` | `true` | 分析结果 Redis 缓存 |
| `ANALYSIS_CACHE_TTL` | `21600` | 缓存有效期（秒，默认 6h） |

### 去重与降噪（`[runtime]` 可热更新）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `DUPLICATE_ALERT_TIME_WINDOW` | `24` | 去重时间窗口（小时） |
| `REANALYZE_AFTER_TIME_WINDOW` | `false` | 超窗后是否重新 AI 分析 |
| `FORWARD_AFTER_TIME_WINDOW` | `false` | 超窗后是否重新转发 |
| `ENABLE_ALERT_NOISE_REDUCTION` | `true` | 开启混合相似度降噪 |
| `NOISE_REDUCTION_WINDOW_MINUTES` | `5` | 相似度比对时间窗口（分钟） |
| `ROOT_CAUSE_MIN_CONFIDENCE` | `0.65` | 根因判定置信度阈值 |
| `NOISE_RELATED_MIN_CONFIDENCE` | `0.35` | 近邻告警纳入关联集合的最低置信度 |
| `NOISE_*_WEIGHT` | 见 `core/config.py` | source/resource/semantic/severity/time 评分权重 |
| `SUPPRESS_DERIVED_ALERT_FORWARD` | `true` | 抑制衍生告警的转发 |

## 📦 依赖管理

`requirements.txt` / `requirements-dev.txt` 是人工维护的直接依赖清单；`requirements.lock` / `requirements-dev.lock` 是安装入口和 CI/Docker 的准绳。锁文件由 uv 生成，但项目当前不是 `[project]` 风格的 uv 工程，因此不维护 `uv.lock`。

更新依赖时使用：

```bash
uv pip compile requirements.txt -o requirements.lock --python-version 3.12
uv pip compile requirements-dev.txt -c requirements.lock -o requirements-dev.lock --python-version 3.12
```

### 转发与重试（`[runtime]` 可热更新）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `ENABLE_FORWARD` | `true` | 开启自动转发 |
| `FORWARD_URL` | — | 默认转发目标 URL |
| `ENABLE_FORWARD_RETRY` | `true` | 失败转发自动重试 |
| `FORWARD_RETRY_MAX_RETRIES` | `3` | 最大重试次数 |
| `FORWARD_RETRY_INITIAL_DELAY` | `60` | 初始重试延迟（秒） |
| `FORWARD_MAX_DELIVERY_AGE_SECONDS` | `1800` | Outbox 最大投递年龄；超龄标记 `expired` 不再发送，`0` 表示关闭 |
| `WEBHOOK_RETRY_MAX_RETRIES` | `5` | Webhook 主处理最大重试次数 |
| `PROCESSING_LOCK_DISTRIBUTED_ENABLED` | `true` | 同一 `alert_hash` 跨 Worker 串行处理 |
| `PROCESSING_LOCK_TTL_SECONDS` | `180` | 分布式处理锁 TTL，会自动续期 |
| `PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS` | `15` | 等待同类告警处理锁的最长秒数 |
| `PROCESSING_LOCK_FAILFAST_THRESHOLD` | `20` | 短窗口内同类告警超过阈值后直接背压 |

### OpenClaw 深度分析

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `OPENCLAW_ENABLED` | `false` | `[runtime]` 启用 OpenClaw 引擎 |
| `OPENCLAW_GATEWAY_URL` | — | OpenClaw 网关地址；默认需重启生效 |
| `OPENCLAW_GATEWAY_TOKEN` | — | 认证 Token；默认需重启生效 |
| `OPENCLAW_HOOKS_TOKEN` | — | Hook 认证 Token；默认需重启生效 |
| `OPENCLAW_HTTP_API_URL` | `http://127.0.0.1:8085` | OpenClaw HTTP 查询地址；默认需重启生效 |
| `OPENCLAW_TIMEOUT_SECONDS` | `900` | `[runtime]` 分析超时（秒） |
| `OPENCLAW_POLL_INITIAL_DELAY_SECONDS` | `10` | `[runtime]` OpenClaw 首次结果拉取延迟 |
| `OPENCLAW_POLL_MAX_DELAY_SECONDS` | `120` | `[runtime]` OpenClaw 轮询指数退避最大延迟 |
| `OPENCLAW_POLL_BACKOFF_MULTIPLIER` | `2.0` | `[runtime]` OpenClaw 轮询退避倍率 |
| `OPENCLAW_POLL_TIMEOUT` | `180` | `[runtime]` 单次轮询请求超时 |
| `DEEP_ANALYSIS_FEISHU_WEBHOOK` | — | 深度分析完成后推送的飞书 Webhook URL |

## 📊 OpenTelemetry 指标

应用侧通过 OTel Meter 产生以下核心指标，Collector 可将它们转成 Prometheus-compatible 后端可查询的时间序列：

| 指标名 | 类型 | 说明 |
|:---|:---|:---|
| `webhook.received` | Counter | 接收 Webhook 总量（按 source/status） |
| `webhook.processed` | Counter | Pipeline 状态流转计数 |
| `webhook.processing.duration` | Histogram | Pipeline 处理耗时分布 |
| `webhook.suppressed` | Counter | 降噪/抑制结果计数 |
| `webhook.storm.suppressed` | Counter | 告警风暴触发抑制次数 |
| `webhook.running_tasks` | Gauge | 当前活跃的 Webhook 后台处理任务数 |
| `webhook.recovery.polled` | Counter | Recovery 扫描处理的僵尸事件数 |
| `ai.tokens` | Counter | Token 消耗量（按 model/token_type） |
| `ai.cost` | Counter | 累计 AI 成本 |
| `ai.request.duration` | Histogram | AI 分析耗时（按 source/engine） |
| `scheduler.task.runs` | Counter | 定时任务执行计数 |
| `db.pool.connections.*` | Gauge | DB 连接池容量与借出连接数 |
| `queue.depth` / `queue.pending` / `queue.lag` | Gauge | Redis Stream 保留长度、已投递未 ack 数、consumer lag；判断消费积压优先看 pending/lag |

## 🔒 安全说明

- 所有管理 API 需 `Authorization: Bearer <API_KEY>` Header
- 写操作（配置变更、重放事件）可配置独立 `ADMIN_WRITE_KEY`
- 支持接收端 HMAC-SHA256 签名校验（`WEBHOOK_SECRET`）
- 内置熔断器（Circuit Breaker）防止下游故障雪崩
- Docker 容器以非 root 用户（appuser:1000）运行

## 📖 文档

详细功能文档见 [docs/](docs/README.md)：

- [故障排查](docs/troubleshooting/TROUBLESHOOTING.md) — 常见问题 & 解决方案
- [查看详情](docs/troubleshooting/HOW_TO_VIEW_DETAILS.md) — Webhook 详情查看说明

## 📜 许可证

MIT License
