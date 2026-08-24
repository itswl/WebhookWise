#!/usr/bin/env python3
"""
Compare deep-analysis report quality either side of an engine cutover.

Usage:
    python -m scripts.ops.engine_quality_compare --cutover 2026-08-24T08:20

Examples:
    # Did report quality survive the provider swap?
    python -m scripts.ops.engine_quality_compare --cutover 2026-08-24T08:20

    # Machine-readable, for a note or an eval run
    python -m scripts.ops.engine_quality_compare --cutover 2026-08-24T08:20 --json

    # Widen the windows when traffic is thin (default 14 days each side)
    python -m scripts.ops.engine_quality_compare --cutover 2026-08-24T08:20 --days 30

WHY THIS EXISTS

The investigator's reports are not decoration. They are the free labels
severity_calibration scores the cheap verdict against, and the source every
runbook is distilled from. So the model behind them is load-bearing, and this
estate has changed it three times in a month — Anthropic, then DeepSeek, then
BigModel — with no measurement either side of any of those switches.

A swap is cheap to do and expensive to get wrong, and the failure is quiet: a
weaker model still returns a report, still parses, still fills the column. What
degrades is whether the report FOUND anything, and nobody notices for weeks
because the pipeline stays green.

Written as a script rather than a number in a note because the number expires:
the useful window rolls forward, and by the time a swap is worth judging the
baseline has fallen out of it. This recomputes both sides on demand.

WHAT IT DELIBERATELY DOES NOT DO

It does not score correctness. Nothing here knows whether a root cause was the
right one; that judgement lives in the run rulings a person or a patrol files on
the investigator's side. This measures the things a machine CAN see — did the
report arrive, did it fill its contract, how long did it take, how much did it
say — which is enough to catch a regression and not enough to declare a winner.

Read-only: no writes, safe to run against production.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# The v1 report contract. A model that returns prose instead of these is a
# regression the renderer hides, because it falls back to showing the prose.
_CONTRACT_FIELDS = ("summary", "root_cause", "impact")
_CONTRACT_LISTS = ("recommendations", "evidence", "next_checks")


def _side(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one side of the cutover."""
    total = len(rows)
    if not total:
        return {"analyses": 0}
    filled: dict[str, int] = {}
    for field in _CONTRACT_FIELDS:
        filled[field] = sum(1 for r in rows if str(r.get(field) or "").strip())
    for field in _CONTRACT_LISTS:
        filled[field] = sum(1 for r in rows if isinstance(r.get(field), list) and r[field])
    durations = [float(r["duration_seconds"]) for r in rows if r.get("duration_seconds")]
    sizes = [int(r["size_bytes"]) for r in rows if r.get("size_bytes")]
    return {
        "analyses": total,
        "engines": sorted({str(r.get("engine") or "?") for r in rows}),
        # Percentages, because the two windows will not hold the same volume and
        # a raw count either side invites the wrong comparison.
        "contract_filled_pct": {k: round(v / total * 100, 1) for k, v in filled.items()},
        "median_seconds": round(sorted(durations)[len(durations) // 2], 1) if durations else None,
        "median_bytes": sorted(sizes)[len(sizes) // 2] if sizes else None,
        "failed_pct": round(sum(1 for r in rows if r.get("analysis_failed")) / total * 100, 1),
    }


async def _collect(cutover: datetime, days: int) -> dict[str, Any]:
    from sqlalchemy import text

    from db.session import session_scope

    query = text(
        """
        SELECT created_at, engine, duration_seconds,
               length(analysis_result::text)          AS size_bytes,
               analysis_result ->> 'summary'          AS summary,
               analysis_result ->> 'root_cause'       AS root_cause_text,
               analysis_result ->  'root_cause'       AS root_cause_obj,
               analysis_result ->> 'impact'           AS impact_text,
               analysis_result ->  'impact'           AS impact_obj,
               analysis_result ->  'recommendations'  AS recommendations,
               analysis_result ->  'evidence'         AS evidence,
               analysis_result ->  'next_checks'      AS next_checks,
               analysis_result ->> 'analysis_failed'  AS analysis_failed
          FROM deep_analyses
         WHERE status = 'completed'
           AND created_at >= :start AND created_at < :end
        """
    )

    async def window(start: datetime, end: datetime) -> list[dict[str, Any]]:
        async with session_scope() as session:
            rows = (await session.execute(query, {"start": start, "end": end})).mappings().all()
        shaped: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            # root_cause and impact became OBJECTS in contract v2 and were strings
            # before. Either shape counts as filled — the question here is whether
            # the model answered the field, not which contract version it used.
            for field in ("root_cause", "impact"):
                item[field] = item.pop(f"{field}_text", None) or item.pop(f"{field}_obj", None)
            item["analysis_failed"] = str(item.get("analysis_failed") or "").lower() == "true"
            shaped.append(item)
        return shaped

    before = await window(cutover - timedelta(days=days), cutover)
    after = await window(cutover, cutover + timedelta(days=days))
    return {
        "cutover": cutover.isoformat(),
        "window_days_each_side": days,
        "before": _side(before),
        "after": _side(after),
    }


def _render(report: dict[str, Any]) -> str:
    lines = [
        "",
        f"  Deep-analysis report quality around {report['cutover']}",
        f"  (up to {report['window_days_each_side']} days each side, completed analyses only)",
        "",
    ]
    before, after = report["before"], report["after"]
    if not after.get("analyses"):
        lines.append("  Nothing has run since the cutover yet — the baseline below is what to compare against.")
        lines.append("")
    header = f"  {'':22} {'before':>12} {'after':>12}"
    lines.append(header)
    lines.append(f"  {'-' * 22} {'-' * 12:>12} {'-' * 12:>12}")

    def row(label: str, key: str, suffix: str = "") -> str:
        b, a = before.get(key), after.get(key)
        fb = "—" if b is None else f"{b}{suffix}"
        fa = "—" if a is None else f"{a}{suffix}"
        return f"  {label:22} {fb:>12} {fa:>12}"

    lines.append(row("analyses", "analyses"))
    lines.append(row("median seconds", "median_seconds"))
    lines.append(row("median bytes", "median_bytes"))
    lines.append(row("declared failed", "failed_pct", "%"))
    lines.append("")
    lines.append("  contract fields filled")
    for field in (*_CONTRACT_FIELDS, *_CONTRACT_LISTS):
        b = before.get("contract_filled_pct", {}).get(field)
        a = after.get("contract_filled_pct", {}).get(field)
        fb = "—" if b is None else f"{b}%"
        fa = "—" if a is None else f"{a}%"
        lines.append(f"  {'  ' + field:22} {fb:>12} {fa:>12}")
    lines.append("")
    lines.append(f"  engines  before={before.get('engines') or '—'}  after={after.get('engines') or '—'}")
    lines.append("")
    lines.append("  A drop in a filled-field percentage is the signal. Bytes and seconds moving is")
    lines.append("  not: a terser model that still answers every field has not regressed, and")
    lines.append("  neither has a slower one. Correctness is not measured here — that is what the")
    lines.append("  run rulings on the investigator's own ledger are for.")
    lines.append("")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cutover", required=True, help="ISO timestamp of the engine change, e.g. 2026-08-24T08:20")
    parser.add_argument("--days", type=int, default=14, help="window each side of the cutover (default 14)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()

    # Same bootstrap as the other ops scripts: the session factory reads its
    # settings from the AppContext, which nothing has built in a bare process.
    from core.app_context import init_default_app_context

    init_default_app_context()

    report = await _collect(datetime.fromisoformat(args.cutover), args.days)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json else _render(report))


if __name__ == "__main__":
    asyncio.run(main())
