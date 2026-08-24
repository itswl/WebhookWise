---
title: A prompt change faces a gate now, and the ai score is on the record next to the rules one
status: implemented
date: 2026-08-24
scope: services
---

## Decision

`scripts/eval_analysis.py assert-fresh` fails when the committed material that
steers the model moves without the recorded `ai` score being re-recorded. It is
in `scripts/gate.sh` and in `ci.yml`, added in the same change.

The digest covers `prompts/webhook_analysis_detailed.txt`,
`prompts/deep_analysis.txt`, and the declared defaults `AI_SYSTEM_PROMPT`,
`OPENAI_MODEL`, `AI_USER_PROMPT_FILE`. Every one of those is asserted to move the
digest, so the gate cannot silently shrink to a subset.

`Report` now records WHICH model produced a score, and `baseline.json` grows an
`ai` engine beside `rules` — the shape that file was built for and never used.

## Why

A code change in this repository faces 1465 tests, a coverage floor, an OpenAPI
contract, a design-language contract, a generated-docs check and a lockfile
check. A prompt rewrite faced nothing. The model is at least as load-bearing as
the code — it produces the severity that decides who gets woken up — and it was
the one input with no door in front of it.

The ai engine cannot gate on its SCORE, and that judgement was already made
correctly before this change: it spends money and does not repeat exactly, so it
must not sit in CI. But that is an argument against gating the answer, not
against gating the question. What the model is told is committed text, and
"was the measurement redone after the input moved" is exactly the check the
generated-reference and lockfile steps already make. Offline, free, deterministic.

The no-baseline case is a FAILURE, not a skip. A gate that reports "not
applicable" while the thing it guards is unguarded is precisely how this path
stayed unmeasured for months, so the message names the single command that fixes
it.

## What the first recording actually said

Worth writing down, because it is the finding and not a formality. Scored on
GLM-5.3 the day the investigator moved to BigModel:

| | rules | ai (glm-5.3) |
| --- | --- | --- |
| importance exact | 17/17 (1.00) | 13/17 (0.765) |
| under-calls | 0 | 3 |
| over-calls | 0 | 1 |
| high recall | 1.00 over 8 | **0.75 over 8** |

The model **under-called three labelled cases and missed two of eight `high`s**.
The ones I could read are regression locks mined from production: a payment
backlog that arrives with `Level=info` and must not be believed, and an alert
with no level field at all that must default to medium rather than drop. The
cheap keyword path gets every case right; the model does not.

The first partial reading of this looked better — 14/16, high recall 0.875 —
because six calls had died on provider rate limits and one labelled case went
unscored. Fixing the retry classification (below) turned a flattering incomplete
number into a worse complete one, which is the more useful direction for a
measurement to move.

Read carefully, because the corpus is 16 labelled cases and these were mined to
be hard. This is not "GLM is worse than keywords" — it is "on the cases we
deliberately kept because they had bitten us, the model regresses and the rules
do not", which is a reason to keep the rules path authoritative for the floor and
a reason this gate exists at all.

The `ai` thresholds are recorded as the SAME exacting contract as the rules
engine (min_high_recall 1.0, max_miss_rate 0.0) even though the current score
fails them and nothing enforces them for this engine. Recording the score's own
numbers as its thresholds would have written down "missing a high is acceptable
here", which is the opposite of what the rules entry says in the same file.

## The bug this uncovered

Six of eighteen calls in the first pass died on `429`, and 429 is in the
retryable status set `is_ai_provider_retryable_error` checks. It never reached
the check. instructor raises `InstructorRetryException`, carries the real errors
in `failed_attempts`, and chains **none** of them — so what arrived had no
status_code, no "rate" in its type name and an empty `__cause__`/`__context__`
chain. Classified terminal. The analysis fell back to rules and the only trace
was a log line.

That is a production defect, not an eval one: this account demonstrably rate
limits, and every 429 was silently costing an AI verdict. `iter_exception_chain`
now walks `failed_attempts` as well as the cause chain, with a visited set
because walking two directions at once invites a cycle. Wrapped 429 retryable,
wrapped 400 still terminal, and a test proves the visited set is load-bearing.

It also means the gate is usable at all. "Re-record after a prompt change" is the
one instruction this whole decision rests on, and before the fix that command
could not complete against a rate-limited provider.

## Consequences

It does not see the DEPLOYED model. That is an env var, and an offline gate
cannot read production — a swap done by editing `.env` slips past this entirely,
which is how the investigator changed provider three times in a month unmeasured.
Two things cover that instead: the score records the model it was produced on, so
a reader can see when the baseline describes a provider production has left
behind, and `scripts/ops/engine_quality_compare.py` measures report quality
either side of a cutover.

It does not score correctness of the deep-analysis reports. That judgement is
what the run rulings on the investigator's own ledger are for.

The cost of the gate itself is one command before a prompt merge, and one paid
eval run — $0.80 by this system's own accounting, which is Claude-era pricing on
GLM tokens and so an overstatement of several times over. The cost of not having
it was demonstrated the same afternoon: nobody would have known the model
under-calls a `high` on a case kept in the corpus BECAUSE it had bitten before.

## The same afternoon found the hole in this

The recorded score is ONE draw per case, and a few hours after committing it the
sibling judge was measured on the same question: same model, same input, three
draws — **11 of 32 cases changed answer**, and 59% of rows flipped for at least
one of two judges. So a single-draw baseline cannot separate a real regression
from a resample, and the gate above sends people to compare against exactly that.

Half-fixed, deliberately. `--votes N` now draws each case N times and keeps the
modal answer, and the baseline records `samples`, which `assert-fresh` prints —
saying "from ONE draw" when it is, rather than letting the number pass as a
measurement. The existing recording was NOT redone: nobody compares against it
yet, the gate forces a re-record on the next prompt change anyway, and buying
thirty minutes and three times the tokens to restate a number already labelled as
weak is worse value than the label.

The number to watch on that re-record is whether 13/17 holds. If it moves by two
cases on identical prompts, the corpus is too small for this engine to be gated on
at all, and the honest response is more cases rather than looser thresholds.

## What would change the answer

- The corpus growing past the point where an ai run is cheap. 17 cases at
  concurrency 2 took about ten minutes and 49.8k in / 43.5k out tokens; at ten
  times the cases this becomes a scheduled job rather than a command somebody
  runs. The output figure is the one to watch — GLM-5.3 bills reasoning as
  output, so tokens out nearly equal tokens in on a classification task.
- The ai score reaching the rules engine's thresholds consistently. Then the
  question becomes whether ai should gate for real, and the answer depends on
  whether the provider has become repeatable enough — not on the score.
- Anyone adding a fourth steering source and not adding it to STEERING_SOURCES.
  The test asserting each entry moves the digest is what makes that visible, but
  only for the entries that are in the list; a new prompt file nobody registers
  is still invisible. That is the known soft spot.
