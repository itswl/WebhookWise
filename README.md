# WebhookWise: 智能 Webhook 接收与 AI 运维分析服务

一个工业级、高性能的 Webhook 智能管家。基于 FastAPI 异步架构，具备 AI 根因分析、智能告警降噪、分布式去重、冷热数据归档以及全方位可观测性。

## 🚀 核心架构演进

- **高性能基座**: 由 Flask 同步架构全面迁移至 **FastAPI + Uvicorn**，支持毫秒级响应与高并发 Webhook 吞吐。
- **纯异步 I/O**: 全链路接入 `httpx` 与 `AsyncOpenAI`，AI 分析与消息转发完全非阻塞，彻底消除线程池耗尽隐患。
- **分布式状态管理**: 引入 **Redis** 实现分布式锁（SET NX EX）与状态共享，支持多节点/多 Worker 横向扩展。
- **AIOps 智能降噪**: 结合启发式算法（Jaccard 相似度）识别衍生告警，支持 `root_cause` (根因) 判定与抑制转发。
- **运行时策略中心化**: 动态策略从数据库 `system_configs` 热更新，并通过 Redis Pub/Sub 同步到各进程内存（`policies.*` 统一读取），消除“到底来自 .env 还是 DB”的歧义。
- **多分析引擎适配**: 完美兼容 **Hermes (HMAC 签名)** 与 **OpenClaw (Bearer Token)** 双套深度分析协议。

## ✨ 功能特性

### 核心能力
- ✅ **异步 Webhook 接收**: 采用 `BackgroundTasks`，立即返回 202 Accepted，后台处理 AI 逻辑。
- ✅ **AI 深度分析**: 自动识别事件重要性，提供根因定位、影响评估及可执行修复建议。
- ✅ **智能降噪去重**: 分布式去重机制，支持自定义 24h+ 时间窗口及窗口外自动重分析策略。
- ✅ **数据生命周期管理**: 自动冷热数据归档，每天凌晨 3 点自动将旧告警搬迁至备份表，并按批次循环搬空过期数据，避免吞吐上限导致主表膨胀。
- ✅ **告警风暴背压**: 同一 `alert_hash` 并发激增时启用 Fail-Fast + 聚合写入，避免大量协程挂起等待复用导致资源耗尽。
- ✅ **全方位可观测性**: 原生集成 **Prometheus** 指标（含降噪率、AI 成本 USD、处理耗时分布）。

### 安全加固
- 🔐 **管理接口鉴权**: 全量管理 API 受 `API_KEY` (Bearer Token) 保护。
- 🔐 **签名验证**: 支持接收端 HMAC-SHA256 签名校验，确保告警来源可信。
- 🔐 **熔断器机制**: 内置智能熔断器（Circuit Breaker），防止下游 AI 接口故障导致系统雪崩。

## 🛠️ 技术栈
- **后端**: Python 3.12 + FastAPI
- **数据库**: PostgreSQL 15+ (复合索引优化 & 自动归档)
- **缓存/锁**: Redis 7+
- **HTTP 客户端**: httpx (单例连接池)
- **监控**: Prometheus + Prometheus FastAPI Instrumentator

## 🏃 快速开始

### Docker Compose 启动 (推荐)
```bash
# 1. 配置文件
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY, DATABASE_URL, REDIS_URL, API_KEY 等

# 2. 一键启动
docker-compose up -d --build
```

### 数据库迁移
系统在 Docker 启动时会自动执行全部迁移（含 Alembic）。如需手动执行：
```bash
# 执行数据库表初始化
python3 -c "from db.session import init_db; init_db()"
# 执行旧系统迁移（索引优化、归档表创建）
python3 -m migrations.init_migrations
# Alembic 增量 schema 迁移
alembic upgrade head
```

创建新迁移：
```bash
alembic revision --autogenerate -m "描述变更内容"
```

## 📊 监控指标 (Prometheus)
服务默认在 `:8000/metrics` 暴露以下核心业务指标：
- `webhook_noise_reduced_total`: 降噪引擎节省的告警数量（按 source/relation 维度）。
- `ai_cost_usd_total`: 累计消耗的 AI 成本（美元）。
- `ai_analysis_duration_seconds`: AI 分析耗时分布（Histogram）。
- `webhook_received_total`: 接收到的 Webhook 总量。
- `webhook_storm_suppressed_total`: 告警风暴触发 Fail-Fast 抑制/聚合的总次数（按 source 维度）。

## 📖 配置说明
优先级（从低到高）：默认值 < `.env/环境变量` < `system_configs` 运行时策略（可热更新）

运行时策略与来源追踪：
- `GET /api/config`: 获取当前有效配置（管理端展示用）
- `POST /api/config`: 写入运行时策略到数据库并广播热更新
- `GET /api/config/sources`: 查看每个 key 的来源（db/env/default）与更新时间（用于排障）

| 关键变量 | 说明 |
| :--- | :--- |
| `API_KEY` | 管理接口的访问令牌。生产环境必须设置（或显式开启 `ALLOW_UNAUTHENTICATED_ADMIN=true` 仅用于本地）。 |
| `DEEP_ANALYSIS_PLATFORM` | 深度分析协议。可选 `hermes` (HMAC) 或 `openclaw` (Bearer)。 |
| `DUPLICATE_ALERT_TIME_WINDOW` | 重复告警去重窗口（单位：小时）。 |
| `LOG_LEVEL` | 日志级别。排查 AI 内容建议设为 `DEBUG`，生产环境建议 `INFO`。 |
| `PROCESSING_LOCK_FAILFAST_THRESHOLD` | 告警风暴 Fail-Fast 阈值（同一 alert_hash 在窗口内超过该数量将触发抑制/聚合）。 |
| `PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS` | 告警风暴统计窗口（秒）。 |
| `PROCESSING_LOCK_STORM_KEEP_LATEST_N` | 风暴期间每个 alert_hash 仅保留最新 N 条事件记录（其余旧记录会被自动删除）。 |

## 📁 目录结构
```text
/
├── api/                # API 路由定义 (Webhook, 深度分析, 管理端等)
├── core/               # 核心基础设施 (配置, Logger, HTTP连接池, 监控指标)
├── models/             # SQLAlchemy 数据库模型定义
├── crud/               # 数据库操作隔离层
├── db/                 # 数据库连接池与 session 管理
├── services/           # 核心业务逻辑 (AI 分析引擎, 降噪算法, 主处理管道, 轮询器)
├── adapters/           # 生态适配器 (Prometheus, 华为云等格式转换)
├── migrations/         # 旧系统数据库迁移脚本
├── alembic/            # Alembic 数据库迁移（增量 schema 变更）
├── prompts/            # AI 提示词模板目录
├── scripts/            # 运维工具脚本 (手动归档, 权限诊断等)
└── templates/          # 前端 Dashboard 静态文件
```

## 📜 许可证
MIT License
