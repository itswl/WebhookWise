---
title: Corrections generalize to sibling instances, as a recorded prior
status: implemented
date: 2026-08-18
scope: services
---

## Decision

`services/analysis/correction_prior.py` states to the model what operators
corrected on **other instances of the same alert rule**: same rule name, same
source, a different `alert_hash`, within `AI_CORRECTION_PRIOR_LOOKBACK_DAYS`,
and only when those corrections **agree**. Below
`AI_CORRECTION_PRIOR_MIN_CORRECTIONS` (default 2) the prompt says nothing at
all. Off by default.

The block is evidence plus explicit permission to disagree, not an instruction.
The result carries `_correction_prior` — the rule, the verdict, how many
corrections, how often they have been applied, and `followed`: whether the
model's verdict came back agreeing. The exact-hash override still runs
afterwards in the pipeline and still wins.

`scripts/eval_analysis.py` reports `prior_shown` / `prior_followed`, so
`--engine ai` answers whether the prior moves verdicts at all.

## Why

This reopens something two docstrings had rejected, so the reasons are worth
restating:

> Deliberately NOT few-shot in the prompt: with a handful of samples that
> teaches nothing, and with many it would quietly move judgements on alerts
> nobody corrected, in a way no one could trace back.
> — `models/operations.py`, `ImportanceOverride`

> Applied after analysis rather than fed into the prompt, so the decision is
> traceable to a person and scoped to the one condition they decided about.
> — `services/webhooks/pipeline_stages.py`, `_run_fresh_analysis`

Both objections stand, and neither is answered by doing few-shot more carefully.
What is built here is a different thing, and each difference maps to one of them:

- **Not samples, an aggregate.** "Operators corrected 5 other instances of this
  rule to high" is a fact a model can act on. Five near-identical rule names in
  a prompt are not, which is what "a handful teaches nothing" was about. The
  `MIN_CORRECTIONS` floor is that objection turned into a setting.
- **Scoped by construction.** The query is keyed on rule name and source, so a
  correction cannot reach an alert of a different rule — the "alerts nobody
  corrected" case. Disagreeing corrections produce nothing, because averaging a
  split verdict states something no person said.
- **Recorded.** The prior travels on the analysis into persistence, so "which
  corrections steered this verdict" has an answer. That was the untraceability
  objection, and it was a property of the implementation, not of the idea.

The gap it fills is real: an override generalizes to nothing. The same rule
firing on a new host, partition or region is a new `alert_hash` and a fresh
judgement, even after operators corrected five siblings of it the same way.

`followed` exists because it is the only number that decides this feature's
future. A prior nothing follows is noise in the prompt and should be turned off.
A prior everything follows is a hard override in a costume and should be made
one. Neither is visible without the count.

## Consequences

- Off by default, and disabled means free: the enabled check precedes the
  session, so the default path opens no transaction and adds no tokens.
- Best-effort. A lookup failure logs and returns no prior; analysis proceeds
  exactly as it does today.
- The prompt gains a `{correction_prior}` slot. `str.format` ignores unknown
  keyword arguments, so a deployment with a custom prompt file keeps working and
  silently does not get the block — the same trade the `{kb_context}` slot
  already makes.
- Chinese, like the rest of the prompt and the KB block: it steers the model's
  Chinese output. The `importance` value stays the English schema literal so the
  model echoes the enum.
- `_correction_prior` is underscore-prefixed, so `save_to_cache` strips it. A
  cache hit therefore never claims a prior computed for an earlier call — but it
  also means a cached verdict predates a correction made since. Acceptable: the
  exact-hash override still applies on the cache path, and the cache TTL bounds
  it.
- The rule name interpolated into the prompt comes from the sender, so the block
  goes through `neutralize_untrusted_text` like the payload does. A rule named
  with a fence must not be able to close it.
- No index on `(source, alert_name)`. `importance_overrides` holds one row per
  corrected condition — dozens to hundreds in practice — and the query is
  bounded and grouped, in a path that is about to make an LLM call. Worth an
  index if the table ever grows by orders of magnitude; not worth a migration
  now.
- The corrections are a biased sample: conditions nobody bothered to correct are
  absent. The prior therefore speaks confidently only about rules somebody was
  once annoyed by, which is also exactly where it is most useful.
