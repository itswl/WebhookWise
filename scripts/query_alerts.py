#!/usr/bin/env python3
"""
告警数据查询脚本
用法:
    python query_alerts.py --id 5           # 查询单条
    python query_alerts.py --list           # 列出最近20条
    python query_alerts.py --hash <hash>   # 按 hash 查询
    python query_alerts.py --source <src>  # 按来源筛选
    python query_alerts.py --importance high # 按重要性筛选
    python query_alerts.py --duplicate     # 查询重复告警
    python query_alerts.py --limit 50      # 自定义数量
"""

import argparse
import asyncio
import json

# 确保项目根目录在 path 中
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select

from db.session import init_engine, session_scope
from models import WebhookEvent


def print_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def query_by_id(event_id: int):
    async with session_scope() as session:
        result = await session.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))
        event = result.scalar_one_or_none()
        if event:
            print(f"\n=== 告警 ID {event_id} ===")
            print_json(event.to_dict())
        else:
            print(f"未找到 ID={event_id} 的告警")
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
            stmt = stmt.where(WebhookEvent.is_duplicate == 1)

        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        events = result.scalars().all()
        print(f"\n=== 最近 {len(events)} 条告警 ===")
        for e in events:
            d = e.to_dict()
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

        print(f"\n=== Hash={alert_hash[:16]}... 的 {len(events)} 条告警 ===")
        for e in events:
            d = e.to_dict()
            ts = d.get("timestamp", "")[:19]
            imp = d.get("importance", "-")
            dup = "🔁" if d.get("is_duplicate") else "🆕"
            print(f"  [{dup}] #{d['id']} | {ts} | {imp} | beyond_window={d.get('beyond_window')}")


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
            select(func.count()).select_from(WebhookEvent).where(WebhookEvent.is_duplicate == 1)
        )
        dup = result.scalar()

        print("\n=== 统计概览 ===")
        print(f"  总告警数: {total}")
        print(f"  high:     {high}")
        print(f"  medium:   {medium}")
        print(f"  low:      {low}")
        print(f"  重复告警: {dup}")


async def main():
    parser = argparse.ArgumentParser(description="告警数据查询")
    parser.add_argument("--id", type=int, help="按 ID 查询")
    parser.add_argument("--list", action="store_true", help="列出最近告警")
    parser.add_argument("--hash", type=str, help="按 alert_hash 查询")
    parser.add_argument("--source", type=str, help="按来源筛选")
    parser.add_argument("--importance", type=str, choices=["high", "medium", "low"], help="按重要性筛选")
    parser.add_argument("--duplicate", action="store_true", help="仅显示重复告警")
    parser.add_argument("--stats", action="store_true", help="显示统计信息")
    parser.add_argument("--limit", type=int, default=20, help="列出数量 (默认20)")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON (仅限 --id)")

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
