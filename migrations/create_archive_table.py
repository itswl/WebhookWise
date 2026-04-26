import logging

from core.models import ArchivedWebhookEvent, Base, get_engine

logger = logging.getLogger(__name__)

def migrate():
    """创建归档表"""
    engine = get_engine()
    try:
        # 只创建不存在的表
        ArchivedWebhookEvent.__table__.create(engine, checkfirst=True)
        logger.info("✅ 已成功创建 archived_webhook_events 表（如果不存在）")
    except Exception as e:
        logger.error(f"❌ 创建归档表失败: {e}")
        raise

if __name__ == "__main__":
    from core.config import Config
    logging.basicConfig(level=logging.INFO)
    migrate()
