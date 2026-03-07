# 超时间窗口告警行为独立配置

## 功能概述

此功能允许您独立控制**超过时间窗口后**的重复告警的两个核心行为：

1. **是否重新分析**（调用 AI API）
2. **是否推送转发**（转发到飞书或其他目标）

## 配置项说明

### 1. REANALYZE_AFTER_TIME_WINDOW

**含义**：超过时间窗口后，收到相同告警时是否重新调用 AI 分析

**可选值**：
- `true`：重新分析（调用 AI API，产生新的分析结果）
- `false`：复用历史分析结果（不调用 AI API）

**默认值**：`true`

**作用**：
- ✅ `true`：确保分析结果反映最新状态，但会产生 AI API 费用
- ✅ `false`：节省 AI API 费用，但使用旧的分析结果

### 2. FORWARD_AFTER_TIME_WINDOW

**含义**：超过时间窗口后，收到相同的高风险告警时是否推送转发

**可选值**：
- `true`：推送转发（如果 importance=high）
- `false`：不推送转发

**默认值**：`true`

**作用**：
- ✅ `true`：持续问题会定期提醒（例如每 24 小时提醒一次）
- ✅ `false`：避免重复通知，即使问题持续存在

---

## 配置组合场景

### 场景 1：完全重新处理（默认）

```bash
REANALYZE_AFTER_TIME_WINDOW=true
FORWARD_AFTER_TIME_WINDOW=true
```

**行为**：
- ✅ 超过 24 小时后收到相同告警 → 重新调用 AI 分析
- ✅ 如果 importance=high → 推送转发

**适用**：
- 需要确保分析结果最新
- 希望持续问题得到关注
- AI API 成本可接受

**示例**：
```
第 1 天 10:00 - 告警 A (hash=xxx)
              ↓
              创建原始告警 ID=1000, 分析: importance=high
              ✅ 推送转发

第 1 天 15:00 - 告警 A (hash=xxx) ← 5小时后，窗口内
              ↓
              标记为重复 ID=1001, duplicate_of=1000
              ❌ 不重新分析，不推送

第 2 天 11:00 - 告警 A (hash=xxx) ← 25小时后，超过窗口
              ↓
              创建新原始告警 ID=1002
              ✅ 重新调用 AI 分析
              ✅ 如果 importance=high，推送转发
```

---

### 场景 2：重新分析但不推送

```bash
REANALYZE_AFTER_TIME_WINDOW=true
FORWARD_AFTER_TIME_WINDOW=false
```

**行为**：
- ✅ 超过 24 小时后收到相同告警 → 重新调用 AI 分析
- ❌ 即使 importance=high 也**不推送转发**

**适用**：
- 需要最新的分析结果（用于记录或后续手动查看）
- 但不希望重复通知（避免通知疲劳）

**示例**：
```
第 1 天 10:00 - 告警 A (hash=xxx)
              ↓
              创建原始告警 ID=1000, 分析: importance=high
              ✅ 推送转发

第 2 天 11:00 - 告警 A (hash=xxx) ← 25小时后
              ↓
              创建新原始告警 ID=1002
              ✅ 重新调用 AI 分析 → importance=high
              ❌ 不推送转发（配置禁止）
              📝 可在 Web 界面查看最新分析结果
```

---

### 场景 3：复用分析且推送

```bash
REANALYZE_AFTER_TIME_WINDOW=false
FORWARD_AFTER_TIME_WINDOW=true
```

**行为**：
- ❌ 超过 24 小时后收到相同告警 → **不**调用 AI 分析，复用历史结果
- ✅ 如果历史分析结果 importance=high → 推送转发

**适用**：
- 节省 AI API 费用
- 告警内容不变，历史分析结果仍然有效
- 需要定期提醒持续问题

**示例**：
```
第 1 天 10:00 - 告警 A (hash=xxx)
              ↓
              创建原始告警 ID=1000
              ✅ AI 分析 → importance=high
              ✅ 推送转发

第 2 天 11:00 - 告警 A (hash=xxx) ← 25小时后
              ↓
              创建新原始告警 ID=1002
              ❌ 不调用 AI（节省费用）
              ✅ 复用 ID=1000 的分析结果 → importance=high
              ✅ 推送转发（基于历史分析结果）
```

---

### 场景 4：完全忽略（静默模式）

```bash
REANALYZE_AFTER_TIME_WINDOW=false
FORWARD_AFTER_TIME_WINDOW=false
```

**行为**：
- ❌ 超过 24 小时后收到相同告警 → 不调用 AI 分析
- ❌ 不推送转发

**适用**：
- 已知的持续性问题，不需要反复提醒
- 节省 AI API 费用和通知成本
- 仅用于记录和存档

**示例**：
```
第 1 天 10:00 - 告警 A (hash=xxx)
              ↓
              创建原始告警 ID=1000
              ✅ AI 分析 → importance=high
              ✅ 推送转发

第 2 天 11:00 - 告警 A (hash=xxx) ← 25小时后
              ↓
              创建新原始告警 ID=1002
              ❌ 不调用 AI
              ✅ 复用历史分析结果
              ❌ 不推送转发（即使 importance=high）
              📝 仅记录到数据库
```

---

## 配置方法

### 方法 1：通过 Web 界面

1. 访问 `https://dejavu.prod.common-infra.hony.love`
2. 点击右上角 **⚙️ 配置**
3. 找到新增的配置项：
   - **超时间窗口后重新分析** → `reanalyze_after_time_window`
   - **超时间窗口后推送转发** → `forward_after_time_window`
4. 切换开关并保存

### 方法 2：修改 .env 文件

```bash
# 在服务器上
cd /opt/docker-compose/webhook
vim .env

# 添加或修改以下行
REANALYZE_AFTER_TIME_WINDOW=true   # 或 false
FORWARD_AFTER_TIME_WINDOW=true     # 或 false

# 重启服务以应用配置
docker-compose restart webhook-service
```

### 方法 3：环境变量

```bash
# docker-compose.yml
services:
  webhook-service:
    environment:
      - REANALYZE_AFTER_TIME_WINDOW=false
      - FORWARD_AFTER_TIME_WINDOW=true
```

---

## 与现有配置的关系

### 时间窗口配置

```bash
DUPLICATE_ALERT_TIME_WINDOW=24  # 去重时间窗口（小时）
```

**作用**：
- 在此窗口内（例如 24 小时）的重复告警 → 不重新分析，不推送
- **超过**此窗口的重复告警 → 根据新配置项决定行为

### 并发与通知冷却配置（新增）

```bash
PROCESSING_LOCK_TTL_SECONDS=120
PROCESSING_LOCK_WAIT_SECONDS=3
RECENT_BEYOND_WINDOW_REUSE_SECONDS=30
NOTIFICATION_COOLDOWN_SECONDS=60
SAVE_MAX_RETRIES=3
SAVE_RETRY_DELAY_SECONDS=0.1
```

**作用**：
- 并发 worker 之间的等待、复用窗口、重试退避可配置
- 通知冷却时间（默认 60 秒）可配置，避免短时间内重复推送

### 窗口内重复告警配置

```bash
FORWARD_DUPLICATE_ALERTS=false  # 是否转发窗口内的重复告警
```

**作用**：
- 控制窗口**内**的重复告警是否转发（通常设置为 `false`）
- 窗口**外**的行为由新配置项控制

### 完整配置示例

```bash
# 去重时间窗口
DUPLICATE_ALERT_TIME_WINDOW=24

# 窗口内的重复告警行为
FORWARD_DUPLICATE_ALERTS=false  # 窗口内不转发重复告警

# 窗口外的重复告警行为（新增）
REANALYZE_AFTER_TIME_WINDOW=false  # 不重新分析（节省费用）
FORWARD_AFTER_TIME_WINDOW=true     # 但仍然推送（定期提醒）
```

**效果**：
- 24 小时内：重复告警不分析、不推送
- 24 小时后：复用历史分析，如果 importance=high 则推送

---

## 判断逻辑流程图

```
收到 Webhook
    ↓
生成 alert_hash
    ↓
检查 24h 窗口内是否有相同 hash 的原始告警
    ↓
    ├─ 有 → 标记为重复告警
    │      ✅ 复用分析结果
    │      ❌ 不推送（除非 FORWARD_DUPLICATE_ALERTS=true）
    │
    └─ 没有 → 检查窗口外是否有历史告警
           ↓
           ├─ 没有历史 → 新告警
           │             ✅ 调用 AI 分析
           │             ✅ 如果 importance=high 则推送
           │
           └─ 有历史 → 超时间窗口的重复告警
                      ↓
                      ├─ REANALYZE_AFTER_TIME_WINDOW=true?
                      │  ├─ Yes → ✅ 调用 AI 重新分析
                      │  └─ No  → ✅ 复用历史分析结果
                      ↓
                      ├─ FORWARD_AFTER_TIME_WINDOW=true?
                      │  ├─ Yes → ✅ 如果 importance=high 则推送
                      │  └─ No  → ❌ 不推送
```

---

## 当前实现细节（与代码一致）

> 以下行为以 `app.py` 与 `utils.py` 当前实现为准。

### 1. 窗口起点策略（防止持续告警永远停留在窗口内）

系统判断是否“窗口内/窗口外”时，不是只看最早原始告警时间，而是按以下优先级取窗口起点：

1. 最近一条 `beyond_window=1` 的记录（如果存在）
2. 否则退回到原始告警时间

这意味着持续性告警会以最近一次“窗口外事件”重新起算，避免长期告警一直被判定为窗口内重复。

### 2. 窗口外记录的落库形式

当前实现中，窗口外事件会作为“重复告警记录”落库：

- `is_duplicate=1`
- `duplicate_of=<原始告警ID>`
- `beyond_window=1`

不是创建全新的原始告警链路。

### 3. 并发一致性与重试兜底

为了避免多 worker 并发写入造成重复原始记录，保存阶段采用三层保护：

1. **同事务内重新判重**：写入前在事务内再次调用判重逻辑，避免外层状态过期。
2. **冲突重试**：遇到 `IntegrityError` 时最多重试 3 次，并做小步退避。
3. **最终兜底**：重试仍失败时，读取最新原始告警并降级写入重复记录，避免请求直接失败。

---

## 响应字段变化

### 新增字段：beyond_time_window

**API 响应示例**：

```json
{
  "success": true,
  "webhook_id": 9000,
  "is_duplicate": false,          // 窗口内不重复
  "duplicate_of": null,
  "beyond_time_window": true,     // ← 新增：窗口外有历史告警
  "ai_analysis": {
    "importance": "high",
    "summary": "...",
    "reanalyzed": false           // 复用了历史分析
  },
  "forward_status": "success"     // 或 "skipped"
}
```

**字段说明**：
- `is_duplicate`: `false` 表示窗口内没有重复
- `beyond_time_window`: `true` 表示窗口外有历史告警
- `ai_analysis.reanalyzed`: 标识是否重新分析（前端可选显示）

---

## 监控和验证

### 检查配置是否生效

```bash
# 查看当前配置
curl -s "https://dejavu.prod.common-infra.hony.love/api/config" | jq '.data | {
  reanalyze_after_time_window,
  forward_after_time_window,
  duplicate_alert_time_window
}'

# 预期输出
{
  "reanalyze_after_time_window": false,
  "forward_after_time_window": true,
  "duplicate_alert_time_window": 24
}
```

### 验证超时间窗口行为

```sql
-- 查看窗口外重复告警的处理情况
SELECT
    w1.id,
    w1.timestamp,
    w1.is_duplicate,
    w1.ai_analysis IS NOT NULL AS has_analysis,
    w1.forward_status,
    w2.id AS history_id,
    EXTRACT(EPOCH FROM (w1.timestamp - w2.timestamp)) / 3600 AS hours_since_last
FROM webhook_events w1
LEFT JOIN webhook_events w2 ON w1.alert_hash = w2.alert_hash
    AND w2.is_duplicate = 0
    AND w2.timestamp < w1.timestamp
WHERE w1.alert_hash = '<某个hash>'
ORDER BY w1.timestamp DESC
LIMIT 5;
```

### 日志关键字

```bash
# 搜索窗口外重复告警日志
grep "窗口外历史告警" logs/app.log

# 示例日志
# 2026-02-28 10:00:00 INFO 窗口外历史告警(ID=5814)，复用历史分析结果
# 2026-02-28 10:00:00 INFO 跳过自动转发: 窗口外重复告警，配置跳过转发
```

---

## 成本和性能对比

### AI API 调用成本

| 配置 | AI API 调用 | 成本 | 说明 |
|------|-------------|------|------|
| `REANALYZE=true` | 每次都调用 | 高 | 每个告警（窗口外）都产生费用 |
| `REANALYZE=false` | 复用历史 | 低 | 首次分析后不再产生费用 |

**示例**：
- 某告警每天触发 1 次，持续 7 天
- 时间窗口 = 24 小时
- AI 单次调用成本 = $0.01

| 配置 | 调用次数 | 总成本 |
|------|---------|--------|
| `REANALYZE=true` | 7 次 | $0.07 |
| `REANALYZE=false` | 1 次（仅首次） | $0.01 |

### 通知频率对比

| 配置 | 通知频率 | 说明 |
|------|---------|------|
| `FORWARD=true` | 每 24h 一次 | 定期提醒持续问题 |
| `FORWARD=false` | 仅首次 | 避免重复通知 |

---

## 常见问题

### Q1: 如何判断告警是否被复用了分析？

**A**: 查看日志或数据库记录：

```bash
# 日志
grep "复用历史分析结果" logs/app.log

# 数据库（检查两条记录的 ai_analysis 是否相同）
SELECT id, alert_hash, ai_analysis->>'summary'
FROM webhook_events
WHERE alert_hash = '<hash>'
ORDER BY timestamp DESC;
```

### Q2: 复用的分析结果会过时吗？

**A**: 可能会。建议：
- 如果告警内容可能变化 → 设置 `REANALYZE=true`
- 如果告警内容固定 → 设置 `REANALYZE=false` 节省费用

### Q3: 窗口内的重复告警是否受这些配置影响？

**A**: **不受影响**。新配置项仅影响**超过时间窗口**后的告警。

窗口内的重复告警仍然：
- ❌ 不调用 AI 分析
- ❌ 不推送转发（除非 `FORWARD_DUPLICATE_ALERTS=true`）

### Q4: 如果 REANALYZE=false 但历史分析为空怎么办？

**A**: 当前实现会优先复用历史分析（`original_event.ai_analysis or {}`）。
如果历史分析确实为空，结果可能仍为空，不会在该分支强制触发 AI。

建议：
- 需要保证每次窗口外都有新分析结果时，设置 `REANALYZE_AFTER_TIME_WINDOW=true`
- 成本优先且可接受偶发空分析时，保持 `false`

### Q5: 能否针对不同告警类型设置不同配置？

**A**: 当前不支持，但可以扩展。如果需要，可以添加：

```python
# config.py
TIME_WINDOW_RULES = {
    'mongodb': {
        'time_window': 12,
        'reanalyze': True,
        'forward': True
    },
    'nginx': {
        'time_window': 24,
        'reanalyze': False,
        'forward': True
    }
}
```

如需此功能，请联系开发团队。

---

## 总结

### 核心概念

1. **时间窗口内**（< 24h）：
   - 始终标记为重复
   - 始终复用分析
   - 始终不推送（除非 `FORWARD_DUPLICATE_ALERTS=true`）

2. **时间窗口外**（>= 24h）：
   - 根据 `REANALYZE_AFTER_TIME_WINDOW` 决定是否分析
   - 根据 `FORWARD_AFTER_TIME_WINDOW` 决定是否推送

### 推荐配置

| 场景 | REANALYZE | FORWARD | 说明 |
|------|-----------|---------|------|
| **成本敏感** | `false` | `true` | 节省费用，仍然提醒 |
| **准确性优先** | `true` | `true` | 最新分析，定期提醒 |
| **静默模式** | `false` | `false` | 节省费用，避免通知 |

### 配置建议

- **生产环境**：`REANALYZE=false`, `FORWARD=true` 平衡成本和效果
- **测试环境**：`REANALYZE=true`, `FORWARD=false` 验证分析准确性
- **低频告警**：`REANALYZE=true`, `FORWARD=true` 确保及时响应
- **高频告警**：`REANALYZE=false`, `FORWARD=false` 避免重复

---

## 版本信息

- **功能版本**: v1.0
- **添加日期**: 2026-02-28
- **相关文档**:
  - [DUPLICATE_TIME_WINDOW.md](./DUPLICATE_TIME_WINDOW.md) - 时间窗口机制详解
  - [DEDUPLICATION_FIX.md](./DEDUPLICATION_FIX.md) - 去重机制修复方案

---

如有疑问或需要自定义配置，请查阅相关文档或联系技术支持。
