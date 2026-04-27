#!/usr/bin/env python3
"""添加 deep_analyses 深度分析历史表"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

from core.config import Config


def migrate():
    """创建 deep_analyses 表（幂等）"""
    engine = create_engine(Config.DATABASE_URL)
    inspector = inspect(engine)

    # 检查表是否已存在
    if 'deep_analyses' in inspector.get_table_names():
        print("deep_analyses 表已存在，跳过")
        return True

    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE deep_analyses (
                id SERIAL PRIMARY KEY,
                webhook_event_id INTEGER NOT NULL,
                engine VARCHAR(20) DEFAULT 'local',
                user_question TEXT DEFAULT '',
                analysis_result JSON,
                duration_seconds FLOAT DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text("""
            CREATE INDEX idx_deep_analyses_webhook_event_id ON deep_analyses(webhook_event_id)
        """))
        conn.commit()
        print("deep_analyses 表创建成功")

    return True


if __name__ == '__main__':
    migrate()
