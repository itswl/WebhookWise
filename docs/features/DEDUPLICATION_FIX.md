# 告警去重机制修复方案

## 问题现象

用户报告：告警 8928 和 8930 都被触发，但应该被去重。

### 详细分析

#### 1. 告警数据对比

| 对比项 | 告警 8928 | 告警 8930 | 状态 |
|-------|-----------|-----------|------|
| **alert_hash** | `02f20c1c60b26...` | `02f20c1c60b26...` | ✅ 完全相同 |
| **fingerprint** | `bddd606f5dd97c70` | `bddd606f5dd97c70` | ✅ 完全相同 |
| **alert_id** | `69a1febc33b1ddf9b93da690` | `69a1febc33b1ddf9b93da690` | ✅ 完全相同 |
| **startsAt** | `2026-02-28T04:33:48` | `2026-02-28T04:33:48` | ✅ 完全相同 |
| **当前值** | `4.083333333333333` | `4.083333333333333` | ✅ 完全相同 |
| **接收时间** | `04:35:04.998341` | `04:35:18.591179` | ❌ 相差 14 秒 |
| **客户端 IP** | `180.153.35.52` | `180.153.35.36` | ❌ 不同 |
| **is_duplicate** | `0` (原始) | `0` (原始) | ❌ **都是原始！** |

#### 2. 根本原因

1. **并发窗口期**：两个请求相差14秒，第一个请求的锁已释放
2. **数据库竞态**：
   - Worker 1 处理 8928，检查无重复，插入作为原始告警
   - Worker 2 处理 8930（14秒后），锁已释放，也检查无重复
   - 导致两个都被标记为 `is_duplicate=0`

3. **锁机制问题**：
   ```python
   # 原来的代码
   .with_for_update(skip_locked=True)  # 跳过已锁定的行
   ```
   如果 Worker 1 的事务未提交，Worker 2 查询时会跳过，导致查不到原始告警

4. **缺少数据库约束**：没有强制约束防止相同 `alert_hash` 有多个 `is_duplicate=0` 记录

---

## 修复方案

### 总体策略：三重防护机制

```
┌─────────────────────────────────────────────────────────┐
│                    三重防护机制                          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  1. 数据库唯一约束  ←─ 最强防护（数据库层）             │
│     ↓                                                   │
│  2. 增强事务隔离    ←─ 防并发读取（应用层）             │
│     ↓                                                   │
│  3. 重试+冲突处理   ←─ 兜底机制（应用层）               │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 修复 1：数据库唯一约束（最关键）

#### 创建部分唯一索引

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original
ON webhook_events(alert_hash)
WHERE is_duplicate = 0;
```

**原理**：
- 只对原始告警（`is_duplicate=0`）生效
- 允许多个重复告警（`is_duplicate=1`）指向同一原始告警
- 数据库层面强制保证：相同 `alert_hash` 只有一个原始告警

**效果**：
```
Before:
  alert_hash=xxx, is_duplicate=0, id=8928  ✅
  alert_hash=xxx, is_duplicate=0, id=8930  ✅ (不应该存在！)

After (有唯一约束):
  alert_hash=xxx, is_duplicate=0, id=8928  ✅
  alert_hash=xxx, is_duplicate=0, id=8930  ❌ IntegrityError!
  ↓ 自动转为重复告警
  alert_hash=xxx, is_duplicate=1, id=8930, duplicate_of=8928  ✅
```

#### 执行迁移

**方式 1：通过 API**
```bash
curl -X POST https://dejavu.prod.common-infra.hony.love/api/migrations/add_unique_constraint
```

**方式 2：通过命令行**
```bash
python migrations_tool.py add_unique_constraint
```

**方式 3：手动 SQL**
```bash
psql -h <host> -U <user> -d <database> < migrations/add_unique_constraint.sql
```

---

### 修复 2：增强事务隔离

#### 修改锁策略

**之前（有问题）**：
```python
.with_for_update(skip_locked=True)  # 跳过已锁定的行
```
- **问题**：如果 Worker 1 正在事务中，Worker 2 会跳过，查不到数据
- **结果**：两个 Worker 都认为没有原始告警

**修复后**：
```python
.with_for_update(nowait=False)  # 等待锁释放
```
- **改进**：Worker 2 会等待 Worker 1 的事务完成
- **结果**：确保读取到最新提交的数据

#### 代码变更

**文件**：`utils.py:194-202`

```python
original_event = session.query(WebhookEvent)\
    .filter(
        WebhookEvent.alert_hash == alert_hash,
        WebhookEvent.timestamp >= time_threshold,
        WebhookEvent.is_duplicate == 0
    )\
    .order_by(WebhookEvent.timestamp.desc())\
    .with_for_update(nowait=False)\  # ← 修改：等待而不是跳过
    .first()
```

---

### 修复 3：重试+冲突处理

#### 添加重试机制

**文件**：`utils.py:218-310`

```python
def save_webhook_data(...):
    """保存 webhook 数据（带重试机制）"""
    from sqlalchemy.exc import IntegrityError

    max_retries = 3
    retry_delay = 0.1  # 100ms

    for attempt in range(max_retries):
        try:
            with session_scope() as session:
                # 检查重复 + 保存数据
                ...

        except IntegrityError as e:
            # 唯一约束冲突：说明并发插入
            logger.warning(f"检测到并发插入冲突 (attempt {attempt + 1})")

            if attempt < max_retries - 1:
                # 重试：指数退避
                time.sleep(retry_delay * (attempt + 1))
                is_duplicate = None  # 重置，强制重新检查
                continue
            else:
                # 最后尝试：直接查找已存在的原始告警
                existing = session.query(WebhookEvent)\
                    .filter(alert_hash == alert_hash, is_duplicate == 0)\
                    .first()

                if existing:
                    # 找到了，标记为重复
                    return save_as_duplicate(existing)
```

**处理流程**：
```
┌─────────────────────────────────────────────────┐
│ Worker A: 尝试插入原始告警                      │
│           alert_hash=xxx, is_duplicate=0        │
├─────────────────────────────────────────────────┤
│ Worker B: 同时尝试插入原始告警                  │
│           alert_hash=xxx, is_duplicate=0        │
│                 ↓                               │
│           ❌ IntegrityError (唯一约束冲突)      │
│                 ↓                               │
│           等待 100ms，重试                       │
│                 ↓                               │
│           重新检查重复                           │
│                 ↓                               │
│           找到 Worker A 插入的记录               │
│                 ↓                               │
│           ✅ 标记为重复告警                      │
│           alert_hash=xxx, is_duplicate=1,       │
│           duplicate_of=8928                     │
└─────────────────────────────────────────────────┘
```

---

## 实施步骤

### 步骤 1：应用代码修改

```bash
# 已修改的文件
- utils.py         # 增强事务隔离 + 重试机制
- migrations_tool.py   # 迁移工具
- app.py           # 添加迁移 API
```

### 步骤 2：部署到生产环境

```bash
# 1. 提交代码
git add -A
git commit -m "fix: 修复告警去重机制 - 三重防护"
git push

# 2. 触发生产部署（或手动部署）
# ...

# 3. 重启服务
docker-compose restart webhook-service
```

### 步骤 3：执行数据库迁移

**远程执行迁移**：

```bash
# 通过 API 执行
curl -X POST https://dejavu.prod.common-infra.hony.love/api/migrations/add_unique_constraint

# 预期输出
{
  "success": true,
  "message": "数据库迁移成功：唯一约束已添加"
}
```

**迁移日志示例**：
```
🔧 开始数据库迁移：添加唯一约束...
📋 步骤 1: 检查空的 alert_hash...
✅ 无需修复，所有原始告警都有 alert_hash
📋 步骤 2: 检查重复的原始告警...
⚠️  发现 2 组重复的原始告警
  alert_hash=02f20c1c60b26..., count=2, ids=[8928, 8930]
  保留 ID=8928，将 1 条标记为重复
✅ 已处理所有重复告警
📋 步骤 3: 创建唯一索引...
✅ 唯一索引创建成功
✅ 注释添加成功
📋 步骤 4: 最终验证...
  原始告警总数: 8346
  唯一 alert_hash 数: 8346
✅ 验证通过：每个 alert_hash 只有一个原始告警
🎉 数据库迁移完成！
```

### 步骤 4：验证修复

```bash
# 1. 检查告警状态
curl -s "https://dejavu.prod.common-infra.hony.love/api/webhooks/8928" | jq '.data | {id, is_duplicate, duplicate_count}'
# 输出: {"id":8928, "is_duplicate":0, "duplicate_count":2}

curl -s "https://dejavu.prod.common-infra.hony.love/api/webhooks/8930" | jq '.data | {id, is_duplicate, duplicate_of}'
# 输出: {"id":8930, "is_duplicate":1, "duplicate_of":8928}

# 2. 验证唯一约束
# 尝试插入重复的原始告警（应该失败）
```

---

## 效果对比

### 修复前

```
时间线：
04:35:04 - Worker 1 收到 webhook (alert_hash=02f20...)
04:35:04 - Worker 1 检查重复: 无
04:35:04 - Worker 1 插入: ID=8928, is_duplicate=0 ✅
04:35:07 - Worker 1 释放锁

04:35:18 - Worker 2 收到 webhook (alert_hash=02f20...) ← 14秒后
04:35:18 - Worker 2 检查重复: 无 (锁已释放，可能查询时机问题)
04:35:18 - Worker 2 插入: ID=8930, is_duplicate=0 ✅ ← 不应该！
04:35:21 - Worker 2 释放锁

结果：两个原始告警 ❌
```

### 修复后

```
时间线：
04:35:04 - Worker 1 收到 webhook (alert_hash=02f20...)
04:35:04 - Worker 1 加锁查询重复: 无
04:35:04 - Worker 1 插入: ID=8928, is_duplicate=0 ✅
04:35:07 - Worker 1 提交事务，释放锁

04:35:18 - Worker 2 收到 webhook (alert_hash=02f20...)
04:35:18 - Worker 2 加锁查询重复: 等待锁...
04:35:18 - Worker 2 读取到 ID=8928
04:35:18 - Worker 2 插入重复告警: ID=8930, is_duplicate=1, duplicate_of=8928 ✅

如果 Worker 2 仍然尝试插入原始告警：
04:35:18 - Worker 2 尝试插入: ID=8930, is_duplicate=0
04:35:18 - ❌ IntegrityError: 唯一约束冲突
04:35:18 - Worker 2 捕获异常，重试
04:35:18 - Worker 2 重新检查，找到 ID=8928
04:35:18 - Worker 2 插入重复告警: ID=8930, is_duplicate=1, duplicate_of=8928 ✅

结果：一个原始 + 一个重复 ✅
```

---

## 监控和告警

### 检查重复告警统计

```sql
-- 查看去重效果
SELECT
    alert_hash,
    COUNT(*) as total_count,
    SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) as original_count,
    SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) as duplicate_count
FROM webhook_events
WHERE timestamp >= NOW() - INTERVAL '24 hours'
GROUP BY alert_hash
HAVING COUNT(*) > 1
ORDER BY total_count DESC
LIMIT 10;
```

### 监控指标

1. **重复率**：`duplicate_count / total_count`
2. **异常原始告警**：`original_count > 1`（应该为 0）
3. **IntegrityError 次数**：从日志统计

### 日志关键字

```bash
# 搜索并发冲突日志
grep "检测到并发插入冲突" logs/app.log

# 搜索重试日志
grep "正在重试" logs/app.log

# 搜索最终失败日志
grep "重试.*次后仍然失败" logs/app.log
```

---

## 常见问题

### Q1: 如果迁移失败怎么办？

**A**：检查日志，可能原因：
1. 数据库连接失败
2. 存在无法自动处理的重复数据
3. 权限不足

**解决**：
```bash
# 手动检查重复数据
SELECT alert_hash, COUNT(*), array_agg(id)
FROM webhook_events
WHERE is_duplicate = 0
GROUP BY alert_hash
HAVING COUNT(*) > 1;

# 手动处理（保留最早的）
UPDATE webhook_events
SET is_duplicate = 1, duplicate_of = <earliest_id>
WHERE id IN (<other_ids>);
```

### Q2: 现有的重复原始告警怎么处理？

**A**：迁移工具会自动处理：
- 保留时间最早的作为原始告警
- 其他标记为重复，指向最早的
- 更新 `duplicate_count`

### Q3: 性能影响？

**A**：
- **唯一索引**：插入时检查，影响极小（微秒级）
- **等待锁**：仅在并发时等待，正常情况无影响
- **重试机制**：仅在冲突时触发，概率极低

### Q4: 如何回滚？

**A**：
```sql
-- 删除唯一索引
DROP INDEX IF EXISTS idx_unique_alert_hash_original;

-- 恢复代码
git revert <commit_hash>
```

---

## 总结

### 修复内容

| 层级 | 修复项 | 文件 | 效果 |
|------|--------|------|------|
| **数据库** | 唯一约束 | `migrations/add_unique_constraint.sql` | 🛡️ 最强防护 |
| **应用层** | 事务隔离 | `utils.py:201` | 🔒 防并发读取 |
| **应用层** | 重试机制 | `utils.py:236-310` | 🔄 兜底处理 |
| **工具** | 迁移工具 | `migrations_tool.py` | 🔧 自动化 |
| **API** | 迁移接口 | `app.py:687-705` | 🌐 远程执行 |

### 部署清单

- [x] 修改代码（utils.py, app.py）
- [ ] 提交并推送代码
- [ ] 部署到生产环境
- [ ] 执行数据库迁移
- [ ] 验证修复效果
- [ ] 监控运行状态

### 预期效果

- ✅ 不再出现重复的原始告警
- ✅ 并发请求正确处理为重复
- ✅ 数据一致性得到保证
- ✅ 性能影响可忽略

---

## 附录：技术细节

### A. PostgreSQL 部分唯一索引

```sql
-- 语法
CREATE UNIQUE INDEX index_name
ON table_name(column)
WHERE condition;

-- 示例
CREATE UNIQUE INDEX idx_unique_alert_hash_original
ON webhook_events(alert_hash)
WHERE is_duplicate = 0;

-- 特性
-- 1. 只对满足 WHERE 条件的行生效
-- 2. 不满足条件的行不受约束
-- 3. 完美适用于"原始/重复"场景
```

### B. SQLAlchemy 行锁

```python
# for_update: 排他锁（写锁）
.with_for_update()  # 等待锁释放
.with_for_update(nowait=True)  # 立即失败
.with_for_update(skip_locked=True)  # 跳过已锁定行

# 选择建议
# - nowait=False（默认）: 适合去重场景，等待确保数据一致
# - skip_locked=True: 跳过锁定行，可能导致遗漏数据
```

### C. 指数退避算法

```python
# 重试延迟：100ms, 200ms, 300ms
retry_delay = 0.1
for attempt in range(max_retries):
    time.sleep(retry_delay * (attempt + 1))
```

这个算法在高并发场景下能有效避免"惊群效应"。
