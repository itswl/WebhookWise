# Changelog

All notable project changes should be summarized here after merge or release.
This project follows SemVer release headings.

## [Unreleased]

### Security
- Every place that names a caller — the `[Auth]` warnings, the `[HTTP]`
  request log and the per-IP rate-limit buckets — now resolves the trusted
  forwarded address (`core/request_ip.get_client_ip`) instead of the TCP peer.
  Behind Cloudflare → Caddy → docker-proxy the peer is always the bridge
  gateway, so every stored event carried the same address and every caller
  shared one admin rate-limit bucket. `get_client_ip` tolerates requests
  without an application (test doubles, bare ASGI scopes).
- Untrusted text is neutralized before it enters Feishu `lark_md` (and the
  DingTalk/WeCom markdown) cards: alert bodies and model output could place
  links, `<at id=all>` mentions or fake headings in the operator's card.
- Published ports bind to loopback by default: `API_BIND_ADDRESS` and
  `OBSERVABILITY_BIND_ADDRESS` (default `127.0.0.1`) front the API and the
  nine observability ports, on the assumption of a host reverse proxy; set
  `0.0.0.0` for a bare host.

### Added
- `INCIDENT_SUMMARY_MIN_IMPORTANCE` (runtime-policy, default `low`): the
  lowest incident importance that still earns a paid AI summary. Measured on
  production, summaries were about a fifth of paid model calls and most of
  them described low-importance business-threshold episodes nobody reads;
  `medium` stops paying for those while keeping the reason on the incident.
- Decide a remediation proposal from its Feishu card (#8).
- One-command evaluation stack from published images
  (`docker-compose.quickstart.yml`), with `please-change-` credentials that
  the production startup checks refuse.
- CI runs an experimental, non-gating Python 3.14 leg next to the gating
  3.12 job — the image has shipped 3.14 while every test ran on 3.12.
- Retention for the two unbounded stores in the local observability stack:
  Loki 30 days (compactor + `retention_period`), Pyroscope 30 days with its
  data on the named volume it actually writes to (`/data`).
- `bypass_relay: "true"` on the alert rules that mean WebhookWise or its
  host is down, so they reach chat directly when the relay on the same host
  is down with them.
- The overview opens with a "needs me" block — open incidents (each opens
  its incident), pending Action Center items, dead letters and SLA breaches —
  and says so when the queue is empty; `#/alerts/<id>` now lands on the
  alert's decision chain instead of a collapsed row.
- Chat cards link back to the alert's decision chain (`🔎 查看决策链` →
  `#/alerts/<id>`) when `DASHBOARD_PUBLIC_URL` is set; the setting is now a
  runtime policy shared by alert, digest, incident, proposal and report cards.
- Healthchecks.io and Netdata join the declarative adapter specs, and the
  specs README now lists what ships. Both post an operator-composed body, so
  each spec documents the exact JSON to configure; their recovery words
  (`up`, `CLEAR`) are understood by the incident grouping (#7).
- Dashboard copy: the inbound-rule action `cap_importance` has a label (it
  rendered as a raw key), `critical` importance is labelled, relative times
  have singular forms in English, incident rows say "N alerts" in the page
  language, bare list links follow the primary token instead of the
  browser default, and the language toggle no longer wraps.

- Digest delivery for noisy alert rules: a fourth inbound-rule verb, `digest`,
  whose `action_value` is a window in minutes (5–1440, empty = 60). Chat
  deliveries (Feishu bot / Feishu app / DingTalk / WeCom) for a matching rule
  wait for the window to close and go out as ONE card (`📦 汇总通知`, up to 15
  lines, header coloured by the highest importance); deep-analysis, relay and
  generic webhook targets keep per-alert delivery, and periodic reminders are
  not raised for digested rules. Every alert is still stored, judged and traced
  (`_digest` on the analysis); outbox rows carry `digest_key` /
  `digest_window_end` (migration 0039) and the first row claimed delivers for
  its due siblings. Measured motive: two rules were 56% of a week's volume
  (187 of 331 alerts), each one a card, an incident and a paid summary.

### Fixed
- The AI cost view reads the rates an operator set at runtime. Both rates
  were already live-editable and the pricing path honoured the override, so a
  dashboard edit changed the arithmetic while the view kept quoting the
  shipped defaults and warning that nobody had reconciled them.
- A digest row's delivery age is measured from its window end, not its
  creation: measured from `created_at`, every hourly digest older than the
  30-minute delivery ceiling expired with zero attempts before the window
  closed (seen in production within the first hour of the feature).
- Pyroscope's compactor and metastore directories get named volumes; the
  metastore's raft log no longer lands on a fresh anonymous volume at every
  recreate.
- Outbox creation is race-safe: two workers creating the same forwarding
  intent no longer fail the whole forwarding stage on the `idempotency_key`
  unique constraint; the existing row is reused.
- `scripts/eval/score_severity.py --ai` bootstraps an AppContext, so the live
  provider leg runs in a bare process instead of dying before the first call.
- `.gitignore` covers `.env*.bak*` (a production backup named
  `.env.env.bak-…` was not ignored).
- `.env.example.all` pinned observability images several major versions
  behind the compose defaults (Loki 3.2 vs 3.7, Prometheus 2.55 vs 3.11,
  Pyroscope 1.9 vs 2.0, Grafana 11 vs 13); copying it as `.env` silently
  downgraded the whole stack.
- pre-commit's ruff matches the locked version; CI's shellcheck uses the
  same globs as `scripts/gate.sh`, so the gate script itself and new shell
  scripts are checked.

### Docs
- The twelve documentation screenshots are recaptured from the quickstart
  demo stack instead of a production deployment: synthetic sources only, the
  current navigation and the "needs me" block, and a picture that shows the
  thesis (60 alerts in, 39 suppressed, 21 delivered, 100% delivery success).
- `.agents/README.md`, `docs/capabilities.md` and the observability
  query-tools page no longer describe the removed `.claude/` and `.codex/`
  directories; the Chinese README gains the one-command trial section.

## [0.1.1] - 2026-08-31

The version line restarts at 0.1.x. This repository's public history begins at
the 2026-08-19 recreation, and a 5.0.0 with a single checkable release behind
it overstated what a reader could verify. The recreation release keeps its
content below, renumbered 0.1.0; the pre-recreation entries further down keep
their original 1.x–3.8.0 numbers — they describe a history whose artifacts are
no longer published.

### Security
- Webhook secret rotation with an overlap window (`WEBHOOK_SECRET_PREVIOUS`):
  a second accepted secret across all three auth forms (bearer/token, body
  HMAC, timestamped HMAC) so senders that only their own operators can edit
  migrate without a 401 window. Requests it authenticates are counted under
  the `webhook_auth` / `allowed_previous_secret` security check — the counter
  that says when the overlap can end; startup warns while it is active and
  rejects placeholder values.

### Added
- Shadow-mode AI review of pull requests (`.github/workflows/ai-review.yml`,
  `scripts/ci/ai_review.sh`): one sticky advisory comment per PR, scoped to
  correctness, ingress security, contract drift, and behavioral Chinese
  strings. `continue-on-error` plus a keyless skip path mean it cannot gate a
  merge, a fork PR, or a clone; it is inert until `AI_REVIEW_API_KEY` is set.
- Off/shadow/enforce mode ladder for risky automations
  (`services/operations/feature_modes.py`); first consumers are the AI budget
  brake (`AI_COST_BUDGET_MODE`) and the dedup fingerprint below. Shadow records
  what WOULD have happened; an unknown value degrades to off.
- Per-source dedup fingerprint fields (`DEDUP_FINGERPRINT_FIELDS` +
  `DEDUP_FINGERPRINT_MODE`): a noisy source can name the dot-paths that ARE its
  alert identity; shadow mode counts divergence from the built-in key before
  anything changes. `alert_hash` never moves.
- Synthetic severity evaluation suite (`tests/synthetic/severity/`, 18
  scenarios with ground truth by construction) enforced at 100% in the gate,
  plus `scripts/eval/score_severity.py` to re-measure the live AI provider
  against the same fixtures (`--ai`).
- Same-target readback after every executed Action Center command: a delayed
  worker re-reads the target and records verified / unrecovered / unverifiable
  on the proposal and audit trail (`REMEDIATION_VERIFY_DELAY_SECONDS`);
  unrecovered verdicts surface as a critical Action Center card.
- Reversible identifier masking for AI-bound prompts
  (`AI_PSEUDONYMIZE_*`): estate identifiers leave as stable `anon-*` tokens and
  the answer is unmasked before anything persists; the deep-analysis round-trip
  carries the map on the DeepAnalysis row and clears it on first use.

### Fixed
- The AI request timeout sat 1.7s above the p99 of the calls that actually
  succeeded, so slow-but-fine calls were killed; two alert rules could not
  return to normal and an SLO arithmetic was unreachable.
- The alertmanager bypass route could not start (`url_file` vs an env
  reference the config never expands) and hid it for three weeks; container
  logs are now capped (the json-file driver has no default limit).
- The AI-cost basis reached the client as null; the dashboard now says when a
  hash route is unknown instead of quietly showing Overview.
- `scripts/eval/score_severity.py --ai` bootstraps an AppContext, so the live
  provider leg runs in a bare process instead of dying before the first call.

### Operations
- The nightly database backup that had never been scheduled now is
  (`scripts/ops/backup_ww_db.sh`); dumps record which pg client wrote them,
  because the database container's own older `pg_restore` refuses these
  archives — restore from the application image.
- Agent material consolidated under `.agents/` (operator skills + decision
  notes), with gate checks for note shape and per-machine skill pointers.

## [0.1.0] - 2026-08-19

Originally published as 5.0.0 and renumbered to 0.1.0 on 2026-08-31 when the
project moved to a version line matching its actual public history (see the
0.1.1 preamble). Not a feature list first: the repository this code was
published from is gone. A public-repository audit found an internal
architecture in plain sight — a platform org chart with @-mentionable handles,
object-storage bucket names for prod and dev, internal service names, a
company domain, and two credentials that were still live in production, every
one of them reachable in the git history since the first commit. The history
was rewritten, the repository was recreated to purge the pull-request refs a
rewrite cannot touch, and CI now fails on any of those names returning.

### Added
- **Offline eval for the importance verdict** (`evals/`,
  `scripts/eval_analysis.py`): a frozen corpus of labelled alerts is replayed
  through the analysis engine and held to recorded thresholds in
  `evals/baseline.json`, wired into `scripts/gate.sh` and ci.yml's test job.
  Importance selects forward rules, decides what a silence swallows, and gates
  deep analysis, so a keyword or prompt edit that lowers it used to ship on
  judgement alone. Only one combination gates — the rule engine on the whole
  committed corpus with the keyword policy built from the **committed field
  defaults**, deliberately ignoring `.env`, so CI and a laptop agree and no
  local export can move the gate. `--policy env` and `--engine ai` score and
  report without gating, and say why. Errors are counted by direction:
  `high_recall` is the headline and `max_miss_rate` is pinned at zero, because
  an under-called alert reaches nobody while an over-called one costs
  attention. `export` mines labels from the corrections operators already made
  (`importance_overrides`, `analysis_feedback.corrected_importance`).
- **Corrections generalize to sibling instances**
  (`AI_CORRECTION_PRIOR_ENABLED`, runtime-policy, default off): an importance
  override applies only to the exact condition it was made on, so the same rule
  firing on a new host is a fresh judgement even after operators corrected five
  siblings the same way. When enabled, the prompt states that history as a
  prior — scoped to the same rule and source, only when the corrections agree,
  only above a floor of agreeing corrections, and within a lookback window. The
  analysis records which corrections were shown and whether the model followed
  them (`_correction_prior.followed`); the eval reports `prior_shown` /
  `prior_followed`. It is a prior, not a decision: the model may disagree with
  the payload in front of it, and the exact-hash override still wins afterwards.
- **Approval-gated remediation** (`remediation_proposals`, migration 0033): an
  agent can propose one of the Action Center's commands and nothing happens
  until an operator approves it, at which point it executes through the same
  `run_remediation` path the dashboard button uses — so adding a proposer did
  not widen the set of things that can happen to a deployment. Arguments are
  validated by constructing the executor's own request model, so a proposal
  that could not run cannot be created. Bounded: a required reason, one pending
  proposal per action+resource, a capped queue, and an expiry enforced on read
  and on decision rather than by a sweeper. `approved` (allowed and ran) and
  `failed` (allowed, execution raised) are separate states, and the approve
  endpoint returns 502 for the latter.
- **MCP gains its first write**: `propose_remediation` records a proposal and
  executes nothing; `list_remediation_proposals` reads the queue back. The
  transport authenticates with the read API key, so proposing needs only read
  access while approval needs admin-write — that split is the boundary. The
  agent guide and `docs/reference/mcp.md` no longer claim the surface is
  strictly read-only, because it is not.
- **AI engineering map** (`docs/architecture/ai-engineering.md`): every
  mechanism around the model — cheap-pass routing, structured output, prompt
  injection defence, retrieval, memory, both correction loops, cost governance,
  failure isolation, provenance, the decision trace, the agent surface — with
  the one question asked of each: what consumes it.
- **Triage verdict rides the relay envelope**: the `feishu_relay` processed
  envelope's analysis block carries `triage_verdict`/`triage_confidence`, so
  a shadow consumer (hookjudge) can accumulate three-way comparison data —
  platform importance / platform triage / judge importance.
- **Evidence pack for investigations**: every deep-analysis trigger now ships
  the investigator system-side context — decision trace, 7-day repeat count,
  the prior verdict on the same alert hash, incident membership, and top KB
  hits — fenced as untrusted data in the gateway message. Saves hookprobe a
  round of MCP calls per run; best-effort, never blocks the trigger.
- **KB bulk review**: the drafts view gained per-card checkboxes with
  select-page / publish-selected / discard-selected (sequential on purpose —
  publishing re-embeds server-side). Sixty-six stacked drafts no longer cost
  sixty-six confirm dialogs.
- **Search/filter on four more views**: inbound rules, KB drafts, knowledge
  gaps (paged too), and the rule audit table (summary cards stay
  estate-wide). Runtime settings gained a live filter as well.
- **Operator guide** (`#/guide`, Operations → More): a six-step tour of the
  pipeline — connect a source, route with rules, read the decision trace,
  silence and de-noise, work incidents and handoffs, hook up an AI agent via
  MCP — each card linking to the page where the job is done. Static content,
  fully bilingual.
- **Agent skills in-repo** (`.claude/skills/`): three workflow skills for any
  agent connected to the WebhookWise MCP server — `ww-investigate-alert`
  (why did/didn't alert N notify), `ww-shift-review` (handoff brief), and
  `ww-noise-tuning` (zombie rules, dead silences, repeat clusters). They
  orchestrate the existing read-only tools and defer field semantics to the
  `agent-guide` MCP resource. `.gitignore` now carves `.claude/skills/` out
  of the ignored `.claude/` state.
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
- **A live tour of all 22 MCP capabilities against production** (every tool,
  resource, and prompt) found the triage verdict invisible from the read
  side: list rows never projected it, and `get_ai_analysis` let deep reports
  shadow the quick verdict entirely. List rows now carry
  `triage_verdict`/`triage_confidence`, `get_ai_analysis` returns a
  top-level `quick_analysis` alongside deep reports, and the alert list
  cards show the verdict as a dot badge.
- **An agent usage guide ships as an MCP resource**
  (`webhookwise://reference/agent-guide`): which tool answers which question,
  investigation recipes, field semantics, and the read-only boundary — the
  authoritative copy lives in `api/mcp/agent_guide.py` because docs/ does not
  ship in the image. The `investigate_alert` prompt and the decision-trace
  field guide caught up with `triage_verdict` and `list_incidents`.
- The MCP face caught up with the month's features — four new read-only
  tools: `list_incidents` (grouped alerts with workflow state), 
  `get_handoff_brief` (the shift digest with its paste-ready markdown),
  `get_response_metrics` (global MTTA/MTTR/ack-rate), and
  `list_forward_outbox` (delivery intents with status and last error).
  Analyses returned by the existing tools now carry
  `triage_verdict`/`triage_confidence`. Docs updated (18 tools).

### Changed
- **Runtime settings table rebuilt**: three columns (key+description, value
  with provenance, edit) instead of six — a long keyword CSV used to shove
  the override/effective/edit columns, including the only edit affordance,
  behind an unnoticed horizontal scroll. Long values wrap; overrides show a
  badge, the env default and who changed it; 81 setting descriptions are
  now localized via zh-dictionary overlays (English payload stays the
  contract).
- **Server-copy localization**: action-center items carry `title_key` +
  `title_params` so the dashboard renders localized titles/buttons (English
  payload unchanged); incident cards no longer stack CLOSED + RESOLVED
  badges for the same fact and their labels are localized; the postmortem
  export and KB sediment drafts use Chinese scaffolding (title prefix
  事故复盘, section headings) since both are read by Chinese-language
  operations and retrieved by Chinese queries.
- **Sandbox header** matches the standard title/subtitle/action layout, and
  the terminology is 沙箱 everywhere.
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

### Fixed
- **A failed periodic report stamped the sent-marker anyway**, making the
  loss invisible twice over: startup catch-up saw the marker and stood
  down, and the completion line logged at INFO. A dead fallback webhook
  token silently ate weeks of weekly/monthly reports this way. The marker
  is now recorded only on success (failure logs at ERROR and leaves the
  occurrence for catch-up to retry).
- **Reused recovery analyses kept the firing's red glyph**: a recovery that
  joins its firing's thread inherits the whole analysis, so its summary
  opened with 🔴 on the card body, the dashboard list, the relay envelope
  and the shadow ledger — a green recovery quoting a red glyph. The reuse
  path now swaps the leading status glyph to 🟢 (copy-on-write; wording and
  importance untouched, so routing is unaffected).
- **Reused static-binding ids cross-wired handlers**: the bind helper
  attaches to every match, and sb21–sb24 each belonged to two elements —
  clicking the forward-rules search box invoked clearAuthKeys (instant
  credential wipe, no confirm), and the delivery refresh button also closed
  the incident-resolution modal. Renumbered, plus a contract test banning
  duplicate data-sb ids.
- **`.data-table` was a ghost class** (no CSS definition), leaving the noise
  and rule-audit tables with browser-default centered headers over
  left-aligned cells. Defined, with right-aligned numeric columns.
- **MCP single-lookup envelopes unified**: `get_alert_decision_trace` returns
  `{trace: {...} | null}` and `get_dead_letter_alert` returns
  `{alert: {...} | null}` instead of the SDK-generated `{result: ...}`
  wrapper their `dict | None` annotations used to trigger — every tool now
  answers with a plain object envelope. A test ratchet bans union-`None`
  tool returns so the split shape cannot come back.
- The handoff copy button copied nothing: it handed a STRING to the
  code-block button helper, which walked `.closest()` on it — a silent
  TypeError. A dedicated `wwCopyText` with toast feedback does the job, and
  the delegated-action dispatcher now wraps every handler in try/catch and
  surfaces failures as an error toast — a throwing handler must never
  degrade to a button that does nothing.
- Revoked source connections no longer appear in the wizard's "existing
  connections" list (revoking is the end of that credential's story; the
  audit log keeps the history), and a direct entry to a revoked connection
  resets to the picker instead of re-entering the dead step-4 flow.
- The runtime-settings key column crushed into vertical letter-soup under
  width pressure: `overflow-wrap: anywhere` made its min-content one
  character wide, so the horizontal-scroll wrapper never engaged. Keys are
  nowrap identifiers now; the description column keeps a readable minimum.
- A full 22-destination screenshot sweep against production data, plus a
  ghost-class audit, turned up and fixed: the tonal button classes the
  renderers had used all along (`btn-danger`/`btn-warn`/`btn-ghost` —
  including the danger confirm in `wwConfirm`) existed in no stylesheet and
  rendered as plain buttons — now defined, colour in the point; one more
  ghost `primary` on the inbound page's add button; the alert-quality
  feedback block regressed onto the handoff page's retired classes (now
  stat cards); and the runtime-settings env-default column resolved through
  a hand-maintained map that had fallen 53 keys behind the registry — it now
  reflects over the config groups, with a behavioral parity test.
- Terminology unified in the Chinese UI: incident is 事故 everywhere (nav,
  page titles, Overview/handoff stats, the resolve recap card — the
  incidents.* namespace already said 事故), and silence is 静默 (the nav
  said 静音 while the page said 静默).
- The integration catalog and ingress source-type cards now render
  translated names/descriptions in the Chinese UI (dictionary overlay by id,
  falling back to the server's English for unknown ids).
- A ghost-class ratchet contract freezes today's 48 known unstyled-hook
  classes and fails on any NEW class that no stylesheet defines — the exact
  failure family behind the inbound form, the audit list, the handoff
  window buttons, and the toast palette.
- Revoking a source credential from the ingress wizard now clears the whole
  run and returns to the source picker — it used to stay parked on the dead
  flow (stale payload editor, a lifecycle panel for a credential that no
  longer exists).
- All seven remaining native `window.confirm()` calls replaced with the
  themed dialog (one carried a hardcoded English string, now in the
  dictionaries); a contract keeps native dialogs out for good.
- The delivery queue rendered one `NaN` row instead of data: `/v1/outbox`
  answers `{data: {items, total, next_cursor, has_more}}` — an object — and
  the module concat'd it as the row array. Dead-letter paging also derived
  nothing: that endpoint's pagination has no `has_more`, so the next-page
  button could never appear. Both envelopes are now pinned verbatim in a
  frontend harness.
- The handoff window buttons never showed which window was active (`primary`
  is not a class any stylesheet defines), and the audit list had no `.audit-row`
  layout at all — raw text soup. The page now uses the system idioms: the
  segmented Day/Week/Month-style control, stat cards, the shared `.ww-pre`
  code surface for the markdown brief (replacing a private pre style island),
  and audit rows with action badges.
- The inbound-rule form rendered as bare browser-default boxes — its markup
  used classes that exist nowhere (`.input`, `.btn.primary`, `.mem-bar`).
  Rebuilt on `form-group`/`form-label`/`form-input` with a responsive grid,
  a form title, and `btn-primary`; its native `confirm()` replaced with the
  themed dialog.
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
