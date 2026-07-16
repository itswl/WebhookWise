#!/usr/bin/env python
"""Seed a running WebhookWise with realistic demo alerts (5-minute evaluation).

Posts a mixed batch of synthetic alerts through the REAL ingest endpoint
(POST /v1/webhook/{source}), so everything downstream — adapters, dedup, noise
reduction, incidents, dashboard — lights up exactly as with live traffic:

- a noisy GPU source firing the same alert repeatedly (dedup/noise story)
- a Zabbix-shaped and an Aliyun-CloudMonitor-shaped source (declarative specs)
- an Uptime-Kuma monitor that goes down and recovers (incident + recovery)
- a flapping identity oscillating firing↔recovered (flapping detection)
- a sprinkle of one-off alerts across severities

Usage:
    python scripts/seed_demo_data.py --base-url http://localhost:8000 \
        [--webhook-token <WEBHOOK_SECRET>] [--count 60] [--fast]

The token is only needed when the server runs with REQUIRE_WEBHOOK_AUTH=true.
Pacing: ~0.2s between posts so timestamps spread naturally; --fast removes it.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from typing import Any

import httpx

RNG = random.Random(42)

_HOSTS = ["web-01", "web-02", "db-01", "cache-01", "gpu-node-02"]
_SERVICES = ["checkout", "search", "render", "auth"]


def _generic(name: str, level: str, host: str, message: str) -> dict[str, Any]:
    return {
        "alert_name": name,
        "level": level,
        "host": host,
        "service": RNG.choice(_SERVICES),
        "id": f"evt-{RNG.randrange(10**8)}",
        "message": message,
    }


def _zabbix(status: str = "PROBLEM") -> dict[str, Any]:
    return {
        "event_name": "High CPU utilization on db-01",
        "event_id": str(RNG.randrange(10**6)),
        "host_name": "db-01",
        "event_severity": "High",
        "event_status": status,
        "event_value": "1" if status == "PROBLEM" else "0",
    }


def _aliyun(state: str = "ALERT") -> dict[str, Any]:
    return {
        "alertName": "cpu_total GreaterThanOrEqualToThreshold 90",
        "alertState": state,
        "curValue": "96.5" if state == "ALERT" else "12.0",
        "metricName": "cpu_total",
        "namespace": "acs_ecs_dashboard",
        "instanceName": "prod-worker-3",
        "triggerLevel": "CRITICAL",
        "dimensions": "{userId=123, instanceId=i-abc}",
        "expression": "$Average>=90",
    }


def _uptime_kuma(up: bool) -> dict[str, Any]:
    return {
        "heartbeat": {
            "status": 1 if up else 0,
            "msg": "200 - OK" if up else "connect ECONNREFUSED",
            "time": "2026-07-16 08:00:00",
        },
        "monitor": {"name": "payments-api", "url": "https://payments.internal/health"},
        "msg": "[payments-api] " + ("✅ Up" if up else "🔴 Down"),
    }


def _batch(count: int) -> list[tuple[str, dict[str, Any]]]:
    """(source-path, payload) pairs telling a coherent demo story."""
    posts: list[tuple[str, dict[str, Any]]] = []

    # 1) The noisy repeat offender: same GPU alert over and over (~40%).
    posts.extend(
        ("generic", _generic("gpu-mem-high", "critical", "gpu-node-02", "GPU memory above 95% for 5m"))
        for _ in range(max(6, int(count * 0.4)))
    )

    # 2) Zabbix problem + eventual recovery.
    posts.append(("zabbix", _zabbix("PROBLEM")))
    posts.append(("zabbix", _zabbix("PROBLEM")))
    posts.append(("zabbix", _zabbix("RESOLVED")))

    # 3) Aliyun CloudMonitor alert + recovery.
    posts.append(("aliyun_cms", _aliyun("ALERT")))
    posts.append(("aliyun_cms", _aliyun("OK")))

    # 4) Uptime-Kuma down → up.
    posts.append(("uptime_kuma", _uptime_kuma(up=False)))
    posts.append(("uptime_kuma", _uptime_kuma(up=True)))

    # 5) A flapping identity: rapid down/up oscillation.
    posts.extend(
        ("generic", _generic("disk-io-latency", "info" if up else "warning", "cache-01", "flap"))
        for up in (False, True, False, True, False, True, False)
    )

    # 6) One-off variety until we reach the requested count.
    levels = ["critical", "warning", "info", "error"]
    while len(posts) < count:
        posts.append(
            (
                "generic",
                _generic(
                    RNG.choice(["conn-pool-exhausted", "tls-cert-expiring", "queue-lag", "oom-killed"]),
                    RNG.choice(levels),
                    RNG.choice(_HOSTS),
                    "synthetic demo alert",
                ),
            )
        )
    RNG.shuffle(posts)
    return posts[:count] if count < len(posts) else posts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--webhook-token", default="", help="WEBHOOK_SECRET when auth is enabled")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--fast", action="store_true", help="no pacing between posts")
    args = parser.parse_args()

    headers = {"Content-Type": "application/json"}
    if args.webhook_token:
        headers["Authorization"] = f"Bearer {args.webhook_token}"

    posts = _batch(args.count)
    ok = 0
    failed = 0
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=10.0) as client:
        for index, (source, payload) in enumerate(posts, 1):
            try:
                response = client.post(f"/v1/webhook/{source}", json=payload)
                if response.status_code == 200:
                    ok += 1
                else:
                    failed += 1
                    print(f"[{index}] {source} -> HTTP {response.status_code}: {response.text[:120]}")
            except httpx.HTTPError as e:
                failed += 1
                print(f"[{index}] {source} -> {e}")
            if not args.fast:
                time.sleep(0.2)

    print(f"\nSeeded {ok} demo alerts ({failed} failed) into {args.base_url}.")
    print("Open the dashboard: alerts list, incidents, noise center, and the Action Center")
    print("should now show dedup chains, a recovery-resolved incident, and a flapping identity.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
