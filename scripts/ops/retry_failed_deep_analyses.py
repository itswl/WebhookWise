#!/usr/bin/env python3
"""
Batch retry failed deep-analysis records

Usage:
    python -m scripts.ops.retry_failed_deep_analyses [OPTIONS]

Examples:
    # Retry all failed records
    python -m scripts.ops.retry_failed_deep_analyses

    # Only list the records pending retry, without actually executing
    python -m scripts.ops.retry_failed_deep_analyses --list

    # Only retry records associated with the given webhook_event_id
    python -m scripts.ops.retry_failed_deep_analyses --webhook-id 20177

    # Retry the most recent N failed records
    python -m scripts.ops.retry_failed_deep_analyses --limit 50
"""

import argparse
import asyncio
import os
import sys

# Add the project root directory to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import select

from core import json
from core.app_context import get_config_manager, init_default_app_context
from core.config import get_settings
from core.logger import get_logger
from db.engine import init_engine
from db.session import session_scope
from models import DeepAnalysis
from services.analysis.openclaw_poll import poll_openclaw_result_via_http

logger = get_logger("scripts.retry_failed_deep_analyses")


async def find_failed_records(webhook_id=None, limit=None):
    """Query failed records pending retry"""
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
    """Retry a single record"""
    async with session_scope() as session:
        record = await session.get(DeepAnalysis, record_id)
        if not record:
            return False, "Record does not exist"

        if record.status not in ("failed", "completed"):
            return False, f"Status is not failed/completed: {record.status}"

        if not record.openclaw_session_key:
            return False, "Missing session key"

        config = get_settings()
        if not config.openclaw.OPENCLAW_HTTP_API_URL:
            return False, "OPENCLAW_HTTP_API_URL not configured, cannot retry"

        result = await poll_openclaw_result_via_http(record.openclaw_session_key, retry_count=3)

        if result.get("status") == "error":
            return False, f"API error: {result.get('error')}"

        if result.get("status") != "completed":
            return False, f"Not completed: {result.get('status')}"

        text = result.get("text", "")
        import re

        json_match = re.search(r"\{[\s\S]*\}", text)

        if json_match:
            try:
                parsed = json.loads(json_match.group())
                record.analysis_result = parsed
                record.status = "completed"
                logger.info("Deep analysis #%d retried successfully", record_id)
                return True, "Success"
            except json.JSONDecodeError:
                record.analysis_result = {"text": text}
                record.status = "completed"
                return True, "Success (JSON parsing failed, raw text stored)"
        else:
            record.analysis_result = {"text": text}
            record.status = "completed"
            return True, "Success (no JSON)"


async def main():
    parser = argparse.ArgumentParser(description="Batch retry failed deep-analysis records")
    parser.add_argument("--list", action="store_true", help="Only list records, do not perform retries")
    parser.add_argument("--webhook-id", type=int, metavar="ID", help="Limit to a specific webhook_event_id")
    parser.add_argument("--limit", type=int, metavar="N", help="Process at most N records")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution (only effective with --list)")
    args = parser.parse_args()

    init_default_app_context(get_settings())
    await init_engine()

    records = await find_failed_records(webhook_id=args.webhook_id, limit=args.limit)

    if not records:
        print("No failed records pending retry were found")
        return

    print(f"Found {len(records)} failed records:")
    print(f"{'ID':<10} {'webhook_event_id':<20} {'session_key':<40} {'current status'}")
    print("-" * 90)
    for rec in records:
        print(f"{rec[0]:<10} {rec[1]:<20} {rec[2] or '':<40} {rec[3]}")

    if args.list:
        print(f"\n{len(records)} records total; --list skips actual execution")
        return

    config = get_config_manager()
    if not config.openclaw.OPENCLAW_HTTP_API_URL:
        print("\nError: OPENCLAW_HTTP_API_URL not configured, cannot retry")
        sys.exit(1)

    print(f"\nStarting retry of {len(records)} records...\n")
    success, failed = 0, []

    for record_id, webhook_event_id, _session_key, _ in records:
        ok, msg = await retry_record(record_id)
        status = "✓" if ok else "✗"
        print(f"  [{status}] #{record_id} (webhook #{webhook_event_id}): {msg}")
        if ok:
            success += 1
        else:
            failed.append((record_id, msg))

    print(f"\nDone: {success} succeeded, {len(failed)} failed")
    if failed:
        print("\nFailure list:")
        for rid, msg in failed:
            print(f"  #{rid}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
