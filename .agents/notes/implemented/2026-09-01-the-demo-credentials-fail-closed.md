---
title: The quickstart's demo credentials are placeholder-shaped on purpose
status: implemented
date: 2026-09-01
scope: deploy
---

## Decision

`docker-compose.quickstart.yml` starts the whole stack from published images
with no checkout, no `.env` and no keys. Every credential in it —
`WEBHOOK_SECRET`, `API_KEY`, `ADMIN_WRITE_KEY`, the Postgres password — begins
with `please-change-`, which is exactly the prefix
`core/web/startup_checks.looks_like_placeholder_secret` rejects when
`APP_ENV=production`. The file runs happily as a demo and refuses to boot the
moment somebody promotes it.

`scripts/assert_quickstart_compose.py` (gate + CI) holds the four properties
that make the claim true: no `build:`, no `env_file`, no bind mounts, and a
default image tag equal to `pyproject.toml`'s version. It imports the real
predicate rather than restating its prefixes.

`BACKGROUND_SCAN_INTERVAL_SECONDS` is 30 here against a production default of
300. Incident grouping is a background sweep, so at 300 a freshly seeded demo
shows zero incidents for five minutes and reads as broken.

## Why

The rejected alternative was the obvious one: generate real random secrets on
first run, via an entrypoint or a `make` target. It produces a stack that looks
production-ready, and that is the failure — the next step after "it works on my
laptop" is `APP_ENV=production` on a VPS, and a generated-secret demo would
have started, exposing a dashboard and an admin write key that the operator
never chose and does not know. Making the demo *unable* to start in production
is a stronger guarantee than making it secure enough to try.

The tag check earns its place from a measured failure in the sibling
repository: a release shipped whose images could not build, because the
Dockerfile and the build context had drifted and nothing compared them. A
pinned tag in a quickstart drifts the same silent way — it keeps working, it
just serves the previous release forever.

## Consequences

- The quickstart cannot be used as a deployment template without replacing
  every credential, which is the intent; `deploy/compose/` remains the real path.
- A release that bumps `pyproject.toml` must bump the quickstart's default tag
  in the same commit, or the gate goes red. That is one more step per release,
  accepted deliberately over a quickstart that silently serves stale images.
- The 30s scan interval must not be copied into a deployment. It is commented
  as a pacing choice at the point of use.
- `assert_quickstart_compose.py` sets a throwaway `DATABASE_URL` before
  importing, because `startup_checks` builds a logger at module scope and so
  constructs the whole `AppConfig`. Worth removing at the source one day; the
  same workaround already exists in `scripts/eval_analysis.py`.
