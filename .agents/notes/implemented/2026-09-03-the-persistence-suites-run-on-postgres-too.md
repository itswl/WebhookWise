---
title: The persistence suites run a second time on real PostgreSQL
status: implemented
date: 2026-09-03
scope: whole
---

## Decision

`WW_TEST_DATABASE_URL` switches the shared `db_engine` fixture from in-memory
SQLite to a real PostgreSQL. Unset — the gate, every laptop, the CI `test` job —
nothing changes. The CI `integration` job sets it and runs
`tests/forwarding tests/incidents tests/kb tests/webhooks tests/operations`
a second time against the service container, after `tests/real_infra/`.

SQLite stays the default. It is what makes 1682 tests finish in ten seconds, and
that number is why people run them.

Each pytest-xdist worker builds the full ORM schema into a PostgreSQL schema of
its own (`ww_test_gw0`, `ww_test_gw1`, …) once per session, and each test starts
with one `TRUNCATE … RESTART IDENTITY CASCADE` of every table. Two consequences
of that shape are load-bearing and non-obvious:

- The engine's `search_path` is the worker's schema and NOTHING else. `public`
  in the same database holds the migrated schema that `alembic check` inspects;
  with `public` on the path, a table missing from the test schema would resolve
  there and the suite would quietly pass against the wrong database.
- Schema creation uses `checkfirst=False` for the same reason, on a connection
  that DOES have `public` on its path (the `gin_trgm_ops` operator class lives
  with the pg_trgm extension). A `has_table()` probe would find every table in
  `public` and create none of them.

## Why

The audit measured what the SQLite default cannot see: the advisory lock in
`db/session.py` is a documented no-op on any non-PostgreSQL dialect, the
`ON CONFLICT DO UPDATE` upsert in `services/kb/store.py` never compiles, and
every `FOR UPDATE SKIP LOCKED` claim is silently ignored. Only the 11 tests in
`tests/real_infra/` ever touched a real engine.

Running the five persistence suites on PostgreSQL failed **36 tests, all one
bug class**: SQLite does not enforce foreign keys unless `PRAGMA foreign_keys=ON`
is issued per connection, and it is not. Fixtures across six files were filing
outbox rows and decision traces against `webhook_event_id` and
`forward_rule_id` values that had never existed — 114 FK violations from
`forward_outboxes_webhook_event_id_fkey`, `forward_outboxes_forward_rule_id_fkey`
and `deep_analyses_webhook_event_id_fkey`. The fix is `ensure_webhook_events()` /
`ensure_forward_rules()` in `tests/helpers/db.py`, called at the seed sites: the
tests now build the graph production requires.

Nothing else broke. No dialect assumption, no ordering dependency, no SAVEPOINT
difference — 623 tests pass on both backends.

## Consequences

- A future test cannot invent a parent row id and get away with it, which is the
  whole point. The failure arrives in CI, in the `integration` job, with the
  constraint name in the message.
- The `integration` job is CI-only by design and `scripts/gate.sh` does NOT
  replicate it: the gate is an exact replica of the `test` job, and a check that
  needs two service containers does not belong in a local pre-push script. This
  is the one place the "add it to both" rule does not apply, and ci.yml says so
  in a comment.
- Running locally needs a PostgreSQL and one environment variable:
  `WW_TEST_DATABASE_URL=postgresql+asyncpg://ci:ci@localhost:5432/webhookwise_ci pytest -n 4 tests/forwarding …`
- The `@compiles(JSONB, "sqlite")` shim in `tests/conftest.py` is registered only
  on the SQLite path. On PostgreSQL those columns are real JSONB, and leaving a
  JSONB→JSON downgrade installed is an invitation to "fix" a containment
  operator by degrading the column type.
- Rejected: running the whole suite on PostgreSQL. The 60x slowdown buys nothing
  for the ~1000 tests that never touch a session, and a slow default suite is a
  suite people stop running. Rejected too: one shared schema for all workers —
  the between-tests TRUNCATE would delete another worker's fixtures mid-test.
