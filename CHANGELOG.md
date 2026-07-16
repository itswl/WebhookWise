# Changelog

All notable project changes should be summarized here after merge or release.
This project follows SemVer release headings.

## Unreleased

## [3.4.0] - 2026-07-16

### Added
- Maintenance windows (recurring silences): `maintenance_windows` table + CRUD API (`/v1/maintenance-windows`) + dashboard section. A scheduler sweep materializes each active occurrence into a normal expiring silence (tagged `created_by=maintenance-window`, comment marker `[mw:{id}:{date}]`), so suppression accounting/debt keep working; disabling or deleting a window lifts its live silence. Cross-midnight windows and per-window IANA timezones supported (migration 0017).
- Escalation-lite via auto-SLA: `WEBHOOK_INCIDENT_AUTO_SLA_MINUTES` ("high=30,medium=240", default off) arms each incident's SLA from its importance, so the existing breach sweep escalates unacknowledged incidents. Breach cards can @all (`SLA_BREACH_MENTION_ALL`) and route to a dedicated webhook (`SLA_BREACH_FEISHU_WEBHOOK`); the breach is stamped on `incidents.escalated_at` and shown in incident payloads.
- Status-flapping detection: an alert identity (source + rule) oscillating firing↔recovered ≥ `FLAPPING_MIN_TRANSITIONS` flips within `FLAPPING_WINDOW_MINUTES` is flagged (Action Center `flapping_identity` item; always on, fail-open, Redis flip window). Withholding its notifications while it flaps is opt-in (`FLAPPING_SUPPRESS_ENABLED`, decision-trace skip code `flapping`).
- KB → alert cards: outgoing Feishu alert cards attach the best-matching published KB entries as a "相关知识库" runbook block (cheap token matching at delivery time, no LLM call; `KB_CARD_LINKS_ENABLED`, default on).
- Postmortem export: `GET /v1/incidents/{id}/postmortem` renders the incident as a Markdown draft — header facts, member-alert timeline with decision-trace outcomes, ack/escalation/resolution milestones, AI summary sections, recommendations as action items, linked KB entry.
- DingTalk and WeCom bot channels: forward-rule target URLs on `oapi.dingtalk.com/robot/send` / `qyapi.weixin.qq.com/cgi-bin/webhook/send` are auto-detected and delivered as native markdown messages (zero config, same circuit-breaker/idempotency path as other channels).
- Declarative adapter spec library: zabbix, uptime_kuma, aliyun_cms, tencent_cloud_monitor, jenkins, sentry YAML specs ship under `adapters/specs/` with fixture tests.
- Config export/import: `GET /v1/admin/config/export` (YAML bundle of forward rules + active silences + maintenance windows; write-key-gated since it contains bot tokens) and `POST /v1/admin/config/import` (additive upsert by natural key, `dry_run` preview, audit-logged, cache-invalidating).
- Feature-adoption ledger: `GET /v1/admin/feature-adoption` returns monthly action/view counters for recently shipped operator features (Redis hash; the post-release "does anyone use this" instrument).
- Periodic report value lines: interruptions avoided (duplicates absorbed + deliberate suppressions), and new-alert-type count vs the previous window.
- Demo seeding: `python scripts/seed_demo_data.py` posts a realistic mixed batch (dup storm, recoveries, a flapping identity, multi-vendor payloads) through the real ingest path for a 5-minute evaluation.

## [3.3.0] - 2026-07-15

### Added
- Ingest queue backlog is now visible and defensible: a dashboard queue-health tile + `GET /v1/queue-health` expose stream depth, pending, and consumer lag; the Action Center raises a critical item once the unconsumed backlog (lag + pending) crosses `WEBHOOK_MQ_BACKLOG_WARN_FRACTION` of `MAXLEN` (default 0.8) — before the silent trim boundary. The signal is the unconsumed backlog, not total stream length (a busy stream sits at `MAXLEN` of already-acked entries).
- Optional ingress backpressure: above `WEBHOOK_MQ_INGRESS_HIGH_WATER_FRACTION` of `MAXLEN` (default 0, disabled) the API rejects new webhooks with `503 Retry-After` so a retrying upstream holds them, instead of the stream trimming its oldest un-acked entries. Keyed on the cached unconsumed backlog (not total length) and fails open.

## [3.2.0] - 2026-07-15

### Added
- Silence debt report: `GET /v1/silences/debt` ranks active silences by suppression volume over a trailing window and flags "chronic" no-expiry mutes that are hiding a still-firing source; the periodic report gains a matching line. (`get_silence_suppression_counts` now accepts an optional window.)
- Declarative file adapters: onboard a simple JSON webhook source with a YAML spec under `adapters/specs/` (detect conditions + identity field mapping) loaded at startup and registered alongside the built-in adapters — no Python or redeploy-of-code needed. Ships a `generic_json` example and format docs.
- Knowledge-base learning loop: resolved/summarized incidents are sedimented into KB **drafts** (composed from the existing incident summary — no new LLM call) via a scheduled sweep; drafts are excluded from RAG until an operator publishes them. New admin endpoints `GET /v1/admin/kb/drafts`, `POST /v1/admin/kb/drafts/{source_ref}/publish`, `DELETE /v1/admin/kb/drafts/{source_ref}` (migration 0016 adds `kb_documents.status`).
- AI-vs-rules review queue: `GET /v1/decision-traces/ai-disagreements` lists recent alerts where a deterministic rule overrode the AI's importance, as a drill-down for the existing override-rate stat.

### Changed
- `count_with_timeout` rolls its SAVEPOINT back rather than releasing it, so the scoped `statement_timeout` can no longer leak onto later queries in the same request session.
- Unfiltered alert-list totals below 10k rows use an exact COUNT instead of the lagging `pg_class.reltuples` estimate.
- Per-alert payload normalization no longer rebuilds the payload tree (validate-in-place at the adapter boundary), removing a full recursive copy from the ingress and worker paths.
- Pub/Sub cache reloads no longer clobber an invalidation that arrives mid-load; the listener uses redis-py 8 `aclose()`.
- MCP token comparison is done on bytes, so a non-ASCII `Authorization` header returns 401 instead of 500.

## [3.1.0] - 2026-07-14

### Added
- Read-path database indexes (migration 0014) for the alert list, forward-rule health panel, and decision-trace source aggregates; migration 0015 drops now-redundant single-column indexes and orphaned legacy tables.
- Silence backtests report a `scan_truncated` flag and cap the scan at a fixed row budget instead of scanning unbounded history.

### Changed
- Forward-rule hit counts (`get_forward_rule_roi`) are computed over a rolling 90-day window instead of full lifetime; silence suppression counts remain lifetime.
- AI usage records are buffered and flushed in batches rather than written once per call.
- Dashboard assets are versioned by content hash and served immutable with gzip; the HTML entry point is served no-cache. Manual `?v=` cache-busting bumps are no longer needed.
- Dashboard i18n split into per-language files (`i18n.en.js`, `i18n.zh.js`) loaded on demand.
- The circuit breaker emits its state signal only on the CLOSED→OPEN transition; steady-state rejections no longer flood the signal counter or warning logs.
- Ingress/normalization and query paths optimized (projected list columns, less payload re-normalization, cached config parsing on hot forward/DNS paths).
- `TASKIQ_RESULT_TTL_SECONDS` default lowered from 86400 to 3600 to bound Redis result-key and AOF growth.
- Redis runs with an explicit memory cap (`--maxmemory 192mb --maxmemory-policy noeviction`); PostgreSQL preloads `pg_stat_statements` for slow-query visibility.

### Dependencies
- Upgraded FastAPI to 0.139, redis to 8, openai to 2.45, and OpenTelemetry to 1.43.

### CI
- Parallelized tests with pytest-xdist (`-n auto`), added a persistent mypy cache, a Docker buildx GHA layer cache for the e2e image, and requirements-lock floor-satisfaction checks.

## [3.0.0] - 2026-06-04

- Breaking: moved business API and webhook ingestion endpoints to `/v1/*`.
- Added multi-architecture release image publishing to GHCR and Docker Hub.
- Added grouped Dependabot update PRs to reduce dependency-update noise.
- Added explicit runtime version metadata.

## [0.1.0] - 2026-06-03

- Added lock-environment OpenAPI freshness checks and exported API schemas.
- Expanded observability dashboards with log, trace, and profile drilldowns.
- Added Prometheus alert rules and local Alertmanager wiring.
- Improved trace propagation, span status reporting, and profiling docs.
