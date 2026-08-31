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
| **Machine corrections (distillation)** — the cheap severity verdict is scored against the investigator that actually read the alert, and a per-rule ceiling applied where they disagree | `scripts/ops/severity_calibration.py`, `services/webhooks/types.py` (`apply_importance_cap`), `inbound_rules` | [`docs/features/inbound-rules.md`](../features/inbound-rules.md); `_importance_cap` on the stored analysis. Measured 2026-08: 90% of alerts were filed `high` and the reports agreed on 26% |
| **Cost governance** — per-alert token/cost recorded on the analysis; a monthly budget that degrades to rules instead of overspending | `services/analysis/ai_usage.py`, `ai_budget.py` | The AI cost view, `get_ai_cost_stats`, and the budget brake itself |
| **Failure isolation** — a circuit breaker on the provider, retries with jitter, and an explicit degraded route with a reason | `services/analysis/circuit_breakers.py`, `ai_analyzer.py` | `degraded_reason` in the decision trace; the AI-error notification |
| **Reuse** — Redis-cached analyses keyed on alert identity *and* prompt fingerprint, so editing a prompt invalidates stale answers | `services/analysis/ai_cache.py`, `ai_prompt.py` | `route="cache"`; the cache-hit share of the cost view |
| **Provenance** — which prompt text produced an analysis, and what it spent | `services/webhooks/types.py` (`set_analysis_prompt`, `set_analysis_usage`) | The question asked when a report reads wrong |
| **Decision trace** — one queryable record per alert of the ordered gate decisions and the AI-quality signals | `models/decision_trace.py` | The dashboard's quality view, `get_alert_decision_trace`, and every investigation recipe |
| **Agentic deep analysis** — a multi-step investigator behind a neutral gateway contract, triggered only for alerts that earn it | `services/analysis/deep_analysis_*.py` | The deep-analysis report rendered into the Chinese incident report |
| **Agent surface (MCP)** — 20 tools, 2 resources, 2 prompt templates over the existing query layer | `api/mcp/` | Any MCP client; the four repo skills in `.agents/skills/` |
| **Approval-gated action** — an agent proposes an Action Center command; a person approves; the existing executor runs it | `services/operations/remediation_proposals.py` | [`docs/features/approval-gated-remediation.md`](../features/approval-gated-remediation.md) |
| **Remediation readback** — an executed command is not "fixed" until a delayed readback of the TARGET says so; unrecovered raises a critical card | `services/operations/remediation_verification.py` | `verify_status` on the proposal row; the Action Center's unrecovered card |
| **Mode ladder** — risky automations carry an off/shadow/enforce mode; unknown values fail loudly to off | `services/operations/feature_modes.py` | Every shadow ledger below; the promotion decision in [`.agents/notes/`](../../.agents/notes/implemented/2026-08-31-shadow-first-promotion-needs-a-ladder.md) |
| **Dedup fingerprint (shadow-able)** — a source can name the payload fields that ARE its alert identity; shadow mode counts where that key disagrees with the built-in one | `services/dedup.py` | `dedup.fingerprint/diverged` signals; the enforce decision |
| **Synthetic severity evals** — 18 constructed scenarios with ground truth by construction, held at 100% for the rule floor; one flag re-measures the live model | `tests/synthetic/severity/`, `scripts/eval/score_severity.py` | `scripts/gate.sh`; the rules-vs-model gap number |
| **Reversible pseudonymization** — estate identifiers leave for the model as tokens and come back real; the map is stored, applied, then cleared | `services/analysis/pseudonymizer.py` | What the provider is allowed to see; the stored `pseudonym_map` until unmasking |
| **Offline eval + gate** — a frozen corpus of labelled alerts, replayed and held to recorded thresholds in CI | `evals/`, `scripts/eval_analysis.py` | `scripts/gate.sh` and ci.yml's test job |
| **Shadow code review** — a model reviews every PR diff for behavior, in a sticky advisory comment that cannot gate | `.github/workflows/ai-review.yml`, `scripts/ci/ai_review.sh` | The PR author; the sampled hit rate that decides promotion |

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
- **A shadow ledger nobody reads is default-off with extra steps.** Every
  mechanism in shadow owes a review date; the promotion ladder note records
  them.

## The vocabulary bridge

The mechanisms above were built need-first, but they land on the same patterns
the industry is converging on — Anthropic's [AI-native SDLC
playbook](https://claude.com/blog/the-ai-native-sdlc-playbook) and its
[SDLC-security account](https://claude.com/blog/how-anthropic-secures-its-ai-native-software-development-lifecycle)
describe them at the scale of ~80% model-authored merged code. The mapping,
for anyone arriving with that vocabulary:

| Industry term (those posts) | Here |
| --- | --- |
| Shadow mode — a new automation observes real traffic, acts on nothing, until sampled trust is earned | The mode ladder's `shadow`; the relay/judge shadow-run; the shadow code reviewer |
| Tiered autonomy / response tiers — observe, then read-only diagnosis, then action through pre-approved routes | Alert → hookprobe investigates read-only → Action Center proposal a person approves → executor runs it |
| Agents cannot deploy their own fixes | Approval-gated remediation: `propose_remediation` executes nothing |
| Verification loop — done means verified, not executed | Remediation readback: the TARGET confirms recovery or a critical card fires |
| Deterministic hooks over advisory guidance | The budget brake, the circuit breaker, `scripts/gate.sh` as an exact CI replica |
| Continuous evals gating configuration changes | The offline eval + `assert-fresh` on prompts; the synthetic severity floor |
| Versioned org knowledge as skills | `.agents/skills/`, the four operator skills shipping with the service |
| Artifact chain / decisions as audit trail | `.agents/notes/`, shape-enforced by the gate |
| Observable secret rotation | `WEBHOOK_SECRET_PREVIOUS` + the `allowed_previous_secret` counter as the cutover gauge |

The discipline is the same one this page opens with: a mechanism exists when
something consumes it. The posts' numbers (substantive-review share 16%→54%,
a third of past incidents catchable) are the shape of evidence each mapped
mechanism still owes here.

## Reading order

- [`../features/approval-gated-remediation.md`](../features/approval-gated-remediation.md)
  — the propose → approve → execute loop.
- [`../../evals/README.md`](../../evals/README.md) — the corpus format, what the
  metrics mean, and what gates.
- [`../reference/mcp.md`](../reference/mcp.md) — the agent surface, tool by tool.
- [`../../.agents/notes/`](../../.agents/notes/) — the decisions behind all of
  it, including the rejected ones a commit log never keeps.
