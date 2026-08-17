---
title: Agent skills ship in .claude/skills; the operator guide is a static destination
status: implemented
date: 2026-08-17
scope: whole
---

## Decision

Agent-facing workflow skills (`ww-investigate-alert`, `ww-shift-review`,
`ww-noise-tuning`) live in `.claude/skills/` in this repository, carved out of
the `.claude/` gitignore with `.claude/*` + `!.claude/skills/`. The operator
tutorial is a 23rd dashboard destination (`#/guide`), pure static HTML behind
the normal i18n keys — no module, no fetch, no backend.

## Why

- Skills encode *procedures over the MCP surface* (which tools, in what order,
  with what judgement rules). They rot at the pace of this repo's MCP server,
  so they must version with it — not sit in someone's home directory. Field
  semantics stay in the `agent-guide` MCP resource; skills deliberately point
  there instead of duplicating field docs, so one release updates one place.
- `.claude/` was fully ignored after the history rewrite because it held
  local state and credentials. The carve-out keeps that protection
  (`.claude/*` still ignores settings/locks) while letting the skills tree
  ship. hookprobe's compose already sketches the consumption path: mount a
  skills dir read-only as `/data/home/.claude/skills` — the user layer —
  keeping self-evolved skills (uid 10001, writable volume) separate from
  repo-versioned ones.
- The guide is content, not behaviour: hash links (`#/rules`) reuse the
  router, `data-i18n` reuses the dictionaries, and a static card grid cannot
  break at runtime. A JS module for it would be ceremony. If the guide ever
  needs live data (e.g. "your first rule is missing"), that is the moment it
  earns a module — not before.

## Consequences

- Skills load automatically in any Claude Code session opened in this repo,
  and hookprobe can mount the same tree read-only; both track releases with
  zero extra publishing steps.
- The `.claude/*` ignore still protects local state — only `skills/` ships.
- The guide adds i18n keys and a palette entry but no runtime surface: the
  destination count contracts moved 22 → 23 and the lowFreq tier 6 → 7.

## Rejected

- `docs/`-only tutorial: dockerignored, invisible to the operators who need
  it, and English-only by convention.
- Skills inside the MCP server (as prompts): MCP prompts are per-question
  recipes; skills are client-side and work with *any* MCP client, including
  hookprobe and local Claude Code, without another server release.
