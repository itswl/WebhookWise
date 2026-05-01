## PostgreSQL 表分区方案（Webhook 时序流水表）

Webhook 事件表属于典型的时序写入 + 时间范围查询 + 生命周期清理的流水表。当单表规模达到千万级以上时，常见瓶颈会集中在：

- 历史数据清理成本高（DELETE 触发大量 WAL/索引维护）
- 查询需要扫描大量历史数据（即使有索引也会受到统计信息与页命中率影响）
- VACUUM 压力增大，容易与写入抢资源

本项目当前策略是：

- 主表 `webhook_events` 保持轻量（通过归档清理）
- 历史表 `archived_webhook_events` 承载长期数据

因此更推荐优先对 `archived_webhook_events` 做分区，以获得“Drop Partition 级别的清理效率”，同时避免改动主表的关键约束语义。

## 分区推荐：先分 archived_webhook_events

### 分区键

- 使用 `timestamp` 做 RANGE 分区（按月或按天，取决于量级）
- 典型按月：`FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')`

### 索引策略

- 分区表上的索引会在每个分区各自创建
- 对常用查询维度分别建立索引（与现有主表保持一致即可）

### 清理策略

- 保留 N 天：直接 `DROP TABLE archived_webhook_events_202501`（或 `DETACH PARTITION` 后 DROP）
- 不再对历史分区执行大规模 DELETE

### 写入路径

- 归档任务插入 `archived_webhook_events` 时会自动路由到对应分区
- 需保证目标分区提前创建（可用定时任务每月创建未来 2-3 个分区）

## 为什么不建议直接分区 webhook_events（主表）

当前系统的去重依赖“跨全表”的唯一语义（例如基于 `alert_hash` 的原始告警唯一性约束）。

PostgreSQL 的限制是：分区表上的 UNIQUE/PRIMARY KEY 约束必须包含分区键，否则无法保证跨分区唯一性。

如果按 `timestamp` 分区，无法直接维持“全局 alert_hash 唯一”这一语义，除非：

- 改造唯一性约束（包含 timestamp 分区键）并调整业务含义；或
- 把“原始告警唯一性”抽到单独表维护；或
- 使用不同的分区方式（例如 HASH(alert_hash) 分区），但这会削弱基于时间的清理收益

因此建议：

- 继续保持 `webhook_events` 作为热表（规模控制在较小范围）
- 对 `archived_webhook_events` 做 RANGE(timestamp) 分区，实现秒级清理

## 迁移推进步骤（建议）

1. 新建分区父表 `archived_webhook_events_p`（字段与原表一致）
2. 创建历史分区（回填期间按月建）
3. 将原 `archived_webhook_events` 数据批量迁移到分区表（分批 INSERT INTO ... SELECT ...）
4. 切换读写（rename 表名或创建同名视图）
5. 归档任务写入目标从旧表切换到分区表
6. 增加“创建未来分区”的维护任务

## 回滚

- 保留原 `archived_webhook_events` 作为备份（改名留存）
- 通过视图或表名切回即可（不影响主表写入）

