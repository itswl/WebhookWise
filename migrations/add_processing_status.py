#!/usr/bin/env python3
"""为 webhook_events 表添加 processing_status 字段和组合索引"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from core.config import Config


def migrate():
    """添加 processing_status 字段和 idx_status_created 组合索引（幂等）"""
    engine = create_engine(Config.DATABASE_URL)

    with engine.connect() as conn:
        # 添加 processing_status 列（已有数据默认 'completed'）
        conn.execute(
            text("""
            ALTER TABLE webhook_events
            ADD COLUMN IF NOT EXISTS processing_status VARCHAR(20) NOT NULL DEFAULT 'completed'
        """)
        )

        # 添加组合索引
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_status_created
            ON webhook_events (processing_status, created_at)
        """)
        )

        conn.commit()
        print("webhook_events processing_status 字段和索引添加成功")

    return True


if __name__ == "__main__":
    migrate()
