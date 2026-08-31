"""Score severity verdicts against the synthetic scenario suite.

The rules pass is the floor and must stay at 100% (the pytest suite enforces
that in the gate); this script exists for the other half of the comparison —
scoring the LIVE AI provider against the same ground truth, so "does the model
beat the rules yet" is a number that can be re-measured after every provider or
prompt change instead of a remembered anecdote.

    python -m scripts.eval.score_severity                # rules only, offline
    python -m scripts.eval.score_severity --ai           # adds live provider calls
    python -m scripts.eval.score_severity --json out.json

Exit code is non-zero when the rules score below 100%, so the ops host can cron
it as a canary. --ai needs the deployment's AI env (key, URL, model) and spends
real tokens: one call per scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO_DIR = _REPO_ROOT / "tests" / "synthetic" / "severity" / "scenarios"

# Config validation needs a DATABASE_URL; scoring rules never connects.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://eval:eval@localhost:5432/eval")


def _load_scenarios(directory: Path) -> list[dict[str, Any]]:
    scenarios = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]
    if not scenarios:
        raise SystemExit(f"no scenarios found under {directory}")
    return scenarios


def _score_rules(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from services.analysis.ai_analyzer import analyze_with_rules

    rows = []
    for scenario in scenarios:
        verdict = analyze_with_rules(dict(scenario["payload"]), scenario["source"])
        rows.append(
            {
                "id": scenario["id"],
                "expected": scenario["expected"]["importance"],
                "rules": str(verdict.get("importance", "")),
                "rules_match": str(verdict.get("importance", "")) == scenario["expected"]["importance"],
            }
        )
    return rows


async def _score_ai(scenarios: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    """Best-effort live-provider scoring; failures are recorded, not raised."""
    from core.app_context import AppContext, get_default_app_context, set_default_app_context
    from core.config import get_settings
    from services.analysis.ai_llm_client import _call_ai_with_retry, initialize_openai_client

    # A bare process has no AppContext; the AI client's http client lives there.
    if get_default_app_context() is None:
        set_default_app_context(AppContext(config=get_settings()))
    await initialize_openai_client()
    for scenario, row in zip(scenarios, rows, strict=True):
        try:
            result, _tokens_in, _tokens_out = await _call_ai_with_retry(dict(scenario["payload"]), scenario["source"])
            ai_importance = str(result.get("importance", "")).lower().rsplit(".", 1)[-1]
        except Exception as error:  # noqa: BLE001 - a scorer records failures, it does not crash on one
            row["ai"] = f"error:{type(error).__name__}"
            row["ai_match"] = False
            continue
        row["ai"] = ai_importance
        row["ai_match"] = ai_importance == scenario["expected"]["importance"]


def _print_report(rows: list[dict[str, Any]], *, with_ai: bool) -> None:
    width = max(len(row["id"]) for row in rows)
    for row in rows:
        line = f"{row['id']:<{width}}  expected={row['expected']:<6}  rules={row['rules']:<6}"
        line += " ok" if row["rules_match"] else " RULES-MISS"
        if with_ai:
            line += f"  ai={row.get('ai', '-'):<18}" + (" ok" if row.get("ai_match") else " AI-MISS")
        print(line)
    total = len(rows)
    rules_hits = sum(1 for row in rows if row["rules_match"])
    print(f"\nrules: {rules_hits}/{total} ({rules_hits / total:.0%})")
    if with_ai:
        ai_hits = sum(1 for row in rows if row.get("ai_match"))
        print(f"ai:    {ai_hits}/{total} ({ai_hits / total:.0%})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios", type=Path, default=_SCENARIO_DIR)
    parser.add_argument("--ai", action="store_true", help="also score the live AI provider (spends tokens)")
    parser.add_argument("--json", type=Path, default=None, help="write the full report to this path")
    args = parser.parse_args(argv)

    scenarios = _load_scenarios(args.scenarios)
    rows = _score_rules(scenarios)
    if args.ai:
        asyncio.run(_score_ai(scenarios, rows))
    _print_report(rows, with_ai=args.ai)

    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"report written to {args.json}")

    return 0 if all(row["rules_match"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
