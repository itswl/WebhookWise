# Changelog

All notable project changes should be summarized here after merge or release.
This project follows SemVer release headings.

## Unreleased

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
