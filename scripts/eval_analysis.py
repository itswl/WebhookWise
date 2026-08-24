#!/usr/bin/env python3
"""Offline evaluation of the alert-importance verdict, plus the gate that guards it.

Every prompt edit, keyword change, model swap and routing tweak moves the one
judgement the whole pipeline is built on: how important an alert is. Importance
drives forwarding, silencing and whether deep analysis runs at all, so a change
that quietly lowers it stops alerts reaching anybody.

`analysis_feedback` and `/v1/decision-quality` already answer "how is the AI
judging" — after the fact, on alerts already delivered or already suppressed.
This answers it before the change ships, by replaying a frozen corpus of real
alerts through an analysis engine and scoring the verdict against the label an
operator gave it.

    python3 scripts/eval_analysis.py run                 # rule engine, declared policy
    python3 scripts/eval_analysis.py run --report        # list every disagreement
    python3 scripts/eval_analysis.py run --policy env    # score THIS deployment's tuning
    python3 scripts/eval_analysis.py run --engine ai     # real model calls; costs money
    python3 scripts/eval_analysis.py baseline --write    # re-record after an intended move
    python3 scripts/eval_analysis.py export --limit 500  # mine new cases from the database

The `rules` engine under `--policy default` is deterministic and needs no
database, Redis, API key or `.env`, which is what lets the gate and CI run it on
every change: the score depends on the corpus and the committed defaults, not on
the machine. `--policy env` scores the keywords a deployment actually runs, and
`--engine ai` spends money and does not repeat exactly, so neither one gates.

Errors here are not symmetric and the score must not pretend they are. An
under-call reaches nobody; an over-call costs attention. They are counted
separately and held to separate thresholds, and `high_recall` — of the alerts an
operator called high, how many the engine also called high — is the headline.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set before any core module is imported: the analysis modules configure logging
# at import time, which builds the settings object. DATABASE_URL only has to
# parse — nothing in a scoring run opens a connection, and the verdict itself is
# read from an injected policy, not from configuration.
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://eval:eval@localhost:5432/eval")

from core import json  # noqa: E402

DEFAULT_CORPUS = ROOT / "evals" / "analysis_cases.jsonl"
DEFAULT_BASELINE = ROOT / "evals" / "baseline.json"

# Ordered, so an under-call and an over-call can be told apart.
IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}
TRIAGE_VERDICTS = ("act_now", "monitor", "defer")
ENGINES = ("rules", "ai")
POLICIES = ("default", "env")

# Threshold keys the gate understands. A typo in the baseline must fail loudly
# rather than silently disable the check it was meant to tighten.
THRESHOLD_KEYS = ("min_labeled", "min_exact_rate", "min_high_recall", "max_over_rate", "max_miss_rate")


class CorpusError(Exception):
    """The corpus or baseline is malformed, so no score from it can be trusted."""


# ── Corpus ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One replayable alert, with the verdict an operator says it deserves.

    `expected_importance` is None for an unlabelled case. Those are allowed on
    purpose: dumping real traffic in is cheap and labelling it is the slow part,
    so the corpus has to be useful while it is half-labelled. Unlabelled cases
    are replayed (they still catch a crash on a real payload shape) and excluded
    from every rate.
    """

    id: str
    source: str
    parsed_data: dict[str, Any]
    expected_importance: str | None = None
    expected_triage: str | None = None
    origin: str = ""

    @property
    def labeled(self) -> bool:
        return self.expected_importance is not None


def _parse_case(raw: Any, line_no: int) -> tuple[EvalCase | None, list[str]]:
    where = f"line {line_no}"
    if not isinstance(raw, dict):
        return None, [f"{where}: each line must be a JSON object"]

    problems: list[str] = []
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        problems.append(f"{where}: needs a non-empty string id")
        case_id = ""
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        problems.append(f"{where}: needs a non-empty string source")
        source = ""
    parsed_data = raw.get("parsed_data")
    if not isinstance(parsed_data, dict):
        problems.append(f"{where}: parsed_data must be an object")
        parsed_data = {}

    expected = raw.get("expected")
    importance: str | None = None
    triage: str | None = None
    if expected is not None:
        if not isinstance(expected, dict):
            problems.append(f"{where}: expected must be an object when present")
        else:
            importance = expected.get("importance")
            if importance is not None and importance not in IMPORTANCE_RANK:
                problems.append(f"{where}: expected.importance {importance!r} not one of {', '.join(IMPORTANCE_RANK)}")
                importance = None
            triage = expected.get("triage_verdict")
            if triage is not None and triage not in TRIAGE_VERDICTS:
                problems.append(f"{where}: expected.triage_verdict {triage!r} not one of {', '.join(TRIAGE_VERDICTS)}")
                triage = None

    if problems:
        return None, problems
    return (
        EvalCase(
            id=str(case_id),
            source=str(source),
            parsed_data=parsed_data,
            expected_importance=importance,
            expected_triage=triage,
            origin=str(raw.get("origin") or ""),
        ),
        [],
    )


def load_corpus(path: Path) -> list[EvalCase]:
    """Read a JSONL corpus, or raise with every problem found at once."""
    if not path.exists():
        raise CorpusError(f"corpus not found: {path}")

    cases: list[EvalCase] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        # A '#' line is a section header an operator wrote while labelling.
        if not stripped or stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as e:
            problems.append(f"line {line_no}: invalid JSON ({e})")
            continue
        case, case_problems = _parse_case(raw, line_no)
        problems.extend(case_problems)
        if case is None:
            continue
        first = seen.get(case.id)
        if first is not None:
            # Duplicated ids make a report unreadable: two rows, one name, and
            # no way to tell which case the disagreement belongs to.
            problems.append(f"line {line_no}: duplicate id {case.id!r} (first seen on line {first})")
            continue
        seen[case.id] = line_no
        cases.append(case)

    if problems:
        raise CorpusError(f"{path}:\n  " + "\n  ".join(problems))
    if not cases:
        raise CorpusError(f"{path}: corpus is empty")
    return cases


# ── Scoring ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    origin: str
    expected_importance: str | None
    predicted_importance: str
    expected_triage: str | None
    predicted_triage: str
    verdict: str  # match | miss | overcall | unlabeled | error
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    # Whether a correction prior was shown for this case, and whether the model
    # then agreed with it. The prior only exists to move verdicts; without these
    # two counts, "is it worth its tokens" has no answer.
    prior_shown: bool = False
    prior_followed: bool = False


def classify(expected: str | None, predicted: str) -> str:
    """Name the disagreement, keeping the direction — it decides who is hurt."""
    if expected is None:
        return "unlabeled"
    if expected == predicted:
        return "match"
    return "miss" if IMPORTANCE_RANK[predicted] < IMPORTANCE_RANK[expected] else "overcall"


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


# The committed material that steers the model. Not the deployed model name —
# that is an env var, and a gate that runs offline in CI cannot see it (the
# runtime drift is what scripts/ops/engine_quality_compare.py is for). What CAN
# be gated is this: nobody edits a prompt, or the declared default model, and
# leaves the recorded ai score describing the previous one.
STEERING_SOURCES = (
    "prompts/webhook_analysis_detailed.txt",
    "prompts/deep_analysis.txt",
)
STEERING_DEFAULTS = ("AI_SYSTEM_PROMPT", "OPENAI_MODEL", "AI_USER_PROMPT_FILE")


def steering_fingerprint() -> str:
    """A digest of everything committed that decides what the model is told.

    Offline and deterministic: reads files and the declared defaults, never the
    environment. Truncated to 16 hex chars — this identifies a revision, it does
    not defend against anyone forging one.
    """
    import hashlib

    from core.config import defaults as config_defaults

    digest = hashlib.sha256()
    for rel in STEERING_SOURCES:
        path = ROOT / rel
        digest.update(rel.encode("utf-8"))
        digest.update(path.read_bytes() if path.exists() else b"<absent>")
    fields = config_defaults.AppConfig.model_fields
    for name in STEERING_DEFAULTS:
        field = fields.get(name)
        digest.update(name.encode("utf-8"))
        digest.update(str(field.default if field is not None else "<absent>").encode("utf-8"))
    return digest.hexdigest()[:16]


@dataclass(slots=True)
class Report:
    engine: str
    policy: str
    model: str = ""
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def labeled(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.expected_importance is not None and o.error is None]

    def metrics(self) -> dict[str, Any]:
        labeled = self.labeled
        exact = sum(1 for o in labeled if o.verdict == "match")
        misses = sum(1 for o in labeled if o.verdict == "miss")
        overcalls = sum(1 for o in labeled if o.verdict == "overcall")
        high = [o for o in labeled if o.expected_importance == "high"]
        high_recalled = sum(1 for o in high if o.predicted_importance == "high")
        triage = [o for o in self.outcomes if o.expected_triage is not None and o.error is None]
        triage_exact = sum(1 for o in triage if o.expected_triage == o.predicted_triage)
        errors = sum(1 for o in self.outcomes if o.error is not None)
        return {
            "engine": self.engine,
            "policy": self.policy,
            "total": len(self.outcomes),
            "labeled": len(labeled),
            "unlabeled": len(self.outcomes) - len(labeled) - errors,
            "errors": errors,
            "exact": exact,
            "exact_rate": _rate(exact, len(labeled)),
            "misses": misses,
            "miss_rate": _rate(misses, len(labeled)),
            "overcalls": overcalls,
            "over_rate": _rate(overcalls, len(labeled)),
            "high_labeled": len(high),
            "high_recall": _rate(high_recalled, len(high)),
            "triage_labeled": len(triage),
            "triage_exact_rate": _rate(triage_exact, len(triage)),
            "prior_shown": sum(1 for o in self.outcomes if o.prior_shown),
            "prior_followed": sum(1 for o in self.outcomes if o.prior_followed),
            "tokens_in": sum(o.tokens_in for o in self.outcomes),
            "tokens_out": sum(o.tokens_out for o in self.outcomes),
            "cost_usd": round(sum(o.cost_usd for o in self.outcomes), 6),
            # WHICH model produced this score. The fingerprint gate can only see
            # committed material, so a swap done by editing an env var slips past
            # it — recording the model here is what lets anyone reading the
            # baseline notice that it describes a provider production left behind.
            "model": self.model,
        }

    def disagreements(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.verdict in ("miss", "overcall", "error")]


# ── Engines ───────────────────────────────────────────────────────────────────


def _bootstrap_runtime() -> None:
    """Install an app context, so config-reading code paths have one.

    Only `--policy env`, the `ai` engine and `export` need it: an injected policy
    makes the rule pass read nothing from configuration at all.
    """
    from core.app_context import AppContext, get_default_app_context, set_default_app_context
    from core.config import get_settings

    if get_default_app_context() is None:
        set_default_app_context(AppContext(config=get_settings()))


def build_rule_policy(policy_name: str) -> Any:
    """The keyword policy to score against.

    `default` is built from the committed field defaults, deliberately ignoring
    the environment and `.env`. A gate whose score moves with whatever a
    developer has exported is not a gate; CI and a laptop have to agree.

    `env` is the other question, and a real one: what does THIS deployment,
    with its tuned keywords, actually do to the corpus.
    """
    from services.analysis.analysis_policies import RuleAnalysisPolicy

    if policy_name == "env":
        _bootstrap_runtime()
        return RuleAnalysisPolicy.from_config()

    from core.config.defaults import AIConfig
    from core.text import split_csv_lower

    def declared(name: str) -> Any:
        return AIConfig.model_fields[name].default

    return RuleAnalysisPolicy(
        high_keywords=tuple(split_csv_lower(declared("RULE_HIGH_KEYWORDS"))),
        content_high_keywords=tuple(split_csv_lower(declared("RULE_CONTENT_HIGH_KEYWORDS"))),
        warning_keywords=tuple(split_csv_lower(declared("RULE_WARN_KEYWORDS"))),
        metric_keywords=tuple(split_csv_lower(declared("RULE_METRIC_KEYWORDS"))),
        threshold_multiplier=float(declared("RULE_THRESHOLD_MULTIPLIER")),
    )


def _outcome(case: EvalCase, analysis: dict[str, Any], **extra: Any) -> CaseOutcome:
    predicted = str(analysis.get("importance", "")).lower().rsplit(".", 1)[-1]
    triage = str(analysis.get("triage_verdict", "")).lower().rsplit(".", 1)[-1]
    prior = analysis.get("_correction_prior")
    if isinstance(prior, dict):
        extra["prior_shown"] = True
        extra["prior_followed"] = bool(prior.get("followed"))
    return CaseOutcome(
        case_id=case.id,
        origin=case.origin,
        expected_importance=case.expected_importance,
        predicted_importance=predicted,
        expected_triage=case.expected_triage,
        predicted_triage=triage,
        verdict=classify(case.expected_importance, predicted),
        **extra,
    )


def run_rules(cases: Sequence[EvalCase], policy_name: str) -> Report:
    from services.analysis.ai_analyzer import analyze_with_rules

    policy = build_rule_policy(policy_name)
    report = Report(engine="rules", policy=policy_name, model="")
    for case in cases:
        result = analyze_with_rules(case.parsed_data, case.source, policy=policy)
        report.outcomes.append(_outcome(case, dict(result)))
    return report


async def run_ai(cases: Sequence[EvalCase], concurrency: int) -> Report:
    """Replay through the real model. Costs money; never gates."""
    _bootstrap_runtime()
    from services.analysis import ai_llm_client
    from services.analysis.analysis_policies import AIProviderPolicy

    provider = AIProviderPolicy.from_config()
    if not provider.available:
        raise CorpusError(
            "the ai engine needs a usable provider: set OPENAI_API_KEY and ENABLE_AI_ANALYSIS=true, "
            "or score the rule engine instead"
        )

    report = Report(engine="ai", policy="env", model=getattr(provider, "model", "") or "")
    semaphore = asyncio.Semaphore(max(1, concurrency))
    ordered: list[CaseOutcome | None] = [None] * len(cases)

    async def one(index: int, case: EvalCase) -> None:
        async with semaphore:
            try:
                analysis, tokens_in, tokens_out = await ai_llm_client.call_ai_with_breaker(
                    case.parsed_data, case.source
                )
            except Exception as e:  # noqa: BLE001 - one bad case must not lose the run
                ordered[index] = CaseOutcome(
                    case_id=case.id,
                    origin=case.origin,
                    expected_importance=case.expected_importance,
                    predicted_importance="",
                    expected_triage=case.expected_triage,
                    predicted_triage="",
                    verdict="error",
                    error=f"{type(e).__name__}: {e}",
                )
                return
            ordered[index] = _outcome(
                case,
                dict(analysis),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=provider.cost_for_tokens(tokens_in, tokens_out),
            )

    await asyncio.gather(*(one(i, case) for i, case in enumerate(cases)))
    report.outcomes = [o for o in ordered if o is not None]
    return report


# ── Baseline gate ─────────────────────────────────────────────────────────────


def load_baseline(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CorpusError(f"baseline not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CorpusError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("engines"), dict):
        raise CorpusError(f"{path}: expected an object with an 'engines' object")
    for name, entry in data["engines"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("thresholds"), dict):
            raise CorpusError(f"{path}: engines.{name} needs a 'thresholds' object")
        unknown = sorted(set(entry["thresholds"]) - set(THRESHOLD_KEYS))
        if unknown:
            # Silently ignoring an unknown key is how a gate becomes decoration.
            raise CorpusError(f"{path}: engines.{name}.thresholds has unknown key(s): {', '.join(unknown)}")
    return data


def _number(value: float) -> str:
    """Counts read as counts; rates read as rates."""
    return str(int(value)) if value.is_integer() else str(value)


def check_thresholds(metrics: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    """Every threshold the baseline states, as failures in plain words."""
    failures: list[str] = []
    checks: tuple[tuple[str, str, str], ...] = (
        ("min_labeled", "labeled", "min"),
        ("min_exact_rate", "exact_rate", "min"),
        ("min_high_recall", "high_recall", "min"),
        ("max_over_rate", "over_rate", "max"),
        ("max_miss_rate", "miss_rate", "max"),
    )
    for key, metric_key, direction in checks:
        if key not in thresholds:
            continue
        limit = float(thresholds[key])
        actual = float(metrics[metric_key])
        if direction == "min" and actual < limit:
            failures.append(f"{metric_key} {_number(actual)} is below the required {_number(limit)}")
        elif direction == "max" and actual > limit:
            failures.append(f"{metric_key} {_number(actual)} exceeds the allowed {_number(limit)}")
    if metrics["errors"]:
        failures.append(f"{metrics['errors']} case(s) failed to score")
    return failures


def derive_thresholds(metrics: dict[str, Any]) -> dict[str, Any]:
    """Starting thresholds for an engine the baseline has never recorded.

    Deliberately loose on the aggregate rates and exact on `high_recall`: an
    engine that starts missing alerts it used to catch is never an acceptable
    drift, whatever the average does.
    """
    return {
        "min_labeled": metrics["labeled"],
        "min_exact_rate": max(0.0, round(float(metrics["exact_rate"]) - 0.05, 4)),
        "min_high_recall": metrics["high_recall"],
        "max_over_rate": round(float(metrics["over_rate"]) + 0.05, 4),
    }


# ── Output ────────────────────────────────────────────────────────────────────


def format_metrics(metrics: dict[str, Any]) -> str:
    lines = [
        f"engine={metrics['engine']} policy={metrics['policy']}",
        f"  cases            {metrics['total']} ({metrics['labeled']} labeled, "
        f"{metrics['unlabeled']} unlabeled, {metrics['errors']} errored)",
        f"  importance exact {metrics['exact']}/{metrics['labeled']}  rate={metrics['exact_rate']}",
        f"  under-called     {metrics['misses']}  miss_rate={metrics['miss_rate']}",
        f"  over-called      {metrics['overcalls']}  over_rate={metrics['over_rate']}",
        f"  high recall      {metrics['high_recall']} over {metrics['high_labeled']} high-labeled case(s)",
    ]
    if metrics["triage_labeled"]:
        lines.append(f"  triage exact     rate={metrics['triage_exact_rate']} over {metrics['triage_labeled']} case(s)")
    if metrics["prior_shown"]:
        lines.append(
            f"  correction prior shown for {metrics['prior_shown']} case(s), followed in {metrics['prior_followed']}"
        )
    if metrics["cost_usd"]:
        lines.append(
            f"  spend            ${metrics['cost_usd']} "
            f"({metrics['tokens_in']} in / {metrics['tokens_out']} out tokens)"
        )
    return "\n".join(lines)


def format_disagreements(report: Report) -> str:
    rows = report.disagreements()
    if not rows:
        return "  (no disagreements)"
    lines = []
    for outcome in rows:
        if outcome.error:
            lines.append(f"  ERROR    {outcome.case_id}: {outcome.error}")
            continue
        arrow = f"expected {outcome.expected_importance} -> got {outcome.predicted_importance}"
        origin = f"  [{outcome.origin}]" if outcome.origin else ""
        lines.append(f"  {outcome.verdict.upper():<8} {outcome.case_id}: {arrow}{origin}")
    return "\n".join(lines)


# ── Commands ──────────────────────────────────────────────────────────────────


def _score(args: argparse.Namespace) -> Report:
    cases = load_corpus(Path(args.corpus))
    if args.limit:
        cases = cases[: args.limit]
    if args.engine == "ai":
        return asyncio.run(run_ai(cases, args.concurrency))
    return run_rules(cases, args.policy)


def cmd_run(args: argparse.Namespace) -> int:
    report = _score(args)
    metrics = report.metrics()

    # The baseline records one specific measurement: the rule engine, the
    # declared policy, the whole committed corpus. Anything else is a different
    # question and must not be judged against those numbers.
    failures: list[str] = []
    gated = (
        args.engine == "rules"
        and args.policy == "default"
        and not args.limit
        and Path(args.corpus) == DEFAULT_CORPUS
        and not args.no_gate
    )
    if gated:
        baseline = load_baseline(Path(args.baseline))
        entry = baseline["engines"].get(args.engine)
        if entry is None:
            raise CorpusError(f"{args.baseline}: no thresholds recorded for engine {args.engine!r}")
        failures = check_thresholds(metrics, entry["thresholds"])

    if args.json:
        print(json.dumps({"metrics": metrics, "failures": failures}, indent=True))
    else:
        print(format_metrics(metrics))
        if args.report:
            print("\ndisagreements:")
            print(format_disagreements(report))
        for failure in failures:
            print(f"  FAIL  {failure}")
        if not gated:
            # Never print GREEN for a run that checked nothing: a score without
            # a threshold behind it is a measurement, not a verdict.
            print(f"\nEVAL SCORED (not gated: {_ungated_reason(args)})")
        else:
            print("\nEVAL RED" if failures else "\nEVAL GREEN")
    return 1 if failures else 0


def _ungated_reason(args: argparse.Namespace) -> str:
    if args.no_gate:
        return "--no-gate"
    if args.engine == "ai":
        return "the ai engine is not deterministic"
    if args.policy == "env":
        return "--policy env scores this deployment, not the committed defaults"
    if args.limit:
        return "--limit scores part of the corpus"
    return "a corpus other than the committed one"


def cmd_assert_fresh(args: argparse.Namespace) -> int:
    """Fail when the prompts moved and the recorded ai score did not follow.

    The ai engine cannot gate directly: it spends money and does not repeat
    exactly, so it must not sit in CI. But the thing it measures — what the model
    is told — IS committed, and nothing forced a re-measurement when it changed.
    A code change here faces 1465 tests; a prompt rewrite faced nothing.

    So this is the same shape as the generated-reference check and the lockfile
    check: not "is the answer right" but "was the answer recomputed after the
    input moved". Offline, free, deterministic, safe in CI.
    """
    path = Path(args.baseline)
    baseline = load_baseline(path) if path.exists() else {"engines": {}}
    entry = baseline.get("engines", {}).get("ai") or {}
    recorded = str(entry.get("steering_fingerprint") or "")
    current = steering_fingerprint()

    if not recorded:
        # Deliberately a FAILURE and not a skip. A gate that reports "not
        # applicable" while the thing it guards is unguarded is how the ai path
        # stayed unmeasured in the first place; the fix is one command, and the
        # message says which.
        print("  FAIL  no ai baseline recorded, so nothing holds the prompts to a score")
        print(f"        steering fingerprint is now {current}")
        print("        record one:  python3 scripts/eval_analysis.py baseline --engine ai --write")
        print(
            f"        (needs a provider key and spends money — {len(STEERING_SOURCES)} prompt file(s) are being gated)"
        )
        return 1

    if recorded != current:
        print("  FAIL  the model's instructions changed after the ai score was recorded")
        print(f"        recorded against {recorded}, now {current}")
        print("        Something in the committed steering moved:")
        for rel in STEERING_SOURCES:
            print(f"          - {rel}")
        for name in STEERING_DEFAULTS:
            print(f"          - defaults.{name}")
        print("        Re-score and re-record:")
        print("          python3 scripts/eval_analysis.py run --engine ai --report")
        print("          python3 scripts/eval_analysis.py baseline --engine ai --write")
        return 1

    rec = entry.get("recorded") or {}
    model = rec.get("model") or "(model not recorded)"
    print(f"ai eval: recorded against the current prompts ({current}), on {model}")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    report = _score(args)
    metrics = report.metrics()
    path = Path(args.baseline)
    baseline: dict[str, Any] = {"engines": {}}
    if path.exists():
        baseline = load_baseline(path)

    entry = dict(baseline["engines"].get(args.engine) or {})
    thresholds = entry.get("thresholds") or derive_thresholds(metrics)
    # Keep whatever else the entry carries (the note explaining its thresholds):
    # re-recording a measurement must not delete the reasoning around it.
    entry.update({"recorded": metrics, "thresholds": thresholds})
    # Only the ai engine is steered by prompts; stamping the rules engine with a
    # prompt digest would make an unrelated prompt edit look like a stale score.
    if args.engine == "ai":
        entry["steering_fingerprint"] = steering_fingerprint()
    baseline["engines"][args.engine] = entry

    if not args.write:
        print(json.dumps(baseline, indent=True))
        print("\n(dry run; pass --write to record)")
        return 0

    try:
        path.write_text(json.dumps(baseline, indent=True) + "\n", encoding="utf-8")
    except OSError as error:
        # A paid measurement must not be lost to a write error. The ai engine
        # takes ten minutes and real money, and this failed once for the dullest
        # possible reason — recorded inside a container whose uid cannot write the
        # mounted evals/ directory, after every model call had already been made.
        # Print what was measured so the run is salvageable by hand.
        print(f"scored {args.engine}, but could not write {path}: {error}")
        print("The measurement is below — save it yourself rather than paying for it twice.\n")
        print(json.dumps(baseline, indent=True))
        return 1
    print(f"recorded {args.engine} into {path}")
    print(format_metrics(metrics))
    print(f"\nthresholds: {json.dumps(thresholds)}")
    if not entry.get("thresholds"):
        print("(derived from this run — review them, they are the contract from now on)")
    return 0


async def _export(args: argparse.Namespace) -> int:
    """Mine labelled cases out of the corrections operators already made.

    The best labels in the system are not written for an eval: they are the
    importance an operator corrected an alert to. `importance_overrides` and
    `analysis_feedback` both carry one, against an event whose payload is still
    on the row. Exported unlabelled, a case still earns its place — a real
    payload shape that must not crash the rule pass.
    """
    _bootstrap_runtime()
    from sqlalchemy import select

    from db.session import get_session_factory
    from models import AnalysisFeedback, ImportanceOverride, WebhookEvent

    seen_ids: set[str] = set()
    if args.merge and Path(args.corpus).exists():
        seen_ids = {case.id for case in load_corpus(Path(args.corpus))}

    lines: list[str] = []
    session_factory = get_session_factory()
    async with session_factory() as session:
        labels: dict[int, str] = {}
        if not args.unlabeled_only:
            overrides = (
                await session.execute(
                    select(ImportanceOverride.origin_event_id, ImportanceOverride.importance)
                    .where(ImportanceOverride.origin_event_id.is_not(None))
                    .order_by(ImportanceOverride.updated_at.desc())
                    .limit(args.limit)
                )
            ).all()
            for event_id, importance in overrides:
                if event_id is not None and str(importance).lower() in IMPORTANCE_RANK:
                    labels[int(event_id)] = str(importance).lower()

            feedback = (
                await session.execute(
                    select(AnalysisFeedback.resource_id, AnalysisFeedback.corrected_importance)
                    .where(
                        AnalysisFeedback.resource_type == "webhook_event",
                        AnalysisFeedback.corrected_importance.is_not(None),
                    )
                    .order_by(AnalysisFeedback.created_at.desc())
                    .limit(args.limit)
                )
            ).all()
            for event_id, importance in feedback:
                # An override is the stronger statement: it is still in force.
                if str(importance).lower() in IMPORTANCE_RANK:
                    labels.setdefault(int(event_id), str(importance).lower())

        wanted = list(labels)
        if not args.labeled_only:
            recent = (
                await session.execute(select(WebhookEvent.id).order_by(WebhookEvent.id.desc()).limit(args.limit))
            ).scalars()
            wanted += [event_id for event_id in recent if event_id not in labels]

        if not wanted:
            print("nothing to export")
            return 0

        rows = (
            await session.execute(
                select(WebhookEvent.id, WebhookEvent.source, WebhookEvent.parsed_data, WebhookEvent.timestamp).where(
                    WebhookEvent.id.in_(wanted)
                )
            )
        ).all()

    for event_id, source, parsed_data, timestamp in rows:
        if not isinstance(parsed_data, dict) or not parsed_data:
            continue
        case_id = f"event-{event_id}"
        if case_id in seen_ids:
            continue
        case: dict[str, Any] = {
            "id": case_id,
            "source": source or "unknown",
            "parsed_data": parsed_data,
            "origin": f"event {event_id} @ {timestamp}",
        }
        importance = labels.get(int(event_id))
        # An unlabelled case ships without an `expected` block at all, so the
        # thing a labeller has to do is visible: add one.
        if importance:
            case["expected"] = {"importance": importance}
            case["labeled_by"] = "operator-correction"
        lines.append(json.dumps(case, sort_keys=True))

    if not lines:
        print("nothing new to export")
        return 0

    out = Path(args.out) if args.out else Path(args.corpus)
    mode = "a" if args.merge or args.out is None else "w"
    with out.open(mode, encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    labelled = sum(1 for line in lines if '"expected"' in line)
    print(f"{'appended' if mode == 'a' else 'wrote'} {len(lines)} case(s) to {out} ({labelled} pre-labelled)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    return asyncio.run(_export(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_scoring_args(target: argparse.ArgumentParser) -> None:
        target.add_argument("--corpus", default=str(DEFAULT_CORPUS))
        target.add_argument("--baseline", default=str(DEFAULT_BASELINE))
        target.add_argument("--engine", choices=ENGINES, default="rules")
        target.add_argument("--policy", choices=POLICIES, default="default", help="keyword policy for the rule engine")
        target.add_argument("--limit", type=int, default=0, help="score only the first N cases (never gates)")
        target.add_argument("--concurrency", type=int, default=4, help="parallel model calls for --engine ai")

    run_parser = sub.add_parser("run", help="Score the corpus and check it against the baseline")
    add_scoring_args(run_parser)
    run_parser.add_argument("--report", action="store_true", help="list every disagreement")
    run_parser.add_argument("--no-gate", action="store_true", help="score without checking the baseline")
    run_parser.add_argument("--json", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    baseline_parser = sub.add_parser("baseline", help="Record the current score as the baseline")
    add_scoring_args(baseline_parser)
    baseline_parser.add_argument("--write", action="store_true", help="write the file (default is a dry run)")
    baseline_parser.set_defaults(func=cmd_baseline)

    fresh_parser = sub.add_parser(
        "assert-fresh",
        help="Fail if the committed prompts moved without the ai score being re-recorded",
    )
    fresh_parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    fresh_parser.set_defaults(func=cmd_assert_fresh)

    export_parser = sub.add_parser("export", help="Mine cases out of the database into a corpus")
    export_parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    export_parser.add_argument("--out", default=None, help="write here instead of appending to the corpus")
    export_parser.add_argument("--limit", type=int, default=200)
    export_parser.add_argument("--labeled-only", action="store_true", help="only events an operator corrected")
    export_parser.add_argument("--unlabeled-only", action="store_true", help="skip the correction lookup entirely")
    export_parser.add_argument(
        "--merge", action="store_true", default=True, help="skip ids the corpus already has (default)"
    )
    export_parser.set_defaults(func=cmd_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code: int = args.func(args)
    except CorpusError as e:
        print(f"  FAIL  {e}")
        return 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
