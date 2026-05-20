#!/usr/bin/env python3
"""Query WebhookWise local observability data from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.observability.query_lib import (  # noqa: E402
    PROMQL_PRESETS,
    Endpoints,
    dashboard_queries,
    grafana_dashboard,
    grafana_datasources,
    health,
    loki_query_range,
    prometheus_query,
    prometheus_series,
    result_rows,
    validate_dashboard_queries,
)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = {column: len(column) for column in columns}
    for row in rows:
        for column in columns:
            widths[column] = max(widths[column], len(str(row.get(column, ""))))
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def compact_prometheus_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    for row in rows:
        metric = row.get("metric") or {}
        labels = ",".join(f"{key}={value}" for key, value in sorted(metric.items()) if key != "__name__")
        compact.append({"labels": labels or "{}", "value": str(row.get("value", ""))})
    return compact


def cmd_health(args: argparse.Namespace) -> int:
    rows = health(Endpoints.from_env())
    if args.json:
        print_json(rows)
    else:
        print_table(rows, ["service", "status", "detail"])
    return 0 if all(row["status"] == "ok" for row in rows) else 1


def cmd_promql(args: argparse.Namespace) -> int:
    result = prometheus_query(args.query, Endpoints.from_env())
    if args.json:
        print_json(result)
    else:
        rows = compact_prometheus_rows(result_rows(result))
        print_table(rows, ["labels", "value"])
    return 0


def cmd_preset(args: argparse.Namespace) -> int:
    if args.list:
        rows = [{"name": name, "query": query} for name, query in sorted(PROMQL_PRESETS.items())]
        if args.json:
            print_json(rows)
        else:
            print_table(rows, ["name", "query"])
        return 0
    if args.name not in PROMQL_PRESETS:
        print(f"Unknown preset: {args.name}", file=sys.stderr)
        print("Use --list to see available presets.", file=sys.stderr)
        return 2
    result = prometheus_query(PROMQL_PRESETS[args.name], Endpoints.from_env())
    if args.json:
        print_json({"preset": args.name, "query": PROMQL_PRESETS[args.name], "result": result})
    else:
        print(f"# {args.name}")
        print(PROMQL_PRESETS[args.name])
        rows = compact_prometheus_rows(result_rows(result))
        print_table(rows, ["labels", "value"])
    return 0


def cmd_series(args: argparse.Namespace) -> int:
    result = prometheus_series(args.match, Endpoints.from_env())
    if args.json:
        print_json(result)
    else:
        rows = [
            {
                "metric": item.get("__name__", ""),
                "labels": ",".join(sorted(key for key in item if key != "__name__")),
            }
            for item in result.get("data", [])
        ]
        print_table(rows, ["metric", "labels"])
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    result = loki_query_range(args.query, Endpoints.from_env(), limit=args.limit, since_seconds=args.since)
    if args.json:
        print_json(result)
        return 0
    rows: list[dict[str, str]] = []
    for stream in result.get("data", {}).get("result", []):
        labels = ",".join(f"{key}={value}" for key, value in sorted(stream.get("stream", {}).items()))
        for ts, line in stream.get("values", []):
            rows.append({"timestamp": ts, "labels": labels, "line": line[:220]})
    print_table(rows[: args.limit], ["timestamp", "labels", "line"])
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    if args.validate:
        rows = validate_dashboard_queries(args.path, Endpoints.from_env())
        if args.json:
            print_json(rows)
        else:
            print_table(rows, ["status", "series", "panel", "refId"])
        return 0 if all(row["status"] == "success" for row in rows) else 1
    if args.remote:
        result = grafana_dashboard(args.uid, Endpoints.from_env())
        panels = result.get("dashboard", {}).get("panels", [])
        rows = [{"id": panel.get("id"), "title": panel.get("title"), "type": panel.get("type")} for panel in panels]
    else:
        rows = dashboard_queries(args.path)
    if args.json:
        print_json(rows)
    else:
        columns = ["panel", "refId", "expr"] if rows and "expr" in rows[0] else ["id", "title", "type"]
        print_table(rows, columns)
    return 0


def cmd_datasources(args: argparse.Namespace) -> int:
    result = grafana_datasources(Endpoints.from_env())
    rows = [
        {
            "uid": item.get("uid", ""),
            "name": item.get("name", ""),
            "type": item.get("type", ""),
            "access": item.get("access", ""),
        }
        for item in result
    ]
    if args.json:
        print_json(result)
    else:
        print_table(rows, ["uid", "name", "type", "access"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    health_parser = sub.add_parser("health", help="Check observability endpoints")
    health_parser.add_argument("--json", action="store_true")
    health_parser.set_defaults(func=cmd_health)

    promql_parser = sub.add_parser("promql", help="Run an arbitrary PromQL instant query")
    promql_parser.add_argument("query")
    promql_parser.add_argument("--json", action="store_true")
    promql_parser.set_defaults(func=cmd_promql)

    preset_parser = sub.add_parser("preset", help="Run a named WebhookWise PromQL preset")
    preset_parser.add_argument("name", nargs="?")
    preset_parser.add_argument("--list", action="store_true")
    preset_parser.add_argument("--json", action="store_true")
    preset_parser.set_defaults(func=cmd_preset)

    series_parser = sub.add_parser("series", help="List Prometheus series metadata")
    series_parser.add_argument("match", help='Prometheus series matcher, e.g. "http_server_requests_total"')
    series_parser.add_argument("--json", action="store_true")
    series_parser.set_defaults(func=cmd_series)

    logs_parser = sub.add_parser("logs", help="Run a Loki query_range")
    logs_parser.add_argument("--query", default='{service_name="webhookwise"}')
    logs_parser.add_argument("--limit", type=int, default=20)
    logs_parser.add_argument("--since", type=int, default=3600, help="Lookback in seconds")
    logs_parser.add_argument("--json", action="store_true")
    logs_parser.set_defaults(func=cmd_logs)

    dashboard_parser = sub.add_parser("dashboard", help="Inspect or validate the Grafana dashboard")
    dashboard_parser.add_argument("--path", default="grafana/dashboard.json")
    dashboard_parser.add_argument("--uid", default="webhook-wise-aiops")
    dashboard_parser.add_argument("--remote", action="store_true", help="Read dashboard from Grafana API")
    dashboard_parser.add_argument(
        "--validate", action="store_true", help="Validate dashboard PromQL against Prometheus"
    )
    dashboard_parser.add_argument("--json", action="store_true")
    dashboard_parser.set_defaults(func=cmd_dashboard)

    datasources_parser = sub.add_parser("datasources", help="List Grafana datasources and UIDs")
    datasources_parser.add_argument("--json", action="store_true")
    datasources_parser.set_defaults(func=cmd_datasources)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
