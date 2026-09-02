# Agent Notes

A decision record per file, kept next to the code rather than in a chat log or a
commit message nobody re-reads. The point is not process: it is that six weeks
later "why is it done this way" and — more valuable — "why is it *not* done the
obvious way" have written answers in the repository.

Ported from the hookstack family, which took it from
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
(`.agents/notes`). `docs/` already explains how WebhookWise works; this holds the
decisions behind it, including the ones that left no code.

## One agent directory

Agent material lives in `.agents/` only — this directory. There is no `.claude/`
and no `.codex/` in the repository: the four operator skills are in
`.agents/skills/` and the decision notes are here.

| Reader | How it finds the skills |
| --- | --- |
| Codex | Reads `.agents/skills` natively. |
| Claude Code | Has no project-level discovery path outside `.claude/`, so each machine points at the skills with one symlink per skill under `~/.claude/skills` (the loop is in `AGENTS.md`; `scripts/assert_skill_pointers.py` reports a missing or stale pointer and the gate runs it). |
| hookprobe | Mounts `.agents/skills` read-only as its user skills layer. |
| People | Read `notes/` — prose, no tool reads it to change behaviour. |

It took several passes to get here, and the history explains the shape:

- `AGENTS.md` and `CLAUDE.md` were two copies, and the older one still told an
  agent to hand-pick a few local checks instead of running the gate — the exact
  habit that let bandit, pip-audit and the OpenAPI contract each go red in CI
  while local was green. `AGENTS.md` is now the single instruction file and
  `CLAUDE.md` is a symlink to it.
- Two vendor `skills/` folders once held **four skills with zero overlap**: three
  ops workflows visible only to Claude Code, one observability skill visible only
  to Codex, and different naming conventions. Nobody decided that; it was an
  accident of which tool was open when each was written. Skills are knowledge
  about operating *this* system, so every tool gets all of them — from one
  neutral directory rather than one vendor's folder.

`scripts/assert_agent_notes.py` asserts AGENTS.md and CLAUDE.md have identical
CONTENT rather than checking that a link exists — a checkout without symlink
support materialises a link as a text file holding the target's path, which
reads as "fine" to anything that only tests for existence.

## Buckets

| Directory | Holds |
| --- | --- |
| `notes/implemented/` | Shipped decisions. The code is the truth; the note says why it is shaped that way. |
| `notes/proposed/` | Decisions taken but not built, with enough detail to build or drop them. |
| `notes/rejected/` | Ideas evaluated and declined — including ones that were built and then removed. The most useful bucket, and the one a commit log never keeps. |
| `notes/archived/` | Shipped notes that no longer guide future work. |

## Format

One file per decision, named `YYYY-MM-DD-slug.md`, opening with front matter:

```markdown
---
title: One line naming the decision
status: implemented
date: 2026-08-15
scope: services
---

## Decision

What was decided, in the present tense.

## Why

The reasoning, including the evidence. Numbers and observed behaviour beat
adjectives — a note that says "this was slow" ages badly next to one that says
"19 of 78 rows, measured on production".

## Consequences

What this makes easy, what it makes hard, and what to watch for.
```

`scope` is one of `api`, `services`, `core`, `models`, `templates`, `deploy`,
`whole` — the module boundaries CLAUDE.md tells changes to stay inside. A
decision that cannot name one is usually two decisions.

`scripts/assert_agent_notes.py` enforces the shape, and the gate runs it. A note
that has been superseded says so with a `supersedes:` field naming the older
note's filename stem; it must exist.

## What belongs here

Not everything. A note earns its place when it answers a question the code
cannot:

- a default that looks wrong until you know what it prevents;
- a cheaper approach that was measured and turned down;
- a boundary that exists for a reason no test can state.

Routine changes do not need one. If the commit message covers it, the commit
message is the record.
