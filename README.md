# WebhookWise

**English** | [中文](README.zh.md)

[![CI](https://github.com/itswl/WebhookWise/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/itswl/WebhookWise/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/itswl/WebhookWise)](https://github.com/itswl/WebhookWise/releases)

*Self-hosted alert intelligence between your monitoring and your chat — dedup, noise reduction, AI triage, and a decision trace that can explain every notification (or its absence).*
WebhookWise sits between your monitoring and your chat. It normalizes events
from Prometheus, Grafana, Alertmanager, Feishu or anything that can POST JSON,
judges each one, decides who to tell — and records why, so "why didn't I get
paged?" has an answer that is not a guess.

Self-hosted, MIT, one `docker compose up`.

Running in production at two companies for eight months, across tens of
thousands of real alerts. Every figure and screenshot here comes from the
deployment that is running now — not a benchmark, and not a demo seed.

![The dashboard: how the gateway itself is doing, on one screen](docs/img/01-overview.png)

## The problem it turned out to have

Every alerting tool claims to reduce noise. This one can show whether it did.

An agentic investigator reads the alerts that earn it — searching, correlating,
reasoning for minutes — and returns a report with its own severity. That makes a
labelled set nobody had to annotate, and the first time it was scored the answer
was unflattering:

| | |
| --- | --- |
| Alerts WebhookWise filed `high` | **330 / 367 in a week (90%)** |
| Of those investigated, the investigator agreed | **21 / 80 (26%)** |

`high` had come to mean "there is an alert". The investigator was right and the
cheap keyword pass was wrong — it called the SES bounce rules critical (AWS
really does pause sending) and the business-signal money alerts medium.

So the loop closes: [`scripts/ops/severity_calibration.py`](scripts/ops/severity_calibration.py)
scores the cheap verdict against the reports per alert rule and proposes a
ceiling; a person applies it; 59% of weekly volume stopped being `high`. The
guards matter more than the mechanism — it refuses to propose a downgrade for a
rule the investigator called high more than a third of the time, because that
noise has to be fixed by making the alert more specific, not by muting it.

That is the shape of the whole project: a decision, a record of why, and a way
to find out afterwards that the decision was wrong.

![The decision trace: which gate stopped it, and which rule fired](docs/img/02-decision-trace.png)

## What it costs, and what it saves

Eight suppression gates stand between an alert and a person — dedup, silence,
maintenance window, storm, cooldown, budget — and **every stop is recorded**, so
a silence rule can be scored rather than trusted. The noise centre reads those
records back as ROI per rule: how much each one caught, how many minutes it
bought, and which rules are zombies (ninety days, zero matches). A new rule is
backtested against history before it goes live.

Last week on the live deployment: **$5.55 of model spend, $9.95 saved** by cache
reuse and by not paying for alerts that suppression had already answered. The
cheaper path is the default one; AI is what the expensive minority earns.

![The noise centre: every silence rule scored, with its ROI](docs/img/03-noise-center.png)

## Quick start

### 1. Prepare configuration

```bash
cp .env.example .env
```

At a minimum you need to replace:

| Variable | Purpose |
| --- | --- |
| `API_KEY` | Token for read access to the management API. |
| `ADMIN_WRITE_KEY` | Token for management actions such as writes, replays, forwarding, and re-analysis. |
| `CHANGE_INGEST_TOKEN` | Least-privilege token for CI/CD systems that only submit change events. |
| `WEBHOOK_SECRET` | Webhook HMAC-SHA256 signing key and ingress token. |
| `OPENAI_API_KEY` | Optional; fill in when enabling AI analysis. |

The management API is limited to 300 requests per IP per minute by default. Expensive per-alert actions also have a 60-second distributed cooldown, with a five-minute minimum for starting deep analysis; the base values are configurable through `ADMIN_API_RATE_LIMIT_PER_MINUTE` and `ADMIN_ACTION_COOLDOWN_SECONDS`.

Webhook ingress accepts `X-Webhook-Signature: <HMAC-SHA256>` and token authentication through either `Token: <WEBHOOK_SECRET>`, `Authorization: Token <WEBHOOK_SECRET>`, or `Authorization: Bearer <WEBHOOK_SECRET>`. Grafana webhook contact points should use the Authorization header with scheme `Bearer` and the webhook secret as credentials; HTTP Basic authentication is intentionally not accepted.

For the complete configuration, see [.env.example.all](.env.example.all). Configuration is read only at process startup; after changes you must restart the process or perform a rolling release.

### 2. Start the full local stack

```bash
docker compose up -d --build
curl http://localhost:8000/ready
```

Compose first starts PostgreSQL and Redis, then runs `migrate`, and after the migration succeeds it starts the API, Worker, and Scheduler. When using a cloud database or managed Redis, you can run only `docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.yml up -d --build` and point `DATABASE_URL` / `REDIS_URL` in `.env` at the external instances.

The root `compose.yaml` is the everyday entry point; it only includes the business stack such as PostgreSQL, Redis, API, Worker, and Scheduler, so `docker compose ps/logs/exec` by default only sees this set of containers. The full Compose fragments still live in `deploy/compose/`, and the observability stack is started as a separate Compose project.

### 3. Send a test event

```bash
curl -X POST http://localhost:8000/v1/webhook \
  -H "Content-Type: application/json" \
  -d '{"alertname":"TestAlert","severity":"critical","host":"prod-01"}'
```

Or seed a realistic five-minute demo (dedup storm, recoveries, a flapping
identity, multi-vendor payloads) through the real ingest path:

```bash
python scripts/seed_demo_data.py --base-url http://localhost:8000
```

The business API is only exposed under `/v1`; if Webhook authentication is enabled, you need to add a signature or Token according to the current configuration.

Out-of-the-box source formats: volcengine, Grafana, Prometheus Alertmanager, Datadog, PagerDuty, Feishu cards (code adapters), plus declarative YAML specs for Zabbix, Uptime-Kuma, Alibaba CloudMonitor, Tencent Cloud Monitor, Jenkins, and Sentry under `adapters/specs/` — add your own simple source with one YAML file (see `adapters/specs/README.md`).

### 4. Open the entry points

| Entry point | Address |
| --- | --- |
| Dashboard | `http://localhost:8000/` or `http://localhost:8000/dashboard` |
| Swagger UI | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| Health | `http://localhost:8000/live` / `http://localhost:8000/ready` |

## Ask it questions: a read-only MCP server

The read side is exposed over the Model Context Protocol — **20 tools**, a usage
guide resource and investigation prompts — so any MCP client (Claude Code,
Cursor, your own agent) can query the deployment directly:

```bash
claude mcp add --transport http webhookwise \
  https://<your-host>/mcp/ \
  --header "Authorization: Bearer <API_KEY>"
```

Then ask it *"why didn't #923 page me?"*, *"what happened on this shift?"*, or
*"which rules can I delete?"* — the questions the ledger was built to answer but
which nobody wants to click through a dashboard to assemble. Four ready-made
skills ship in [`.agents/skills/`](.agents/skills/): alert investigation, shift
handover, noise audit, and observability triage.

**Deliberately read-only.** Nothing an agent calls here changes what the
deployment does. The one write is `propose_remediation`, which records an inert
proposal and executes nothing — an operator with write credentials approves it
through the same audited path as the dashboard button. The transport
authenticates with the read key, so that approval is the boundary that matters.
Full reference: [docs/reference/mcp.md](docs/reference/mcp.md).

## Where to go next

| | |
| --- | --- |
| Try the idea in one container | [WebhookWise Lite](lite/README.md) — SQLite, no Redis, ~800 lines |
| What it does, and the knobs | [docs/capabilities.md](docs/capabilities.md) |
| How it works inside | [docs/architecture/system-overview.md](docs/architecture/system-overview.md) |
| What runs around the model | [docs/architecture/ai-engineering.md](docs/architecture/ai-engineering.md) |
| Why it is built this way — including the rejected options | [.agents/notes/](.agents/notes/) |
| Everything else | [docs/README.md](docs/README.md) |
| See the API | `http://localhost:8000/docs` once running; export notes in [docs/reference/api.md](docs/reference/api.md) |
| Deploy it | [Compose](deploy/compose/README.md) · [Kubernetes](deploy/k8s/README.md) |
| Contribute | [CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md) |

**Works well with:** WebhookWise sits upstream of executor platforms — it decides
which alerts deserve attention and hands them off with context, so a downstream
auto-investigation platform such as Ongrid can act on what gets through.

## Community

- Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md); the entire quality gate is one command, `bash scripts/gate.sh`.
- Security issues go **privately** through [GitHub Security Advisories](https://github.com/itswl/WebhookWise/security/advisories/new), never public issues — see [SECURITY.md](SECURITY.md).
- Bugs and feature requests: [GitHub Issues](https://github.com/itswl/WebhookWise/issues).
- We follow the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

MIT License — see [LICENSE](LICENSE).
