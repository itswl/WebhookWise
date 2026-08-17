---
title: Delegated-action roots register themselves; window lookup was never resolving them
status: implemented
date: 2026-08-17
scope: templates
---

## Decision

`wwResolveAction` resolves two-part actions (`Module.method`) through an
explicit registry (`WW_ACTION_ROOT_REFS`), populated by a guarded
`wwRegisterActionRoot('Name', Name)` line at the bottom of each module file.
The allowlist (`WW_ACTION_ROOTS`) is unchanged and still gates what may
register; `window[name]` remains only as a fallback for var-declared or
test-injected roots. A contract test enforces allowlist↔registration parity
in both directions, and a node harness runs the real resolver against a
const module.

## Why

const/let at the top level of a classic script create global LEXICAL
bindings with no window property — so `window['OverviewModule']` was
undefined for every module, and every `Module.method` data-act resolved to
null. Measured in a real headless Firefox against the real sources: the
deep-analysis retry/forward buttons, the decision-trace filters, the
skip-reason drill and ~a dozen more were silently dead. Nothing logged,
nothing threw; the dispatcher's design goal (no code in markup, allowlist at
resolve time) hid the failure as a quiet no-op.

Alternatives considered:

- **`new Function('return ' + name)()`** — blocked by the CSP (no
  `unsafe-eval`), and reintroduces string-to-code exactly where the
  dispatcher exists to prevent it.
- **One registration loop in the last-loaded script** — a single file
  referencing every module by bare identifier dies wholesale when ONE module
  fails to parse (the same const-invisibility failure mode that once killed
  all seven Routing destinations). Per-module registration degrades
  per-module instead.

## Consequences

- Adding an action root is now a three-line ritual: allowlist entry,
  registration line, and the parity contract fails loudly if either half is
  forgotten.
- Registration lines are guarded (`typeof wwRegisterActionRoot ===
  'function'`) so a module still parses standalone (test harnesses slice and
  eval module files without utils.js).
- The browser probe that found this (static server + headless Firefox
  POSTing resolver results) is worth repeating for future "wire-up" changes:
  the python contracts and node harnesses both missed a defect only a real
  script-global environment could show.
