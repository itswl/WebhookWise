#!/usr/bin/env python3
"""
批量重试失败的深度分析记录

用法:
    python -m scripts.ops.retry_failed_deep_analyses [OPTIONS]

示例:
    # 重试所有失败记录
    python -m scripts.ops.retry_failed_deep_analyses

    # 只列出待重试的记录，不实际执行
    python -m scripts.ops.retry_failed_deep_analyses --list

    # 只重试指定 webhook_event_id 关联的记录
    python -m scripts.ops.retry_failed_deep_analyses --webhook-id 20177

    # 重试最近 N 条失败记录
    python -m scripts.ops.retry_failed_deep_analyses --limit 50
"""

import argparse
import asyncio
import os
import sys

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import select

from core import json
from core.app_context import get_config_manager, init_default_app_context
from core.config import UnifiedConfigManager
from core.logger import get_logger
from db.engine import init_engine
from db.session import session_scope
from models import DeepAnalysis
from services.analysis.openclaw import poll_openclaw_result_via_http

logger = get_logger("scripts.retry_failed_deep_analyses")


async def find_failed_records(webhook_id=None, limit=None):
    """查询待重试的失败记录"""
    async with session_scope() as session:
        stmt = select(DeepAnalysis).where(DeepAnalysis.status == "failed")
        if webhook_id is not None:
            stmt = stmt.where(DeepAnalysis.webhook_event_id == webhook_id)

        stmt = stmt.order_by(DeepAnalysis.id.desc())
        if limit:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        records = result.scalars().all()
        return [(r.id, r.webhook_event_id, r.openclaw_session_key, r.status) for r in records]


async def retry_record(record_id: int) -> tuple[bool, str]:
    """重试单条记录"""
    async with session_scope() as session:
        record = await session.get(DeepAnalysis, record_id)
        if not record:
            return False, "记录不存在"

        if record.status not in ("failed", "completed"):
            return False, f"状态非 failed/completed: {record.status}"

        if not record.openclaw_session_key:
            return False, "缺少 session key"

        config = UnifiedConfigManager()
        if not config.openclaw.OPENCLAW_HTTP_API_URL:
            return False, "未配置 OPENCLAW_HTTP_API_URL，无法重试"

        result = await poll_openclaw_result_via_http(record.openclaw_session_key, retry_count=3)

        if result.get("status") == "error":
            return False, f"API 错误: {result.get('error')}"

        if result.get("status") != "completed":
            return False, f"未完成: {result.get('status')}"

        text = result.get("text", "")
        import re

        json_match = re.search(r"\{[\s\S]*\}", text)

        if json_match:
            try:
                parsed = json.loads(json_match.group())
                record.analysis_result = parsed
                record.status = "completed"
                logger.info("深度分析 #%d 重试成功", record_id)
                return True, "成功"
            except json.JSONDecodeError:
                record.analysis_result = {"text": text}
                record.status = "completed"
                return True, "成功（JSON解析失败，已存原文）"
        else:
            record.analysis_result = {"text": text}
            record.status = "completed"
            return True, "成功（无 JSON）"


async def main():
    parser = argparse.ArgumentParser(description="批量重试失败的深度分析记录")
    parser.add_argument("--list", action="store_true", help="只列出记录，不执行重试")
    parser.add_argument("--webhook-id", type=int, metavar="ID", help="限定 webhook_event_id")
    parser.add_argument("--limit", type=int, metavar="N", help="最多处理 N 条")
    parser.add_argument("--dry-run", action="store_true", help="模拟执行（仅 --list 时有效）")
    args = parser.parse_args()

    init_default_app_context(UnifiedConfigManager())
    await init_engine()

    records = await find_failed_records(webhook_id=args.webhook_id, limit=args.limit)

    if not records:
        print("没有找到待重试的失败记录")
        return

    print(f"找到 {len(records)} 条失败记录：")
    print(f"{'ID':<10} {'webhook_event_id':<20} {'session_key':<40} {'当前状态'}")
    print("-" * 90)
    for rec in records:
        print(f"{rec[0]:<10} {rec[1]:<20} {rec[2] or '':<40} {rec[3]}")

    if args.list:
        print(f"\n共 {len(records)} 条，使用 --list 跳过实际执行")
        return

    config = get_config_manager()
    if not config.openclaw.OPENCLAW_HTTP_API_URL:
        print("\n错误：未配置 OPENCLAW_HTTP_API_URL，无法重试")
        sys.exit(1)

    print(f"\n开始重试 {len(records)} 条记录...\n")
    success, failed = 0, []

    for record_id, webhook_event_id, _session_key, _ in records:
        ok, msg = await retry_record(record_id)
        status = "✓" if ok else "✗"
        print(f"  [{status}] #{record_id} (webhook #{webhook_event_id}): {msg}")
        if ok:
            success += 1
        else:
            failed.append((record_id, msg))

    print(f"\n完成：成功 {success}，失败 {len(failed)}")
    if failed:
        print("\n失败列表：")
        for rid, msg in failed:
            print(f"  #{rid}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
