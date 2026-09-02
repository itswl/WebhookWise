---
title: One timezone setting, and a card label derived from the instant
status: implemented
date: 2026-09-03
scope: core
---

## Decision

`REPORT_TIMEZONE` (default `Asia/Shanghai`, an IANA name, validated with
`ZoneInfo` at config load) is the single operator-facing zone. `core/report_time.py`
resolves it, and three places that each hard-coded `Asia/Shanghai` now read it:
the periodic report's cron zone, the Feishu card timestamp, and the default for
a maintenance window created without a zone of its own.

The card's offset suffix is computed from the rendered instant's real
`utcoffset()` — `UTC`, `UTC+8`, `UTC+5:30`, `UTC-7` — not written into the
format string.

Stored timestamps are untouched. The database is naive UTC and stays that way;
this is display and scheduling only.

## Why

`Asia/Shanghai` appeared in three unrelated files, and the Feishu card was worse
than that: it used a FIXED `timezone(timedelta(hours=8))` and appended the
literal text `" UTC+8"`. Change the deployment's zone and the card would have
kept converting to Beijing time under a label that said so — the two errors
cancelling into something that looks right and is not. A half-hour zone
(`Asia/Kolkata`) could not be expressed at all, and a DST zone would have been
labelled with whichever offset the author had in mind.

The zone is resolved per call rather than held in a module constant, because a
constant freezes whatever the config said at import time and no test can then
override it. The one exception is `services/operations/tasks.py`, where
`cron_offset` is read at import — a TaskIQ task's schedule is fixed when the
module is imported, so the value has to be too, exactly as `_maintenance_cron()`
already works.

## Consequences

- A deployment outside UTC+8 sets one variable. `.env.example.all` documents it
  next to the report cadences whose crons it governs.
- An invalid zone name fails at startup with the name in the message, rather
  than at 09:00 on the day the first report is due.
- `services/notifications/digest_cards.py` imported `_CHINA_TZ` from
  `feishu_cards`, so renaming the constant forced four lines there: the import,
  the `astimezone`, and the two window-label format strings that carried the
  same " UTC+8" literal.
- NOT changed: `models/silence.py`'s `timezone` column default and
  `schemas/silences.py`'s field default, both still the literal
  `"Asia/Shanghai"`. The column default is baked into migration 0017's
  `server_default` and the schema default is published in
  `build/openapi/openapi.yaml` — a dynamic default there would make the OpenAPI
  export depend on the environment that generated it, which is a contract
  changing under a check designed to catch contract changes. A window created
  through the API therefore still gets `Asia/Shanghai` written into its row;
  `REPORT_TIMEZONE` is the fallback for a window whose column is empty. If that
  becomes confusing, the fix is a migration that makes the column nullable, not
  a dynamic default.
