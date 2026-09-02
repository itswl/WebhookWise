---
title: The five analysis pages share one sidebar group and one time window
status: implemented
date: 2026-09-03
scope: templates
---

## Decision

Decision Trace, AI Cost, Alert Quality, Rule Audit and Noise Center move out
of three sidebar groups into one, "分析 / Analysis", and read one time window:
a period (`day` | `week` | `month`) persisted under the single localStorage
key `ww-analysis-window` by `templates/static/js/analysis-window.js`. The
Day/Week/Month buttons write and read the period directly. The three pages
with a day-count `<select>` map through it — `1 → day`, `7 → week`,
`30 → month` — and values with no mapping (90, 180) stay local to that page
and are never persisted, so no select loses an option it had.

A `<select>` remembers the shared period it last reconciled with
(`data-ww-window-seen`): an unmapped local pick survives refreshes of its own
page but yields to a newer shared choice made anywhere else. Rule Audit and
Noise Center have no 1-day option, so a shared `day` leaves them where they
were; adding the option would change what those pages measure, which is a
product decision this change does not take.

## Why

- An operator who set "30 days" on the trace page landed on the audit page
  looking at a different month, and nothing said so. The five pages answer
  the same question — what happened over this window — from different angles;
  a window that differs per page turns every cross-page comparison into a
  silent mistake.
- Grouping and window travel together on purpose: a group named Analysis with
  five private ranges would advertise a coherence the pages did not have.
- One key, a plain string, no JSON: a corrupted or foreign value normalises
  to "unset", and the landing default (today) is what every page already used
  for the trace; pages keep their own default until somebody chooses.
- The breadcrumb now names the destination's palette group instead of its
  tab, so "where am I" agrees with the sidebar after the regrouping — the
  tab map stays as the fallback for anything not in the palette.

## Consequences

- The sidebar shows five group headers, the low-frequency fold holds six
  destinations (Rule Audit left it to sit with its group); the destination
  count and every `#/slug` are unchanged, and `tests/frontend/sidebar.test.mjs`
  pins the new membership.
- `analysis-window.js` loads right after `utils.js`, before every analysis
  module; a static contract pins the order and that each of the five modules
  references the shared window. `tests/frontend/analysis-window.test.mjs`
  runs the real module against a stub `localStorage`.
- A page opened after a change elsewhere re-reads the window on load — not
  live. Live cross-tab sync (a `storage` listener) was left out: the pages
  already refetch on entry, and a list that changes under a reader mid-scroll
  is worse than one that updates when they come back.

## Rejected

- Days as the stored unit (1/7/30/90): the trace and cost endpoints take a
  period enum, and a stored 90 would have had no meaning for them. Period is
  the narrower contract; the day pages map into it.
- One control in the page header for all five pages: three of them live in
  other tabs with their own header layouts, and a floating global control
  would have been a sixth place to look.
