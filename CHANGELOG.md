# Changelog

All notable project changes should be summarized here after merge or release.
This project follows SemVer release headings.

## Unreleased

- Breaking: moved business API and webhook ingestion endpoints to `/v1/*`.
- Added release automation for GHCR image publishing.
- Added explicit runtime version metadata.

## [0.1.0] - 2026-06-03

- Added lock-environment OpenAPI freshness checks and exported API schemas.
- Expanded observability dashboards with log, trace, and profile drilldowns.
- Added Prometheus alert rules and local Alertmanager wiring.
- Improved trace propagation, span status reporting, and profiling docs.
