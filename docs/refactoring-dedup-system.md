# 去重与时间窗口重复检测 — 现状分析与重构设计

## 1. 当前机制详解

### 1.1 身份标识：alert_hash

`services/webhooks/identity.py:15-24`：

```
适配器归一化后的 parsed_data
    │
    ├─ 有 _alert_identity 字段
    │   → 提取 {source, name, resource, service, fingerprint, severity}
    │   → JSON 序列化 → SHA256 → alert_hash
    │
    └─ 无 _alert_identity 字段
        → {source, payload: 整个 parsed_data} → SHA256 → alert_hash  ← 不可靠
```

`AlertIdentity` 的定义（`adapters/normalized.py:17-26`）：

```python
@dataclass(frozen=True)
class AlertIdentity:
    source: str          # "prometheus"
    name: str | None     # "CPUThrottlingHigh"
    resource: str | None # "node-10-0-1-5"
    service: str | None  # "api-gateway"
    fingerprint: str | None  # 适配器自定义
    severity: str | None # "critical"
```

### 1.2 两级缓存查重

在 pipeline 的 `resolve_analysis()` 中先后查两个地方：

**Redis 缓存**（`deduplication.py`）：

```
Key:   webhook:dedupe:{alert_hash}
Value: {"original_event_id": 42, "analysis": {...}}
TTL:   DUPLICATE_ALERT_TIME_WINDOW × 3600 秒 (默认 86400s = 24h)

写入时机：pipeline 完成后，在 DB 事务外部写入
```

**DB 查询**（`repository.py:36-84`）：

```python
check_duplicate_event(alert_hash, time_window_hours=24):

    # 步骤 1：窗口内查找 — 按时间倒序，取最近一条
    SELECT * FROM webhooks
    WHERE alert_hash = X AND timestamp >= now - 24h
    ORDER BY timestamp DESC LIMIT 1

    # 步骤 2：窗口外查找 — 找最近一条 beyond_window=True 的记录
    SELECT * FROM webhooks
    WHERE alert_hash = X AND beyond_window = True
    ORDER BY timestamp DESC LIMIT 1

    # 步骤 1 有结果 → is_duplicate=True
    # 步骤 2 有结果 → is_duplicate=False, beyond_window=True
    # 都没有     → 新事件
```

**关键细节**（`repository.py:69-71`）——窗口起点的计算：

```python
# 如果曾经有过 beyond_window 记录，窗口从那里开始算
window_start = last_beyond.timestamp if last_beyond else original.timestamp
is_within = (now - window_start).total_seconds() / 3600 <= time_window_hours
```

这意味着：如果一条告警在 T+0 首次出现，T+25h 再次出现（被标记为 beyond_window），T+26h 第三次出现时，窗口起点是 T+25h 而不是 T+0，所以第三次会被判定为"窗口内"而非"窗口外"。这个逻辑的目的是避免"每次都在窗口外、每次都重新 AI 分析"。

### 1.3 五层分析复用决策表

`analysis_resolution.py` 的 `_DECISION_TABLE` 按优先级匹配：

```
优先级 1: REUSE_REDIS_CACHE
    条件：Redis 缓存命中 + DB 确认窗口内 + original_event_id 一致
    行为：直接复用 Redis 缓存的分析结果，跳过降噪计算
    
优先级 2: REUSE_RECENT_BEYOND_WINDOW
    条件：beyond_window + 最近 beyond_window 事件在 30 秒内
    行为：复用最近 beyond_window 事件的分析（刚过窗口，大概率还是同一事件）

优先级 3: REUSE_ORIGINAL_BEYOND_WINDOW
    条件：beyond_window + 不重分析 + 原事件分析可用（非 degraded）
    行为：复用原事件分析

优先级 4: REUSE_IN_WINDOW
    条件：窗口内重复 + 原事件分析可用
    行为：复用原事件分析

优先级 5: ANALYZE (兜底)
    条件：以上都不满足
    行为：调用 AI 重新分析
```

### 1.4 写入时的三道防线

`command_service.py` 中写 DB 时的防护：

```
防线 1: request_id 幂等
    → 同一个 request_id 只处理一次，已完成的直接返回已有结果

防线 2: pipeline 传入的 is_duplicate/original_event
    → pipeline 已经判定好了状态，写 DB 时直接用，不再查

防线 3: idx_unique_alert_hash_original 唯一约束
    → 两个并发的同 hash 请求都判定自己是"新事件"时，后提交的撞约束，
      在 IntegrityError 捕获中转为 duplicate
```

### 1.5 转发决策中与去重相关的行为

`decisioning.py:decide_forwarding()`：

| 告警类型 | 默认行为 | 可控开关 |
|---------|---------|---------|
| 新告警 | 转发（如果 importance=high 或有匹配规则） | — |
| 窗口内重复 | **不转发**（除非 importance=high 且有规则） | `FORWARD_DUPLICATE_ALERTS` |
| 窗口外重复 | 转发 | `FORWARD_AFTER_TIME_WINDOW` |
| 冷却期内 | 抑制 | `NOTIFICATION_COOLDOWN_SECONDS`（60s） |
| 触发周期提醒 | 转发（标记 is_periodic_reminder） | `ENABLE_PERIODIC_REMINDER` / `REMINDER_INTERVAL_HOURS`（6h） |

---

## 2. 当前设计的问题

### 2.1 alert_hash 稳定性依赖适配器实现质量

**严重程度：高**

`_alert_identity` 字段由适配器的 normalizer 函数选择性产出。如果适配器没有调用 `with_alert_identity()`（例如用户自定义的适配器），降级到 `{source, payload}` 做 hash。Prometheus payload 中包含 `startsAt`、`endsAt`、动态标签值，每次 hash 都不同，**去重完全失效**。

而且系统只在 `logger.warning` 中记录降级，没有任何 metrics 暴露这个状态，运维无法感知。

### 2.2 五层决策表是对窗口边界问题的修补

**严重程度：中**

为什么需要 5 层？因为固定窗口（24h）有硬边界问题：

```
T+0:       事件 A  #1 → 新事件，AI 分析
T+23h59m:  事件 A  #2 → 窗口内重复，复用分析 ✓
T+24h01m:  事件 A  #3 → beyond_window，正常逻辑应重新 AI 分析
            但 30 秒前刚处理过同一事件，重新 AI 毫无意义
```

所以加了 `RECENT_BEYOND_WINDOW_REUSE_SECONDS=30`（优先级 2），专门处理"刚过窗口边界"的情况。优先级 3 和 4 又分别处理"过窗口但不重分析"和"窗口内"。

本质上，**固定窗口 + 硬边界 = 需要多个 patch 来弥补边界问题**。

### 2.3 Redis 写入在 DB 事务外部

**严重程度：中**

`forwarding_stage.py:223`：
```python
# DB 事务在 session_scope 内已提交
await remember_duplicate_source(...)  # Redis 写 — 在事务外
```

如果进程在 DB 提交后、Redis 写入前崩溃：DB 里已有记录，Redis 缓存缺失。下次同 hash 进来时，Redis 缓存 miss，走 DB 查询路径，正确性不受影响。但 DB 查询路径的开销比 Redis 缓存大，且分析结果可能已被标记为 degraded（此时需要重新 AI 分析而不是复用）。

### 2.4 beyond_window 字段是一个衍生状态

**严重程度：中**

`beyond_window` 不是告警本身的属性，而是"A 事件相对于 B 事件的时间关系"。把它持久化在 `WebhookEvent` 表里，使得：
- 查询逻辑依赖于上一次 beyond_window 记录的持久化正确性（`window_start = last_beyond.timestamp`）
- 数据清理后（`data_maintenance.py` 按保留策略删除旧记录），beyond_window 链条断裂，窗口判断出错
- 同一条事件可能在多次查询中被判定为不同的 beyond_window 状态（取决于查询时其他记录的状态）

### 2.5 WebhookEvent 表承载了去重状态

**严重程度：低**

`WebhookEvent` 表混合了两种职责：事件存储 + 去重状态。`is_duplicate`、`duplicate_of`、`duplicate_count`、`beyond_window` 这些字段本质上是去重元数据，但它和事件本身存在同一张表里。查询去重状态需要扫事件表，而事件表可能因为数据清理策略不完整。

### 2.6 转发决策概念过多

**严重程度：低**

retry 配置中有 6 个与去重/转发直接相关的配置项：

```
DUPLICATE_ALERT_TIME_WINDOW = 24       # 去重窗口
FORWARD_DUPLICATE_ALERTS = False       # 窗口内重复是否转发
FORWARD_AFTER_TIME_WINDOW = True       # 窗口外是否转发
REANALYZE_AFTER_TIME_WINDOW = True     # 窗口外是否重新 AI 分析
RECENT_BEYOND_WINDOW_REUSE_SECONDS = 30 # 窗口边界容忍
NOTIFICATION_COOLDOWN_SECONDS = 60     # 冷却期
ENABLE_PERIODIC_REMINDER = True        # 周期提醒开关
REMINDER_INTERVAL_HOURS = 6           # 周期提醒间隔
```

这些概念互相交织。例如：窗口内重复 + 开启周期提醒 + 到达提醒间隔 → 产生一个"标记为 periodic_reminder 的转发"。这个组合路径在 `decisioning.py` 中是嵌套 if/else 实现的，难以测试全覆盖。

---

## 3. 重新设计

### 3.1 核心原则

1. **身份标识必须可靠** — 适配器必须产出最小身份字段，不可靠的 hash 要有告警
2. **滑动窗口替代固定窗口** — 每次重复事件自动刷新窗口，消除硬边界问题
3. **Redis 是主、DB 是兜底** — Redis 存去重状态，DB 只做 fallback 查询
4. **去重状态独立存储** — 不寄生在 WebhookEvent 表中
5. **概念精简** — 减少配置项，消除衍生状态

### 3.2 滑动窗口替代固定窗口

**当前（固定窗口）**：

```
窗口 = 24h, 起点固定在 first_seen_at

T+0:       事件 #1 → 新 (窗口 [T+0, T+24h))
T+12h:      事件 #2 → 窗口内重复 ✓
T+23h:      事件 #3 → 窗口内重复 ✓
T+24h01m:  事件 #4 → 窗口外 !!! (但可能和 #3 只差 61 分钟)
```

**改为（滑动窗口）**：

```
窗口 = 4h, 每次事件自动刷新 last_seen_at

T+0:       事件 #1 → 新 (last_seen = T+0)
T+1h:      事件 #2 → now - last_seen = 1h < 4h → 重复 ✓ (last_seen 更新为 T+1h)
T+3h:      事件 #3 → now - last_seen = 2h < 4h → 重复 ✓ (last_seen 更新为 T+3h)
T+6h:      事件 #4 → now - last_seen = 3h < 4h → 重复 ✓ (last_seen 更新为 T+6h)
T+11h:     事件 #5 → now - last_seen = 5h > 4h → 新事件, 新窗口开始
```

滑动窗口的核心：**只要告警还在持续（间隔不超过窗口长度），就一直属于同一个去重组。** 没有"刚好在边界"的问题，不需要 30 秒的容忍期。

窗口从 24h 缩短到 4h 是因为滑动窗口会随每次事件自动延长，不需要一个很大的固定窗口来覆盖长持续时间的告警。

### 3.3 去重状态独立存储

```python
# services/dedup/state.py

@dataclass
class DedupState:
    """存储在 Redis 中的去重状态。滑动窗口，每次重复自动延长。"""
    dedup_key: str               # 稳定的去重键
    original_event_id: int       # 该去重组的第一个事件 ID
    first_seen_at: float         # 该去重组首次出现时间 (epoch)
    last_seen_at: float          # 最近一次出现时间 (epoch)
    count: int                   # 该窗口内出现次数
    analysis: dict | None        # 缓存的分析结果

    def is_active(self, now: float, window_seconds: int) -> bool:
        return (now - self.last_seen_at) <= window_seconds
```

**Redis 存储**：

```
Key:   dedup:{dedup_key}
Value: JSON {
    "original_event_id": 42,
    "first_seen_at": 1717000000.0,
    "last_seen_at": 1717003600.0,
    "count": 3,
    "analysis": {...}
}
TTL:   window_seconds * 2  (留一些余量防止刚过期就被清理)
```

**DB fallback 查询**（Redis 不可用或 key 被淘汰时）：

```python
async def find_original_event(dedup_key: str, window_seconds: int) -> WebhookEvent | None:
    """Fallback: 从 DB 中查找最近的非重复事件"""
    threshold = datetime.now() - timedelta(seconds=window_seconds)
    stmt = (
        select(WebhookEvent)
        .where(
            WebhookEvent.alert_hash == dedup_key,
            WebhookEvent.is_duplicate == False,
            WebhookEvent.timestamp >= threshold,
        )
        .order_by(WebhookEvent.timestamp.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
```

### 3.4 简化的去重决策流程

**之前（5 层决策表 + 3 种状态）**：

```
   Redis缓存? → DB窗口内? → DB近beyond? → DB原beyond? → DB窗口内复用? → 重新分析
   5 层，每层有独立的匹配条件和构建函数
```

**改为（2 层，2 种状态）**：

```python
async def resolve_dedup(dedup_key: str, payload: dict) -> DedupResult:
    """
    返回两种结果之一：
    - DedupResult(action=NEW, ...)          → 新事件，需要 AI 分析
    - DedupResult(action=REUSE, analysis=...) → 重复事件，复用分析
    """
    now = time.time()
    window = get_dedup_window_seconds()

    # 第 1 层：Redis
    state = await redis_get_dedup_state(dedup_key)
    if state and state.is_active(now, window):
        return DedupResult(
            action=Action.REUSE,
            original_event_id=state.original_event_id,
            analysis=state.analysis,
            duplicate_count=state.count,
            source="redis",
        )

    # 第 2 层：DB fallback
    original = await find_original_event(dedup_key, window)
    if original and _has_valid_analysis(original):
        return DedupResult(
            action=Action.REUSE,
            original_event_id=original.id,
            analysis=original.ai_analysis,
            duplicate_count=original.duplicate_count or 1,
            source="db",
        )

    # 兜底：新事件
    return DedupResult(action=Action.NEW)
```

**对比**：

| 维度 | 当前 | 改为 |
|------|------|------|
| 决策层级 | 5 层 | 2 层 |
| 事件状态 | 3 种（新/窗口内重复/窗口外重复） | 2 种（新/重复） |
| 窗口类型 | 固定窗口 | 滑动窗口 |
| 边界容忍 | 30 秒 hardcode | 滑动窗口天然解决 |
| beyond_window 字段 | 持久化在 DB | 删除，不需要 |
| 配置项 | 8 个 | 4 个 |

### 3.5 配置精简

**删除的配置项**：

| 配置项 | 原因 |
|--------|------|
| `RECENT_BEYOND_WINDOW_REUSE_SECONDS` | 滑动窗口无边界问题，不需要容忍期 |
| `REANALYZE_AFTER_TIME_WINDOW` | 不再有"窗口外"概念，窗口过去就是新事件，自然重新分析 |
| `FORWARD_AFTER_TIME_WINDOW` | 不再有"窗口外"，窗口过去就是新事件，自然按新事件处理 |
| `DUPLICATE_ALERT_TIME_WINDOW` → 改为 `DEDUP_WINDOW_SECONDS` | 重命名，语义更准确；默认从 24h 改为 4h |

**保留并简化的配置项**：

```python
class DedupConfig(BaseSettings):
    DEDUP_WINDOW_SECONDS: int = Field(default=14400)     # 4h 滑动窗口
    FORWARD_DUPLICATE_ALERTS: bool = Field(default=False) # 窗口内重复是否转发
    NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=60) # 冷却期
    ENABLE_PERIODIC_REMINDER: bool = Field(default=True)   # 周期提醒
    REMINDER_INTERVAL_HOURS: int = Field(default=6)        # 提醒间隔
```

从 8 个减少到 5 个，消除了 3 个与"窗口外"相关的概念。

### 3.6 转发决策简化

**之前**：

```python
if is_duplicate:
    → 冷却期检查 → 周期提醒检查 → forward_duplicate 开关 → 决策
elif beyond_window:
    → forward_after_time_window 开关 → 冷却期检查 → 决策
else:
    → 新事件 → 决策
```

**改为**：

```python
if result.action == Action.REUSE:
    # 窗口内重复 — 统一的重复处理
    if in_cooldown(original_event, cooldown_seconds):
        return suppress("冷却期内")
    if should_periodic_remind(original_event, reminder_hours):
        return forward(is_periodic_reminder=True)
    if not forward_duplicate_alerts:
        return suppress("配置不转发重复告警")
    # 否则按正常规则决策

else:
    # 新事件 — 正常决策
    ...
```

不再有 `beyond_window` 分支。窗口过去的告警就是新事件。

### 3.7 身份标识可靠性保障

```python
# adapters/normalized.py

# 最小必须字段 — 适配器必须提供这些
REQUIRED_IDENTITY_FIELDS = ("source", "name")

def extract_alert_identity(data: dict) -> dict[str, str]:
    value = data.get(IDENTITY_FIELD)
    if not isinstance(value, dict):
        return _fallback_identity(data)

    identity = {k: v for k, v in value.items() if v}
    
    # 检查最小必须字段
    missing = [f for f in REQUIRED_IDENTITY_FIELDS if f not in identity]
    if missing:
        logger.warning("AlertIdentity 缺少必须字段: %s，降级到 payload hash", missing)
        DEDUP_IDENTITY_DEGRADED_TOTAL.inc()  # ← 新增 metrics
        return _fallback_identity(data)
    
    return identity
```

同时提供一个 `GET /api/dedup/stats` 端点，展示：
- 当前活跃的去重组数量
- 降级 hash 的使用比例
- 各 source 的去重命中率

### 3.8 WebhookEvent 模型简化

**删除的字段**（通过 migration 移除或标记 deprecated）：

| 字段 | 原因 |
|------|------|
| `beyond_window` | 滑动窗口无此概念 |

**保留的字段**：

| 字段 | 用途 |
|------|------|
| `alert_hash` | 去重键（改名为 `dedup_key`） |
| `is_duplicate` | 是否重复事件 |
| `duplicate_of` | 指向原事件 |
| `duplicate_count` | 当前计数（由原事件维护） |

**新增去重专用表**（可选，轻量替代 Redis）：

```sql
CREATE TABLE dedup_state (
    dedup_key VARCHAR(64) PRIMARY KEY,
    original_event_id INTEGER NOT NULL,
    first_seen_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP NOT NULL,
    count INTEGER DEFAULT 1,
    analysis JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_dedup_last_seen ON dedup_state(last_seen_at);
```

这张表是可选的——当 Redis 不可用时作为 fallback，或者干脆替代 Redis 成为主去重存储（如果 Redis 稳定性的顾虑大于 DB 查询开销）。

---

## 4. 改造对比

### 4.1 数据流对比

**当前**：

```
webhook 到达
    │
    ▼
adapters normalize → parsed_data
    │
    ├─ 有 _alert_identity? → SHA256(identity字段) → alert_hash
    └─ 无 → SHA256(source + 整个payload) → alert_hash  ← 不可靠，无告警
    │
    ▼
Redis 缓存: webhook:dedupe:{hash}
    ├─ 命中 → 检查与 DB 一致性 → 复用或走 DB 路径
    └─ 未命中 → 查 DB
    │
    ▼
DB 查询: check_duplicate_event()
    ├─ 窗口内 → is_duplicate=True
    ├─ 窗口外(beyond_window) → is_duplicate=False, beyond_window=True
    └─ 历史记录 → is_duplicate=False, beyond_window=True
    │
    ▼
五层决策表 → 选 REUSE_REDIS / REUSE_RECENT_BEYOND / REUSE_ORIGINAL_BEYOND / REUSE_IN_WINDOW / ANALYZE
    │
    ▼
写 DB + 更新 Redis 缓存 (DB 事务外)
```

**改为**：

```
webhook 到达
    │
    ▼
adapters normalize → parsed_data
    │
    ├─ 有 {source, name} 最小身份? → SHA256 → dedup_key
    └─ 无 → 记录 metrics 降级 + SHA256(source + payload_hash) → dedup_key
    │
    ▼
Redis 去重状态: dedup:{dedup_key}
    │
    ├─ 命中 + is_active(now, window) → REUSE（滑动窗口内）
    └─ 未命中 / 过期 → DB fallback 查询
    │
    ▼
DB fallback: SELECT original WHERE alert_hash=X AND is_duplicate=False AND timestamp >= now-window
    ├─ 找到 → REUSE
    └─ 未找到 → NEW
    │
    ▼
┌─ REUSE → 更新 Redis 状态 (last_seen, count) → 转发决策 (不再区分窗口内外)
└─ NEW   → 创建 Redis 状态 → AI 分析 → 转发决策
    │
    ▼
写 DB (is_duplicate, duplicate_of, duplicate_count) + 更新 Redis 状态 (事务内或事务后均可，Redis 有 TTL 兜底)
```

### 4.2 配置项对比

| 当前配置（8 个） | 改为（5 个） | 变化 |
|-----------------|-------------|------|
| `DUPLICATE_ALERT_TIME_WINDOW` = 24 (小时) | `DEDUP_WINDOW_SECONDS` = 14400 (秒) | 重命名 + 缩短 |
| `FORWARD_DUPLICATE_ALERTS` = False | 保留 | — |
| `REANALYZE_AFTER_TIME_WINDOW` = True | **删除** | 不再需要 |
| `FORWARD_AFTER_TIME_WINDOW` = True | **删除** | 不再需要 |
| `RECENT_BEYOND_WINDOW_REUSE_SECONDS` = 30 | **删除** | 滑动窗口解决 |
| `NOTIFICATION_COOLDOWN_SECONDS` = 60 | 保留 | — |
| `ENABLE_PERIODIC_REMINDER` = True | 保留 | — |
| `REMINDER_INTERVAL_HOURS` = 6 | 保留 | — |

### 4.3 代码复杂度对比

| 维度 | 当前 | 改为 |
|------|------|------|
| `analysis_resolution.py` | 360 行，5 层决策表 + 2 个 dataclass + 10 个辅助函数 | ~80 行，2 层决策 |
| `repository.py:check_duplicate_event` | 50 行，3 种返回状态 | ~25 行，2 种返回状态 |
| `command_service.py` 去重相关 | 200 行，beyond_window/is_duplicate 交织 | ~100 行，只区分 new/duplicate |
| `decisioning.py` 转发决策 | 3 个分支（new/dup/beyond） | 2 个分支（new/dup） |
| 去重状态存储 | 寄生在 WebhookEvent 表 | 独立的 DedupState (Redis) + WebhookEvent 保留兜底 |

---

## 5. 迁移路径

### Step 1：新增去重状态模块（不改现有逻辑）

- 新增 `services/dedup/` 目录
- 实现 `DedupState` dataclass + Redis 读写 + DB fallback 查询
- 实现 `resolve_dedup()` 函数（2 层决策）
- 用 feature flag 控制新旧逻辑：`DEDUP_V2_ENABLED = False`
- **验收**：模块存在，单元测试覆盖，但不影响现有流程

### Step 2：WebhookEvent 增加 dedup_key 字段 + 身份标识保障

- 新增 `dedup_key` 字段（初始值 = alert_hash）
- 适配器增加最小身份字段检查 + metrics 暴露
- 新增 `DEDUP_IDENTITY_DEGRADED_TOTAL` counter
- **验收**：所有适配器产出 identity 或触发降级 metrics

### Step 3：切换去重逻辑

- 将 feature flag 置为 `True`
- Pipeline 中的 `resolve_analysis()` 调用改为 `resolve_dedup()`
- `check_duplicate_event()` 简化为 DB fallback 查询
- `beyond_window` 字段标记为 deprecated（不再写入，读取时默认 False）
- **验收**：E2E 测试通过，去重行为与旧逻辑一致

### Step 4：清理旧代码

- 删除 `_DECISION_TABLE` 及相关辅助函数
- 删除 `RECENT_BEYOND_WINDOW_REUSE_SECONDS` 等 3 个废弃配置项
- 删除 `beyond_window` 列（migration）
- 简化 `decisioning.py` 转发决策

### Step 5：可选的 DedupState 表

- 如果 Redis 稳定性是顾虑，创建 `dedup_state` PostgreSQL 表
- Redis 作为 L1 缓存，DB 表作为 L2 持久化存储（不只是 fallback 查询）

---

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 滑动窗口导致去重组意外延长 | 窗口从 24h 缩短到 4h，且每次重复只延长剩余窗口 |
| Redis 状态丢失导致所有事件变"新" | DB fallback 查询兜底；可选 DedupState 表 |
| 迁移期间新旧逻辑不一致 | feature flag 控制，可快速回滚 |
| 旧 beyond_window 数据影响新逻辑 | 新逻辑不读取 beyond_window，旧数据无需迁移 |

---

## 7. 滑动窗口的边界行为示例

```
配置: DEDUP_WINDOW_SECONDS = 14400 (4h)

场景 1: 持续抖动
T+0:      事件 #1 → 新, first_seen=T+0, last_seen=T+0
T+1h:     事件 #2 → now-last=1h<4h → 重复, last_seen=T+1h
T+3h:     事件 #3 → now-last=2h<4h → 重复, last_seen=T+3h
T+6h:     事件 #4 → now-last=3h<4h → 重复, last_seen=T+6h
T+10h:    事件 #5 → now-last=4h>=4h → 新事件, 新窗口开始

场景 2: 短暂爆发后消失
T+0:      事件 #1 → 新
T+5min:   事件 #2 → 重复
T+10min:  事件 #3 → 重复
T+5h:     事件 #4 → now-last=4h50m>4h → 新事件

场景 3: 刚好在边界（滑动窗口无此问题）
T+0:      事件 #1 → 新
T+3h59m: 事件 #2 → 重复 (差 3h59m < 4h)
T+7h59m: 事件 #3 → 重复 (差 4h < 4h, 但实际只比 #2 晚 4h)
T+12h:    事件 #4 → now-last=4h01m>4h → 新事件 (与 #3 差 4h01m)
```
