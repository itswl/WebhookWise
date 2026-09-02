---
title: Published ports bind to loopback by default
status: implemented
date: 2026-09-02
scope: deploy
---

## Decision

Every host port the Compose files publish binds to `127.0.0.1` unless the
operator says otherwise: `${API_BIND_ADDRESS:-127.0.0.1}:8000:8000` for the API
in `deploy/compose/docker-compose.yml`, and `${OBSERVABILITY_BIND_ADDRESS:-127.0.0.1}`
in front of the nine observability ports (Grafana 3000, Loki 3100, Tempo 3200,
Pyroscope 4040, Alloy 4317/4318/12345, Prometheus 9090, Alertmanager 9093).
PostgreSQL and Redis already did this. Both variables are documented in
`.env.example.all`; setting one to `0.0.0.0` restores the old behaviour for a
bare host, and `docker-compose.quickstart.yml`, which was already
`127.0.0.1:8000:8000`, is left as it was.

## Why

A 2026-09-02 audit of the production host counted 10 ports published on
`0.0.0.0`, a host firewall whose chain is not persisted, and a reboot pending.
After that reboot the firewall is gone and each of those ports — Grafana with
its default `admin/admin`, Prometheus's unauthenticated query API,
Alertmanager's silence API, two OTLP receivers — answers to the internet, with
nothing in the repository saying so.

Nothing needed them there. Caddy runs in host network mode and proxies to
`127.0.0.1:8000`; the app containers send telemetry to Alloy and are scraped by
Prometheus over the Compose networks; hookprobe reaches the API over the shared
`hookstack_net`. Every documented URL under `docs/operations/` already reads
`http://localhost:<port>`, so the loopback default changes no instruction. The
only consumer a `0.0.0.0` binding ever had was whoever scanned the host.

The default is the right place for the fix, rather than the firewall alone.
The firewall is host state, and a reboot just showed how host state is lost;
the compose file is repository state that every deploy re-applies. Docker also
programs its published ports ahead of most host firewalls (only `DOCKER-USER`
runs first), so a port can be reachable while an operator believes a rule
blocks it. Binding at publish time removes the question.

Rejected: leaving the observability stack on `0.0.0.0` because "it is behind
the diagnostics profile". Five of the nine ports belong to the default profile
that runs all day.

## Consequences

- A bare host with no reverse proxy sets `API_BIND_ADDRESS=0.0.0.0` — and, if
  it really wants Grafana on the wire, `OBSERVABILITY_BIND_ADDRESS=0.0.0.0` —
  and owns the firewall. The variables exist so nobody edits the compose file.
- Reaching Grafana or Prometheus from a laptop is an SSH tunnel
  (`ssh -L 3000:127.0.0.1:3000 <host>`) or the proxy, not `http://host:3000`.
- Anything on the host that reached a service through the Docker bridge
  gateway address (`172.x.0.1:<port>`) instead of a Compose network stops
  working. None is known; if one appears, the variable takes that address.
- An empty variable renders `:8000:8000`, which Compose rejects — the
  `:-` default covers unset and empty, so only an explicit blank can do that.
- The firewall persistence and the pending reboot are host work and remain
  open; this note does not close them.
