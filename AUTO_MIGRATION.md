# 自动数据库迁移机制

## 概述

项目已配置**自动数据库迁移**，新项目部署时会自动处理数据库初始化和结构更新，无需手动执行迁移命令。

## 工作流程

### 启动顺序

容器启动时会自动执行以下步骤（通过 `entrypoint.sh`）：

```
┌──────────────────────────────────────────────────────────┐
│ 1. 等待数据库就绪                                         │
│    ↓ 最多重试30次，每次等待2秒                            │
│    ↓ 使用 test_db_connection() 检测连接                  │
├──────────────────────────────────────────────────────────┤
│ 2. 初始化数据库表                                         │
│    ↓ Base.metadata.create_all(engine)                   │
│    ↓ 创建 webhook_events 和 processing_locks 表          │
│    ↓ 如果表已存在则跳过                                   │
├──────────────────────────────────────────────────────────┤
│ 3. 执行数据库迁移                                         │
│    ↓ migrate_db.py                                       │
│    ↓ 添加 alert_hash, is_duplicate 等字段               │
│    ↓ 创建索引和分布式锁表                                │
│    ↓ 如果字段已存在则跳过                                │
├──────────────────────────────────────────────────────────┤
│ 4. 添加唯一约束                                           │
│    ↓ init_migrations.py（静默模式）                      │
│    ↓ 检查 idx_unique_alert_hash_original 是否存在        │
│    ↓ 如果不存在：                                        │
│    │   - 修复已有的重复数据                              │
│    │   - 创建唯一索引                                    │
│    ↓ 如果已存在：跳过                                    │
├──────────────────────────────────────────────────────────┤
│ 5. 启动应用服务                                           │
│    ↓ gunicorn --bind 0.0.0.0:8000 ...                   │
└──────────────────────────────────────────────────────────┘
```

---

## 涉及的文件

### 1. entrypoint.sh

**位置**: `/app/entrypoint.sh`

**作用**: 容器启动入口点，编排所有初始化步骤

**关键逻辑**:
```bash
#!/bin/bash
# 1. 等待数据库
# 2. 初始化表结构
python3 -c "from models import init_db; init_db()"
# 3. 执行字段迁移
python3 migrate_db.py
# 4. 添加唯一约束
python3 init_migrations.py
# 5. 启动服务
exec gunicorn ...
```

### 2. models.py

**函数**: `init_db()`

**作用**: 创建数据库表结构

**实现**:
```python
def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)  # 幂等操作，已存在的表不会重复创建
```

**创建的表**:
- `webhook_events` - 事件主表
- `processing_locks` - 分布式锁表

### 3. migrate_db.py

**作用**: 添加告警去重相关字段

**迁移内容**:
- 添加 `alert_hash` 字段（VARCHAR(64)）
- 添加 `is_duplicate` 字段（INTEGER）
- 添加 `duplicate_of` 字段（INTEGER）
- 添加 `duplicate_count` 字段（INTEGER）
- 创建索引 `idx_alert_hash`
- 创建 `processing_locks` 表

**幂等性**: 所有操作都检查字段是否已存在，避免重复执行

### 4. init_migrations.py

**作用**: 添加唯一约束防止重复告警（静默模式）

**迁移内容**:
1. 检查唯一索引 `idx_unique_alert_hash_original` 是否存在
2. 如果不存在：
   - 修复已有的重复原始告警数据
   - 创建部分唯一索引：
     ```sql
     CREATE UNIQUE INDEX idx_unique_alert_hash_original
     ON webhook_events(alert_hash)
     WHERE is_duplicate = 0;
     ```

**静默模式**: 仅在首次创建索引时输出信息，后续启动静默跳过

### 5. Dockerfile

**关键配置**:
```dockerfile
# 复制启动脚本
COPY entrypoint.sh .
COPY init_migrations.py .

# 设置执行权限
RUN chmod +x entrypoint.sh

# 配置入口点
ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", ...]
```

---

## 新项目部署流程

### Docker Compose 部署

```bash
# 1. 克隆代码
git clone <repo-url>
cd webhooks

# 2. 配置环境变量
cp .env.example .env
vim .env  # 修改数据库连接等配置

# 3. 启动服务（自动执行所有迁移）
docker-compose up -d --build

# 4. 查看启动日志
docker-compose logs -f webhook-service
```

**预期日志输出**:
```
======================================
Webhook 服务启动中...
======================================
[1/4] 等待数据库就绪...
✅ 数据库连接成功
[2/4] 初始化数据库表...
数据库表初始化完成
✅ 数据库表检查完成
[3/4] 执行数据库迁移...
跳过迁移(字段已存在): 添加 alert_hash 字段
...
✅ 数据库迁移完成
[4/4] 检查唯一约束...
⚙️  首次启动：正在添加数据库唯一约束...
   检测到 0 组重复告警，正在修复...
   ✅ 唯一约束添加成功
✅ 数据库约束检查完成
======================================
数据库准备完成，启动应用服务...
======================================
```

### 手动部署

```bash
# 1. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境
cp .env.example .env
vim .env

# 4. 手动执行迁移（可选）
python3 models.py          # 创建表
python3 migrate_db.py      # 添加字段
python3 init_migrations.py # 添加唯一约束

# 5. 启动服务
python3 app.py
# 或使用 gunicorn
gunicorn --bind 0.0.0.0:8000 --workers 4 app:app
```

**注意**: 手动部署时，即使不执行步骤4，服务启动时也会自动检查和执行必要的迁移（如果使用entrypoint.sh）。

---

## 迁移幂等性保证

所有迁移操作都是**幂等**的，可以安全地重复执行：

### 表创建
```python
Base.metadata.create_all(engine)  # SQLAlchemy 自动检查表是否存在
```

### 字段添加
```sql
ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS alert_hash VARCHAR(64);
```

### 索引创建
```sql
CREATE INDEX IF NOT EXISTS idx_alert_hash ON webhook_events(alert_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original ...;
```

### 唯一约束
```python
# 检查索引是否已存在
result = conn.execute(text("""
    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'idx_unique_alert_hash_original'
    )
"""))
if result.scalar():
    return True  # 已存在，跳过
```

---

## 回滚和故障处理

### 迁移失败不阻止启动

所有迁移步骤都设置了错误处理，**迁移失败不会阻止服务启动**：

```bash
python3 init_migrations.py || {
    echo "⚠️  唯一约束检查失败，继续启动..."
}
```

**好处**:
- 即使某个迁移失败，服务仍可启动
- 避免因迁移错误导致服务不可用
- 可以启动后手动修复

**风险**:
- 需要查看日志确认迁移状态
- 未执行的迁移可能影响功能

### 手动执行迁移

如果自动迁移失败，可以进入容器手动执行：

```bash
# 进入容器
docker exec -it webhook-receiver bash

# 手动执行迁移
python3 models.py           # 创建表
python3 migrate_db.py       # 添加字段
python3 init_migrations.py  # 添加唯一约束

# 或使用迁移工具
python3 migrations_tool.py add_unique_constraint
```

### 删除唯一约束（如需回滚）

```sql
-- 连接到数据库
psql -h localhost -U webhook_user -d webhooks

-- 删除唯一索引
DROP INDEX IF EXISTS idx_unique_alert_hash_original;
```

---

## 验证迁移状态

### 检查表结构

```sql
-- 查看表结构
\d webhook_events

-- 检查字段是否存在
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'webhook_events';
```

### 检查索引

```sql
-- 查看所有索引
\di

-- 检查唯一约束索引
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'webhook_events';
```

### 检查数据一致性

```sql
-- 检查是否有重复的原始告警
SELECT alert_hash, COUNT(*) as count
FROM webhook_events
WHERE is_duplicate = 0
GROUP BY alert_hash
HAVING COUNT(*) > 1;

-- 预期结果：0 行（无重复）
```

### 通过API检查

```bash
# 健康检查
curl http://localhost:8000/health

# 查看配置（确认服务正常）
curl http://localhost:8000/api/config
```

---

## 监控和日志

### 启动日志

查看容器启动日志以确认迁移状态：

```bash
docker-compose logs webhook-service | grep -E "迁移|唯一约束|数据库"
```

### 关键日志

**成功日志**:
```
✅ 数据库连接成功
✅ 数据库表检查完成
✅ 数据库迁移完成
✅ 唯一约束添加成功
```

**警告日志**（可能需要关注）:
```
⚠️  迁移警告: ...
⚠️  唯一约束检查失败，继续启动...
```

**错误日志**（需要修复）:
```
❌ 数据库连接超时，启动失败
```

---

## 常见问题

### Q1: 新部署的项目需要手动执行迁移吗？

**A**: **不需要**。Docker 部署时会自动执行所有迁移。

如果使用手动部署（非 Docker），虽然可以手动执行，但服务启动时也会自动检查并执行必要的迁移。

### Q2: 如果迁移失败会怎样？

**A**: 迁移失败**不会阻止服务启动**。服务会输出警告日志并继续启动，但可能缺少某些功能。建议查看日志并手动修复。

### Q3: 多个容器同时启动会重复执行迁移吗？

**A**: 会，但这是**安全的**：
- 所有操作都是幂等的
- 唯一约束依赖数据库的原子性
- PostgreSQL 的 `CREATE INDEX IF NOT EXISTS` 和 `CREATE TABLE IF NOT EXISTS` 保证并发安全

### Q4: 如何跳过迁移直接启动？

**A**: 修改 docker-compose.yml，覆盖 ENTRYPOINT：

```yaml
services:
  webhook-service:
    entrypoint: ["gunicorn", "--bind", "0.0.0.0:8000", ...]
```

**不推荐**，除非你确定数据库已正确迁移。

### Q5: 迁移会影响现有数据吗？

**A**: **不会删除数据**。迁移操作仅包括：
- 添加新字段（默认值不影响现有记录）
- 创建索引（不修改数据）
- 修复重复数据（标记为重复，不删除）

---

## 版本升级迁移

### 新增配置项（如 v2.1.0）

```bash
# 新增的环境变量配置
REANALYZE_AFTER_TIME_WINDOW=true
FORWARD_AFTER_TIME_WINDOW=true
```

**特点**:
- 只是环境变量，不涉及数据库表结构
- 无需执行迁移
- 更新 .env 文件并重启服务即可

### 新增表字段（未来可能）

如果未来版本需要添加新字段，只需：

1. 在 `migrate_db.py` 中添加迁移：
   ```python
   {
       'name': '添加新字段',
       'check': "SELECT COUNT(*) FROM information_schema.columns WHERE ...",
       'sql': "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS ..."
   }
   ```

2. 重新构建镜像：
   ```bash
   docker-compose up -d --build
   ```

3. 自动迁移会在启动时执行

---

## 总结

✅ **自动化**: 新项目无需手动执行迁移，完全自动化

✅ **幂等性**: 所有操作可重复执行，不会导致错误

✅ **安全性**: 迁移失败不阻止启动，避免服务不可用

✅ **向后兼容**: 现有数据不受影响，可平滑升级

✅ **可监控**: 详细的日志输出，便于排查问题

---

## 相关文档

- [DEDUPLICATION_FIX.md](./DEDUPLICATION_FIX.md) - 去重机制修复方案
- [MIGRATION_RESULT.md](./MIGRATION_RESULT.md) - 迁移执行报告
- [README.md](./README.md) - 项目主文档

如有疑问或需要帮助，请查阅相关文档或提交 Issue。
