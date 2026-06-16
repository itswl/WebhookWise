#!/usr/bin/env python3
"""
Alert data query script
Usage:
    python -m scripts.ops.query_alerts --id 5
    python -m scripts.ops.query_alerts --list
    python -m scripts.ops.query_alerts --hash <hash>
    python -m scripts.ops.query_alerts --source <src>
    python -m scripts.ops.query_alerts --importance high
    python -m scripts.ops.query_alerts --duplicate
    python -m scripts.ops.query_alerts --limit 50
"""

import argparse
import asyncio

# Ensure the project root directory is on the path
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import func, select

from core import json
from db.engine import init_engine
from db.session import session_scope
from models import WebhookEvent
from schemas.webhook import webhook_event_to_full_dict


def print_json(data):
    print(json.dumps(data, indent=True))


async def query_by_id(event_id: int):
    async with session_scope() as session:
        result = await session.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))
        event = result.scalar_one_or_none()
        if event:
            print(f"\n=== Alert ID {event_id} ===")
            print_json(webhook_event_to_full_dict(event))
        else:
            print(f"No alert found with ID={event_id}")
        return event


async def query_list(
    limit: int = 20, source: str | None = None, importance: str | None = None, duplicate_only: bool = False
):
    async with session_scope() as session:
        stmt = select(WebhookEvent).order_by(WebhookEvent.id.desc())

        if source:
            stmt = stmt.where(WebhookEvent.source == source)
        if importance:
            stmt = stmt.where(WebhookEvent.importance == importance)
        if duplicate_only:
            stmt = stmt.where(WebhookEvent.is_duplicate.is_(True))

        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        events = result.scalars().all()
        print(f"\n=== Most recent {len(events)} alerts ===")
        for e in events:
            d = webhook_event_to_full_dict(e)
            ts = d.get("timestamp", "")[:19]
            imp = d.get("importance", "-")
            src = d.get("source", "-")
            dup = "🔁" if d.get("is_duplicate") else "🆕"
            summary = d.get("ai_analysis", {}).get("summary", "-") or "-"
            if len(summary) > 60:
                summary = summary[:60] + "..."
            print(f"  [{dup}] #{d['id']} | {ts} | {imp:5} | {src:15} | {summary}")


async def query_by_hash(alert_hash: str):
    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent).where(WebhookEvent.alert_hash == alert_hash).order_by(WebhookEvent.id.asc())
        )
        events = result.scalars().all()

        print(f"\n=== {len(events)} alerts with Hash={alert_hash[:16]}... ===")
        for e in events:
            d = webhook_event_to_full_dict(e)
            ts = d.get("timestamp", "")[:19]
            imp = d.get("importance", "-")
            dup = "🔁" if d.get("is_duplicate") else "🆕"
            print(f"  [{dup}] #{d['id']} | {ts} | {imp}")


async def query_stats():
    async with session_scope() as session:
        result = await session.execute(select(func.count()).select_from(WebhookEvent))
        total = result.scalar()

        result = await session.execute(
            select(func.count()).select_from(WebhookEvent).where(WebhookEvent.importance == "high")
        )
        high = result.scalar()

        result = await session.execute(
            select(func.count()).select_from(WebhookEvent).where(WebhookEvent.importance == "medium")
        )
        medium = result.scalar()

        result = await session.execute(
            select(func.count()).select_from(WebhookEvent).where(WebhookEvent.importance == "low")
        )
        low = result.scalar()

        result = await session.execute(
            select(func.count()).select_from(WebhookEvent).where(WebhookEvent.is_duplicate.is_(True))
        )
        dup = result.scalar()

        print("\n=== Statistics overview ===")
        print(f"  Total alerts: {total}")
        print(f"  high:     {high}")
        print(f"  medium:   {medium}")
        print(f"  low:      {low}")
        print(f"  Duplicate alerts: {dup}")


async def main():
    parser = argparse.ArgumentParser(description="Alert data query")
    parser.add_argument("--id", type=int, help="Query by ID")
    parser.add_argument("--list", action="store_true", help="List recent alerts")
    parser.add_argument("--hash", type=str, help="Query by alert_hash")
    parser.add_argument("--source", type=str, help="Filter by source")
    parser.add_argument("--importance", type=str, choices=["high", "medium", "low"], help="Filter by importance")
    parser.add_argument("--duplicate", action="store_true", help="Show only duplicate alerts")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--limit", type=int, default=20, help="Number to list (default 20)")
    parser.add_argument("--json", action="store_true", help="Output full JSON (only with --id)")

    args = parser.parse_args()

    await init_engine()

    if args.id:
        await query_by_id(args.id)
    elif args.hash:
        await query_by_hash(args.hash)
    elif args.stats:
        await query_stats()
    elif args.list or any([args.source, args.importance, args.duplicate]):
        await query_list(
            limit=args.limit, source=args.source, importance=args.importance, duplicate_only=args.duplicate
        )
    else:
        await query_list(limit=args.limit)


if __name__ == "__main__":
    asyncio.run(main())
