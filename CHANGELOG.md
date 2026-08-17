# Changelog

All notable project changes should be summarized here after merge or release.
This project follows SemVer release headings.

## Unreleased

### Added
- **Triage verdict on every analysis**: the LLM (and the rule fallback) now
  emit `triage_verdict` (`act_now` / `monitor` / `defer`) plus a 0-1
  `triage_confidence` — the act-now-vs-defer answer importance alone does not
  give. Rendered on the Feishu alert card (suppressed on recoveries) and as a
  dot chip in the dashboard's AI report. Display-only: routing decisions do
  not read it yet.
- **Recap card on resolve** (`INCIDENT_RESOLVE_RECAP_ENABLED`, runtime-policy,
  default off): resolving an incident — from the dashboard or the Feishu card
  — queues one idempotent recap card with duration, alert count, resolver,
  and the AI incident summary when it has landed. Assembled from existing
  data; no extra model call. Routed like other system notifications
  (`event_type=incident_resolved` rules win, fallback cascade otherwise).
- **Delivery queue destination** (`#/delivery`, Operations → More): browse
  the forwarding outbox with a status filter and cursor paging, retry
  exhausted rows, and triage dead letters — single / selected / replay-all
  with confirms. The API for all of it existed with no UI reaching it; bulk
  dead-letter replay was API-only.
- **Response metrics on Overview**: global MTTA / MTTR / acknowledgement rate
  over the last 30 days (new `/v1/services/response-metrics`, same arithmetic
  as the per-service profiles) plus a noise-suppressed tile from silence
  debt. Each card drills into its owning view.
- **Feishu card actions grew up**: Acknowledge now actually assigns the
  operator (its confirm dialog had promised this all along; an existing
  assignee is never overwritten), and a new Silence-2h button creates a
  bounded silence scoped to the incident's source/project/environment —
  refused outright when the incident carries no match dimension, because an
  all-empty silence would mute the entire ingress from one card tap.
- **Search + paging on config views**: forward rules and silences get a
  search box and 20-per-page pager (client-side, over the loaded list) via a
  shared `wwFilterPage`/`wwPagerHtml` helper — the views that grow
  monotonically no longer render unboundedly.
- **Real-Redis Lua semantics tests**: the multi-tier rate limiter, the atomic
  dedup read-modify-write, the circuit-breaker state machine, and the AI
  cache now execute against a real Redis in `tests/real_infra/` (env-gated,
  as before) — the mocked `eval` in the main suite returns 1 unconditionally
  and can never exercise them.

### Fixed
- **Every two-part `data-act="Module.method"` action was dead in production**:
  const-declared modules create global lexical bindings with no `window`
  property, and the dispatcher resolved roots via `window[name]` — measured
  in a real browser, deep-analysis retry/forward, decision-trace filters, the
  skip-reason drill and more all resolved to null and did nothing. Modules
  now register themselves in an explicit allowlisted registry
  (`wwRegisterActionRoot`); a parity contract and a resolver harness pin it.
- Webhook ingress now catches `RedisError` on enqueue: a Redis outage used to
  report success in two queue metrics and answer the sender with a bare 500;
  it now returns `503 Retry-After: 10` and counts as `error`, matching the
  queue-backpressure path and the documented delivery semantics.
- Every toast rendered as a green success and lost its first two characters —
  an emoji purge had rewritten the keyword guards to `includes('')`, which is
  always true. Toasts now honour the caller's type (keywords only upgrade the
  default), are styled by tokened CSS classes instead of a frozen inline
  palette, and announce errors as `role="alert"`. New headless harness plus
  a static contract banning empty-string guards keep both regressions out.
- The `feishu_relay` channel built a throwaway `httpx.AsyncClient` per
  delivery — unpooled, honouring `HTTP(S)_PROXY` from the environment, no
  trace headers. It now shares the internal-hop client, which is also closed
  on shutdown (it used to leak on every graceful stop); a contract test bans
  ad-hoc `AsyncClient` construction outside `core/http_client`.
- The primary AI-analysis prompt now neutralizes payload/identity/source text
  and carries an explicit data-not-instructions boundary (deep analysis
  already did): the model's verdict drives forwarding and silencing, so a
  crafted alert body could previously steer its own routing.
- ARIA buttons (`role="button"` spans: the incidents badge, skip-reason drill
  chips) were focusable but dead to Enter/Space; one delegated keydown bridge
  activates them all.
- `Ctrl/Cmd+R` soft-refreshes the view actually open instead of always
  reloading the alerts list; the duplicate close button in the incident
  resolution modal is gone.
- The AI budget brake now counts spend still buffered in-process, closing the
  window where a crash-looping worker systematically under-reported and
  released the brake early.
- `feishu-app://` targets pass rule validation (the channel existed but no
  rule could be saved to it through the API); `SET LOCAL statement_timeout`
  interpolation replaced with a bound `set_config()` call.

### Changed
- OpenAPI metadata is finally the product's: title `WebhookWise`, version from
  `core/version.py` (was `Webhook AI Assistant` v0.1.0), and every operation
  (~120) is grouped by tags instead of one flat list.
- The integration catalog covers every delivery channel — DingTalk, WeCom, and
  Feishu relay gained guided entries (test-before-enable included; the bot
  tests post real markdown payloads, not raw JSON the bots reject) — and
  catalog icons moved from server-supplied emoji to sprite names.
- Production startup now warns (non-fatally) when ingress rate limiting is
  fully disabled, when replay protection is off, and when `KB_ENABLED` runs
  on placeholder embeddings.
- The emoji contract widened (entity decoding, wider Unicode blocks) and
  caught four documented escapes plus two undocumented ones (⌛, 🆕) — all six
  replaced with sprite icons or dropped; a retired-palette contract bans the
  five pre-retheme rgba() triplets.
- The alerts stat cards that count only loaded rows now say so (`(loaded)`);
  untranslated strings in action-center/silences/integrations/overview moved
  into the dictionaries; number/time formatting follows the UI language
  instead of hardcoded `zh-CN`; the legacy OpenClaw engine filter option is
  labelled as legacy.
- README capability tables (EN/中文) now list the runtime-settings plane, the
  read-only MCP server, and WebhookWise Lite.

### Removed
- Four dead sub-view switcher mechanisms (`.nav-tab`, `[data-inbox-view]`,
  `[data-operations-view]`, `[data-dt-view]`, `[data-routing-view]` binders
  and their three empty header containers), the `PILL_PARENT` map and the
  contract test guarding a pill bar that no longer exists, and the no-op
  `[data-ov-period]` binding.

## [3.8.0] - 2026-08-05

### Added
- Bulk workflow actions on the alert list: a checkbox per row plus a
  selection bar (select page / acknowledge / resolve). Clearing twenty stale
  alerts was forty clicks.
- Alert filters live in the URL (`#/alerts?q=&imp=&src=&dup=&ps=&win=`), so a
  filtered investigation is a refresh-proof, shareable address.
- Command palette answers a record id: `245`, `#245`, `alert 245` or `事件 7`
  jump straight to that alert/incident instead of navigate-then-hunt.
- Opt-in desktop notifications for high-importance arrivals, only while the
  tab is hidden, primed on enable so history is never re-announced.
- KB drafts are reviewable and editable before approval: a detail read
  (`GET /v1/admin/kb/drafts/{source_ref}`) and an amend path
  (`PUT`, re-chunked and re-embedded server-side, drafts only).
- Knowledge-gap cards can be left: drills to the incidents/alerts that formed
  the pattern, plus inline runbook authoring that tags the published document
  so the gap detector recognises it and the card's state flips.
- Virtual `processing_status=stuck` on the alert list, expanded to the same
  predicate the Action Center's stuck card counts (shared constants).
- Rarely-used sidebar tier ("More") and a per-destination usage counter
  feeding the adoption review; dashboard build stamp in the rail.
- ESLint (pinned) in both `scripts/gate.sh` and CI; modal focus trap;
  `prefers-reduced-motion` honoured.

### Changed
- **BREAKING (deployment)**: container images run Python 3.14. The source
  floor stays `>=3.12` and the CI unit job stays 3.12 so `scripts/gate.sh`
  remains an exact local replica; 3.14 is exercised by `docker-e2e`.
- **BREAKING (CSP)**: `script-src-attr` tightened from `'unsafe-inline'` to
  `'none'`. All 97 inline event handlers moved to delegated dispatch — markup
  carries a function name plus scalar args, resolved against an allowlist.
  Any future inline handler is now rejected by both a contract and the browser.
- Rule-grain aggregates carry their sending system(s), rendered as a muted
  "· source" suffix.
- One time family across the dashboard (`formatTime` / `formatTimeFull`)
  replacing four dialects; one code-surface design (`--code-*` tokens plus
  `.ww-pre`) replacing a hardcoded palette island and five ad-hoc `<pre>`
  styles; dark is the default by decision rather than by OS preference.
- Dependencies: fastapi 0.141, uvicorn 0.52, redis 8.1, openai 2.52,
  websockets 17, cryptography 50, OTel 1.44, ruff 0.16, and nine GitHub
  Actions. `mcp` is pinned `<2` — MCP 2.0 renames `FastMCP` → `MCPServer`
  and drops the `.settings` surface this server configures transport security
  through, which is a migration, not a bump.

### Fixed
- A cold load of `#/overview` never fetched: `currentDestination` was seeded
  with the default, so the router's "already here" guard swallowed the boot
  navigation and the landing page span forever.
- `decision_trace.alert_name` had been written NULL since 4d866cb (the writer
  read `name` off the forward-match identity, which only carries
  project/region/environment), silently collapsing per-rule analytics back to
  source grain. Migration 0026 refills the gap.
- Three incident row actions rendered as empty pills (the emoji purge deleted
  their glyphs without substituting icons).
- The e2e TLS fixture carried two latent defects that only 3.14 exposed: its
  certifi shadow mounted at a hardcoded `python3.12` path (a silent mount miss
  on any bump — now substituted and contract-bound to the image version), and
  its generated certificates never emitted SKI/AKI/KeyUsage (a non-standard
  chain the older OpenSSL happened to accept).
- Frontend RUM removed: the Faro SDK loaded from a CDN the strict CSP blocks,
  so it never initialized, and its default collector guessed at a local port.

### Removed
- The 2026-05 frontend credential-migration shim, 49 orphaned dictionary keys
  per language (three navigation eras of fossils), and dead CSS.

## [3.7.0] - 2026-08-04

### Removed
- The deprecated ingress-storm fallback to `PROCESSING_LOCK_FAILFAST_*`
  (announced in 3.6.x). `WEBHOOK_INGRESS_STORM_THRESHOLD` /
  `WEBHOOK_INGRESS_STORM_WINDOW_SECONDS` are now the sole storm knobs; their
  defaults changed 0/0 → 20/10 to mirror what the fallback delivered, so
  unconfigured deployments keep the exact same effective gate.
  `PROCESSING_LOCK_FAILFAST_*` remains for its original single duty (the
  per-alert processing-slot fail-fast). The startup deprecation warning is
  gone with the fallback.
- Frontend legacy-credential migration shim (`webhook_api_key` /
  `webhook_admin_write_key` localStorage cleanup from the pre-May token
  storage scheme) and dead CSS/i18n left behind by removed UI (action-count
  badge styles, orphaned dictionary keys).

### Changed
- Version stamped on the dashboard (`<body data-app-version>` + sidebar
  footer) — introduced in the taste pass, first release carrying it.

## [3.6.1] - 2026-08-02

### Added
- Last-resort self-notification: when a forward exhausts its retries the
  process posts one minimal text message DIRECTLY to
  `SELF_NOTIFY_WEBHOOK_URL` (Feishu bot text or generic JSON), bypassing the
  rules/outbox/channel stack that just failed — including when the
  `outbox_exhausted` meta-card itself dies, which previously left a full
  channel outage silent. Rate-limited (`SELF_NOTIFY_MIN_INTERVAL_MINUTES`,
  live-tunable as the 27th runtime-settings key) with a suppressed-failure
  count folded into the next message; fails open to a per-process gate when
  Redis is down; never raises into the delivery path.

### Fixed
- The `[runtime-policy]` tags in the env reference over-promised: the two
  deprecated `PROCESSING_LOCK_FAILFAST_*` keys carried the tag (and the 3.6.0
  notes counted them) but were never in the registry, so they were not in fact
  live-editable. Tags and registry now match exactly in both directions, and a
  contract test keeps them from drifting apart again.

### Changed
- `PROCESSING_LOCK_FAILFAST_*` is now formally deprecated for ingress storm
  control in favor of `WEBHOOK_INGRESS_STORM_THRESHOLD/_WINDOW_SECONDS`: env
  references updated, and a one-shot startup warning fires while the legacy
  fallback is load-bearing. The fallback will be removed in a future release.
- README (en/zh) gained a Runtime Settings operations section; the Runtime
  Contract line claiming config is never DB-overridden now documents the
  runtime-settings exception.

## [3.6.0] - 2026-08-02

### Added
- The runtime settings plane: the 26 operator-policy keys (flapping,
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
