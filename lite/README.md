# WebhookWise Lite

*The core idea, in one container: receive an alert, decide whether it deserves a human, deliver it if so — and record why, either way.*

No PostgreSQL, no Redis, no worker processes, no message queue. One SQLite file, one
process, four dependencies. About 800 lines you can read in a sitting.

```bash
docker build -f lite/Dockerfile -t webhookwise-lite .
docker run -p 8000:8000 -v wwlite:/data webhookwise-lite
```

Open <http://localhost:8000> and send something:

```bash
curl -X POST localhost:8000/webhook/demo \
  -H 'content-type: application/json' \
  -d '{"title":"disk full on db-01","body":"/ is at 95%, critical"}'
```

It will be triaged, and the dashboard will show it as **no rule matched** — because you
haven't told it where to send anything yet. That is the point: the system is never
silent about its own silence.

Add a destination and send it again:

```bash
curl -X POST localhost:8000/api/rules -H 'content-type: application/json' \
  -d '{"name":"oncall","target_kind":"feishu","target_url":"https://open.feishu.cn/open-apis/bot/v2/hook/XXX"}'
```

## What it does

An alert walks four gates. Each one can only pass it along or stop it **with a named
reason**, and that name is written to the decision trace:

| Gate | Stops when | `skip_code` |
| --- | --- | --- |
| ① dedup | the identical alert arrived inside the dedup window | `duplicate` |
| ② silence | an operator asked for quiet on a matching pattern | `silenced` |
| ③ cooldown | this identity was notified about too recently | `cooldown` |
| ④ rules | no forwarding rule claims this alert | `no_match` |

Whatever survives is written to an outbox and delivered by a background loop with
retries and backoff. Every alert — delivered or not — produces exactly one decision row,
which is what the dashboard renders. Click any row to see the full chain.

Between the gates sits **triage**: an LLM assigns importance and a one-line summary, and
falls back to keyword rules whenever the LLM is absent, slow, or wrong-shaped. The trace
records which path ran (`ai` vs `rule`), so a degraded judgement is visible rather than
silently assumed.

## Configuration

Every variable is optional; the defaults are a working install. The full
annotated template is [`.env.example`](.env.example) — copy it and pass it with
`docker run --env-file .env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `DB_PATH` | `/data/webhookwise.db` | Put it on a volume to survive restarts |
| `DEDUP_WINDOW_SECONDS` | `300` | Collapses a burst of the identical alert |
| `COOLDOWN_SECONDS` | `1800` | Paces re-notification of one identity |
| `OPENAI_API_KEY` | *(empty)* | Empty = rule-based triage only |
| `OPENAI_API_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `INGEST_TOKEN` | *(empty)* | Required in the `X-Ingest-Token` header when set |
| `ADMIN_TOKEN` | *(empty)* | Required in `X-Admin-Token` for rule/silence writes |
| `OUTBOX_POLL_SECONDS` | `2` | Delivery loop interval |
| `OUTBOX_BACKOFF_SECONDS` | `15` | Delay before a retry |

**Set `INGEST_TOKEN` and `ADMIN_TOKEN` before exposing this to a network you don't
control.** They are empty by default so the first run works with no setup, which is the
right trade for a laptop and the wrong one for the internet.

The two suppression windows must not be set to the same magnitude. Dedup collapses a
*burst*; cooldown paces *re-notification*. If `DEDUP_WINDOW_SECONDS >= COOLDOWN_SECONDS`,
the dedup gate catches every candidate first and the cooldown gate can never fire.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/webhook/{source}` | Ingest. Any JSON; Alertmanager and Grafana shapes are recognized |
| `GET` | `/api/decisions?limit=50` | Recent decisions with their full gate chain |
| `GET` | `/api/stats` | 24h outcome counts + outbox status |
| `GET/POST/DELETE` | `/api/rules` | Forwarding rules |
| `GET/POST/DELETE` | `/api/silences` | Time-boxed silences |
| `GET` | `/health` | Liveness + outbox summary |

Rules match on `match_source` and `match_importance` (comma-separated, empty = any) and
deliver to `feishu` (interactive card) or `generic` (plain JSON) targets.

### Routing to several receivers

Rules are evaluated in `priority` order (highest first, then creation order) and **every**
match produces its own delivery — one alert can reach an on-call group and an archive
sink at once, each retried independently, so one dead target cannot take the other down.

Set `stop_on_match` on a rule to end evaluation there. That is what makes "everything
else" expressible without hand-maintaining disjoint match sets:

```bash
# high → on-call, and stop
curl -X POST localhost:8000/api/rules -H 'content-type: application/json' \
  -d '{"name":"oncall","match_importance":"high","priority":10,"stop_on_match":true,
       "target_kind":"feishu","target_url":"https://.../hook/AAA"}'

# everything else → general channel
curl -X POST localhost:8000/api/rules -H 'content-type: application/json' \
  -d '{"name":"general","priority":0,"target_kind":"feishu","target_url":"https://.../hook/BBB"}'
```

The rule that ended evaluation is recorded in the trace as `stopped_by`, so "why didn't
my catch-all fire?" stays answerable.

**One caveat when you fan out to receivers with different purposes:** the cooldown gate
is keyed on the alert identity, not on the target. A repeat alert suppressed by cooldown
is suppressed for *every* rule — including an archive sink that may want the full stream.
Raise `COOLDOWN_SECONDS` deliberately, or keep archival targets on a separate ingest
source that no cooldown-sensitive rule matches.

## What it deliberately leaves out

Lite is a distillation, not a cut-down build. The [full edition](../README.md) adds the
things that only matter once alerts become an operational practice: incident
aggregation and SLA escalation, maintenance windows, flapping detection, semantic noise
correlation and root-cause grouping, a knowledge-base learn loop, signed interactive
card actions, per-source credentials, OpenTelemetry, a runtime settings plane, and
horizontal scale through Postgres + Redis + TaskIQ.

Use Lite to understand the idea, to run a small deployment, or to decide whether the
full thing is worth its operational weight.

## Development

```bash
pip install -r lite/requirements.txt
DB_PATH=/tmp/lite.db uvicorn lite.app:app --reload
pytest tests/lite/          # the gate contract
```
