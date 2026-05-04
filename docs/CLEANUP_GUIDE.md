# 告警清理与维护指南

## 📖 概述

系统现在支持**自动维护与归档**。通过配置保留策略，系统会每日凌晨自动将过期告警移动到归档表 (`archived_webhook_events`)，以保持主表轻量。

---

## ⚙️ 自动维护配置

自动维护逻辑集成在 `MaintenanceService` 中，由 `maintenance_poller` 驱动。你可以在 `.env` 或运行时配置中调整以下参数：

### 1. 保留天数策略 (`RETENTION_POLICIES`)

按告警重要性设置不同的保留天数：
- `high`: 90天 (默认)
- `medium`: 30天 (默认)
- `low`: 7天 (默认)
- `unknown`: 3天 (默认)

### 2. 来源保留策略 (`SOURCE_RETENTION_POLICIES`)

针对特定来源设置保留天数，例如：
- `prometheus`: 30天
- `grafana`: 30天

### 3. 关键字自动清理 (`CLEANUP_KEYWORDS`)

匹配特定关键字的告警将被自动识别为低价值并清理。默认包含：
- 摘要包含 `"一般事件:"`
- 内容包含 `"测试告警"`

### 4. 执行时间 (`MAINTENANCE_HOUR`)

默认在每日凌晨 `3:00` 执行。

---

## 🗑️ 手动清理

虽然系统会自动清理，但在紧急情况下你可能仍需手动操作。

### 方式：直接使用 SQL

如果你确定要删除，可以直接执行 SQL：

```sql
-- 删除低优先级告警
DELETE FROM webhook_events WHERE importance = 'low';

-- 删除 unknown 来源的中低优先级
DELETE FROM webhook_events
WHERE source = 'unknown'
  AND importance IN ('low', 'medium');

-- 删除30天前的低优先级
DELETE FROM webhook_events
WHERE importance = 'low'
  AND timestamp < NOW() - INTERVAL '30 days';
```

---

## 📦 归档表查询

清理后的数据并未直接物理删除，而是进入了 `archived_webhook_events`。如需查询历史归档：

```sql
SELECT * FROM archived_webhook_events
WHERE alert_hash = '...'
ORDER BY timestamp DESC;
```

---

## ⚠️ 注意事项

1. **自动归档**：默认开启。如果磁盘空间紧张，建议缩短 `RETENTION_POLICIES` 中的天数。
2. **性能影响**：自动维护任务使用分布式锁，确保多实例环境下每日仅执行一次。任务采用批量处理（每次5000条），避免长时间锁表。
3. **空间回收**：大量删除后，PostgreSQL 可能不会立即释放磁盘空间。如有必要，可执行 `VACUUM ANALYZE webhook_events;`。

---

## 📝 更新日志

- **2026-05-02**：全面升级为基于策略的自动维护系统，废弃手动清理脚本。
- **2026-03-03**：初始版本
