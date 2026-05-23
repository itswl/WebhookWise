# WebhookWise 整体重构方案

> 合并自 [推送系统重构](./refactoring-push-system.md) 和 [去重系统重构](./refactoring-dedup-system.md)。

---

## 1. 重构总览

### 1.1 目标架构

```
                          Webhook 请求
                              │
                              ▼
                    API (校验/限流/背压)
                      Redis 故障 → fail-open
                              │
                          202 Accepted
                              │
                              ▼
                       Redis Stream
                              │
                              ▼
                     Worker Pipeline
                    ┌─────────────────┐
                    │ 1. Parse        │  适配器归一化 + 身份提取
                    │ 2. Dedup        │  滑动窗口, 2层决策 (Redis → DB)
                    │ 3. AI/Reuse     │  新 → AI分析, 重复 → 复用缓存
                    │ 4. Noise        │  相似度降噪
                    │ 5. Decide       │  2分支决策 (新/重复), 修复P0漏洞
                    │ 6. Build        │  Channel.format() 构建消息
                    │ 7. Outbox       │  DB事务写入 + 即时入队
                    └─────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
              should_forward      不应该转发
                    │              (含降噪抑制)
                    │                   │
                    ▼                   ▼
            Outbox Worker         写 SuppressedRecord
           ┌────────────────┐        (dashboard可见)
           │ Channel.send() │
           │ ├─ feishu      │
           │ ├─ dingtalk    │
           │ ├─ slack       │
           │ └─ webhook     │
           └────────────────┘
                    │
              ┌─────┴─────┐
            成功          失败
           (SENT)    (重试/耗尽通知)
```

### 1.2 重构要解决的 10 个问题

| # | 问题 | 严重度 | 归属 |
|---|------|--------|------|
| P0 | high 告警无目标时静默丢弃 | 高 | 推送 |
| P0 | Redis 故障全线停摆 (fail-close) | 高 | 推送 |
| P1 | alert_hash 稳定性依赖适配器实现 | 高 | 去重 |
| P2 | AI 错误通知无可靠性保障 | 中 | 推送 |
| P2 | 五层决策表是对固定窗口的修补 | 中 | 去重 |
| P3 | outbox 即时入队在事务外 | 中 | 推送 |
| P3 | Redis 缓存在 DB 事务外写入 | 中 | 去重 |
| P4 | 消息构建和投递耦合 | 中 | 推送 |
| P4 | beyond_window 衍生状态持久化 | 中 | 去重 |
| P5 | 转发规则匹配维度有限 | 低 | 两者 |

### 1.3 配置项精简

| 配置项 | 当前 | 改为 | 变化 |
|--------|------|------|------|
| `DUPLICATE_ALERT_TIME_WINDOW` = 24h | `DEDUP_WINDOW_SECONDS` = 14400 | 重命名 + 缩短 + 语义改为滑动窗口 |
| `REANALYZE_AFTER_TIME_WINDOW` = True | **删除** | 滑动窗口无"窗口外"概念 |
| `FORWARD_AFTER_TIME_WINDOW` = True | **删除** | 同上 |
| `RECENT_BEYOND_WINDOW_REUSE_SECONDS` = 30 | **删除** | 滑动窗口无边界问题 |
| `FORWARD_DUPLICATE_ALERTS` = False | 保留 | — |
| `NOTIFICATION_COOLDOWN_SECONDS` = 60 | 保留 | — |
| `ENABLE_PERIODIC_REMINDER` = True | 保留 | — |
| `REMINDER_INTERVAL_HOURS` = 6 | 保留 | — |

去重相关：8 个 → 5 个。加上推送系统删除的 OpenClaw 14 个配置项和合并的通知配置，总计减少约 20 个环境变量。

---

## 2. 分模块变更

### 2.1 去重系统

**变更前**：

```
alert_hash 生成
    ├─ 有 _alert_identity → SHA256({source,name,resource,service,fingerprint,severity})
    └─ 无 → SHA256({source, payload})  ← 不可靠, 无告警

查重: Redis → DB(3种返回) → 五层决策表 → 写DB(beyond_window 持久化)
去重: 固定窗口 24h, 硬边界, 30秒容忍patch
```

**变更后**：

```
dedup_key 生成
    ├─ 有 {source, name} → SHA256({source, name, resource?, fingerprint?})
    └─ 无 → SHA256({source, stable_payload_hash}) + metrics 告警

查重: Redis(DedupState, 滑动窗口) → DB fallback → 2层决策
去重: 滑动窗口 4h, 每次重复刷新 last_seen_at
```

**关键代码变化**：

`services/dedup/state.py`（新增）:
```python
@dataclass
class DedupState:
    dedup_key: str
    original_event_id: int
    first_seen_at: float
    last_seen_at: float       # ← 滑动窗口的核心
    count: int
    analysis: dict | None

    def is_active(self, now: float, window_seconds: int) -> bool:
        return (now - self.last_seen_at) <= window_seconds
```

`services/dedup/resolver.py`（新增）:
```python
async def resolve_dedup(dedup_key: str) -> DedupResult:
    # 第 1 层：Redis 去重状态
    state = await get_dedup_state(dedup_key)
    if state and state.is_active(now, window):
        return DedupResult(action=REUSE, ...)

    # 第 2 层：DB fallback
    original = await find_original_event(dedup_key, window)
    if original and has_valid_analysis(original):
        return DedupResult(action=REUSE, ...)

    # 兜底
    return DedupResult(action=NEW)
```

### 2.2 推送系统

**变更前**：

```
2 条通知路径:
  ├─ 告警转发 → Outbox (DB事务 + 幂等 + 重试)
  └─ AI错误/深度分析 → FeishuNotificationChannel 直接 HTTP (无持久化, 无重试)

消息构建: forward_to_remote() 中 is_feishu_url() → build_feishu_card()
          格式化和投递耦合在同一函数中
```

**变更后**：

```
1 条通知路径:
  所有外部消息 → enqueue_external_message() → Outbox → Worker → Channel.send()

消息构建: pipeline 决策阶段通过 Channel.format() 构建好 payload
          存入 Outbox.formatted_payload, Worker 只做 HTTP POST
```

**关键代码变化**：

`services/channels/base.py`（新增）:
```python
class Channel(Protocol):
    name: str
    def supports(self, target_url: str) -> bool: ...
    def format(self, ctx: FormatContext) -> dict: ...
    async def send(self, url: str, payload: dict) -> SendResult: ...

channel_registry: dict[str, Channel] = {}
```

`services/forwarding/enqueue.py`（新增）:
```python
async def enqueue_external_message(
    *, channel_name: str, target_url: str,
    event_type: str, formatted_payload: dict,
    webhook_id: int | None = None,
    idempotency_hint: str = "",
) -> int:
    """唯一的外部消息入口。所有通知都走这里。"""
```

`services/forwarding/deliver.py`（新增）:
```python
async def deliver_outbox(outbox_id: int):
    record = await claim_outbox(outbox_id)
    channel = channel_registry.get(record.channel_name)
    result = await channel.send(record.target_url, record.formatted_payload)
    # 成功/失败处理...
```

### 2.3 Pipeline 简化

**变更前** (`pipeline.py + analysis_resolution.py + decisioning.py`):

```python
# 5 层分析决策
_DECISION_TABLE = (
    REUSE_REDIS_CACHE → REUSE_RECENT_BEYOND_WINDOW →
    REUSE_ORIGINAL_BEYOND_WINDOW → REUSE_IN_WINDOW → ANALYZE
)

# 3 分支转发决策
if is_duplicate: ...
elif beyond_window: ...
else: ...

# P0 bug: high 告警 + 无目标 → should_forward=True 但静默丢弃
```

**变更后**:

```python
# 2 层去重决策
result = await resolve_dedup(dedup_key)
# REUSE → 复用分析, 跳过 AI
# NEW   → AI 分析

# 2 分支转发决策
if result.action == REUSE:
    if in_cooldown: return suppress
    if should_periodic_remind: return forward(periodic=True)
    if not forward_duplicate_alerts: return suppress
else:
    # 新事件正常决策

# P0 修复: 无目标就不该 should_forward
has_target = bool(matched_rules) or bool(default_target_url)
should_forward = (importance == "high" and has_target) or bool(matched_rules)
```

### 2.4 Redis 降级策略

| 组件 | 当前 (fail-close) | 改为 (fail-open) |
|------|-------------------|-----------------|
| `ingress_backpressure.py` | `suppressed=True` 丢弃 | `suppressed=False` 放行 |
| `circuit_breaker.py` | `state=OPEN` 拒绝 | `state=CLOSED` 放行 |
| `tasks.py` 分布式槽位 | 暂停等待 | `asyncio.Semaphore(limit/2)` |

统一降级指标：`UPSTREAM_REDIS_DEGRADED_TOTAL`。

### 2.5 消息构建前置

**变更前** (`forwarding_stage.py` → `outbox.py` → `remote.py`):

```
Pipeline 决策 → Outbox 存原始 webhook_data
    → Worker 取出 → remote.py: is_feishu_url() → build_feishu_card() → HTTP POST
```

**变更后**:

```
Pipeline 决策 → 选 Channel → Channel.format(ctx) → Outbox 存 formatted_payload
    → Worker 取出 → Channel.send(url, payload) → HTTP POST
```

这样 Outbox 记录中的 `formatted_payload` 是已经构建好的最终消息体，Worker 不需要知道飞书/钉钉/Slack 的区别。

---

## 3. 完整的文件变更清单

### 新增文件（13 个）

```
services/
├── dedup/
│   ├── __init__.py
│   ├── state.py              # DedupState dataclass + Redis 读写
│   ├── resolver.py           # resolve_dedup() 2层决策
│   └── key.py                # dedup_key 生成 + 身份校验
├── channels/
│   ├── __init__.py
│   ├── base.py               # Channel Protocol + FormatContext + registry
│   ├── feishu.py             # 飞书 Channel 实现
│   ├── dingtalk.py           # 钉钉 Channel 实现 (可选)
│   ├── webhook.py            # 通用 JSON Channel
│   └── slack.py              # Slack Channel 实现 (可选)
└── forwarding/
    ├── enqueue.py            # enqueue_external_message() 统一入口
    └── deliver.py            # deliver_outbox() Worker 投递逻辑
```

### 修改文件（12 个）

| 文件 | 变更内容 |
|------|---------|
| `services/webhooks/pipeline.py` | `_ProcessingRun` 调用 `resolve_dedup()` 替代 `resolve_analysis()` |
| `services/webhooks/forwarding_stage.py` | 消息格式化前置 (Channel.format)；调用 `enqueue_external_message` |
| `services/webhooks/decisioning.py` | 修复 P0；3 分支 → 2 分支；增加 `match_payload` 匹配 |
| `services/webhooks/noise_stage.py` | 抑制时写 SuppressedRecord |
| `services/webhooks/command_service.py` | 移除 beyond_window 逻辑；简化 `_save_duplicate_event` |
| `services/webhooks/repository.py` | `check_duplicate_event` 简化为 DB fallback 查询 |
| `services/forwarding/outbox.py` | 增加 `channel_name`/`event_type`/`formatted_payload`；EXHAUSTED 通知 |
| `services/forwarding/remote.py` | 大幅缩减，移除渠道判断和消息构建 |
| `core/config/defaults.py` | 删除 3 个废弃配置项；合并通知配置；新增 `DEDUP_WINDOW_SECONDS` |
| `core/circuit_breaker.py` | Redis 不可用时 fail-open |
| `services/webhooks/ingress_backpressure.py` | Redis 不可用时 fail-open |
| `services/operations/tasks.py` | 分布式槽位本地 Semaphore fallback |

### 删除/大幅缩减文件（6 个）

| 文件 | 原因 |
|------|------|
| `services/webhooks/analysis_resolution.py` | 被 `services/dedup/resolver.py` 替代 |
| `adapters/plugins/feishu_card.py` | 合并到 `services/channels/feishu.py` |
| `services/notifications/channels.py` | 被 `services/channels/base.py` 替代 |
| `services/notifications/factory.py` | 被 Channel registry 替代 |
| `services/notifications/target_detection.py` | 逻辑分散到各 Channel 的 `supports()` |

### 数据模型变更

```sql
-- ForwardOutbox 新增
ALTER TABLE forward_outbox ADD COLUMN channel_name VARCHAR(32);
ALTER TABLE forward_outbox ADD COLUMN event_type VARCHAR(32);
ALTER TABLE forward_outbox ADD COLUMN formatted_payload JSONB;

-- ForwardRule 新增
ALTER TABLE forward_rules ADD COLUMN match_payload VARCHAR(512) DEFAULT '';

-- WebhookEvent 新增
ALTER TABLE webhooks ADD COLUMN dedup_key VARCHAR(64);
-- 初始值填充
UPDATE webhooks SET dedup_key = alert_hash WHERE dedup_key IS NULL;

-- WebhookEvent 废弃 (后续 migration 删除)
-- ALTER TABLE webhooks DROP COLUMN beyond_window;
```

---

## 4. 改造前后完整数据流对比

### 改造前

```
POST /webhook
    │
    ▼
API: 签名验证 → 速率限制 → 背压检查(Redis) → process_webhook_task.kiq()
                                                      │
                                                  202 Accepted
                                                      │
                                                      ▼
                                              Worker Pipeline
                                              ┌──────────────────────┐
                                              │ Parse                │
                                              │  → 适配器归一化       │
                                              │  → alert_hash 生成    │
                                              │     (可能不可靠)      │
                                              │                      │
                                              │ Analysis Resolution  │
                                              │  → Redis缓存查重      │
                                              │  → DB查重 (3种状态)  │
                                              │  → 五层决策表         │
                                              │  → AI分析 or 复用     │
                                              │                      │
                                              │ Noise Reduction      │
                                              │  → 相似度计算         │
                                              │  → 抑制标记           │
                                              │                      │
                                              │ Forward Decision     │
                                              │  → 3分支决策          │
                                              │  → P0: high无目标     │
                                              │     静默丢弃          │
                                              │                      │
                                              │ Persist + Outbox     │
                                              │  → DB事务写           │
                                              │  → beyond_window持久化│
                                              └──────────────────────┘
                                                      │
                                              ┌───────┴───────┐
                                         不转发             转发
                                              │               │
                                              ▼               ▼
                                         (结束)      Outbox Worker
                                                      ┌──────────────┐
                                                      │ 取出记录      │
                                                      │ remote.py:    │
                                                      │ is_feishu?    │
                                                      │ build_card    │
                                                      │ HTTP POST     │
                                                      └──────────────┘
                                                  AI错误 → 直接HTTP (无重试)
```

### 改造后

```
POST /webhook
    │
    ▼
API: 签名验证 → 速率限制 → 背压检查(Redis fail-open) → process_webhook_task.kiq()
                                                              │
                                                          202 Accepted
                                                              │
                                                              ▼
                                                      Worker Pipeline
                                                      ┌──────────────────────┐
                                                      │ 1. Parse             │
                                                      │   → 适配器归一化      │
                                                      │   → dedup_key 生成    │
                                                      │     (最小身份保障)    │
                                                      │                      │
                                                      │ 2. Dedup             │
                                                      │   → Redis DedupState │
                                                      │   → DB fallback      │
                                                      │   → 2层决策          │
                                                      │   → 滑动窗口         │
                                                      │                      │
                                                      │ 3. Analyze           │
                                                      │   → NEW: AI分析      │
                                                      │   → REUSE: 复用缓存  │
                                                      │                      │
                                                      │ 4. Noise             │
                                                      │   → 相似度计算       │
                                                      │   → SuppressedRecord │
                                                      │                      │
                                                      │ 5. Decide            │
                                                      │   → 2分支决策        │
                                                      │   → P0已修复         │
                                                      │   → match_payload    │
                                                      │                      │
                                                      │ 6. Build             │
                                                      │   → Channel.format() │
                                                      │   → 格式化payload    │
                                                      │                      │
                                                      │ 7. Outbox            │
                                                      │   → DB事务写         │
                                                      │   → 即时入队         │
                                                      └──────────────────────┘
                                                              │
                                              ┌───────────────┴───────────────┐
                                        should_forward                   不应该转发
                                              │                               │
                                              ▼                               ▼
                                      Outbox Worker                  写 SuppressedRecord
                                      ┌──────────────────┐           (dashboard可见)
                                      │ Channel.send()   │
                                      │ 纯HTTP POST       │
                                      │ + 熔断            │
                                      └──────────────────┘
                                              │
                                      ┌───────┴───────┐
                                    成功              失败
                                   (SENT)      (重试 → EXHAUSTED通知)
```

---

## 5. 迁移计划（6 步）

### Phase 1：基础设施（风险低，不改业务逻辑）

| 步骤 | 内容 | 验收 |
|------|------|------|
| 1.1 | 新增 `services/dedup/` + `DedupState` + Redis 读写 + 单元测试 | 测试通过，不影响现有流程 |
| 1.2 | 新增 `services/channels/base.py` + Channel Protocol + registry | 接口定义完成 |
| 1.3 | 新增 `services/forwarding/enqueue.py` + `deliver.py` | 模块存在，未接入 |

### Phase 2：数据模型兼容变更

| 步骤 | 内容 | 验收 |
|------|------|------|
| 2.1 | ForwardOutbox 新增 3 个字段（nullable，兼容旧记录） | Migration 成功 |
| 2.2 | WebhookEvent 新增 `dedup_key`，填充初始值 = alert_hash | Migration 成功 |
| 2.3 | ForwardRule 新增 `match_payload` | Migration 成功 |

### Phase 3：去重切换（feature flag 控制）

| 步骤 | 内容 | 验收 |
|------|------|------|
| 3.1 | Pipeline 中 `resolve_analysis()` → `resolve_dedup()`（flag 控制） | 新旧逻辑并行可切换 |
| 3.2 | 5 层决策表标记 deprecated，不再新增调用 | 无编译/类型错误 |
| 3.3 | 全量切到 2 层决策，flag 设为默认 | E2E 测试通过 |
| 3.4 | 添加身份降级 metrics | Prometheus 可观测 |

### Phase 4：推送切换

| 步骤 | 内容 | 验收 |
|------|------|------|
| 4.1 | 实现 `FeishuChannel` + `WebhookChannel`，registry 注册 | 单元测试通过 |
| 4.2 | Pipeline 消息构建前置，Outbox 存 formatted_payload | E2E 测试通过 |
| 4.3 | AI 错误通知改为 `enqueue_external_message()` | 通知走 outbox |
| 4.4 | 删除 `services/notifications/` 旧代码 | 无引用报错 |
| 4.5 | 废弃 `forward_to_remote` 中的渠道判断，简化为纯 HTTP | E2E 测试通过 |

### Phase 5：韧性改进

| 步骤 | 内容 | 验收 |
|------|------|------|
| 5.1 | 背压 fail-open | Redis 宕机时 webhook 正常接收 |
| 5.2 | 熔断 fail-open | Redis 宕机时转发正常进行 |
| 5.3 | 分布式槽位本地 fallback | Redis 宕机时 Worker 继续处理 |
| 5.4 | EXHAUSTED 通知 | 重试耗尽时运维收到通知 |

### Phase 6：清理

| 步骤 | 内容 | 验收 |
|------|------|------|
| 6.1 | 删除 `analysis_resolution.py` 五层决策表 | 无引用 |
| 6.2 | 删除 3 个废弃配置项 | 启动不报错 |
| 6.3 | 删除 `beyond_window` 字段 + migration | 列已移除 |
| 6.4 | 删除 `adapters/plugins/feishu_card.py` 等旧文件 | 无引用 |
| 6.5 | 更新文档 | README/架构图反映新设计 |

---

## 6. 风险矩阵

| 风险 | 阶段 | 影响 | 概率 | 缓解 |
|------|------|------|------|------|
| 滑动窗口导致去重组不当延长 | Phase 3 | 中 | 低 | 窗口从 24h 缩到 4h；可配置 |
| Channel registry 替换导致投递失败 | Phase 4 | 高 | 低 | E2E 覆盖飞书全链路；feature flag |
| Redis fail-open 导致风暴 | Phase 5 | 中 | 中 | 本地 Semaphore 限制；指标告警 |
| beyond_window 列删除影响查询 | Phase 6 | 低 | 低 | 先标记 deprecated，观察一个版本 |
| 迁移中配置项不兼容 | Phase 5-6 | 中 | 低 | 废弃配置保留默认值，标记 deprecated |
| formatted_payload 字段容量 | Phase 4 | 低 | 低 | JSONB 无大小限制，飞书卡片 < 10KB |

---

## 7. 预期收益

| 指标 | 改造前 | 改造后 |
|------|--------|--------|
| 对外通知路径 | 2 条（outbox + 直接 HTTP） | 1 条（outbox） |
| 去重决策层数 | 5 层 | 2 层 |
| 事件状态种类 | 3 种（新/窗口内/窗口外） | 2 种（新/重复） |
| 转发决策分支 | 3 分支 | 2 分支 |
| 去重+转发配置项 | 13 个 | 7 个 |
| 新增渠道需要改动的文件 | 3 个 | 1 个（新增 Channel 实现） |
| Redis 故障行为 | 全线停摆 | 降级运行 |
| 分析结果复用代码行数 | ~360 行 | ~80 行 |
| P0 告警静默丢弃 | 存在 | 已修复 |
