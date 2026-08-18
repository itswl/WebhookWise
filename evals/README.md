# Analysis evals

A frozen corpus of alerts, the importance an operator says each one deserves, and
a scorer that replays them through an analysis engine.

Importance is the judgement everything downstream is built on: it selects forward
rules, it decides what a silence swallows, and a `high` verdict is what triggers
deep analysis. A change that quietly lowers it — a keyword edit, a prompt
rewrite, a cheaper model, tiered routing turned on — stops alerts reaching
anybody, and nothing in the pipeline complains. `analysis_feedback` and
`/v1/decision-quality` measure that after the fact, on alerts already delivered
or already suppressed. This measures it before the change ships.

```bash
python3 scripts/eval_analysis.py run                 # the gate: rule engine, declared policy
python3 scripts/eval_analysis.py run --report        # ... and list every disagreement
python3 scripts/eval_analysis.py run --policy env     # score THIS deployment's tuned keywords
python3 scripts/eval_analysis.py run --engine ai      # replay through the real model (costs money)
python3 scripts/eval_analysis.py baseline --write     # re-record after an intended move
python3 scripts/eval_analysis.py export --limit 500   # mine cases out of the database
```

## What the numbers mean

| metric | question |
| --- | --- |
| `exact_rate` | how often the engine agreed with the operator |
| `high_recall` | of the alerts an operator called `high`, how many the engine also called `high` |
| `miss_rate` | how often the engine scored an alert **below** its label |
| `over_rate` | how often it scored one **above** its label |
| `triage_exact_rate` | agreement on act-now / monitor / defer, where that is labelled |
| `cost_usd` | what the run spent (`--engine ai` only) |

The errors are not symmetric and the score does not average them together. An
under-call reaches nobody; an over-call costs attention. `high_recall` is the
headline and `max_miss_rate` is pinned at zero in the baseline, because an alert
the engine used to call `high` and now calls lower is never an acceptable drift.

## What gates, and why only that

Only `run` with the rule engine, the declared policy and the whole committed
corpus is checked against `baseline.json`. That combination is deterministic:
the keyword policy is built from the **committed field defaults**, deliberately
ignoring the environment and `.env`, so CI and a laptop produce the same number
and no local export can move the gate. It needs no database, Redis, API key or
network.

Everything else scores and reports without gating, and says so:

- `--policy env` answers a different, real question — what does *this*
  deployment's tuning do to the corpus — and its answer is per-machine.
- `--engine ai` spends money and does not repeat exactly. A model is a decision
  aid here, never a gate.
- `--limit` and a non-committed `--corpus` are partial measurements.

## The corpus

`analysis_cases.jsonl`, one JSON object per line; `#` lines are notes.

```json
{"id": "event-41221",
 "source": "volcengine",
 "parsed_data": {"RuleName": "充值订单积压", "Level": "info"},
 "expected": {"importance": "high", "triage_verdict": "act_now"},
 "origin": "event 41221 @ 2026-08-03",
 "labeled_by": "operator-correction"}
```

`expected` may be omitted entirely — an **unlabelled** case. That is deliberate:
dumping real traffic in is cheap and labelling it is the slow part, so the corpus
has to be useful while it is half-labelled. Unlabelled cases are still replayed,
which catches a crash on a real payload shape, and are excluded from every rate.

`origin` is required in practice (a test enforces it): a case nobody can trace
back is a case nobody dares to relabel.

The seed cases are a **behaviour lock**, not a quality measurement. Each one
records something production paid to discover — a payment alert arriving as
`Level=info`, `reorder queue drained` not being an order alert, the GPU override
firing only on the high bucket. Scoring 100% on them means today's behaviour is
intact, nothing more. The quality number starts existing when the corpus carries
labelled production traffic.

## Growing it

`export` mines the database. The best labels in the system were never written for
an eval: they are the importance an operator **corrected** an alert to, in
`importance_overrides` (still in force, so the stronger statement) and
`analysis_feedback.corrected_importance`. Those come out pre-labelled; everything
else comes out unlabelled, for a human to judge.

```bash
python3 scripts/eval_analysis.py export --labeled-only     # just the corrections
python3 scripts/eval_analysis.py export --limit 500        # corrections + recent traffic
```

Cases are appended and existing ids are skipped, so re-running is safe. Review
what lands: `export` copies a payload verbatim, and a payload can carry a
hostname, an internal URL or a customer identifier. The corpus is committed to
this repository — treat it as published.

Growing the corpus is a **baseline event**. More cases move every rate, which is
not a regression, so re-record and read the delta:

```bash
python3 scripts/eval_analysis.py run --report        # see what the new cases disagree about
python3 scripts/eval_analysis.py baseline --write    # accept the new measurement
git diff evals/baseline.json                         # the delta is the review
```

`recorded` in `baseline.json` moves with every re-record. `thresholds` only moves
when somebody decides it should — that is the contract, and an unknown key in it
is an error rather than a check that silently stopped running.
