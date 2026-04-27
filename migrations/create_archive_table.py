import logging

from db.session import get_engine
from models import ArchivedWebhookEvent

logger = logging.getLogger(__name__)

def migrate():
    """创建归档表"""
    engine = get_engine()
    try:
        # 只创建不存在的表
        ArchivedWebhookEvent.__table__.create(engine, checkfirst=True)
        logger.info("✅ 已成功创建 archived_webhook_events 表（如果不存在）")
    except Exception as e: # noqa: PERF203
        logger.error(f"❌ 创建归档表失败: {e}")
        raise

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate()
