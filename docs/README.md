# 文档索引

本目录只保留“上线可用/运维必需”的文档；历史修复过程与一次性分析结论不在此长期维护（以 Git 记录为准）。

## 📁 目录结构

```
docs/
├── setup/              # 部署和设置
├── features/           # 功能说明
├── troubleshooting/    # 故障排查
├── performance/        # 性能优化
└── changelogs/         # 变更日志（仅保留长期有效的变更说明）
```

---

## 🚀 部署和设置

### [AUTO_MIGRATION.md](setup/AUTO_MIGRATION.md)
Alembic 自动数据库迁移机制详解：启动时自动执行、幂等性保证、手动迁移命令。

---

## ⚙️ 功能说明

### 功能文档（按需阅读）

- [DUPLICATE_TIME_WINDOW.md](features/DUPLICATE_TIME_WINDOW.md) — 告警去重时间窗口原理与配置
- [TIME_WINDOW_BEHAVIOR_CONFIG.md](features/TIME_WINDOW_BEHAVIOR_CONFIG.md) — 超窗后行为配置组合
- [DEDUPLICATION_FIX.md](features/DEDUPLICATION_FIX.md) — 去重修复要点（并发/约束）
- [ALERT_NOISE_REDUCTION_ROOT_CAUSE.md](features/ALERT_NOISE_REDUCTION_ROOT_CAUSE.md) — 降噪与根因判定算法说明
- [ALERT_STORM_BACKPRESSURE.md](features/ALERT_STORM_BACKPRESSURE.md) — 告警风暴背压策略
- [PERIODIC_REMINDER.md](features/PERIODIC_REMINDER.md) — 周期提醒机制与参数
- [PROMPT_CONFIG.md](features/PROMPT_CONFIG.md) — Prompt 动态配置与热重载
- [AI_PROMPT_USAGE.md](features/AI_PROMPT_USAGE.md) — Prompt 使用建议与变量替换
- [OPEN_ECOSYSTEM_INTEGRATION.md](features/OPEN_ECOSYSTEM_INTEGRATION.md) — 多来源 Webhook 归一化与适配

---

## 🔧 故障排查

### [TROUBLESHOOTING.md](troubleshooting/TROUBLESHOOTING.md)
常见问题排查指南（优先查阅）。

### [HOW_TO_VIEW_DETAILS.md](troubleshooting/HOW_TO_VIEW_DETAILS.md)
Web 界面使用指南：如何查看事件详情、深度分析结果、转发状态。

---

## 📊 性能优化

### [PERFORMANCE_OPTIMIZATION.md](performance/PERFORMANCE_OPTIMIZATION.md)
API 响应速度提升 20x、数据传输减少 95% 的优化方案（按需加载、游标分页）。

### [POSTGRES_PARTITIONING.md](performance/POSTGRES_PARTITIONING.md)
时序流水表的 PostgreSQL 原生分区方案（适用于归档表场景）。

---

## 📝 变更日志

- `changelogs/MIGRATION_RESULT.md` — 数据库迁移执行报告
- `changelogs/CHANGELOG_PROMPT.md` — Prompt 功能更新日志

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

- 以 Git 历史为准（避免文档时间戳与实现不一致）。
