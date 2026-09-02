---
title: Two of the four missing indexes were never wanted; alembic check now says so
status: implemented
date: 2026-09-03
scope: models
---

## Decision

`alembic check` runs in the CI `integration` job, after `alembic upgrade head`,
against the real PostgreSQL. Migration `0040_declared_indexes` and a set of model
edits close the drift it found, so it starts green and stays that way.

The drift split three ways, and each way got a different answer:

**Four columns carried `index=True` with no index in any migration.** Two of
them are dropped from the model rather than created in the database:

| Column | Verdict | Reason |
| --- | --- | --- |
| `audit_log.resource_type` | drop `index=True` | `ix_audit_log_type_created` is `(resource_type, created_at)`. Every query that filters on resource_type also orders by created_at — `api/v1/activity.py:73`, `services/operations/action_center.py:92` — which is exactly that composite. |
| `incidents.status` | drop `index=True` | `ix_incidents_status_started` leads with status, and `ix_incidents_active` is a partial index over the hot subset. |
| `audit_log.created_at` | CREATE | The activity feed's default query (`api/v1/activity.py:71`) has NO resource_type filter and orders by created_at DESC. A composite leading with resource_type cannot serve it. |
| `incidents.started_at` | CREATE | `services/incidents/change_impact.py:168` and `service_profiles.py:100/324/370` window on started_at with no status predicate. |

**Eight indexes existed only in migrations** and are now declared in the models —
`Index(...)` in `__table_args__`, `postgresql_where` where the migration used a
partial index, and the six pg_trgm ones marked `.ddl_if(dialect="postgresql")`
because SQLite has neither GIN nor trigrams. No DDL: the database already has
them.

**`maintenance_windows.created_at/updated_at`** were `Mapped[datetime]`
(NOT NULL) in the model and `nullable=True` in migration 0017. The model is
right — the ORM default writes both on every insert — so 0040 backfills any NULL
with `now()` and tightens the columns.

`alembic/env.py` gained an `include_object` hook that excludes `alembic_version`.

## Why

There was no comparison at all. The models and the migrations are two
descriptions of one schema, and the default test suite runs on SQLite via
`Base.metadata.create_all`, so it reads the MODEL and never the migrations —
a divergence between them is structurally invisible to it. Replaying
`alembic upgrade head --sql` against `Base.metadata` found 14 differences that
had accumulated unnoticed.

The `index=True` cases are the interesting ones. "Create the missing index" is
the reflex, and for two of the four it would have added a second index whose
every use is already served by the composite's leading column: more write
amplification, more bloat, no plan that improves. The question is not "does the
model say index" but "is there a query whose predicate this index leads". For
`created_at` and `started_at` there is, and it is a query with no other filter
to hang a composite on.

`alembic_version` needed the filter because `env.py` pins
`version_table_schema="public"` while autogenerate compares the DEFAULT
(unnamed) schema. Alembic's own version-table filter compares those two and
never matches, so every `alembic check` would have opened with "drop the
alembic_version table".

## Consequences

- Adding a column without a migration now fails CI instead of failing at
  runtime, and so does writing a migration the models do not describe.
- The six pg_trgm indexes are matched by NAME only. Alembic reports
  `Cannot compare index …, assuming equal and skipping. expression #1
  "lower(source) gin_trgm_ops" detected as including operator clause`, because
  the opclass sits inside the `text()` element. The documented alternative —
  `postgresql_ops` — is keyed on `expr.key`, which a `text()` element does not
  have, and SQLAlchemy wants a labelled expression that `text()` cannot produce;
  attempting it silently dropped `gin_trgm_ops` from the emitted DDL and
  PostgreSQL rejected the CREATE INDEX. So: a RENAMED trigram index is caught, an
  EDITED expression is not. If that matters later, the fix is to rebuild those
  six as `func.lower(Column).label(...)` expressions, not to reach for
  `postgresql_ops` with a text element.
- Verified beyond `alembic check`: all 130 index definitions in a migrated
  `public` schema are byte-identical to the 130 that `Base.metadata.create_all`
  produces. That comparison covers what alembic skips.
- The revision id is `0040_declared_indexes`, shorter than the filename, because
  `alembic_version.version_num` is VARCHAR(32) — the same reason 0030, 0032 and
  0038 are shortened, and a rule `tests/runtime/test_migration_healthcheck.py`
  already enforces.
