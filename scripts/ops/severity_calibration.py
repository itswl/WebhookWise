#!/usr/bin/env python3
"""
Score WebhookWise's own severity against the investigator that actually looked.

Usage:
    python -m scripts.ops.severity_calibration [OPTIONS]

Examples:
    # The report: per alert rule, what WebhookWise said vs what the reports said
    python -m scripts.ops.severity_calibration

    # Machine-readable, for a dashboard or an eval run
    python -m scripts.ops.severity_calibration --json

    # Only rules with at least this much evidence (default 3)
    python -m scripts.ops.severity_calibration --min-reports 5

WHY THIS EXISTS

Deep analysis produces a severity of its own, from a model that read the
payload, searched, and reasoned for minutes. WebhookWise produces one in
milliseconds from keywords. Nothing compared them, so nobody could say whether
the cheap judgement was any good — and measured over 80 production reports it
is not: 90% of alerts are filed `high`, and the investigator agrees on a
quarter of them.

That gap is free labelled data. This script turns it into a number per alert
rule, which is the grain a fix can act on.

WHAT IT DELIBERATELY DOES NOT DO

It proposes, it does not apply. A downgrade is a decision about who gets woken
up, and the direction of the error matters asymmetrically: over-escalating
costs a glance, under-escalating means nobody was told. So the proposal is
conservative by construction (see `_propose`) and an operator applies it.

Read-only: no writes, safe to run against production.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# The investigator answers on a four-level scale; WebhookWise stores three.
# Mapping critical onto high loses no safety: both mean "wake someone".
_TO_WW_SCALE = {"critical": "high", "high": "high", "medium": "medium", "low": "low"}
_RANK = {"low": 0, "medium": 1, "high": 2}
# A verdict of "unknown" means the investigation could not establish severity.
# Counted separately: it is evidence about the ALERT (it is unanswerable), not
# evidence about the severity, and folding it into either side would be a lie.
_UNKNOWN = "unknown"


async def _collect(min_reports: int) -> dict[str, Any]:
    from sqlalchemy import text

    from db.session import session_scope

    query = text(
        """
        SELECT
            COALESCE(NULLIF(e.parsed_data->>'RuleName', ''),
                     NULLIF(e.parsed_data->>'alert_name', ''),
                     NULLIF(e.parsed_data->>'AlertName', ''),
                     'unknown')                              AS rule_name,
            e.source                                          AS source,
            e.importance                                      AS ww_importance,
            LOWER(COALESCE(d.analysis_result->'impact'->>'severity', ''))  AS report_severity,
            d.webhook_event_id                                AS event_id,
            e.alert_hash                                      AS alert_hash
        FROM deep_analyses d
        JOIN webhook_events e ON e.id = d.webhook_event_id
        WHERE d.status = 'completed'
        """
    )
    async with session_scope() as session:
        rows = (await session.execute(query)).mappings().all()

    per_rule: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"source": "", "ww": Counter(), "report": Counter(), "unknown": 0, "events": [], "hashes": set()}
    )
    for row in rows:
        bucket = per_rule[row["rule_name"]]
        bucket["source"] = row["source"] or bucket["source"]
        bucket["ww"][str(row["ww_importance"] or "unknown")] += 1
        bucket["events"].append(row["event_id"])
        if row["alert_hash"]:
            bucket["hashes"].add(row["alert_hash"])
        mapped = _TO_WW_SCALE.get(row["report_severity"] or "")
        if mapped is None:
            bucket["unknown"] += 1
        else:
            bucket["report"][mapped] += 1

    report: dict[str, Any] = {
        "totals": {"reports": len(rows), "rules": len(per_rule)},
        "rules": [_score(name, data, min_reports) for name, data in per_rule.items()],
    }
    report["rules"].sort(key=lambda r: (-r["reports"], r["rule"]))
    ww_high = sum(1 for r in rows if r["ww_importance"] == "high")
    agreed = sum(1 for r in rows if _TO_WW_SCALE.get(r["report_severity"] or "") == "high")
    report["totals"]["ww_high"] = ww_high
    report["totals"]["report_high"] = agreed
    return report


def _propose(ww: str, verdicts: "Counter[str]", unknown: int, reports: int, min_reports: int) -> dict[str, Any]:
    """A conservative downgrade proposal, or none.

    Three guards, each there to stop a specific way this could page nobody:

    * enough evidence — one report is an anecdote;
    * never below the investigator's MEDIAN, so the majority of occurrences are
      still covered at the level a real investigation assigned them;
    * never downgrade a rule the investigator has ever called `high`
      more than a third of the time. A condition that is genuinely severe a
      third of the time must keep waking someone; the noise there has to be
      fixed by making the alert more specific, not by muting it.
    """
    if reports < min_reports or not verdicts:
        return {"action": "insufficient_evidence", "reports": reports}
    ranked = sorted(_RANK[v] for v in verdicts.elements())
    median = statistics.median_low(ranked)
    proposed = next(k for k, v in _RANK.items() if v == median)
    high_share = verdicts["high"] / max(1, sum(verdicts.values()))

    if _RANK.get(ww, 2) <= median:
        return {"action": "already_aligned", "reports": reports, "high_share": round(high_share, 2)}
    if high_share > 1 / 3:
        return {
            "action": "keep_high",
            "reason": f"the investigator called it high {high_share:.0%} of the time",
            "reports": reports,
            "high_share": round(high_share, 2),
        }
    if unknown > reports / 2:
        return {
            "action": "unanswerable",
            "reason": f"{unknown}/{reports + unknown} investigations could not establish severity",
            "reports": reports,
        }
    return {
        "action": "downgrade",
        "from": ww,
        "to": proposed,
        "reports": reports,
        "high_share": round(high_share, 2),
    }


def _score(name: str, data: dict[str, Any], min_reports: int) -> dict[str, Any]:
    verdicts: Counter[str] = data["report"]
    reports = sum(verdicts.values())
    ww_modal = data["ww"].most_common(1)[0][0] if data["ww"] else "unknown"
    return {
        "rule": name,
        "source": data["source"],
        "reports": reports,
        "ww_importance": ww_modal,
        "report_verdicts": dict(verdicts),
        "unknown_verdicts": data["unknown"],
        # How many DISTINCT conditions these reports covered. When this equals
        # the report count, alert_hash is per-occurrence and an alert_hash-keyed
        # override cannot generalise — the fix has to key on the rule name.
        "distinct_hashes": len(data["hashes"]),
        "proposal": _propose(ww_modal, verdicts, data["unknown"], reports, min_reports),
    }


def _render(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        "SEVERITY CALIBRATION — WebhookWise vs the investigator that looked",
        "",
        f"  {totals['reports']} completed investigations over {totals['rules']} alert rules",
        f"  WebhookWise said high: {totals['ww_high']}    the reports said high: {totals['report_high']}",
        "",
        f"  {'rule':<34} {'n':>3} {'ww':>7} {'report verdicts':<26} proposal",
        f"  {'-' * 34} {'-' * 3} {'-' * 7} {'-' * 26} {'-' * 34}",
    ]
    for row in report["rules"]:
        verdicts = " ".join(f"{k}:{v}" for k, v in sorted(row["report_verdicts"].items())) or "-"
        if row["unknown_verdicts"]:
            verdicts += f" ?:{row['unknown_verdicts']}"
        proposal = row["proposal"]
        if proposal["action"] == "downgrade":
            verdict = f"DOWNGRADE {proposal['from']} -> {proposal['to']}"
        elif proposal["action"] == "keep_high":
            verdict = "keep (genuinely severe sometimes)"
        elif proposal["action"] == "unanswerable":
            verdict = "unanswerable — stop investigating it"
        elif proposal["action"] == "already_aligned":
            verdict = "aligned"
        else:
            verdict = f"need more data ({proposal['reports']})"
        lines.append(f"  {row['rule'][:34]:<34} {row['reports']:>3} {row['ww_importance']:>7} {verdicts:<26} {verdict}")

    downgrades = [r for r in report["rules"] if r["proposal"]["action"] == "downgrade"]
    if downgrades:
        lines += ["", "  Per-occurrence identity check (does an alert_hash override generalise?)"]
        for row in downgrades:
            hashes, n = row["distinct_hashes"], row["reports"]
            note = "one hash per occurrence — needs a RULE-scoped fix" if hashes >= n else "hash-scoped override works"
            lines.append(f"    {row['rule'][:34]:<34} {hashes} distinct hashes / {n} reports — {note}")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--min-reports", type=int, default=3, help="evidence needed before proposing (default 3)")
    args = parser.parse_args()

    # Same bootstrap as the other ops scripts: the session factory reads its
    # settings from the AppContext, which nothing has built in a bare process.
    from core.app_context import init_default_app_context

    init_default_app_context()

    report = await _collect(args.min_reports)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json else _render(report))


if __name__ == "__main__":
    asyncio.run(main())
