# 推送系统重构报告

## 1. 现状描述

### 1.1 当前链路

```
Webhook → API(校验/限流/背压) → Redis Stream → Worker Pipeline
                                                      │
                                          Parse → AI分析 → 降噪 → 转发决策
                                                                      │
                                                              ┌───────┴───────┐
                                                          不转发             转发
                                                          (结束)              │
                                                                      DB事务写入
                                                                   (webhook + outbox)
                                                                             │
                                                                      Outbox Worker
                                                                   ├─ 飞书 → 交互卡片
                                                                   ├─ OpenClaw → 深度分析
                                                                   └─ 通用 Webhook → JSON
```

### 1.2 当前存在两条对外通知路径

| 路径 | 使用的组件 | 可靠性机制 |
|------|-----------|-----------|
| 告警转发 | ForwardOutbox + Worker | DB 事务、幂等键、指数退避重试、过期回收、定时补扫 |
| AI 错误通知 | FeishuNotificationChannel.send_card() 直接调 HTTP | 无持久化、无重试、熔断拒绝即丢失 |
| 深度分析完成通知 | FeishuNotificationChannel.send_deep_analysis() 直接调 HTTP | 同上 |

### 1.3 当前转发决策关键逻辑

`services/webhooks/decisioning.py:216`：
```python
base_should_fwd = importance == "high" or bool(matched_rules)
```

high 重要性告警不需要匹配任何转发规则即可进入转发路径，但 fallback 目标 URL（`DEFAULT_FORWARD_TARGET_URL`）可能为空，导致 `should_forward=True` 却无目标可投递。

---

## 2. 发现的问题

### P0 — high 告警无目标时静默丢弃

**位置**：`decisioning.py:216` + `outbox.py:111-113`

**现象**：high 告警被判定 `should_forward=True`，但没有匹配的转发规则且 default_target_url 为空时，outbox 创建循环直接 `continue` 跳过，不创建任何投递记录。告警静默丢失，无日志，无死信。

**影响**：所有以 high 重要性进来的告警，如果管理员没有配置转发规则且没有设置默认目标 URL，全部丢失。

### P1 — Redis 故障导致全线停摆（fail-close）

**位置**：
- `ingress_backpressure.py:74` — Redis 不可用时 `suppressed=True`，丢弃所有 webhook
- `circuit_breaker.py:77-79` — Redis 不可用时 state=OPEN，拒绝所有转发
- `tasks.py:125-127` — Redis 不可用时暂停处理等待恢复

**现象**：Redis 成为比 PostgreSQL 更关键的单点。一个告警系统在缓存层故障时完全停止告警，违背了它的根本目的。

**影响**：Redis 宕机 = 整个系统瘫痪。告警既收不进来也发不出去。

### P2 — AI 错误通知和深度分析通知无可靠性保障

**位置**：`services/notifications/feishu.py` — `send_card()` 和 `send_deep_analysis()`

**现象**：这两类通知绕过 outbox，直接 HTTP 调飞书。飞书故障或熔断器开启时永久丢失。

**影响**：运维人员可能在 AI 分析已降级数小时后才发现。

### P3 — outbox 即时入队在事务外，崩溃场景有延迟

**位置**：`forwarding_stage.py:223-228`

```python
async with session_scope() as session:
    # 事务在此提交
    ...
await schedule_forward_outbox_many(outbox_ids)  # 事务外入队
```

**现象**：如果进程在 DB 提交成功后、入队前崩溃，outbox 记录停留在 PENDING 状态，需要等定时补扫（默认 300 秒间隔）才能被捡起。

**影响**：崩溃恢复后最多 5 分钟的告警延迟。

### P4 — 消息构建和投递耦合

**位置**：`services/forwarding/remote.py` — `forward_to_remote()`

**现象**：同一函数同时负责判断目标类型（`is_feishu_url`）、构建消息格式（`build_feishu_card`）、执行 HTTP 投递。新增渠道（钉钉、企微、Slack）需要在此函数内加分支。

**影响**：每增一个渠道就要改核心投递逻辑，且 `remote.py` 会持续膨胀。

### P5 — 转发规则匹配维度有限

**位置**：`decisioning.py:85-104` — `_rule_matches()`

**现象**：当前只支持 `match_importance`、`match_source`、`match_duplicate` 三个维度。无法按 payload 内容路由（例如 "labels.namespace=production 的告警走 A 渠道，其他走 B 渠道"）。

### P6 — 降噪抑制完全不可见

**位置**：`noise_stage.py` + `decisioning.py:205-206`

**现象**：当 `noise.suppress_forward=True` 时，告警被静默跳过，不留任何痕迹。运维人员看不到系统抑制了什么。

### P7 — EXHAUSTED 记录无后续处理

**位置**：`outbox.py:400-401`

**现象**：outbox 重试耗尽变为 EXHAUSTED 后永久停止。无通知、无管理端点、无自动恢复。

---

## 3. 重新设计方案

### 3.1 核心原则

1. **统一可靠性** — 所有对外通知走同一条 outbox 路径，不搞特殊通道
2. **渠道可插拔** — 新增渠道注册一个 Channel 实现即可，不改核心代码
3. **基础设施故障时 fail-open** — 宁可多发不漏发
4. **抑制可观测** — 被降噪拦掉的告警留有痕迹

### 3.2 目标架构

```
Pipeline (parse → analyze → noise → decide)
    │
    ├─ noise_suppressed  → 写 SuppressedRecord (轻量记录，dashboard可查)
    ├─ should_not_forward → 结束
    └─ should_forward ──→ Message Builder ──→ Write Outbox ──→ Worker ──→ Channel.send()
         (选channel        (构建好的payload     (DB事务+          (取出      (纯HTTP POST
          选target_url)     存入outbox)         即时入队)         记录)       + 熔断)
```

### 3.3 Channel 插件化

```
services/channels/
├── base.py          # Channel Protocol + registry
├── feishu.py        # 飞书交互卡片
├── dingtalk.py      # 钉钉 Markdown (新增)
├── slack.py         # Slack Block Kit (新增)
├── webhook.py       # 通用 JSON
└── __init__.py
```

#### Channel Protocol

```python
class Channel(Protocol):
    name: str  # "feishu" | "dingtalk" | "slack" | "webhook"

    def supports(self, target_url: str) -> bool: ...

    def format(self, ctx: FormatContext) -> dict:
        """输入标准化上下文，输出可直接 POST 的 JSON payload"""

    async def send(self, url: str, payload: dict) -> SendResult:
        """纯 HTTP POST + 熔断，不做任何业务判断"""
```

#### FormatContext（渠道无关的标准化结构）

```python
@dataclass(frozen=True)
class FormatContext:
    source: str
    importance: str
    event_type: str
    summary: str
    impact: str
    suggestions: list[str]
    timestamp: str
    is_periodic_reminder: bool
    noise_relation: str        # standalone | derived | root_cause
    original_event_id: int | None
```

#### 注册机制

```python
# services/channels/feishu.py
@channel_registry.register
class FeishuChannel:
    name = "feishu"

    def supports(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return host.endswith((".feishu.cn", ".larksuite.com"))

    def format(self, ctx: FormatContext) -> dict:
        # 将 FormatContext 转为飞书交互卡片 payload
        ...

    async def send(self, url: str, payload: dict) -> SendResult:
        # HTTP POST，经过熔断器
        ...
```

### 3.4 统一 Outbox

#### 改动一：存储格式化后的 payload

```python
# 当前 ForwardOutbox 模型新增字段
channel_name: str                 # "feishu" | "dingtalk" | "webhook"
event_type: str                   # "alert_forward" | "ai_error" | "deep_analysis" | "test"
formatted_payload: dict           # 已构建好的消息体，Worker 直接 POST
```

#### 改动二：唯一的外部消息入口

```python
# services/forwarding/enqueue.py

async def enqueue_external_message(
    *,
    channel_name: str,
    target_url: str,
    event_type: str,
    formatted_payload: dict,
    webhook_id: int | None = None,
    idempotency_hint: str = "",
) -> int:
    """所有对外的消息都通过此函数写入 outbox 并即时入队。
    返回 outbox_id。"""
```

AI 错误通知和深度分析通知改为调用此函数写入 outbox，不再直接调 FeishuNotificationChannel。

#### 改动三：Worker 投递逻辑退化

```python
# services/forwarding/deliver.py

async def deliver_outbox(outbox_id: int):
    record = await claim_outbox(outbox_id)
    if not record:
        return

    channel = channel_registry.get(record.channel_name)
    if not channel:
        await finalize_failure(record, f"unknown channel: {record.channel_name}")
        return

    result = await channel.send(record.target_url, record.formatted_payload)

    if result.success:
        await finalize_success(record, result)
    else:
        await finalize_failure(record, result.error)
```

### 3.5 Redis 降级策略：fail-close → fail-open

| 组件 | 当前行为（Redis 不可用） | 改为 |
|------|------------------------|------|
| 背压检查 | `suppressed=True`，丢弃 webhook | `suppressed=False`，放行 + 记录降级指标 |
| 熔断器 | `state=OPEN`，拒绝所有转发 | `state=CLOSED`，放行 + 记录降级指标 |
| 分布式槽位 | 暂停等待 Redis 恢复 | fallback 到本地 `asyncio.Semaphore(limit/2)` |

统一原则：**基础设施故障时牺牲精确性保可用性**。告警系统的第一优先级是把告警发出去。

### 3.6 修复 P0 逻辑漏洞

```python
# decisioning.py — decide_forwarding()

# 修正：high 告警如果没有匹配规则且没有默认目标，不应该 should_forward
has_delivery_target = bool(matched_rules) or bool(policy.default_target_url)
base_should_fwd = (importance == "high" and has_delivery_target) or bool(matched_rules)
```

### 3.7 抑制可见性

```python
# noise_stage.py — 当 suppress_forward=True 时
await write_suppressed_record(
    alert_hash=alert_hash,
    source=source,
    reason=noise.reason,
    related_alert_ids=noise.related_alert_ids,
    confidence=noise.confidence,
)
```

SuppressedRecord 是一个轻量表或 Redis key（短期 TTL），Dashboard 展示"过去 1 小时内抑制了 N 条告警"。

### 3.8 新增转发规则匹配维度

```python
# 在 ForwardRule 模型中增加字段
match_payload: str  # "labels.severity=critical,labels.ns=production"

# decisioning.py
def _payload_matches(rule: ForwardRuleSnapshot, parsed_data: dict) -> bool:
    """支持 key=value 的简单匹配，递归在 payload 中查找 key"""
    if not rule.match_payload:
        return True
    for pair in rule.match_payload.split(","):
        key, _, value = pair.partition("=")
        if _find_in_payload(parsed_data, key.strip()) != value.strip():
            return False
    return True
```

不引入表达式引擎，保持简单。

### 3.9 EXHAUSTED 处理

```python
# outbox.py — _finalize_outbox_failure
if record.attempts >= record.max_attempts:
    record.status = ForwardOutboxStatus.EXHAUSTED
    # 新增：创建一条 AI 错误类型的 outbox 通知运维
    await enqueue_external_message(
        channel_name="feishu",
        target_url=config.DEEP_ANALYSIS_FEISHU_WEBHOOK,
        event_type="ai_error",
        formatted_payload=build_delivery_exhausted_card(record),
    )
```

同时在 admin API 新增 `POST /api/admin/outbox/{id}/retry` 端点。

---

## 4. 文件变更清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `services/channels/__init__.py` | Channel registry |
| `services/channels/base.py` | Channel Protocol + FormatContext + registry |
| `services/channels/feishu.py` | 飞书 Channel 实现（从 remote.py 和 feishu_card.py 抽取） |
| `services/channels/dingtalk.py` | 钉钉 Channel 实现（新增） |
| `services/channels/webhook.py` | 通用 Webhook Channel 实现（从 remote.py 抽取） |
| `services/forwarding/enqueue.py` | 统一外部消息入队入口 |
| `services/forwarding/deliver.py` | Outbox Worker 投递逻辑 |
| `models/suppressed_record.py` | 抑制记录模型 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `services/webhooks/decisioning.py` | 修复 P0 漏洞；增加 `has_delivery_target` 判断 |
| `services/webhooks/forwarding_stage.py` | 消息格式化前置，调用 `enqueue_external_message` |
| `services/forwarding/outbox.py` | 增加 EXHAUSTED 通知；增加 `channel_name`/`event_type`/`formatted_payload` 字段 |
| `services/forwarding/remote.py` | 大幅缩减，移除渠道判断和消息构建逻辑 |
| `services/notifications/feishu.py` | AI 错误通知改为写 outbox |
| `core/config/defaults.py` | 减少通知相关配置项（合并到 outbox 策略） |
| `services/webhooks/noise_stage.py` | 抑制时写 SuppressedRecord |
| `core/circuit_breaker.py` | Redis 不可用时 fail-open（CLOSED） |
| `services/webhooks/ingress_backpressure.py` | Redis 不可用时 fail-open（不抑制） |
| `services/operations/tasks.py` | 分布式槽位增加本地 Semaphore fallback |
| `models/forwarding.py` | ForwardOutbox 新增 `channel_name`、`event_type`、`formatted_payload` |
| `models/forwarding.py` | ForwardRule 新增 `match_payload` |
| `api/forwarding.py` | 转发规则 API 增加 `match_payload` 字段 |
| `api/admin.py` | 新增 outbox retry 端点 |

### 删除/大幅缩减文件

| 文件 | 原因 |
|------|------|
| `adapters/plugins/feishu_card.py` | 逻辑合并到 `services/channels/feishu.py` |
| `services/notifications/channels.py` | 被 `services/channels/base.py` 替代 |
| `services/notifications/factory.py` | 被 Channel registry 替代 |
| `services/notifications/target_detection.py` | 逻辑分散到各 Channel 的 `supports()` |
| `services/notifications/feishu.py` | 投递逻辑挪到 `services/channels/feishu.py`，通知触发逻辑挪到 outbox enqueue |

### 数据库迁移

```sql
ALTER TABLE forward_outbox ADD COLUMN channel_name VARCHAR(32);
ALTER TABLE forward_outbox ADD COLUMN event_type VARCHAR(32);
ALTER TABLE forward_outbox ADD COLUMN formatted_payload JSONB;
ALTER TABLE forward_rules ADD COLUMN match_payload VARCHAR(512) DEFAULT '';
```

---

## 5. 迁移步骤

### Step 1：统一通知路径（最小改动，风险最低）

- 新增 `services/forwarding/enqueue.py`，实现 `enqueue_external_message`
- 修改 `services/notifications/feishu.py`，AI 错误通知和深度分析通知改为写 outbox 而不是直接 HTTP
- 新增 `OutboxEventType` 枚举，ForwardOutbox 新增 `event_type` 字段
- **验收标准**：AI 分析失败后，outbox 中出现 event_type=ai_error 的记录，Worker 正常投递

### Step 2：抽取 Channel 接口（中期重构）

- 新增 `services/channels/` 目录，定义 Channel Protocol + registry
- 实现 `FeishuChannel` 和 `WebhookChannel`，从 `remote.py` 和 `feishu_card.py` 中抽取逻辑
- 修改 outbox Worker 使用 Channel registry 投递
- 删除 `services/notifications/` 目录下的旧文件
- **验收标准**：现有 E2E 测试（飞书转发）仍然通过

### Step 3：Redis 降级策略修改（逐个改）

- `ingress_backpressure.py`：Redis 不可用时返回 `suppressed=False`
- `circuit_breaker.py`：Redis 不可用时返回 `CLOSED`
- `tasks.py`：分布式槽位增加本地 Semaphore fallback
- 每个改动配一个测试
- **验收标准**：Redis 宕机场景下 webhook 仍能接收和转发

### Step 4：修复 P0 + 增量功能

- P0 漏洞修复（high 告警无目标时不进入转发）
- 转发规则新增 `match_payload` 匹配
- EXHAUSTED 通知
- 抑制记录可见性

### Step 5：新渠道（可选）

- 钉钉 Channel 实现
- Slack Channel 实现
- 企微 Channel 实现

---

## 6. 风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|---------|
| ForwardOutbox 新增字段导致迁移失败 | 低 | 新增字段均为 nullable，向后兼容 |
| Channel registry 替换导致投递失败 | 中 | E2E 测试覆盖飞书转发全链路 |
| Redis fail-open 导致风暴 | 中 | 本地 Semaphore 限制并发上限 |
| 消息格式化前置后 payload 过大 | 低 | JSONB 列无大小限制，且格式化后的飞书卡片通常 < 10KB |
| 数据库迁移回滚 | 低 | 新增字段无 NOT NULL 约束，可直接 DROP COLUMN |
