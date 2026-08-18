# Deep-analysis engine

Deep analysis hands an alert to an external investigator, which is free to run
its own tools, and returns a report that WebhookWise normalizes into
`deep_analysis_report.v1`. WebhookWise talks to that investigator over two HTTP
endpoints and nothing else, so any engine answering them can serve this leg.

The layer is neutral: `DEEP_ANALYSIS_PLATFORM` names which product answers at
`DEEP_ANALYSIS_GATEWAY_URL`, and the dialects live as data in
`services/analysis/deep_analysis_platforms.py`. hookprobe deliberately implements
OpenClaw's contract, so it shares that dialect instead of getting a near-identical
copy of it; `hermes` differs (its own agent path, HMAC instead of bearer).

## The contract

| Call | Endpoint | Meaning |
| --- | --- | --- |
| Trigger | `POST {DEEP_ANALYSIS_GATEWAY_URL}{agent_path}` | Start an investigation. Bearer `DEEP_ANALYSIS_HOOKS_TOKEN` (or an HMAC signature, per dialect); the body carries the session key, the prompt, the alert and the evidence pack. Extra fields are ignored by the engine. |
| Poll | `GET {DEEP_ANALYSIS_HTTP_API_URL}/sessions/{session_key}/final` | Fetch the report. 200 means the run is over; `isFinal: true` says so explicitly. |

`agent_path` is `/hooks/agent` for the bearer dialects and `/webhooks/agent` for
`hermes` — the gateway table decides, not the caller.

Polling runs on TaskIQ delayed tasks with exponential backoff
(`DEEP_ANALYSIS_POLL_*`). An engine that answers `isFinal: true` collapses
`DEEP_ANALYSIS_STABILITY_REQUIRED_HITS` to a single hit, because there is nothing
left to stabilize; engines that stream partial results are read twice by default,
so a half-written report is never stored as final. Pending records are re-armed
every `BACKGROUND_SCAN_INTERVAL_SECONDS` by a database-side scan, so a poll lost
to a worker restart is picked up rather than stranded.

## Point WebhookWise at an in-cluster engine

```bash
DEEP_ANALYSIS_ENABLED=true
DEEP_ANALYSIS_PLATFORM=hookprobe
DEEP_ANALYSIS_GATEWAY_URL=http://hookprobe:8088
DEEP_ANALYSIS_HTTP_API_URL=http://hookprobe:8088
DEEP_ANALYSIS_HOOKS_TOKEN=<the token the engine requires>
```

Both URLs point at the same service here — trigger and poll live on one port. The
engine only has to be reachable from the API and worker processes; use
`deploy/compose/docker-compose.deep-analysis.yml` to join them to its network.

**No SSRF allowlist is involved, and that is deliberate.** A private service name
is exactly what the shared outbound client refuses: forward targets come from
rules written about payload data, so their resolved address must be proven public
or an alert becomes an SSRF primitive. The gateway is the opposite case — its URL
is process configuration and its intended target *is* private infrastructure — so
it gets its own client (`core/http_client.get_deep_analysis_client`) that skips
the private-address rule for this leg only. Earlier versions asked operators to
list the host in an `INTERNAL_TARGET_HOSTS` setting; that setting is gone, because
a deployment should not have to repair a policy that was wrong for this caller.
Never reach for `ALLOW_PRIVATE_TARGET_URLS=true` instead: that opens every private
range — cloud metadata at 169.254.169.254 included — to every forwarding rule.

For a gateway on another host, use its public address; nothing else changes.

## More than one engine

`DEEP_ANALYSIS_GATEWAYS` adds named gateways as a JSON array, and a forward rule
picks the one it wants. The flat settings above are the gateway called `default`,
because one gateway needs no name, and a named entry inherits every field it
omits from it:

```bash
DEEP_ANALYSIS_GATEWAYS='[{"name":"hermes-eu","platform":"hermes","url":"https://…","token":"…"}]'
```

## Verify the round trip

Send an alert, then trigger analysis on it and watch the record settle. Both
headers are required: the `/v1` router is behind the read-key guard and the write
endpoints add an admin-write dependency on top of it.

```bash
curl -fsS -X POST http://localhost:8000/v1/deep-analyze/<webhook_id> \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Write-Key: $ADMIN_WRITE_KEY"
curl -fsS http://localhost:8000/v1/deep-analyses/detail/<analysis_id> \
  -H "X-API-Key: $API_KEY"
```

The trigger answers immediately with `status: pending` and the session key it
handed the engine; the record turns `completed` once a poll reads the report.
`status` staying `pending` past `DEEP_ANALYSIS_TIMEOUT_SECONDS` means the report
never arrived — check that the engine has the session (its own console lists runs
by session key) before suspecting the poller.
