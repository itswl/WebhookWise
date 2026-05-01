## 配置边界：BootConfig vs RuntimePolicy

本项目将配置分为两类：

- 静态配置（BootConfig）：来自 `.env/环境变量`，修改后需重启生效（例如 DB/Redis 连接、并发、超时等）
- 运行时策略（RuntimePolicy）：来自数据库表 `system_configs`，通过 Redis Pub/Sub 同步到各进程内存，修改后无需重启即可生效

为避免开发与排障时出现“到底读的是 env 还是 DB？”的歧义，运行时策略统一通过 `core/config_provider.py` 中的 `policies` 读取：

- `policies.ai.*`
- `policies.retry.*`
- `policies.server.*`

对应实现：

- `core/runtime_config.py`：从 DB 加载与订阅热更新，并写入 `policies`
- `core/config_provider.py`：维护内存镜像与元信息（来源/更新时间/更新者）

## 配置来源追踪接口

管理端提供如下端点用于排障：

- `GET /api/config`：获取当前有效配置（管理端展示用）
- `POST /api/config`：批量更新运行时策略（写入 DB + 广播热更新）
- `GET /api/config/sources`：返回每个 key 的来源与更新时间

`/api/config/sources` 的 `source` 取值：

- `db`：来自 `system_configs`（可热更新）
- `env`：来自 `.env/环境变量`
- `default`：未显式配置，使用默认值

