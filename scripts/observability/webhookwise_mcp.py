#!/usr/bin/env python3
"""Small JSON-RPC stdio server exposing WebhookWise observability tools.

This is intentionally dependency-free. It implements the MCP-style methods used
by simple stdio clients: initialize, tools/list, and tools/call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import json  # noqa: E402
from scripts.observability.query_lib import (  # noqa: E402
    PROMQL_PRESETS,
    Endpoints,
    grafana_datasources,
    health,
    loki_query_range,
    profile_links,
    prometheus_query,
    result_rows,
    runbook_summary,
    runtime_acceptance,
    smoke,
    telemetry_contract,
    tempo_search,
    validate_dashboard_queries,
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "webhookwise_health",
        "description": "Check WebhookWise observability endpoints.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "webhookwise_datasources",
        "description": "List Grafana datasource names and UIDs for proxy queries.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "webhookwise_promql",
        "description": "Run a PromQL instant query against WebhookWise Prometheus.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_preset",
        "description": "Run a named WebhookWise observability PromQL preset.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": sorted(PROMQL_PRESETS)}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_logs",
        "description": "Run a Loki query_range against WebhookWise Loki.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": '{service_name="webhookwise"}'},
                "limit": {"type": "integer", "default": 20},
                "since_seconds": {"type": "integer", "default": 3600},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_tempo_search",
        "description": "Search recent Tempo traces for a WebhookWise service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "default": "webhookwise-api"},
                "limit": {"type": "integer", "default": 5},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_profiles",
        "description": "Build Pyroscope profile selectors and Grafana/Pyroscope links for a WebhookWise service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "default": "webhookwise-api"},
                "from_expr": {"type": "string", "default": "now-1h"},
                "to_expr": {"type": "string", "default": "now"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_dashboard_validate",
        "description": "Validate deploy/observability/grafana/dashboards/dashboard.json PromQL against Prometheus.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "deploy/observability/grafana/dashboards/dashboard.json"}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_smoke",
        "description": "Run an end-to-end WebhookWise observability smoke check.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "send_webhook": {"type": "boolean", "default": True},
                "wait_seconds": {"type": "integer", "default": 8},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_acceptance",
        "description": "Run runtime observability acceptance checks across health, data sources, dashboard queries, SLOs, logs, and traces.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "send_webhook": {"type": "boolean", "default": True},
                "wait_seconds": {"type": "integer", "default": 8},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "webhookwise_contract",
        "description": "Run offline telemetry contract checks for metrics, PromQL, Loki labels, schema URLs, and structured logging.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "webhookwise_runbook",
        "description": "Collect a compact runbook summary for a WebhookWise alert.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alert_name": {"type": "string"},
                "since_seconds": {"type": "integer", "default": 3600},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["alert_name"],
            "additionalProperties": False,
        },
    },
]


def _text_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=True),
            }
        ]
    }


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    endpoints = Endpoints.from_env()
    if name == "webhookwise_health":
        return _text_result(health(endpoints))
    if name == "webhookwise_datasources":
        rows = [
            {
                "uid": item.get("uid", ""),
                "name": item.get("name", ""),
                "type": item.get("type", ""),
                "access": item.get("access", ""),
            }
            for item in grafana_datasources(endpoints)
        ]
        return _text_result(rows)
    if name == "webhookwise_promql":
        result = prometheus_query(str(arguments["query"]), endpoints)
        return _text_result(result_rows(result))
    if name == "webhookwise_preset":
        preset = str(arguments["name"])
        result = prometheus_query(PROMQL_PRESETS[preset], endpoints)
        return _text_result({"name": preset, "query": PROMQL_PRESETS[preset], "rows": result_rows(result)})
    if name == "webhookwise_logs":
        result = loki_query_range(
            str(arguments.get("query", '{service_name="webhookwise"}')),
            endpoints,
            limit=int(arguments.get("limit", 20)),
            since_seconds=int(arguments.get("since_seconds", 3600)),
        )
        return _text_result(result)
    if name == "webhookwise_tempo_search":
        return _text_result(
            tempo_search(
                endpoints,
                service_name=str(arguments.get("service_name", "webhookwise-api")),
                limit=int(arguments.get("limit", 5)),
            )
        )
    if name == "webhookwise_profiles":
        return _text_result(
            profile_links(
                str(arguments.get("service_name", "webhookwise-api")),
                endpoints,
                from_expr=str(arguments.get("from_expr", "now-1h")),
                to_expr=str(arguments.get("to_expr", "now")),
            )
        )
    if name == "webhookwise_dashboard_validate":
        return _text_result(
            validate_dashboard_queries(
                str(arguments.get("path", "deploy/observability/grafana/dashboards/dashboard.json")), endpoints
            )
        )
    if name == "webhookwise_smoke":
        return _text_result(
            smoke(
                endpoints,
                send_webhook=bool(arguments.get("send_webhook", True)),
                wait_seconds=int(arguments.get("wait_seconds", 8)),
            )
        )
    if name == "webhookwise_acceptance":
        return _text_result(
            runtime_acceptance(
                endpoints,
                send_webhook=bool(arguments.get("send_webhook", True)),
                wait_seconds=int(arguments.get("wait_seconds", 8)),
            )
        )
    if name == "webhookwise_contract":
        return _text_result(telemetry_contract())
    if name == "webhookwise_runbook":
        return _text_result(
            runbook_summary(
                str(arguments["alert_name"]),
                endpoints,
                since_seconds=int(arguments.get("since_seconds", 3600)),
                limit=int(arguments.get("limit", 5)),
            )
        )
    raise ValueError(f"unknown tool: {name}")


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "webhookwise-observability", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            result = _call_tool(str(params.get("name")), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = _handle(json.loads(line))
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
