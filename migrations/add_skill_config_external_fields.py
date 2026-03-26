#!/usr/bin/env python3
"""
Migration: 为 skill_configs 表添加外部 Skill 相关字段

添加 source, skill_version, external_path 字段以支持外部加载的 Skill。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from core.config import Config
from core.logger import logger


def migrate():
    """添加 source, skill_version, external_path 字段"""
    engine = create_engine(Config.DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            # 检查字段是否已存在
            result = conn.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'skill_configs' AND column_name IN ('source', 'skill_version', 'external_path')
            """))
            existing = {row[0] for row in result}
            
            added = []
            
            if 'source' not in existing:
                conn.execute(text("ALTER TABLE skill_configs ADD COLUMN source VARCHAR(20) DEFAULT 'builtin'"))
                added.append('source')
                logger.info("已添加 source 字段")
                print("✅ 已添加 source 字段")
            
            if 'skill_version' not in existing:
                conn.execute(text("ALTER TABLE skill_configs ADD COLUMN skill_version VARCHAR(20)"))
                added.append('skill_version')
                logger.info("已添加 skill_version 字段")
                print("✅ 已添加 skill_version 字段")
            
            if 'external_path' not in existing:
                conn.execute(text("ALTER TABLE skill_configs ADD COLUMN external_path VARCHAR(255)"))
                added.append('external_path')
                logger.info("已添加 external_path 字段")
                print("✅ 已添加 external_path 字段")
            
            conn.commit()
            
            if added:
                print(f"迁移完成！已添加字段: {', '.join(added)}")
            else:
                print("✅ 所有字段已存在，跳过迁移")
            
            return True
            
    except Exception as e:
        logger.error(f"迁移失败: {e}")
        print(f"❌ 迁移失败: {e}")
        return False


def rollback():
    """回滚迁移"""
    engine = create_engine(Config.DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            for column in ['source', 'skill_version', 'external_path']:
                try:
                    conn.execute(text(f"ALTER TABLE skill_configs DROP COLUMN IF EXISTS {column}"))
                    print(f"✅ 已删除 {column} 字段")
                except Exception as e:
                    print(f"⚠️ 删除 {column} 字段失败: {e}")
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"回滚失败: {e}")
        print(f"❌ 回滚失败: {e}")
        return False


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Skill 配置表外部字段迁移')
    parser.add_argument('--rollback', action='store_true', help='回滚迁移')
    args = parser.parse_args()
    
    if args.rollback:
        rollback()
    else:
        migrate()
