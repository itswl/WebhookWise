# 告警清理指南

## 📖 概述

当数据库中积累了大量低价值告警时，使用此脚本批量清理。

**警告**：删除操作不可逆，请务必先备份数据库！

---

## 🔍 第一步：识别需要清理的告警

### 查看告警统计

```sql
-- 按重要性统计
SELECT importance, COUNT(*)
FROM webhook_events
GROUP BY importance
ORDER BY COUNT(*) DESC;

-- 按来源统计
SELECT source, COUNT(*)
FROM webhook_events
GROUP BY source
ORDER BY COUNT(*) DESC;

-- 按重要性和来源统计
SELECT importance, source, COUNT(*)
FROM webhook_events
GROUP BY importance, source
ORDER BY COUNT(*) DESC;
```

### 常见的"垃圾告警"特征

- `importance='low'`：低优先级
- `importance='medium'` + `source='unknown'`：未知来源的中等告警
- 超过30天的旧告警

---

## 💾 第二步：备份数据库

**强烈建议**在清理前备份！

```bash
# 完整备份
pg_dump -h <host> -U <user> -d <database> > backup_$(date +%Y%m%d_%H%M%S).sql

# 或者只备份 webhook_events 表
pg_dump -h <host> -U <user> -d <database> -t webhook_events > webhooks_backup.sql
```

---

## 🗑️ 第三步：执行清理

### 方式1：使用脚本（推荐）

```bash
# 设置数据库连接
export DATABASE_URL="postgresql://user:pass@host:5432/database"

# 执行清理（先预览，再确认）
python3 scripts/cleanup_alerts.py
```

### 脚本内置清理规则

编辑 `scripts/cleanup_alerts.py` 中的 `filters` 变量：

```python
# 示例1：删除所有低优先级告警
filters = {'importance': 'low'}

# 示例2：删除低和中等优先级
filters = {'importance': ['low', 'medium']}

# 示例3：删除来源为 unknown 的告警
filters = {'source': 'unknown'}

# 示例4：删除30天前的低优先级告警
filters = {
    'importance': 'low',
    'keep_recent_days': 30
}

# 示例5：删除 unknown 来源的低/中等告警（默认）
filters = {
    'source': 'unknown',
    'importance': ['low', 'medium']
}
```

### 方式2：直接 SQL

如果你确定要删除，可以直接执行SQL：

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

-- 查看影响行数（执行前先预览）
SELECT COUNT(*)
FROM webhook_events
WHERE source = 'unknown'
  AND importance IN ('low', 'medium');
```

---

## 📊 示例：清理流程

```bash
# 1. 设置数据库连接
export DATABASE_URL="postgresql://webhook_user:xxx@host:5432/webhooks"

# 2. 预览（修改脚本中的 filters 后）
python3 scripts/cleanup_alerts.py

# 输出示例：
# ================================================================================
# 将要删除的记录统计：
# ================================================================================
# 重要性        来源             数量        最早                  最新
# --------------------------------------------------------------------------------
# medium       unknown         1234       2026-01-01 10:00     2026-03-03 09:00
# low          unknown          567       2026-01-15 14:30     2026-03-02 18:20
# --------------------------------------------------------------------------------
# 总计：1801 条记录
# ================================================================================

# 3. 确认删除
# 确认删除以上 1801 条记录？(输入 'yes' 确认，其他取消): yes

# 4. 删除完成
# ✅ 删除完成！共删除 1801 条记录
```

---

## 🔧 高级用法

### 只删除重复告警，保留原始告警

```sql
-- 删除低优先级的重复记录
DELETE FROM webhook_events
WHERE importance = 'low'
  AND is_duplicate = 1;
```

### 按时间范围删除

```sql
-- 删除2026年1月之前的低优先级
DELETE FROM webhook_events
WHERE importance = 'low'
  AND timestamp < '2026-01-01';
```

### 删除特定告警链

```sql
-- 先找到原始告警 ID
SELECT id, alert_hash, duplicate_count
FROM webhook_events
WHERE is_duplicate = 0
  AND importance = 'low'
ORDER BY duplicate_count DESC;

-- 删除某个告警链的所有记录（包括原始和重复）
DELETE FROM webhook_events
WHERE alert_hash = '<某个hash>';
```

---

## ⚠️ 注意事项

1. **不可逆**：删除后无法恢复，务必先备份
2. **级联影响**：删除原始告警会导致重复告警的 `duplicate_of` 指向无效ID
3. **性能影响**：大量删除可能锁表，建议在低峰期执行
4. **空间回收**：删除后执行 `VACUUM FULL webhook_events;` 回收磁盘空间

---

## 🔄 定期清理建议

### 自动清理策略（crontab）

```bash
# 每周日凌晨3点清理30天前的低优先级告警
0 3 * * 0 /path/to/cleanup_old_low_priority.sh
```

创建脚本 `cleanup_old_low_priority.sh`：

```bash
#!/bin/bash
export DATABASE_URL="postgresql://user:pass@host:5432/database"

psql "$DATABASE_URL" <<EOF
DELETE FROM webhook_events
WHERE importance = 'low'
  AND timestamp < NOW() - INTERVAL '30 days';

VACUUM ANALYZE webhook_events;
EOF
```

---

## 📞 恢复方法

如果误删，可以从备份恢复：

```bash
# 恢复整个数据库
psql -h <host> -U <user> -d <database> < backup.sql

# 或只恢复 webhook_events 表
psql -h <host> -U <user> -d <database> < webhooks_backup.sql
```

---

## ❓ 常见问题

### Q1: 删除会影响统计吗？

**是的**。删除后历史数据统计会改变。如果需要保留统计，考虑归档而非删除。

### Q2: 可以恢复吗？

**不可以**。除非你有备份。所以务必先备份！

### Q3: 删除后空间没有释放？

**正常**。PostgreSQL 需要手动 VACUUM：

```sql
VACUUM FULL webhook_events;
```

### Q4: 批量删除太慢？

**分批删除**：

```sql
-- 每次删除1000条
DELETE FROM webhook_events
WHERE id IN (
    SELECT id
    FROM webhook_events
    WHERE importance = 'low'
    LIMIT 1000
);
```

重复执行直到 `DELETE 0` 为止。

---

## 📝 更新日志

- **2026-03-03**：初始版本
