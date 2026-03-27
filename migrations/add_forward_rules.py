#!/usr/bin/env python3
"""添加 forward_rules 转发规则表"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from sqlalchemy import create_engine, text, inspect


def migrate():
    """创建 forward_rules 表（幂等）"""
    engine = create_engine(Config.DATABASE_URL)
    inspector = inspect(engine)
    
    # 检查表是否已存在
    if 'forward_rules' in inspector.get_table_names():
        print("forward_rules 表已存在，跳过")
        return True
    
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE forward_rules (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                priority INTEGER DEFAULT 0,
                match_importance VARCHAR(50) DEFAULT '',
                match_duplicate VARCHAR(20) DEFAULT 'all',
                match_source VARCHAR(200) DEFAULT '',
                target_type VARCHAR(20) NOT NULL,
                target_url VARCHAR(500) DEFAULT '',
                target_name VARCHAR(100) DEFAULT '',
                stop_on_match BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.commit()
        print("forward_rules 表创建成功")
    
    return True


if __name__ == '__main__':
    migrate()
