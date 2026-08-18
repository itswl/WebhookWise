# The AI engineering surface

A model call is the cheap part of this system. Everything that makes the call
trustworthy — what reaches the prompt, what the answer is allowed to decide, what
it cost, who can correct it, and how a change to any of that is proved before it
ships — is the engineering. This document is the map of that layer, with the one
question asked of every mechanism: **what consumes it?**

`CLAUDE.md` states the rule for observability instruments — nothing new unless a
dashboard, alert, SLO, or automated decision reads it. The same rule applies
here. A mechanism nobody reads is a demo, and this page names the reader for each
one.

## The judgement everything hangs on

Alert **importance** (`high` | `medium` | `low`) selects forward rules, decides
what a silence swallows, and a `high` verdict is what triggers deep analysis. Get
it wrong low and the alert reaches nobody, silently. That asymmetry is why the
layer below exists in the shape it does.

## What runs around the model

| Mechanism | Where | What consumes it |
| --- | --- | --- |
| **Cheap-pass routing** — a deterministic rule pass answers low-value alerts; the paid model is never called for them | `services/analysis/ai_analyzer.py` (`_maybe_route_to_rules`) | AI spend; `route="rule_routed"` in the decision trace and the cost view |
| **Structured output** — the model must return a typed `WebhookAnalysisResult`, via instructor, with a configurable strictness mode | `services/analysis/ai_llm_client.py`, `schemas/analysis.py` | Every downstream reader: forwarding, dedup, persistence. A free-text answer would have no consumer |
| **Prompt-injection defence** — attacker-controllable payload text is defanged before interpolation, on both analysis legs | `services/analysis/prompt_safety.py` | The importance verdict itself: a crafted alert could otherwise steer its own routing |
| **Retrieval (RAG)** — the alert's identity is embedded, KB chunks cosine-ranked, the top context added to the prompt | `services/kb/retrieval.py`, `knowledge_base/seed/` | The analysis prompt's `{kb_context}` block; the `search_knowledge_base` MCP tool |
| **Memory** — incident conclusions are written back into the knowledge base | `services/kb/incident_sediment.py` | The next retrieval for a similar alert |
| **Human corrections (hard)** — an operator's importance correction is stored against the condition's identity and applied to every later firing of it, visibly | `services/analysis/importance_overrides.py`, `models/operations.py` | The pipeline's analysis stage; `importance_override` in the decision trace; `hit_count` answers whether an override still earns its place |
| **Human corrections (generalizing)** — what operators decided about *other instances of the same rule*, stated to the model as a prior it may disagree with | `services/analysis/correction_prior.py` | The prompt's `{correction_prior}` block; `_correction_prior.followed` on the stored analysis; `prior_shown` / `prior_followed` in the eval |
| **Cost governance** — per-alert token/cost recorded on the analysis; a monthly budget that degrades to rules instead of overspending | `services/analysis/ai_usage.py`, `ai_budget.py` | The AI cost view, `get_ai_cost_stats`, and the budget brake itself |
| **Failure isolation** — a circuit breaker on the provider, retries with jitter, and an explicit degraded route with a reason | `services/analysis/circuit_breakers.py`, `ai_analyzer.py` | `degraded_reason` in the decision trace; the AI-error notification |
| **Reuse** — Redis-cached analyses keyed on alert identity *and* prompt fingerprint, so editing a prompt invalidates stale answers | `services/analysis/ai_cache.py`, `ai_prompt.py` | `route="cache"`; the cache-hit share of the cost view |
| **Provenance** — which prompt text produced an analysis, and what it spent | `services/webhooks/types.py` (`set_analysis_prompt`, `set_analysis_usage`) | The question asked when a report reads wrong |
| **Decision trace** — one queryable record per alert of the ordered gate decisions and the AI-quality signals | `models/decision_trace.py` | The dashboard's quality view, `get_alert_decision_trace`, and every investigation recipe |
| **Agentic deep analysis** — a multi-step investigator behind a neutral gateway contract, triggered only for alerts that earn it | `services/analysis/deep_analysis_*.py` | The deep-analysis report rendered into the Chinese incident report |
| **Agent surface (MCP)** — 20 tools, 2 resources, 2 prompt templates over the existing query layer | `api/mcp/` | Any MCP client; the three repo skills in `.claude/skills/` |
| **Approval-gated action** — an agent proposes an Action Center command; a person approves; the existing executor runs it | `services/operations/remediation_proposals.py` | [`docs/features/approval-gated-remediation.md`](../features/approval-gated-remediation.md) |
| **Offline eval + gate** — a frozen corpus of labelled alerts, replayed and held to recorded thresholds in CI | `evals/`, `scripts/eval_analysis.py` | `scripts/gate.sh` and ci.yml's test job |

## What proves a change

Three layers, and they answer different questions:

1. **The offline eval** (`evals/`) — before the change ships. Replays a frozen
   corpus through the rule engine and fails the gate if the importance verdict
   regressed. Deterministic and offline: the keyword policy comes from the
   committed defaults, so CI and a laptop agree. `high_recall` is the headline;
   `max_miss_rate` is pinned at zero.
2. **The decision trace** (`models/decision_trace.py`) — after it ships, per
   alert. Route, override, degradation and skip reason are flattened columns, so
   "how is the AI judging" is a `GROUP BY`, not a log search.
3. **Operator feedback** (`analysis_feedback`, `importance_overrides`) — after it
   ships, per judgement. A correction both fixes the next occurrence and becomes
   a label the eval can mine (`scripts/eval_analysis.py export`).

The loop closes: corrections become labels, labels become the eval's thresholds,
the thresholds gate the next prompt change.

## Honest limits

- **The seed eval corpus is a behaviour lock, not a quality measurement.** It
  scores 100% because it contains cases the engine gets right on purpose — each
  one recording something production paid to discover. A real quality number
  needs labelled production traffic.
- **The model leg of the eval does not gate.** It costs money per run and does
  not repeat exactly. It informs; a flaky gate gets disabled within a month.
- **Labels are a biased sample.** They come from corrections, so conditions
  nobody bothered to correct are absent, and the corpus over-represents alerts
  somebody was annoyed by.
- **Deep analysis is sparse by design.** Most alerts carry only the lightweight
  verdict. That is a cost decision, not a coverage gap.
- **The correction prior is off by default and unproven.** `prior_followed` is
  the number that will decide whether it stays; it has no production data yet.

## Reading order

- [`../features/approval-gated-remediation.md`](../features/approval-gated-remediation.md)
  — the propose → approve → execute loop.
- [`../../evals/README.md`](../../evals/README.md) — the corpus format, what the
  metrics mean, and what gates.
- [`../reference/mcp.md`](../reference/mcp.md) — the agent surface, tool by tool.
- [`../../.agents/notes/`](../../.agents/notes/) — the decisions behind all of
  it, including the rejected ones a commit log never keeps.
