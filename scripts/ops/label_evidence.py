#!/usr/bin/env python3
"""
Emit, per alert rule, the label evidence WebhookWise owns — and nothing else.

Usage:
    python -m scripts.ops.label_evidence --json > /tmp/ww-evidence.json

Examples:
    # Human-readable, richest rules first
    python -m scripts.ops.label_evidence

    # For the judge's eval dataset to consume
    python -m scripts.ops.label_evidence --json > /tmp/ww-evidence.json

    # Widen or narrow the outcome window (default 30 days)
    python -m scripts.ops.label_evidence --days 90

WHY THIS EXISTS

The judge's eval corpus is 32 rules distilled from 795 real alerts, and every one
is unlabelled. Its README already refused the obvious shortcut: the `expect` block
arrives pre-filled from the system's own verdict, and scoring against that is the
model grading its own homework, so unreviewed rows are never scored.

That leaves the honest question — what DO we know about each rule that is not the
cheap verdict's own opinion? Measured on this deployment, the answer is bleaker
than it looks: `importance_overrides` has 0 rows, `analysis_feedback` has 4, and
every incident's `resolution_record` is `{}`. There are no human labels to mine.

So this emits the two kinds of evidence that are not the cheap verdict:

  TIER 1 — outcome facts. Not opinions: how often the rule fires, how much of that
  is recurrence, whether it actually reached anyone. Only fields this deployment
  really populates are here; `resolved_at` and `workflow_status` are ~0.4% filled,
  so "did it self-resolve" is NOT in this file. That fact lives in the judge's own
  ledger (`self_resolved`, `likely_flapping`), which is the service that owns it.

  TIER 2 — the investigator's verdict. AI, but ASYMMETRIC: a process with tool
  access that searched and reasoned for minutes, grading one that spent
  milliseconds on keywords. That asymmetry is what makes it evidence rather than
  circularity, and it is the same basis the severity calibration loop already acts
  on. It covers 22 of the 32 rules.

WHAT IT DELIBERATELY DOES NOT DO

It does not propose a label, and it does not rank rules by "probably wrong". A
file that suggested labels would be read as labels, and the whole reason the
corpus is still unlabelled is that its author refused exactly that. Adjudication
is the human's, and the disagreement queue that orders the work is assembled on
the judge's side, where the other two opinions live.

Read-only: no writes, safe against production.

The OUTPUT CONTAINS REAL RULE NAMES. Do not commit it — see
scripts/assert_no_estate_identifiers.py for what that costs in a public repo.
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# The investigator answers on a four-level scale; WebhookWise stores three.
_TO_WW_SCALE = {"critical": "high", "high": "high", "medium": "medium", "low": "low"}


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


async def _collect(days: int, with_reports: int = 0) -> dict[str, Any]:
    from sqlalchemy import text

    from db.session import session_scope
    from services.webhooks.types import CAP_IMPORTANCE

    # The rule expression is spelt out in each query rather than interpolated from
    # a constant. Interpolating it is what bandit flags as B608, and the
    # suppression cannot be placed on the line it reports — the opening of a
    # triple-quoted string. Three copies of four lines beats a rule nobody can
    # silence honestly; severity_calibration.py does the same.
    outcomes = text(
        """
        SELECT COALESCE(NULLIF(parsed_data->>'RuleName', ''),
                            NULLIF(parsed_data->>'alert_name', ''),
                            NULLIF(parsed_data->>'AlertName', ''),
                            'unknown') AS rule_name,
               source                                             AS source,
               count(*)                                           AS alerts,
               count(*) FILTER (WHERE is_duplicate)                AS duplicates,
               count(*) FILTER (WHERE duplicate_count > 1)         AS recurring,
               count(*) FILTER (WHERE forward_status = 'sent')     AS forwarded,
               count(*) FILTER (WHERE prev_alert_id IS NOT NULL)   AS chained,
               count(DISTINCT alert_hash)                          AS distinct_hashes,
               json_object_agg(importance, n) FILTER (WHERE importance IS NOT NULL) AS unused
          FROM webhook_events,
               LATERAL (SELECT 1 AS n) AS _
         WHERE created_at > now() - make_interval(days => :days)
         GROUP BY 1, 2
        """
    )
    # The importance distribution needs its own pass: aggregating it inside the
    # query above would need a second GROUP BY and this is a report, not a hot path.
    verdicts = text(
        """
        SELECT COALESCE(NULLIF(parsed_data->>'RuleName', ''),
                            NULLIF(parsed_data->>'alert_name', ''),
                            NULLIF(parsed_data->>'AlertName', ''),
                            'unknown') AS rule_name, importance, count(*) AS n
          FROM webhook_events
         WHERE created_at > now() - make_interval(days => :days)
         GROUP BY 1, 2
        """
    )
    reports = text(
        """
        SELECT COALESCE(NULLIF(e.parsed_data->>'RuleName', ''),
                            NULLIF(e.parsed_data->>'alert_name', ''),
                            NULLIF(e.parsed_data->>'AlertName', ''),
                            'unknown') AS rule_name,
               LOWER(COALESCE(d.analysis_result->'impact'->>'severity', '')) AS report_severity,
               LEFT(COALESCE(d.analysis_result->>'summary', ''), 400)        AS summary,
               d.created_at                                                  AS created_at
          FROM deep_analyses d
          JOIN webhook_events e ON e.id = d.webhook_event_id
         WHERE d.status = 'completed'
         ORDER BY d.created_at DESC
        """
    )
    caps = text(
        """
        SELECT match_rule_name, action_value
          FROM inbound_rules
         WHERE action = :action AND enabled AND COALESCE(match_rule_name, '') <> ''
        """
    )

    async with session_scope() as session:
        outcome_rows = (await session.execute(outcomes, {"days": days})).mappings().all()
        verdict_rows = (await session.execute(verdicts, {"days": days})).mappings().all()
        report_rows = (await session.execute(reports)).mappings().all()
        cap_rows = (await session.execute(caps, {"action": CAP_IMPORTANCE})).all()

    ww: dict[str, Counter[str]] = defaultdict(Counter)
    for row in verdict_rows:
        ww[row["rule_name"]][str(row["importance"] or "unknown")] += int(row["n"])

    investigator: dict[str, Counter[str]] = defaultdict(Counter)
    unanswerable: Counter[str] = Counter()
    # Newest first, capped: an adjudicator reads two or three, not twenty-five,
    # and the whole column is business detail in prose.
    excerpts: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in report_rows:
        mapped = _TO_WW_SCALE.get(row["report_severity"] or "")
        if mapped is None:
            unanswerable[row["rule_name"]] += 1
        else:
            investigator[row["rule_name"]][mapped] += 1
        if with_reports and len(excerpts[row["rule_name"]]) < with_reports and (row["summary"] or "").strip():
            excerpts[row["rule_name"]].append(
                {
                    "severity": mapped or "unanswerable",
                    "when": str(row["created_at"])[:16],
                    "summary": str(row["summary"]).strip(),
                }
            )

    capped = {str(name): str(value or "") for name, value in cap_rows}

    rules: dict[str, Any] = {}
    for row in outcome_rows:
        name = row["rule_name"]
        alerts = int(row["alerts"])
        rules[name] = {
            "source": row["source"],
            "tier1_outcome": {
                "alerts": alerts,
                "recurring_share": _share(int(row["recurring"]), alerts),
                "duplicate_share": _share(int(row["duplicates"]), alerts),
                "forwarded_share": _share(int(row["forwarded"]), alerts),
                "chained": int(row["chained"]),
                "distinct_hashes": int(row["distinct_hashes"]),
            },
            "tier2_investigator": {
                "reports": sum(investigator[name].values()) + unanswerable[name],
                "verdicts": dict(investigator[name]),
                "unanswerable": unanswerable[name],
                "excerpts": excerpts.get(name, []),
            },
            "cheap_verdict": dict(ww[name]),
            "capped_at": capped.get(name, ""),
        }
    return {
        "window_days": days,
        "rules": rules,
        "provenance": {
            "tier1_outcome": "observed facts from webhook_events; not an opinion",
            "tier2_investigator": (
                "deep-analysis report severity. AI, but a process with tool access "
                "and minutes grading one with keywords and milliseconds"
            ),
            "cheap_verdict": "what WebhookWise itself said — evidence ABOUT the system under test, never a label",
            "absent": (
                "self-resolution and flapping are not here: resolved_at is ~0.4% populated on "
                "this deployment. The judge's ledger owns those facts"
            ),
        },
    }


def _render(report: dict[str, Any]) -> str:
    rules = report["rules"]
    ordered = sorted(rules.items(), key=lambda kv: -kv[1]["tier1_outcome"]["alerts"])
    lines = [
        "",
        f"  Label evidence per alert rule, {report['window_days']}-day outcome window",
        "  Evidence only. Nothing here is a label, and nothing here proposes one.",
        "",
        f"  {'rule':<34} {'n':>4} {'recur':>6} {'fwd':>5} {'cheap':<20} {'investigator':<22} cap",
        f"  {'-' * 34} {'-' * 4} {'-' * 6} {'-' * 5} {'-' * 20} {'-' * 22} ---",
    ]
    for name, data in ordered:
        t1, t2 = data["tier1_outcome"], data["tier2_investigator"]
        cheap = " ".join(f"{k[:1]}:{v}" for k, v in sorted(data["cheap_verdict"].items())) or "-"
        inv = " ".join(f"{k[:1]}:{v}" for k, v in sorted(t2["verdicts"].items())) or "-"
        if t2["unanswerable"]:
            inv += f" ?:{t2['unanswerable']}"
        lines.append(
            f"  {name[:34]:<34} {t1['alerts']:>4} {t1['recurring_share']:>6.0%} "
            f"{t1['forwarded_share']:>5.0%} {cheap[:20]:<20} {inv[:22]:<22} {data['capped_at'] or '-'}"
        )
    lines += [
        "",
        "  recur = share whose duplicate_count > 1.  fwd = share actually delivered.",
        "  cheap = what this system said (h/m/l).  investigator = what the reports said.",
        "  A rule with volume, high recurrence and no investigator column is the one",
        "  worth investigating before labelling — there is nothing to adjudicate yet.",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="outcome window in days (default 30)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable evidence")
    parser.add_argument(
        "--with-reports",
        type=int,
        default=0,
        help="include up to N investigator report summaries per rule (bulky, and full of business detail)",
    )
    args = parser.parse_args()

    from core.app_context import init_default_app_context

    init_default_app_context()

    report = await _collect(args.days, args.with_reports)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json else _render(report))


if __name__ == "__main__":
    asyncio.run(main())
