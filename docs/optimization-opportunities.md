# 全链路优化建议

> 补充 [整体重构方案](./refactoring-overall.md) 中未覆盖的其他环节。

---

## 1. Worker 进程适配器未初始化（Bug）

### 现象

`entrypoint.sh` 中 worker 模式启动：
```bash
taskiq worker services.operations.taskiq_wiring:broker
```

TaskIQ 导入 `taskiq_wiring.py`，触发 `_tasks` 模块导入，间接拉起了 `pipeline.py → request_parser.py → ecosystem_adapters.py` 的导入链。但 `initialize_adapters()` —— 实际调用 `register_simple_adapters()` 和 `registry.auto_discover()` 的函数 —— **只在 API 进程的 lifespan 中调用**（`start_runtime_services()`），Worker 进程不会调用它。

### 影响

Worker 处理 webhook 时，`normalize_webhook_event()` 找不到任何适配器，所有 payload 进入 passthrough 路径——没有 `_alert_identity`。导致：

- `generate_alert_hash()` 降级为 `SHA256(source + 整个payload)`，包含动态时间戳和变化字段
- 去重完全失效：同一条 Prometheus 告警每次产生的 alert_hash 都不同
- AI 分析每次都被触发，缓存复用率为零
- AI 成本飙升，但运维从 metrics 中看不到任何异常（降级没有暴露为指标）

### 修复

`worker.py` 的 `startup()` 或 `taskiq_wiring.py` 中加一行：

```python
# worker.py startup()
from adapters.ecosystem_adapters import initialize_adapters
initialize_adapters()
```

或者更彻底：在 `taskiq_wiring.py` 中直接调用（因为这个模块无论如何都会被 worker 进程导入）：

```python
# services/operations/taskiq_wiring.py
from adapters.ecosystem_adapters import initialize_adapters
initialize_adapters()  # 确保 worker 进程也有适配器
```

---

## 2. AppContext 全局单例 + 惰性初始化 = 脆弱的启动顺序

### 现象

`AppContext`（`core/app_context.py`）管理 DB 引擎、Redis 客户端、HTTP 客户端三个重量级资源。资源创建是惰性的（`ensure_*` 方法），但全局单例的访问模式各有不同：

```python
# API 进程：lifespan 中显式创建
context = get_or_create_default_app_context(config)
set_default_app_context(context)

# Worker 进程：直接在模块顶层创建
context = AppContext(config=UnifiedConfigManager())
set_default_app_context(context)

# 各种 service 中：通过 get_default_config() 间接访问
from core.app_context import get_default_config
config = get_default_config()
```

三个问题：

- **`_default_context` 是模块级可变全局变量** — 同一进程中所有协程共享，但 Python 的 contextvars 和模块级变量是不同的隔离层次
- **`get_or_create_default_app_context` 在无参调用时创建一个新的 `UnifiedConfigManager()`** — 它会读 `.env` 文件、执行 `model_validator`，这在 `worker.py` 的 `shutdown()` 函数中也被调用了：`get_or_create_default_app_context()` 只是为了拿到 context 来 close，但它会重新读配置、建对象
- **`ensure_redis_client()` 是同步的** — `context.ensure_redis_client()` 不等待 Redis 就绪，但 `ensure_http_client()` 和 `ensure_db()` 是异步的。不一致

### 修复

- 把 `_default_context` 改为 `ContextVar[AppContext]`，消除模块级可变状态
- 所有 `get_default_config()` 的调用点改用显式参数传递（在已有重构中随带修改）
- `ensure_redis_client()` 保持同步（Redis 客户端创建本身不涉及 I/O），但调用方需要处理 Redis 不可用的情况（见 fail-open 改造）

---

## 3. 日志 JSON 格式在 import 时执行副作用

### 现象

`core/logger.py:226`：

```python
logger = setup_logger()  # 模块 import 时执行
```

副作用包括：
- 读取配置文件获取 `LOG_FILE` 路径
- 创建 `RotatingFileHandler`
- 启动 `QueueListener` 后台线程
- 注册 `TraceIdFilter`

如果 `setup_logger()` 在 fork 之后被调用（如 Gunicorn 的 post-fork），这没问题。但如果在 preload 时被 import（模块被提前加载），fork 后子进程会继承失效的 `QueueListener` 线程。虽然代码中有 PID 检测（`_REUSE_LOGGER_STATE` 逻辑），但这增加了心智负担。

### 修复

不在 import 时调 `setup_logger()`，改为在 `start_runtime_services()` 中显式调用 `setup_logger()`。所有 logger 使用者通过 `get_logger(name)` 获取子 logger（这不触发 setup）。

---

## 4. API 层请求体被重复读取

### 现象

```python
# 依赖链：
check_rate_limit_dep   → 不读 body
verify_webhook_auth_dep → await request.body()  # 第 1 次
_receive_and_enqueue_webhook → await request.body()  # 第 2 次

# verify_webhook_auth_dep 中：
raw_body = await request.body()
headers = dict(request.headers)
ensure_webhook_auth(headers, raw_body, ...)
# raw_body 已读取但只用于签名验证，没有被传递到下游
```

虽然 Starlette 缓存了 `request.body()` 的结果（第二次调用返回缓存值，不会重复读取 socket），但签名验证中已经拿到了 body bytes，而路由处理函数又重新从 request 中取了一遍。如果 Starlette 版本没有缓存（早期版本），这里会死锁或丢数据。

### 修复

`verify_webhook_auth_dep` 返回 body bytes 或使用 `request.state` 传递已验证的 body：

```python
async def verify_webhook_auth_dep(request: Request, config=...) -> bytes:
    raw_body = await request.body()
    # ... 验证 ...
    return raw_body

# 路由中
async def receive_webhook(request: Request, raw_body: bytes = Depends(verify_webhook_auth_dep)):
    # 直接用 raw_body，不再读 request.body()
```

或者更简单：把签名验证和 body 读取合并到一个依赖中，下游只传递 `headers` 和 `raw_body_str`。

---

## 5. Pipeline 中指标记录模板代码过多

### 现象

`pipeline.py` 每个步骤都有重复的模式：

```python
# parse
parse_start = time.perf_counter()
parse_outcome = "success"
with otel_span("webhook.parse", ...):
    try:
        req_ctx = parse_request(...)
    except Exception:
        parse_outcome = "error"
        raise
    finally:
        _record_step_metrics("parse", metric_source, parse_outcome, parse_start)

# analysis
analysis_started = time.perf_counter()
outcome = "success"
with otel_span("webhook.analyze", ...):
    try:
        analysis_res = await resolve_analysis(...)
    except Exception:
        outcome = "error"
        raise
    finally:
        _record_step_metrics("analysis", ...)

# noise — 同样的模式
# persist — 同样的模式
```

每个步骤 10-12 行模板代码。如果加一个新步骤或修改 tracing 策略，需要复制粘贴 4 处。

### 修复

抽一个简单的 context manager：

```python
@asynccontextmanager
async def pipeline_step(name: str, source: str, span_attrs: dict = None):
    started = time.perf_counter()
    outcome = "success"
    with otel_span(f"webhook.{name}", span_attrs or {}):
        try:
            yield
        except Exception:
            outcome = "error"
            raise
        finally:
            WEBHOOK_PIPELINE_STEP_TOTAL.labels(name, source, outcome).inc()
            WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels(name, source, outcome).observe(
                time.perf_counter() - started
            )

# 使用
async with pipeline_step("parse", metric_source):
    req_ctx = parse_request(...)
```

`pipeline.py` 的 `_handle_raw_ingest` 从 90 行缩减到 ~50 行。

---

## 6. 速率限制 fail-open，但背压检查 fail-close（不一致）

### 现象

```python
# webhook_security.py:208 — 速率限制异常时降级放行
except Exception as e:
    logger.error("限流检查异常（降级放行）: %s", e)

# ingress_backpressure.py:74-75 — 背压检查异常时直接抑制
if not await ensure_redis_available(...):
    return IngressBackpressureResult(True, key, 0, threshold, reason="redis_unavailable")
```

同一个 Redis 不可用的场景，两个组件行为相反。虽然已经在整体重构报告中计划统一改 fail-open，但这个不一致本身就是一个潜在的生产问题。

---

## 7. Pipeline 日志级别不一致

### 现象

去重复用的日志：

```python
# pipeline.py:200
logger.info("[Pipeline] 分析结果复用(redis) event_id=%s ...")
```

降噪抑制的日志：

```python
# noise_stage.py:63
logger.info("[Noise] 抑制转发 relation=%s root_cause_id=%s ...")
```

去重 miss 进入 AI 分析：

```python
# pipeline.py:221
logger.info("[Pipeline] 分析完成 event_id=%s ...")
```

这些都打在 INFO 级别。如果系统每天处理 10 万条 webhook，其中 8 万条是去重复用的，会产生大量 INFO 日志。去重复用应该降到 DEBUG 级别，新事件和抑制事件保留 INFO。这样运维可以快速扫描 INFO 日志定位真正需要关注的事件。

---

## 8. 缺少 pipeline 级别的健康检查

### 现象

当前健康检查：

```
/live  → 进程存活
/ready → DB + Redis ping
```

没有端到端的 pipeline 健康检查。无法回答"系统能否成功处理一条 webhook"。如果 AI API key 过期、适配器挂了、或 Worker 积压严重，`/ready` 仍然返回 200。

### 建议

加一个 `/health/deep` 端点（带 admin 认证），做以下检查：

1. DB 连接
2. Redis 连接 + Stream 状态
3. Worker 队列积压（pending count）
4. AI API 可达性（发一个最小请求，可选）
5. 最近 N 分钟内的处理成功率

不做主动检查，只是暴露指标供外部监控系统决策。

---

## 9. 冷启动时的并发尖峰风险

### 现象

当前 Worker 有本地 + 分布式双层并发控制。但如果 Worker 进程重启（例如部署新版本），Redis Stream 中积压的消息会被瞬间取出处理。分布式槽位限制（`MAX_CONCURRENT_WEBHOOK_TASKS=30`）有效，但如果有很多 Worker 实例同时重启，每个都会尝试抢占槽位。

### 建议

在 Worker 启动时加一个随机的 warm-up delay（0-5s），错开多个 Worker 同时抢槽位：

```python
# worker.py startup()
jitter = random.uniform(0, 5)
await asyncio.sleep(jitter)
await broker.startup()
```

这不是大问题（槽位本身就是并发控制），但在大规模部署时可以减少 Redis 的瞬时压力。

---

## 10. 压缩阈值可能与实际 payload 大小不匹配

### 现象

```python
# compression.py
COMPRESS_THRESHOLD_BYTES = 4096
```

Prometheus webhook payload 通常在 2KB-10KB 之间。4KB 以下的 payload 不压缩，直接存 bytes。但 `WebhookEvent.raw_payload` 是 `LargeBinary`，PostgreSQL 的 TOAST 机制会在 2KB 左右触发。所以 2KB-4KB 之间的 payload 不会被 Zstd 压缩也不会被 TOAST 压缩，两者都没享受到。

### 建议

把 `COMPRESS_THRESHOLD_BYTES` 降到 2048 或者改为可配置。

---

## 11. 建议新增的 metrics

当前已有 40+ 个 Prometheus 指标，覆盖质量很高。以下是缺失的关键指标：

| 指标 | 用途 |
|------|------|
| `DEDUP_HIT_RATE` | 去重命中率 (redis_hits / total_lookups)，监控去重是否有效 |
| `IDENTITY_DEGRADED_TOTAL` | 适配器未产出 identity 的次数，监控 hash 质量 |
| `AI_ANALYSIS_REUSE_RATE` | 分析结果复用率 (reused / total)，监控缓存效率 |
| `PIPELINE_BACKLOG_DEPTH` | Worker 队列积压深度 |
| `OUTBOX_AGE_MAX_SECONDS` | 最老的未投递 outbox 记录的年龄 |

---

## 12. 其他小问题

| # | 问题 | 位置 | 建议 |
|---|------|------|------|
| 1 | `worker.py` 的 `shutdown()` 中调用 `get_or_create_default_app_context()` 可能创建不必要的实例 | `worker.py:35` | 存为局部变量，shutdown 时复用 |
| 2 | `TraceContextMiddleware` 的 `finally` 中对 `/live`、`/ready` 做了特殊处理不记录日志，但对 OTEL tracing span 没有对应的过滤 | `middleware.py:165` | 保持一致：health check 路径也不创建 root span |
| 3 | `SecurityHeadersMiddleware` 的 HSTS 头强制了 `includeSubDomains`，对纯 API 服务不必要 | `middleware.py:29` | 改为可配置或默认不加 `includeSubDomains` |
| 4 | `WebhookEvent.fill_fields()` 用 setattr + 字段白名单，比直接赋值慢，且每次写入都更新 `updated_at` | `models/webhook.py:65-79` | 简单场景下直接用构造函数 |
| 5 | `request_parser.py` 在 payload 为空且 raw_body 非空时解析 JSON，但 raw_body 可能已经被解码又编码多次 | `request_parser.py:19-21` | 在 API 层传递已解析的 dict，不要反复序列化 |
| 6 | `entrypoint.sh` 的 jemalloc 加载逻辑使用了 `LD_PRELOAD`，在 musl libc 环境下可能失败 | `entrypoint.sh:7-9` | 检查 `ldd` 输出或忽略加载失败 |

---

## 优先级汇总

| 优先级 | 问题 | 影响 |
|--------|------|------|
| **P0** | Worker 适配器未初始化 | 去重全线失效 + AI 成本飙升 |
| **P1** | 双重 body 读取 | 潜在的 Starlette 兼容性问题 |
| **P1** | 日志 body 复用打到 INFO | 高吞吐时日志爆炸 |
| **P2** | Pipeline 模板代码过多 | 维护负担 |
| **P2** | backpressure/rate-limit fail 行为不一致 | 运维困惑 |
| **P2** | 冷启动并发尖峰 | Redis 瞬时压力 |
| **P3** | 缺少 pipeline 健康检查 | 生产排障效率 |
| **P3** | 压缩阈值不够精确 | 存储效率 |
| **P3** | 其他小问题 | 代码质量 |
