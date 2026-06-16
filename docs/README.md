# Documentation Map

These documents are layered by use case to avoid mixing architecture, operations, and reference material together.

## Architecture

| Document | Purpose |
| --- | --- |
| [architecture/boundaries.md](architecture/boundaries.md) | Module ownership, process boundaries, and the runtime contract. |

## Operations

| Document | Purpose |
| --- | --- |
| [operations/observability/overview.md](operations/observability/overview.md) | OTel-first observability architecture and metric catalog. |
| [operations/observability/dashboards.md](operations/observability/dashboards.md) | Grafana dashboard coverage, No data semantics, and maintenance checklist. |
| [operations/observability/query-tools.md](operations/observability/query-tools.md) | PromQL, LogQL, Tempo, and Pyroscope query CLI. |
| [operations/observability/local-lab/README.md](operations/observability/local-lab/README.md) | Local observability lab entry point, including service coverage and troubleshooting paths. |
| [operations/troubleshooting.md](operations/troubleshooting.md) | Common issue troubleshooting. |
| [operations/view-details.md](operations/view-details.md) | Notes on viewing event details. |

## Reference

| Document | Purpose |
| --- | --- |
| [reference/api.md](reference/api.md) | OpenAPI viewing, export, and regeneration notes. |
| [../deploy/k8s/README.md](../deploy/k8s/README.md) | Kubernetes manifest usage notes. |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Development and submission process. |
| [../CHANGELOG.md](../CHANGELOG.md) | Version change records. |

## Local Observability Lab Booklets

| Booklet | Purpose |
| --- | --- |
| [local-lab/README.md](operations/observability/local-lab/README.md) | Startup, service coverage, and unified troubleshooting paths. |
| [local-lab/metrics.md](operations/observability/local-lab/metrics.md) | Business service metrics and a metric interpretation cheat sheet. |
| [local-lab/logs-traces.md](operations/observability/local-lab/logs-traces.md) | Logs, traces, smoke, and alerts. |
| [local-lab/profiling.md](operations/observability/local-lab/profiling.md) | How to read Pyroscope profiles. |
| [local-lab/backends-rum-load.md](operations/observability/local-lab/backends-rum-load.md) | Observability backends, Faro, Beyla, and k6. |

Keep interim conclusions, fix-process records, and short-term notes in the Git history or in issues/PRs as much as possible, instead of piling them up in the documentation directory.
