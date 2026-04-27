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
import json

# 确保项目根目录在 path 中
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import WebhookEvent, session_scope


def print_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def query_by_id(event_id: int):
    with session_scope() as session:
        event = session.query(WebhookEvent).filter_by(id=event_id).first()
        if event:
            print(f"\n=== 告警 ID {event_id} ===")
            print_json(event.to_dict())
        else:
            print(f"未找到 ID={event_id} 的告警")
        return event


def query_list(limit: int = 20, source: str = None, importance: str = None, duplicate_only: bool = False):
    with session_scope() as session:
        query = session.query(WebhookEvent).order_by(WebhookEvent.id.desc())

        if source:
            query = query.filter(WebhookEvent.source == source)
        if importance:
            query = query.filter(WebhookEvent.importance == importance)
        if duplicate_only:
            query = query.filter(WebhookEvent.is_duplicate == 1)

        events = query.limit(limit).all()
        print(f"\n=== 最近 {len(events)} 条告警 ===")
        for e in events:
            d = e.to_dict()
            ts = d.get('timestamp', '')[:19]
            imp = d.get('importance', '-')
            src = d.get('source', '-')
            dup = '🔁' if d.get('is_duplicate') else '🆕'
            summary = d.get('ai_analysis', {}).get('summary', '-') or '-'
            if len(summary) > 60:
                summary = summary[:60] + '...'
            print(f"  [{dup}] #{d['id']} | {ts} | {imp:5} | {src:15} | {summary}")


def query_by_hash(alert_hash: str):
    with session_scope() as session:
        events = session.query(WebhookEvent).filter(
            WebhookEvent.alert_hash == alert_hash
        ).order_by(WebhookEvent.id.asc()).all()

        print(f"\n=== Hash={alert_hash[:16]}... 的 {len(events)} 条告警 ===")
        for e in events:
            d = e.to_dict()
            ts = d.get('timestamp', '')[:19]
            imp = d.get('importance', '-')
            dup = '🔁' if d.get('is_duplicate') else '🆕'
            print(f"  [{dup}] #{d['id']} | {ts} | {imp} | beyond_window={d.get('beyond_window')}")


def query_stats():
    with session_scope() as session:
        total = session.query(WebhookEvent).count()
        high = session.query(WebhookEvent).filter_by(importance='high').count()
        medium = session.query(WebhookEvent).filter_by(importance='medium').count()
        low = session.query(WebhookEvent).filter_by(importance='low').count()
        dup = session.query(WebhookEvent).filter_by(is_duplicate=1).count()

        print("\n=== 统计概览 ===")
        print(f"  总告警数: {total}")
        print(f"  high:     {high}")
        print(f"  medium:   {medium}")
        print(f"  low:      {low}")
        print(f"  重复告警: {dup}")


def main():
    parser = argparse.ArgumentParser(description='告警数据查询')
    parser.add_argument('--id', type=int, help='按 ID 查询')
    parser.add_argument('--list', action='store_true', help='列出最近告警')
    parser.add_argument('--hash', type=str, help='按 alert_hash 查询')
    parser.add_argument('--source', type=str, help='按来源筛选')
    parser.add_argument('--importance', type=str, choices=['high', 'medium', 'low'], help='按重要性筛选')
    parser.add_argument('--duplicate', action='store_true', help='仅显示重复告警')
    parser.add_argument('--stats', action='store_true', help='显示统计信息')
    parser.add_argument('--limit', type=int, default=20, help='列出数量 (默认20)')
    parser.add_argument('--json', action='store_true', help='输出完整 JSON (仅限 --id)')

    args = parser.parse_args()

    if args.id:
        query_by_id(args.id)
    elif args.hash:
        query_by_hash(args.hash)
    elif args.stats:
        query_stats()
    elif args.list or any([args.source, args.importance, args.duplicate]):
        query_list(
            limit=args.limit,
            source=args.source,
            importance=args.importance,
            duplicate_only=args.duplicate
        )
    else:
        query_list(limit=args.limit)


if __name__ == '__main__':
    main()
