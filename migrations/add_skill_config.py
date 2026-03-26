#!/usr/bin/env python3
"""
Migration: 添加 Skill 配置表

创建 skill_configs 表用于存储 Skill 平台连接配置。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, JSON, Index
from sqlalchemy.ext.declarative import declarative_base
from core.config import Config
from core.logger import logger

Base = declarative_base()


class SkillConfig(Base):
    """Skill 平台连接配置"""
    __tablename__ = 'skill_configs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False)
    display_name = Column(String(128), nullable=False)
    description = Column(Text)
    skill_type = Column(String(32), nullable=False)
    enabled = Column(Boolean, default=True)
    config = Column(JSON, default=dict)
    code = Column(Text)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)


def migrate():
    """执行迁移"""
    engine = create_engine(Config.DATABASE_URL)
    
    try:
        # 检查表是否已存在
        from sqlalchemy import inspect
        inspector = inspect(engine)
        
        if 'skill_configs' in inspector.get_table_names():
            logger.info("skill_configs 表已存在，跳过迁移")
            print("✅ skill_configs 表已存在，跳过迁移")
            return True
        
        # 创建表
        Base.metadata.create_all(engine, tables=[SkillConfig.__table__])
        logger.info("skill_configs 表创建成功")
        print("✅ skill_configs 表创建成功")
        return True
        
    except Exception as e:
        logger.error(f"迁移失败: {e}")
        print(f"❌ 迁移失败: {e}")
        return False


def rollback():
    """回滚迁移"""
    engine = create_engine(Config.DATABASE_URL)
    
    try:
        Base.metadata.drop_all(engine, tables=[SkillConfig.__table__])
        logger.info("skill_configs 表已删除")
        print("✅ skill_configs 表已删除")
        return True
    except Exception as e:
        logger.error(f"回滚失败: {e}")
        print(f"❌ 回滚失败: {e}")
        return False


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Skill 配置表迁移')
    parser.add_argument('--rollback', action='store_true', help='回滚迁移')
    args = parser.parse_args()
    
    if args.rollback:
        rollback()
    else:
        migrate()
