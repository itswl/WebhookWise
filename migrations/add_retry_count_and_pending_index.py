#!/usr/bin/env python3
"""为 webhook_events 表添加 retry_count 字段和部分索引 idx_pending_webhooks"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from core.config import Config


def migrate():
    """添加 retry_count 字段和 idx_pending_webhooks 部分索引（幂等）"""
    engine = create_engine(Config.db.DATABASE_URL)

    with engine.connect() as conn:
        # 添加 retry_count 列（已有数据默认 0）
        conn.execute(
            text("""
            ALTER TABLE webhook_events
            ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0
        """)
        )

        # 部分索引：加速 RecoveryPoller 查询未完成事件
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_pending_webhooks
            ON webhook_events (created_at)
            WHERE processing_status IN ('received', 'analyzing', 'failed')
        """)
        )

        conn.commit()
        print("webhook_events retry_count 字段和 idx_pending_webhooks 部分索引添加成功")

    return True


if __name__ == "__main__":
    migrate()
