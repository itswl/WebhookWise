#!/usr/bin/env python3
"""Normalize Grafana dashboard drill-down links.

The provisioned dashboards are edited as JSON, but their data links should be
generated consistently: every metric panel gets Logs and Trace links, and
profile-relevant panels also get a Pyroscope link.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import json  # noqa: E402

DEFAULT_DASHBOARDS = (
    ROOT / "deploy/observability/grafana/dashboards/dashboard.json",
    ROOT / "deploy/observability/grafana/dashboards/dashboard-diagnostics.json",
)
PROFILE_TYPE_ID = "process_cpu:cpu:nanoseconds:cpu:nanoseconds"
TIME_RANGE = {"from": "${__from}", "to": "${__to}"}
SERVICE_SELECTOR = 'service_name=~"webhookwise.*|webhookwise"'
BUSINESS_PROFILE_SELECTOR = 'service_name=~"webhookwise-api|webhookwise-worker|webhookwise-scheduler"'

PRIMARY_CONTEXT_LABELS = (
    "webhook_source",
    "service_name",
    "http_route",
    "worker_task_name",
    "scheduler_task_name",
    "pipeline_step",
    "forward_target_type",
    "ai_model",
    "ai_engine",
    "circuit_breaker_name",
    "redis_operation",
    "db_operation",
    "queue_operation",
    "webhook_status",
    "webhook_outcome",
    "webhook_relation",
    "forward_status",
    "severity",
    "alertname",
)

TRACE_ATTR_BY_LABEL = {
    "webhook_source": "span.webhook.source",
    "webhook_status": "span.webhook.status",
    "webhook_outcome": "span.webhook.outcome",
    "webhook_relation": "span.webhook.relation",
    "http_route": "span.http.route",
    "worker_task_name": "span.worker.task.name",
    "worker_task_status": "span.worker.task.status",
    "scheduler_task_name": "span.scheduler.task.name",
    "pipeline_step": "span.pipeline.step",
    "forward_target_type": "span.forward.target_type",
    "forward_status": "span.forward.status",
    "ai_model": "span.ai.model",
    "ai_engine": "span.ai.engine",
    "circuit_breaker_name": "span.circuit_breaker.name",
    "redis_operation": "span.redis.operation",
    "db_operation": "span.db.operation",
    "queue_operation": "span.db.operation",
}

PROFILE_LABELS = {
    "service_name",
    "http_route",
    "webhook_source",
    "worker_task_name",
    "scheduler_task_name",
    "pipeline_step",
    "forward_target_type",
    "ai_model",
    "ai_engine",
    "redis_operation",
    "db_operation",
    "queue_operation",
}


def dashboard_labels(panel: dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    for target in panel.get("targets", []) or []:
        expr = str(target.get("expr") or "")
        for group in re.findall(r"\b(?:by|without)\s*\(([^)]*)\)", expr):
            labels.update(label.strip() for label in group.split(",") if label.strip())
    labels.discard("le")
    return labels


def primary_label(labels: set[str]) -> str | None:
    return next((label for label in PRIMARY_CONTEXT_LABELS if label in labels), None)


def explore_url(datasource: str, query_key: str, query: str, query_type: str, **extra: Any) -> str:
    query_payload = {"refId": "A", query_key: query, "queryType": query_type, **extra}
    left = {"datasource": datasource, "queries": [query_payload], "range": TIME_RANGE}
    state = json.dumps(left).replace("&", "%26")
    return f"/explore?orgId=1&left={state}"


def log_query(labels: set[str]) -> str:
    label = primary_label(labels)
    selector = 'service_name="${__field.labels.service_name}"' if "service_name" in labels else SERVICE_SELECTOR
    query = f"{{{selector}}}"
    if label and label != "service_name":
        query += f' |= "${{__field.labels.{label}}}"'
    return query + " | json"


def trace_query(labels: set[str]) -> str:
    label = primary_label(labels)
    clauses = (
        ['resource.service.name = "${__field.labels.service_name}"']
        if "service_name" in labels
        else ['resource.service.name =~ "webhookwise.*"']
    )
    if label and label in TRACE_ATTR_BY_LABEL:
        clauses.append(f'{TRACE_ATTR_BY_LABEL[label]} = "${{__field.labels.{label}}}"')
    return "{ " + " && ".join(clauses).replace("&", "%26") + " }"


def profile_query(labels: set[str]) -> str | None:
    if not labels.intersection(PROFILE_LABELS):
        return None
    service_matcher = (
        'service_name="${__field.labels.service_name}"' if "service_name" in labels else BUSINESS_PROFILE_SELECTOR
    )
    return f'{{{service_matcher}, profile_type="{PROFILE_TYPE_ID}"}}'


def links_for_panel(panel: dict[str, Any]) -> list[dict[str, Any]]:
    labels = dashboard_labels(panel)
    links = [
        {
            "title": "View related logs",
            "url": explore_url("loki", "expr", log_query(labels), "range"),
            "targetBlank": True,
        },
        {
            "title": "View related Trace",
            "url": explore_url("tempo", "query", trace_query(labels), "traceql"),
            "targetBlank": True,
        },
    ]
    profile_selector = profile_query(labels)
    if profile_selector:
        links.append(
            {
                "title": "View Profile",
                "url": explore_url(
                    "pyroscope",
                    "query",
                    profile_selector,
                    "profile",
                    profileTypeId=PROFILE_TYPE_ID,
                ),
                "targetBlank": True,
            }
        )
    return links


def update_dashboard(path: Path) -> int:
    dashboard = json.loads(path.read_text())
    updated = 0
    for panel in dashboard.get("panels", []):
        if panel.get("type") == "row" or not any(target.get("expr") for target in panel.get("targets", []) or []):
            continue
        panel.setdefault("fieldConfig", {}).setdefault("defaults", {})["links"] = links_for_panel(panel)
        updated += 1
    path.write_text(json.dumps(dashboard, indent=True) + "\n")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=list(DEFAULT_DASHBOARDS))
    args = parser.parse_args()
    for raw_path in args.paths:
        path = raw_path if raw_path.is_absolute() else ROOT / raw_path
        print(f"{path}: updated {update_dashboard(path)} panels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
