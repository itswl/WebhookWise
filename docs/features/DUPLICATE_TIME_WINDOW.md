# 告警去重时间窗口机制详解

## 问题

**用户提问**：24 小时以上的重复告警会重新分析和推送吗？

## 答案

**是的**，超过时间窗口的重复告警会被当作**新告警**重新处理。

---

## 当前配置

### 时间窗口设置

```bash
DUPLICATE_ALERT_TIME_WINDOW=24  # 24 小时（默认）
```

**含义**：
- 在 **24 小时内**收到相同的告警（相同 alert_hash）→ 标记为重复
- **超过 24 小时**后再收到相同告警 → 视为**新告警**

---

## 工作机制

### 去重判断逻辑

```python
# core/utils.py:190
time_threshold = datetime.now() - timedelta(hours=time_window_hours)

# 查询条件
WebhookEvent.alert_hash == alert_hash,
WebhookEvent.timestamp >= time_threshold,  # ← 关键：只查找时间窗口内的
WebhookEvent.is_duplicate == 0
```

### 时间轴示例

```
假设时间窗口 = 24 小时

第 1 天 10:00 - 收到告警 A (hash=xxx)
                ↓
                创建原始告警 ID=1000
                is_duplicate=0

第 1 天 11:00 - 收到告警 A (hash=xxx) ← 1小时后
                ↓
                检测：在 24h 窗口内，找到 ID=1000
                ↓
                创建重复告警 ID=1001
                is_duplicate=1, duplicate_of=1000
                ❌ 不重新分析，不推送

第 1 天 20:00 - 收到告警 A (hash=xxx) ← 10小时后
                ↓
                检测：在 24h 窗口内，找到 ID=1000
                ↓
                创建重复告警 ID=1002
                is_duplicate=1, duplicate_of=1000
                ❌ 不重新分析，不推送

第 2 天 11:00 - 收到告警 A (hash=xxx) ← 25小时后（超过24h）
                ↓
                检测：不在 24h 窗口内，查询结果为空
                ↓
                创建新的原始告警 ID=1003  ← 新告警！
                is_duplicate=0
                ✅ 重新分析
                ✅ 重新推送（如果 importance=high）

第 2 天 12:00 - 收到告警 A (hash=xxx) ← 26小时后
                ↓
                检测：在 24h 窗口内，找到 ID=1003
                ↓
                创建重复告警 ID=1004
                is_duplicate=1, duplicate_of=1003
                ❌ 不重新分析，不推送
```

---

## 具体行为

### 场景 1：在时间窗口内（< 24小时）

**输入**：
```
时间: 2026-02-28 10:00
告警: alert_hash=abc123
```

**处理**：
1. 查询最近 24 小时内是否有相同 hash 的原始告警
2. ✅ 找到了 ID=8928（时间：2026-02-28 09:00，1小时前）
3. 创建重复告警：
   ```json
   {
     "id": 8930,
     "is_duplicate": 1,
     "duplicate_of": 8928,
     "ai_analysis": {...},  // ← 复用 8928 的分析结果
     "importance": "high"   // ← 复用 8928 的重要性
   }
   ```
4. **跳过 AI 分析**（节省成本和时间）
5. **跳过推送**（避免重复通知）

**日志**：
```
检测到重复告警: hash=abc123, 原始告警ID=8928, 时间窗口=24小时
重复告警已保存: ID=8930, 复用原始告警8928的AI分析结果
跳过自动转发: 重复告警（原始 ID=8928），配置跳过转发
```

---

### 场景 2：超过时间窗口（>= 24小时）

**输入**：
```
时间: 2026-03-01 10:00
告警: alert_hash=abc123
上次同类告警: 2026-02-28 09:00（25小时前）
```

**处理**：
1. 查询最近 24 小时内是否有相同 hash 的原始告警
2. ❌ 没找到（上次是 25 小时前，超出窗口）
3. 创建**新的原始告警**：
   ```json
   {
     "id": 9000,
     "is_duplicate": 0,      // ← 新告警！
     "duplicate_of": null,
     "ai_analysis": null,    // ← 需要分析
     "importance": null      // ← 需要判断
   }
   ```
4. **调用 AI 重新分析**
5. **根据重要性决定是否推送**
   - importance=high → ✅ 推送
   - importance=medium/low → ❌ 不推送

**日志**：
```
新告警，开始 AI 分析...
AI 分析完成: importance=high, 事件类型=性能告警
开始自动转发高风险告警...
转发成功: status=success
```

---

## 配置说明

### 修改时间窗口

#### 方法 1：通过 Web 界面

1. 打开 https://dejavu.prod.common-infra.hony.love
2. 点击右上角"⚙️ 配置"
3. 修改"去重时间窗口"
4. 点击保存

#### 方法 2：修改 .env 文件

```bash
# 在服务器上
cd /opt/docker-compose/webhook
vim .env

# 修改这一行
DUPLICATE_ALERT_TIME_WINDOW=48  # 改为 48 小时

# 重启服务
docker-compose restart webhook-service
```

#### 方法 3：环境变量

```bash
# docker-compose.yml
environment:
  - DUPLICATE_ALERT_TIME_WINDOW=72  # 3天
```

### 推荐值

| 场景 | 推荐值 | 说明 |
|------|--------|------|
| **频繁告警** | 6-12 小时 | 短期内重复不通知，但问题持续则再次提醒 |
| **普通监控** | 24 小时（默认） | 平衡去重和及时通知 |
| **长周期监控** | 48-72 小时 | 避免非工作时间的重复告警 |
| **测试环境** | 1 小时 | 快速测试去重机制 |

### 取值范围

- **最小值**：1 小时
- **最大值**：168 小时（7天）
- **默认值**：24 小时

---

## 实际案例分析

### 您的告警 8928 & 8930

**原始数据**：
```json
// 告警 5814（最早）
{
  "timestamp": "2026-02-27 XX:XX:XX",
  "alert_hash": "02f20c1c60b26...",
  "is_duplicate": 0
}

// 告警 8928（13小时后）
{
  "timestamp": "2026-02-28 04:35:04",
  "alert_hash": "02f20c1c60b26...",
  "is_duplicate": 1,
  "duplicate_of": 5814
}

// 告警 8930（14秒后）
{
  "timestamp": "2026-02-28 04:35:18",
  "alert_hash": "02f20c1c60b26...",
  "is_duplicate": 1,
  "duplicate_of": 5814
}
```

**分析**：
- ✅ 8928 和 8930 都在 24 小时窗口内
- ✅ 都正确标记为重复告警
- ✅ 都指向原始告警 5814
- ✅ 都复用了 5814 的 AI 分析结果
- ✅ 都没有重新推送

**如果 8930 在 25 小时后到达**：
- 会被当作新告警
- 重新调用 AI 分析
- 如果 importance=high，会重新推送

---

## 常见问题

### Q1: 为什么要设置时间窗口？

**A**: 两个原因：

1. **避免告警风暴**
   - 某个服务故障可能每分钟触发一次告警
   - 如果都推送，会导致通知轰炸

2. **持续问题提醒**
   - 如果问题一直没解决
   - 24小时后再次提醒，引起重视

### Q2: 时间窗口越长越好吗？

**A**: 不一定，需要平衡：

**时间窗口太短**（如 1 小时）：
- ❌ 持续问题会频繁通知
- ❌ 增加 AI 分析成本
- ❌ 通知疲劳

**时间窗口太长**（如 7 天）：
- ❌ 持续问题可能被忽略
- ❌ 问题恶化才会再次通知
- ❌ 不利于及时响应

**合理范围**（12-48 小时）：
- ✅ 过滤短期重复
- ✅ 持续问题及时提醒
- ✅ 成本和效果平衡

### Q3: 可以针对不同告警设置不同窗口吗？

**A**: 当前不支持，但可以扩展：

**当前实现**：
```python
# 全局配置，所有告警统一
time_window_hours = Config.DUPLICATE_ALERT_TIME_WINDOW  # 24h
```

**可能的扩展**：
```python
# 根据告警类型/来源设置不同窗口
def get_time_window(source, importance):
    rules = {
        ('mongodb', 'high'): 12,    # MongoDB 高风险：12h
        ('nginx', 'medium'): 24,    # Nginx 中风险：24h
        ('*', 'low'): 72           # 低风险：3天
    }
    return rules.get((source, importance), 24)
```

如果需要这个功能，请告知。

### Q4: 超过窗口的重复告警会被转发吗？

**A**: 会的，按照新告警处理：

```python
# 超过窗口 → 新告警 → 重新分析
if importance == 'high':
    should_forward = True  # ✅ 会推送
```

**注意**：即使启用了 `FORWARD_DUPLICATE_ALERTS=false`，超过窗口的告警不会被认为是"重复"，所以仍会推送。

### Q5: 如何查看某个告警是否在窗口内？

**A**: 查询数据库：

```sql
-- 查看最近 24 小时内相同 hash 的告警
SELECT id, timestamp, is_duplicate, duplicate_of
FROM webhook_events
WHERE alert_hash = '02f20c1c60b26cc285fc25f02fda9e334d9c98130e6d21fd7e039c2d118622bb'
  AND timestamp >= NOW() - INTERVAL '24 hours'
ORDER BY timestamp DESC;
```

或通过 API：
```bash
curl -s "https://dejavu.prod.common-infra.hony.love/api/webhooks?page=1&page_size=100" | \
  jq '.data[] | select(.alert_hash == "02f20c1c60b26...") | {id, timestamp, is_duplicate}'
```

---

## 监控建议

### 1. 统计去重效果

```sql
-- 每天的去重率
SELECT
    DATE(timestamp) as date,
    COUNT(*) as total,
    SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) as original,
    SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) as duplicate,
    ROUND(100.0 * SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) / COUNT(*), 2) as duplicate_rate
FROM webhook_events
WHERE timestamp >= NOW() - INTERVAL '7 days'
GROUP BY DATE(timestamp)
ORDER BY date DESC;
```

### 2. 分析时间间隔分布

```sql
-- 重复告警与原始告警的时间间隔分布
SELECT
    CASE
        WHEN interval_hours < 1 THEN '< 1h'
        WHEN interval_hours < 6 THEN '1-6h'
        WHEN interval_hours < 12 THEN '6-12h'
        WHEN interval_hours < 24 THEN '12-24h'
        ELSE '>= 24h'
    END as interval_range,
    COUNT(*) as count
FROM (
    SELECT
        d.id,
        EXTRACT(EPOCH FROM (d.timestamp - o.timestamp)) / 3600 as interval_hours
    FROM webhook_events d
    JOIN webhook_events o ON d.duplicate_of = o.id
    WHERE d.is_duplicate = 1
      AND d.timestamp >= NOW() - INTERVAL '7 days'
) AS intervals
GROUP BY interval_range
ORDER BY interval_range;
```

### 3. 识别频繁告警

```sql
-- 找出窗口内重复次数最多的告警
SELECT
    alert_hash,
    MIN(timestamp) as first_seen,
    MAX(timestamp) as last_seen,
    COUNT(*) as total_count,
    SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) as duplicate_count
FROM webhook_events
WHERE timestamp >= NOW() - INTERVAL '24 hours'
GROUP BY alert_hash
HAVING COUNT(*) > 5
ORDER BY total_count DESC
LIMIT 10;
```

---

## 总结

### 核心逻辑

```
收到告警
    ↓
查询最近 24h 内相同 hash 的原始告警
    ↓
    ├─ 找到 → 标记为重复 → 复用分析 → 不推送
    └─ 没找到 → 新告警 → 重新分析 → 判断是否推送
```

### 关键点

1. **时间窗口 = 24 小时**（默认，可配置）
2. **窗口内重复** → 不重新分析，不推送
3. **超过窗口** → 视为新告警，重新分析和推送
4. **目的** → 平衡去重和持续问题提醒

### 回答您的问题

**24 小时以上的重复告警会重新分析和推送吗？**

**是的**：
- ✅ 会重新调用 AI 分析
- ✅ 会根据 importance 判断是否推送
- ✅ 但如果告警本身已恢复（endsAt 有值），可能不会产生新告警

**不会被认为是重复**：
- ✅ is_duplicate = 0（新的原始告警）
- ✅ 成本：调用 AI API
- ✅ 收益：持续问题得到关注

---

如果您希望修改时间窗口或调整去重策略，请告诉我！
