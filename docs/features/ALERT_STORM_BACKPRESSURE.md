## 告警风暴背压：Fail-Fast + 聚合写入

当同一 `alert_hash` 在短时间内并发激增时，传统的“分布式锁 + 等待复用”会导致大量协程在 Pub/Sub/锁等待处挂起，进一步触发文件描述符/连接池压力，形成级联雪崩风险。

本项目在 `processing_lock(alert_hash)` 基础上引入告警风暴背压：

- 超过阈值后触发 Fail-Fast：不再进入等待复用，直接走抑制/聚合分支
- 聚合写入：同一窗口内把抑制事件聚合到同一条事件记录（减少 DB 写入放大与列表噪音）
- 仅保留最新 N 条：风暴期间仅保留每个 `alert_hash` 最近 N 条事件，其余旧记录自动删除，防止主表膨胀

## 相关配置（静态，需重启）

- `PROCESSING_LOCK_FAILFAST_THRESHOLD`：阈值，超过则触发 Fail-Fast
- `PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS`：统计窗口（秒）
- `PROCESSING_LOCK_STORM_KEEP_LATEST_N`：风暴期间每个 `alert_hash` 仅保留最新 N 条记录

## 观测指标

- `webhook_storm_suppressed_total{source=...}`：告警风暴触发 Fail-Fast 抑制/聚合计数

