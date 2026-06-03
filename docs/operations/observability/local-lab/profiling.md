# 本地可观测实验手册：Profile

[返回总览](README.md)

## 看 Profile

Pyroscope 直接入口：

```text
http://localhost:4040
```

Grafana 里也可以用 Profiles / Pyroscope datasource。Pyroscope 回答的是：

```text
这段时间内，进程 CPU 主要花在哪些函数调用栈上？
```

它不是日志，也不是单个请求 trace。它是连续采样，把一段时间内的调用栈聚合成 top table 和 flamegraph。Metrics / Trace 告诉你哪里慢，Pyroscope 帮你看慢是不是 CPU 热点，以及热点具体落在哪些函数。

当前本地栈为三个 Python 进程打开 profile：

- `webhookwise-api`
- `webhookwise-worker`
- `webhookwise-scheduler`

Pyroscope 页面左上角应用下拉里要选这些业务服务。不要误选 `pyroscope`，否则看到的是 Pyroscope 后端自己。比如 `internal/runtime/syscall.Syscall6` 是 Go runtime 的系统调用 wrapper，表示 Pyroscope 后端在跟操作系统交互，不是 WebhookWise 业务热点。

常用查询形态：

```text
{service_name="webhookwise-api", profile_type="process_cpu:cpu:nanoseconds:cpu:nanoseconds"}
{service_name="webhookwise-worker", profile_type="process_cpu:cpu:nanoseconds:cpu:nanoseconds"}
{service_name="webhookwise-scheduler", profile_type="process_cpu:cpu:nanoseconds:cpu:nanoseconds"}
```

优先在以下场景看 profile：

- API p95/p99 变高，但 DB/Redis/AI 没明显慢调用。
- worker 队列积压，同时 CPU 明显升高。
- scheduler 任务 duration 变长，但日志里没有错误。

### Pyroscope 页面怎么读

| 区域 | 怎么读 | 说明 |
| --- | --- | --- |
| `CPU CORES` 折线 | 进程 CPU 使用量 | `250m` 是 0.25 core，`500m` 是 0.5 core，`1` 是 1 core |
| query 输入框 | 当前 profile selector | 重点确认 `service_name` 是否是业务服务 |
| 时间范围 | profile 聚合窗口 | 跑压测或复现问题后，优先切到对应的 5m/15m 窗口 |
| Top Table | 函数排行榜 | 可按 `Self` 或 `Total` 排序 |
| Flamegraph | 调用栈宽度图 | 横向越宽代表累计采样越多，颜色不代表严重程度 |

Top Table 的两个时间列很关键：

| 列 | 含义 | 排查用法 |
| --- | --- | --- |
| `Self` | 函数自己消耗的 CPU 时间，不含子调用 | 适合找真正自己在烧 CPU 的函数 |
| `Total` | 函数自己加所有子调用的 CPU 时间 | 适合找热点所在的大分支 |

如果一个函数 `Self=0` 但 `Total` 很大，它通常只是父级调用链，例如进程启动、worker 框架、线程入口，不代表它自己慢。需要继续往下钻子函数。

Flamegraph 的读法：

- 最上面的 `total` 是当前时间窗口内采样聚合到的 CPU 时间，不是进程运行总时长。
- 横向越宽，表示这个函数及其子调用占用的 CPU 样本越多。
- 纵向表示调用深度，下面是入口，上面是更深的函数。
- 颜色只用于区分块，不表示红色就是异常。
- 无流量时 profile 很容易被后台线程、采集线程、sleep 循环占据。

### 常见函数名怎么理解

| 函数 / 前缀 | 通常含义 | 是否业务热点 |
| --- | --- | --- |
| `internal/runtime/*`、`runtime.*` | Go 或语言 runtime 底层函数 | 通常不是 WebhookWise 业务代码 |
| `Runner.run`、`sleep` | Pyroscope Python agent 的采样/上传后台循环，或常驻等待 | 通常不是业务热点 |
| `MetricReaderStorage.collect`、`PeriodicExportingMetricReader.collect` | OpenTelemetry metrics 定期采集 | 可观测自身开销 |
| `OTLPMetricExporter`、`encode_metric`、`_encode_*` | OTel metrics 编码和导出到 Alloy | 可观测自身开销 |
| `Session.prepare_request`、`parse_url`、`get_netrc_auth` | Python HTTP client 为 exporter 准备请求 | 多数是 OTel/Pyroscope 上报链路 |
| `BaseProcess.run`、`ForkProcess`、`ProcessManager.*` | worker 进程管理和启动框架 | 父级调用链，不直接代表业务慢 |
| `RedisStreamBroker.listen` | worker 监听 Redis Stream 任务 | 和队列消费有关，值得结合 queue 指标看 |
| `BatchProcessor.worker` | OTel batch processor 后台导出线程 | 可观测自身开销 |
| `_async_traced_execute_factory...` | 被 tracing 包装后的异步任务执行入口 | 值得继续往下钻业务函数 |
| `services/...`、`api/...`、`core/...`、`db/...` | WebhookWise 业务代码 | 重点关注 |

### API profile 示例怎么判断

如果选择 `webhookwise-api` 后，看到类似：

```text
Runner.run
sleep
Server.on_tick
Server.main_loop
MetricReaderStorage.collect
PeriodicExportingMetricReader.collect
OTLPMetricExporter
encode_metric
```

通常说明当前 API 没有明显业务 CPU 热点，页面主要采到了：

- Pyroscope agent 自己的采样/上传循环。
- OpenTelemetry metrics 定时导出。
- 服务运行循环。

这类图在本地空闲或轻流量时很常见，不代表 API 有业务性能问题。要看真实业务热点，先跑 k6 或手动打 webhook 流量，再把时间范围切到压测那几分钟。

### Worker profile 示例怎么判断

如果选择 `webhookwise-worker` 后，`CPU CORES` 偶尔冲高，比如接近 2 cores，但 Top Table 主要是：

```text
start_listen
BaseProcess.run
ProcessManager.start
run_worker
RedisStreamBroker.listen
BatchProcessor.worker
_async_traced_execute_factory...
```

可以这样读：

- `BaseProcess.*`、`ProcessManager.*`、`run_worker` 是 worker 框架和父级调用链，`Total` 大不等于它们自己慢。
- `start_listen`、`RedisStreamBroker.listen` 表示 worker 正在监听和分发 Redis Stream 任务。
- `BatchProcessor.worker` 多半是 OTel 后台导出，不是业务处理。
- `_async_traced_execute_factory...` 更接近任务执行入口，需要点进去或搜索业务模块名。

继续在搜索框里搜：

```text
services
webhooks
operations
forward
analysis
redis
sqlalchemy
json
```

如果能搜到 `services/operations/tasks.py`、`services/webhooks/...`、`services/forwarding/...` 且块很宽，才说明热点落在业务代码。若 worker queue lag 高但业务函数不宽，瓶颈可能在 DB、Redis、AI、HTTP 转发等等待型 IO，此时应回到 Trace 和 duration 指标。

### Profile 和其他信号一起用

| 现象 | Pyroscope 里看到 | 下一步 |
| --- | --- | --- |
| API p95 高，profile 里业务函数很宽 | CPU 热点 | 优化对应函数、减少解析/计算、缓存结果 |
| API p95 高，profile 里业务函数不宽 | 可能是 IO 等待 | 看 Tempo、DB/Redis/AI/Forwarding duration |
| worker queue lag 高，worker CPU 也高 | worker 计算忙 | 找宽的 `services/...` 分支，优化或扩 worker |
| worker queue lag 高，worker profile 不宽 | worker 在等外部依赖或拿不到任务 | 看 Redis、DB、AI、Forwarding、worker 日志 |
| scheduler duration 高，profile 不宽 | 多半是扫描查询或外部等待 | 看 scheduler trace、DB session duration、日志 |
| Top Table 主要是 OTel/Pyroscope 函数 | 可观测后台成本占主导 | 增加业务流量后再看，或调大采样/导出间隔 |

![Pyroscope flamegraph](assets/pyroscope-ui.jpg)

