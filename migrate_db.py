#!/usr/bin/env python3
"""
数据库迁移脚本：添加告警去重相关字段
"""
from sqlalchemy import text
from models import get_engine
from logger import logger


def migrate_database():
    """执行数据库迁移"""
    engine = get_engine()
    
    migrations = [
        # 添加 alert_hash 字段
        {
            'name': '添加 alert_hash 字段',
            'check': "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='webhook_events' AND column_name='alert_hash'",
            'sql': "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS alert_hash VARCHAR(64)"
        },
        # 添加 is_duplicate 字段
        {
            'name': '添加 is_duplicate 字段',
            'check': "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='webhook_events' AND column_name='is_duplicate'",
            'sql': "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS is_duplicate INTEGER DEFAULT 0"
        },
        # 添加 duplicate_of 字段
        {
            'name': '添加 duplicate_of 字段',
            'check': "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='webhook_events' AND column_name='duplicate_of'",
            'sql': "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS duplicate_of INTEGER"
        },
        # 添加 duplicate_count 字段
        {
            'name': '添加 duplicate_count 字段',
            'check': "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='webhook_events' AND column_name='duplicate_count'",
            'sql': "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS duplicate_count INTEGER DEFAULT 1"
        }
    ]
    
    with engine.connect() as conn:
        for migration in migrations:
            try:
                # 检查字段是否已存在
                result = conn.execute(text(migration['check']))
                count = result.scalar()
                
                if count == 0:
                    logger.info(f"执行迁移: {migration['name']}")
                    conn.execute(text(migration['sql']))
                    conn.commit()
                    logger.info(f"迁移完成: {migration['name']}")
                else:
                    logger.info(f"跳过迁移(字段已存在): {migration['name']}")
                    
            except Exception as e:
                logger.error(f"迁移失败: {migration['name']}, 错误: {str(e)}")
                conn.rollback()
                raise
        
        # 为 alert_hash 字段创建索引
        try:
            logger.info("创建 alert_hash 索引")
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_alert_hash ON webhook_events(alert_hash)"))
            conn.commit()
            logger.info("索引创建完成")
        except Exception as e:
            logger.warning(f"创建索引失败: {str(e)}")
    
    logger.info("数据库迁移全部完成！")


if __name__ == '__main__':
    logger.info("开始数据库迁移...")
    migrate_database()
