# 文档索引

本目录包含 WebhookWise 所有功能文档，按类别组织。

## 📁 目录结构

```
docs/
├── setup/              # 部署和设置
├── features/           # 功能说明
├── troubleshooting/    # 故障排查
├── performance/        # 性能优化
└── changelogs/         # 变更日志
    └── history/        # 历史修复记录
```

---

## 🚀 部署和设置

### [AUTO_MIGRATION.md](setup/AUTO_MIGRATION.md)
Alembic 自动数据库迁移机制详解：启动时自动执行、幂等性保证、手动迁移命令。

---

## ⚙️ 功能说明

### 去重机制

#### [DUPLICATE_TIME_WINDOW.md](features/DUPLICATE_TIME_WINDOW.md)
告警去重时间窗口工作原理、配置方法、实际案例分析。

#### [TIME_WINDOW_BEHAVIOR_CONFIG.md](features/TIME_WINDOW_BEHAVIOR_CONFIG.md)
超出时间窗口后的行为配置（4 种组合场景）：
- `REANALYZE_AFTER_TIME_WINDOW` — 超窗后是否重新 AI 分析
- `FORWARD_AFTER_TIME_WINDOW` — 超窗后是否重新转发

#### [DEDUPLICATION_FIX.md](features/DEDUPLICATION_FIX.md)
去重机制三重防护实现：并发竞态处理、唯一约束。

### 降噪与根因分析

#### [ALERT_NOISE_REDUCTION_ROOT_CAUSE.md](features/ALERT_NOISE_REDUCTION_ROOT_CAUSE.md)
Jaccard 相似度算法说明：根因判定逻辑、置信度计算、衍生告警抑制策略。

### AI Prompt 配置

#### [PROMPT_CONFIG.md](features/PROMPT_CONFIG.md)
AI Prompt 动态配置：从文件加载、热重载机制、预置模板说明。

#### [AI_PROMPT_USAGE.md](features/AI_PROMPT_USAGE.md)
Prompt 变量替换规则、自定义最佳实践。

### 配置系统

#### [CONFIG_PROVIDER.md](features/CONFIG_PROVIDER.md)
静态配置（`.env`）与运行时策略（`system_configs` DB）的边界拆分，配置来源追踪接口（`/api/config/sources`）。

### 告警风暴

#### [ALERT_STORM_BACKPRESSURE.md](features/ALERT_STORM_BACKPRESSURE.md)
同一 `alert_hash` 并发激增时的 Fail-Fast + 聚合写入策略，防止协程大量挂起耗尽资源。

### 周期提醒

#### [PERIODIC_REMINDER.md](features/PERIODIC_REMINDER.md)
超时间窗口告警周期提醒机制（`ENABLE_PERIODIC_REMINDER` / `REMINDER_INTERVAL_HOURS`）。

### 生态集成

#### [OPEN_ECOSYSTEM_INTEGRATION.md](features/OPEN_ECOSYSTEM_INTEGRATION.md)
多格式 Webhook 适配：Prometheus、Grafana、Datadog、华为云、GitHub 等来源的自动归一化。

---

## 🔧 故障排查

### [TROUBLESHOOTING.md](troubleshooting/TROUBLESHOOTING.md)
常见问题排查指南（优先查阅）。

### [HOW_TO_VIEW_DETAILS.md](troubleshooting/HOW_TO_VIEW_DETAILS.md)
Web 界面使用指南：如何查看事件详情、深度分析结果、转发状态。

### [CONFIG_RELOAD_FIX.md](troubleshooting/CONFIG_RELOAD_FIX.md)
修改 `.env` 后配置不生效的排查与修复。

### [FIX_PERMISSION_ERROR.md](troubleshooting/FIX_PERMISSION_ERROR.md)
容器文件访问权限问题修复。

---

## 📊 性能优化

### [PERFORMANCE_OPTIMIZATION.md](performance/PERFORMANCE_OPTIMIZATION.md)
API 响应速度提升 20x、数据传输减少 95% 的优化方案（按需加载、游标分页）。

### [PERFORMANCE_RESULTS.md](performance/PERFORMANCE_RESULTS.md)
优化前后实际测试数据对比。

### [POSTGRES_PARTITIONING.md](performance/POSTGRES_PARTITIONING.md)
时序流水表的 PostgreSQL 原生分区方案（适用于归档表场景）。

---

## 📝 变更日志

- `changelogs/MIGRATION_RESULT.md` — 数据库迁移执行报告
- `changelogs/CHANGELOG_PROMPT.md` — Prompt 功能更新日志
- `changelogs/history/` — 历史问题修复记录（仅遇到类似问题时查阅）

---

## 📖 快速导航

| 我想… | 文档 |
|:---|:---|
| 部署新项目 | [AUTO_MIGRATION.md](setup/AUTO_MIGRATION.md) |
| 配置告警去重窗口 | [TIME_WINDOW_BEHAVIOR_CONFIG.md](features/TIME_WINDOW_BEHAVIOR_CONFIG.md) |
| 了解去重原理 | [DUPLICATE_TIME_WINDOW.md](features/DUPLICATE_TIME_WINDOW.md) |
| 了解降噪根因算法 | [ALERT_NOISE_REDUCTION_ROOT_CAUSE.md](features/ALERT_NOISE_REDUCTION_ROOT_CAUSE.md) |
| 自定义 AI 分析 Prompt | [PROMPT_CONFIG.md](features/PROMPT_CONFIG.md) |
| 追踪配置来源 | [CONFIG_PROVIDER.md](features/CONFIG_PROVIDER.md) |
| 应对告警风暴 | [ALERT_STORM_BACKPRESSURE.md](features/ALERT_STORM_BACKPRESSURE.md) |
| 排查运行问题 | [TROUBLESHOOTING.md](troubleshooting/TROUBLESHOOTING.md) |
| 优化性能 | [PERFORMANCE_OPTIMIZATION.md](performance/PERFORMANCE_OPTIMIZATION.md) |

---

## 📚 文档更新历史

- **2026-05-05**: 全量更新文档，反映重构后架构（75% 代码精简、模块整合）
- **2026-02-28**: 重组文档结构，新增超时间窗口行为配置文档
- **2025-11-XX**: 初始文档创建
