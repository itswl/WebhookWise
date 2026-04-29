# 文档索引

本目录包含项目的所有文档，按类别组织。

## 📁 目录结构

```
docs/
├── setup/              # 部署和设置相关
├── features/           # 功能说明文档
├── troubleshooting/    # 故障排查指南
├── performance/        # 性能优化文档
└── changelogs/         # 变更日志
    └── history/        # 历史修复记录
```

## 🚀 部署和设置

### [AUTO_MIGRATION.md](setup/AUTO_MIGRATION.md)
- **内容**: 自动数据库迁移机制详解（含 Alembic 增量迁移）
- **适用**: 新项目部署、数据库初始化、schema 变更
- **关键点**:
  - 启动时自动执行迁移步骤
  - 幂等性保证
  - 故障处理方案

## ⚙️ 功能说明

### 告警去重机制

#### [DEDUPLICATION_FIX.md](features/DEDUPLICATION_FIX.md)
- **内容**: 告警去重机制修复方案
- **关键点**:
  - 三重防护机制
  - 并发竞态处理
  - 唯一约束实现

#### [DUPLICATE_TIME_WINDOW.md](features/DUPLICATE_TIME_WINDOW.md)
- **内容**: 告警去重时间窗口机制详解
- **关键点**:
  - 时间窗口工作原理
  - 配置方法
  - 实际案例分析

#### [TIME_WINDOW_BEHAVIOR_CONFIG.md](features/TIME_WINDOW_BEHAVIOR_CONFIG.md)
- **内容**: 超时间窗口告警行为独立配置
- **关键点**:
  - 重新分析配置 (`REANALYZE_AFTER_TIME_WINDOW`)
  - 转发配置 (`FORWARD_AFTER_TIME_WINDOW`)
  - 4种配置组合场景
  - 成本和性能对比

### AI Prompt 配置

#### [PROMPT_CONFIG.md](features/PROMPT_CONFIG.md)
- **内容**: AI Prompt 动态配置指南
- **关键点**:
  - 从文件加载 Prompt
  - 热重载机制
  - 预置模板说明

#### [AI_PROMPT_USAGE.md](features/AI_PROMPT_USAGE.md)
- **内容**: AI Prompt 使用详细说明
- **关键点**:
  - 变量替换
  - 自定义 Prompt
  - 最佳实践

## 🔧 故障排查

### [TROUBLESHOOTING.md](troubleshooting/TROUBLESHOOTING.md)
- **内容**: 常见问题排查指南
- **适用**: 遇到问题时首先查看

### [CONFIG_RELOAD_FIX.md](troubleshooting/CONFIG_RELOAD_FIX.md)
- **内容**: 配置热重载问题修复
- **问题**: 修改 .env 后配置不生效

### [FIX_PERMISSION_ERROR.md](troubleshooting/FIX_PERMISSION_ERROR.md)
- **内容**: 权限错误修复
- **问题**: 文件访问权限问题

### [HOW_TO_VIEW_DETAILS.md](troubleshooting/HOW_TO_VIEW_DETAILS.md)
- **内容**: 如何查看告警详情
- **适用**: Web 界面使用指南

## 📊 性能优化

### [PERFORMANCE_OPTIMIZATION.md](performance/PERFORMANCE_OPTIMIZATION.md)
- **内容**: API 性能优化方案
- **关键点**:
  - 响应速度提升 20 倍
  - 数据传输减少 95%
  - 按需加载机制

### [PERFORMANCE_RESULTS.md](performance/PERFORMANCE_RESULTS.md)
- **内容**: 性能优化效果对比
- **数据**: 优化前后的实际测试结果

## 📝 变更日志

### [MIGRATION_RESULT.md](changelogs/MIGRATION_RESULT.md)
- **内容**: 数据库迁移执行报告
- **数据**: 2026-02-28 执行的迁移统计

### [CHANGELOG_PROMPT.md](changelogs/CHANGELOG_PROMPT.md)
- **内容**: Prompt 功能更新日志

### 历史修复记录 (changelogs/history/)

这些文档记录了历史问题的修复过程，通常不需要查看，除非遇到类似问题：

- `CONFIG_SAVE_FIX.md` - 配置保存问题修复
- `CONFIG_SAVE_ISSUE.md` - 配置保存问题分析
- `CONCURRENT_DUPLICATE_FIX.md` - 并发重复问题修复
- `DEBUG_PAGINATION.md` - 分页调试记录
- `FILTER_DEBUG.md` - 过滤器调试记录
- `FILTER_PAGINATION_FIX.md` - 过滤分页修复
- `JSON_DISPLAY_IMPROVEMENTS.md` - JSON 展示改进
- `PAGINATION_FIX.md` - 分页修复

## 📖 快速导航

### 我想...

- **部署新项目** → [AUTO_MIGRATION.md](setup/AUTO_MIGRATION.md)
- **配置告警去重** → [TIME_WINDOW_BEHAVIOR_CONFIG.md](features/TIME_WINDOW_BEHAVIOR_CONFIG.md)
- **了解去重原理** → [DUPLICATE_TIME_WINDOW.md](features/DUPLICATE_TIME_WINDOW.md)
- **自定义 AI 分析** → [PROMPT_CONFIG.md](features/PROMPT_CONFIG.md)
- **排查问题** → [TROUBLESHOOTING.md](troubleshooting/TROUBLESHOOTING.md)
- **优化性能** → [PERFORMANCE_OPTIMIZATION.md](performance/PERFORMANCE_OPTIMIZATION.md)

## 📚 文档更新历史

- **2026-02-28**: 重组文档结构，创建文档索引
- **2026-02-28**: 新增超时间窗口行为配置文档
- **2026-02-28**: 新增自动迁移机制文档
- **2025-11-XX**: 初始文档创建

---

> 💡 **提示**: 如果找不到想要的内容，可以使用 grep 搜索：
> ```bash
> grep -r "关键词" docs/
> ```
