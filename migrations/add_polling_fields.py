#!/usr/bin/env python3
"""为 deep_analyses 表添加轮询相关字段"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from core.config import Config


def migrate():
    """添加 openclaw_run_id, openclaw_session_key, status 字段（幂等）"""
    engine = create_engine(Config.db.DATABASE_URL)

    with engine.connect() as conn:
        # 添加字段
        conn.execute(
            text("""
            ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS openclaw_run_id VARCHAR(64)
        """)
        )
        conn.execute(
            text("""
            ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS openclaw_session_key VARCHAR(200)
        """)
        )
        conn.execute(
            text("""
            ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'completed'
        """)
        )

        # 创建索引
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_deep_analyses_openclaw_run_id ON deep_analyses(openclaw_run_id)
        """)
        )
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_deep_analyses_status ON deep_analyses(status)
        """)
        )

        conn.commit()
        print("deep_analyses 表轮询字段添加成功")

    return True


if __name__ == "__main__":
    migrate()
