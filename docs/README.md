# Documentation Map

These documents are layered by use case to avoid mixing architecture, operations, and reference material together.

## Architecture

| Document | Purpose |
| --- | --- |
| [architecture/system-overview.md](architecture/system-overview.md) | Runtime topology, processing sequence, product loop, durable data map, scheduler, observability, and deployment profiles. |
| [architecture/boundaries.md](architecture/boundaries.md) | Module ownership, process boundaries, and the runtime contract. |
| [architecture/ai-engineering.md](architecture/ai-engineering.md) | What runs around the model: routing, guardrails, retrieval, corrections, cost, provenance, the agent surface, and what proves a change. |

## Operations

| Document | Purpose |
| --- | --- |
| [operations/observability/overview.md](operations/observability/overview.md) | OTel-first observability architecture and metric catalog. |
| [operations/observability/dashboards.md](operations/observability/dashboards.md) | Grafana dashboard coverage, No data semantics, and maintenance checklist. |
| [operations/observability/query-tools.md](operations/observability/query-tools.md) | PromQL, LogQL, Tempo, and Pyroscope query CLI. |
| [operations/observability/local-lab/README.md](operations/observability/local-lab/README.md) | Local observability lab entry point, including service coverage and troubleshooting paths. |
| [operations/backup-restore.md](operations/backup-restore.md) | Database backup and restore, retention, and the object-storage upload path. |
| [operations/troubleshooting.md](operations/troubleshooting.md) | Common issue troubleshooting. |
| [operations/view-details.md](operations/view-details.md) | Notes on viewing event details. |

## Reference

| Document | Purpose |
| --- | --- |
| [reference/mcp.md](reference/mcp.md) | The MCP agent surface: tools, resources, and prompt templates. |
| [design-language.md](design-language.md) | Dashboard design system: dark-first tokens, colour-in-points, the icon sprite, the type scale, and the contract tests that enforce them. |
| [reference/api.md](reference/api.md) | OpenAPI viewing, export, and regeneration notes. |
| [features/incident-response-loop.md](features/incident-response-loop.md) | Change impact, service profiles, command summary, manual runbooks, and value reporting. |
| [features/incident-learning-workspace.md](features/incident-learning-workspace.md) | Work queue, structured resolution, recurrence review, knowledge gaps, and recommendation calibration. |
| [features/alert-quality-center.md](features/alert-quality-center.md) | Read-only source scoring, field coverage, recovery matching, identity stability, and schema drift diagnostics. |
| [features/inbound-rules.md](features/inbound-rules.md) | Policy about a named alert rule: skip the model, skip the investigation, or cap its severity — and how to choose a ceiling from the investigator's own verdicts. |
| [features/approval-gated-remediation.md](features/approval-gated-remediation.md) | Proposed Action Center commands: the inert proposal, the human approval, the bounds, and the states. |
| [integrations/change-events.md](integrations/change-events.md) | Change-event contract and CI/CD integration examples. |
| [integrations/inbound-source-onboarding.md](integrations/inbound-source-onboarding.md) | Source-scoped inbound credentials, first-event verification, rotation, and revocation. |
| [integrations/feishu-interactive-cards.md](integrations/feishu-interactive-cards.md) | Optional Feishu custom-app cards with signed, idempotent incident actions. |
| [integrations/deep-analysis-engine.md](integrations/deep-analysis-engine.md) | The two-endpoint investigator contract, the gateway dialects, why this leg has its own outbound client, and how to verify a round trip. |
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
