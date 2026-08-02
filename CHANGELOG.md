# Changelog

All notable project changes should be summarized here after merge or release.
This project follows SemVer release headings.

## Unreleased

## [3.6.0] - 2026-08-02

### Added
- The runtime settings plane: the 28 operator-policy keys (flapping,
  auto-SLA escalation, backpressure/storm gates, KB card links, noise
  weights, notification cadence, decision-trace retention) are now live-
  editable overrides — `GET/PUT/DELETE /v1/runtime-settings` and a dashboard
  "Runtime Settings" panel (en/zh). Resolution is override > env > default;
  changes propagate to every process within ~1 minute (Redis pub/sub nudge +
  interval refresh) with no restart. Writes are validated against a typed
  registry, audited, and counted in the adoption ledger; reads are sync,
  snapshot-backed, and fail-open (migration 0024).
- Canonical `WEBHOOK_INGRESS_STORM_THRESHOLD/_WINDOW_SECONDS` keys for the
  per-alert storm gate; the legacy dual-duty `PROCESSING_LOCK_FAILFAST_*`
  values remain the fallback so existing deployments are unaffected.

This pays down the dual-config-plane concept debt: 6 of the 8 suppression
mechanisms were env-only; now all 8 are tunable live, and the env/DB split
follows one principle — infrastructure in env, policy in the database.


### Changed
- Removed the consumer-less `DecisionInput`/`decide()` indirection added in
  3.5.1 (the keyword `decide_forwarding` remains the single entry point).
- The sandbox dry-run, handoff summary, and remediation commands now feed the
  feature-adoption ledger, so the observation-period keep/kill review covers
  them with data instead of guesses.


## [3.5.1] - 2026-07-30

### Added
- Open-source readiness: LICENSE (MIT), CONTRIBUTING, SECURITY policy, Code of
  Conduct, issue/PR templates, bilingual README (README.zh.md), badges and
  positioning. The committed deploy prompt containing infrastructure details
  was untracked (`.claude/` is now ignored; note: prior revisions remain in
  git history).
- `scripts/gate.sh`: the local quality gate is now an exact replica of the CI
  test job (bandit, pip-audit, and the OpenAPI contract had each gone red in
  CI while hand-picked local lists passed) — CI-enforced to stay in sync.
- Full-stack e2e for the Feishu card-action closed loop (fake Feishu app API:
  tenant token + message create; signed callback → incident acknowledged →
  idempotent replay → wrong-token 401).
- README "suppression stack" chapter + dashboard decision-trace help block
  (en/zh): the eight gates an alert passes, in order, each mapped to its
  decision-trace skip code. The `flapping` skip code also gained its missing
  dashboard icon/label.

### Changed
- The shared per-source event aggregation now lives in one place
  (`services/webhooks/source_stats.py`); source-health and alert-quality
  consume it (rule-audit keeps its rule-level dimension deliberately).
- Structural: `DecisionInput` value object for forward decisions; periodic
  report tasks split from `tasks.py` (task names unchanged); KB corpus /
  config-bundle / adoption endpoints split from `api/v1/admin.py` into
  `admin_config.py` (paths and auth unchanged); silences API imports hoisted.
- Flapping flip accounting keys each observation uniquely (two flips within
  one millisecond no longer collapse into one zset member).


## [3.5.0] - 2026-07-29

Rolls up the incident-response feature waves shipped since 3.4.0 plus a full
adversarial code-review pass (42 findings; 1 P0, 12 P1) with fixes.

### Added (feature waves since 3.4.0)
- Feishu in-chat incident closed loop: interactive cards with verified,
  signed, idempotent Acknowledge/Resolve/Add-note actions (`/v1/integrations/feishu/card-actions`).
- Incident intelligence (similar incidents, related changes, runbook
  suggestions), manual runbook executions, resolution records, recurrence
  review; change ingestion (`/v1/changes`) with least-privilege tokens.
- Source onboarding: scoped revocable per-source credentials
  (`/v1/source-webhooks/{public_id}`), setup wizard, alert-quality overview.
- Grafana bearer webhooks, dashboard credentials modal, Beyla profile,
  compose split (root `compose.yaml` includes infra + app).

### Fixed (review round)
- Maintenance windows (P0): occurrence identity moved from a comment marker to
  real columns with a schedule digest + partial unique index (migration 0023) —
  editing a live window now retires and re-materializes in one sweep instead of
  silently never muting again that day; concurrent sweeps can no longer create
  duplicates; DST-gap starts keep their real duration; comment edits are
  harmless.
- KB card links: CJK bigram tokenization (Chinese content matching actually
  works) and same-source alone no longer crosses the match threshold — no more
  stapling the two newest same-source docs to every alert card.
- Auto-SLA escalation: acknowledged incidents are never armed nor escalated
  (no more @all pings at the person already working); timers arm from the wall
  clock, not backfilled event timestamps.
- Feishu actions: incident row now locked (`FOR UPDATE`) like the dashboard
  path — concurrent card clicks cannot interleave into a contradictory state;
  message idempotency moved from a non-Feishu `Idempotency-Key` header to the
  body `uuid` field (no duplicate live cards on retry); callback `create_time`
  is mandatory for card actions; action-value TTL default dropped 7d → 1d;
  startup warns when tenant/operator allowlists are empty.
- Flapping: identityless payloads (no rule name) are no longer observed —
  unrelated alerts can't pool flips and mute a whole source; observation moved
  OUT of the persist transaction with a 500ms hard budget (a sick Redis no
  longer stretches every event's DB transaction); adoption counters get a
  250ms budget on request paths.
- Config import: entries validated through the same request schemas as the
  create APIs (a criteria-less silence — i.e. a global mute — invalid weekdays,
  or oversize fields are per-entry errors; DB-level rejects return 400 with the
  report instead of 500); maintenance-materialized silences are not importable.
- Auth: all user-facing token comparisons are bytes-based — a non-ASCII
  Authorization header (or Feishu token) is a clean 401, no longer a 500.
- Alert quality: cheap aggregates moved to SQL GROUP BY; the payload scan is
  capped at 2k rows, yields the event loop, and is cached 60s — opening the
  Quality tab can no longer stall webhook ingress.
- Change impact: before/after windows are queried separately with a
  `truncated` flag — busy windows no longer bias a bad deploy into "fewer
  alerts".
- decision_trace: time-based retention (`DECISION_TRACE_RETENTION_DAYS`,
  default 90) — the table previously grew without bound.
- k8s: worker/scheduler liveness probes now check only a local heartbeat file
  (`healthcheck --live`); the full DB+Redis check moved to readiness — a
  dependency blip degrades readiness instead of triggering a restart storm.
- WeCom/DingTalk: message truncation is byte-aware (the WeCom 4096-byte limit
  was being "guarded" by a character slice that never fired for Chinese).
- Scoped source ingress: Content-Length pre-check before buffering; onboarding
  state advances on a structured `outcome` field instead of sniffing display
  copy; honest `has_more` pagination on onboarding/services lists.
- Test infra: the autouse Redis mock now actually covers feature-adoption /
  flapping / stream probes (module-attribute access instead of from-imports);
  OpenAPI export is CI-enforced (`export_openapi.py --check`) and tracked.

### Added

- Incident response work queue with explainable priority, ownership, SLA-risk,
  and recovery-confirmation buckets.
- Operator-owned resolution drafts, non-blocking completeness guidance, and
  human-confirmed postmortem/knowledge sedimentation.
- Reviewable incident recurrence detection with idempotent confirm and dismiss
  actions that never reopen incidents automatically.
- Knowledge-gap discovery for frequent, severe, or slow incident patterns
  without a proven effective runbook.
- Conservative, service-scoped recommendation calibration from explicit
  feedback and runbook outcomes, while retaining raw scores and explanations.
- Guided inbound-source onboarding with per-source credentials, one-time token
  display, rotation/revocation, first-event evidence, and payload-shape drift
  tracking. Managed-source identity is preserved through deduplication,
  grouping, recurrence, response views, and archival so identical vendors
  remain isolated by connection.
- Read-only Alert Quality Center with explainable source scoring, normalized
  field coverage, recovery matching, identity-churn and stale-recovery checks,
  timestamp validation, Schema drift evidence, unattended-incident visibility,
  and bounded scan metadata. It provides upstream recommendations without
  source mutations or automated repair actions.

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
