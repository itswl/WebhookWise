# 文档地图

这些文档按使用场景分层，避免把架构、运维和参考资料混在一起。

## 架构

| 文档 | 用途 |
| --- | --- |
| [architecture/boundaries.md](architecture/boundaries.md) | 模块 ownership、进程边界、运行契约。 |

## 运维

| 文档 | 用途 |
| --- | --- |
| [operations/observability/overview.md](operations/observability/overview.md) | OTel-first 可观测性架构和指标目录。 |
| [operations/observability/dashboards.md](operations/observability/dashboards.md) | Grafana 大盘覆盖、No data 语义和维护清单。 |
| [operations/observability/query-tools.md](operations/observability/query-tools.md) | PromQL、LogQL、Tempo、Pyroscope 查询 CLI。 |
| [operations/observability/local-lab/README.md](operations/observability/local-lab/README.md) | 本地观测实验入口，含服务覆盖和排查路径。 |
| [operations/troubleshooting.md](operations/troubleshooting.md) | 常见问题排查。 |
| [operations/view-details.md](operations/view-details.md) | 查看事件详情说明。 |

## 参考

| 文档 | 用途 |
| --- | --- |
| [reference/api.md](reference/api.md) | OpenAPI 查看、导出和再生成说明。 |
| [../deploy/k8s/README.md](../deploy/k8s/README.md) | Kubernetes 清单使用说明。 |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 开发和提交流程。 |
| [../CHANGELOG.md](../CHANGELOG.md) | 版本变化记录。 |

## 本地观测实验分册

| 分册 | 用途 |
| --- | --- |
| [local-lab/README.md](operations/observability/local-lab/README.md) | 启动、服务覆盖、统一排查路径。 |
| [local-lab/metrics.md](operations/observability/local-lab/metrics.md) | 业务服务指标和指标解释速查。 |
| [local-lab/logs-traces.md](operations/observability/local-lab/logs-traces.md) | 日志、Trace、Smoke 与告警。 |
| [local-lab/profiling.md](operations/observability/local-lab/profiling.md) | Pyroscope profile 阅读方法。 |
| [local-lab/backends-rum-load.md](operations/observability/local-lab/backends-rum-load.md) | 观测后端、Faro、Beyla 和 k6。 |

阶段性结论、修复过程记录和短期说明尽量留在 Git 历史或 issue/PR 里，避免继续堆进文档目录。
