import logging
from datetime import datetime, timedelta
from sqlalchemy import text, insert
from core.models import session_scope, WebhookEvent, ArchivedWebhookEvent
from core.config import Config

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("archive_task")

def archive_old_data(days: int = 30):
    """
    归档旧数据：
    1. 将 30 天前、状态为已完成且非高风险的告警搬迁到归档表
    2. 删除活跃表中的对应记录
    """
    threshold = datetime.now() - timedelta(days=days)
    logger.info(f"开始执行归档任务，清理 {threshold.strftime('%Y-%m-%d %H:%M:%S')} 之前的记录...")
    
    total_moved = 0
    
    try:
        with session_scope() as session:
            # 1. 查找符合条件的记录 IDs
            # 条件：
            # - 时间早于 threshold
            # - importance 不是 'high' (保留重要告警在活跃表供分析)
            # - forward_status 不是 'failed' (避免丢掉还没发送成功的)
            # - 或者是重复告警 (is_duplicate=1)
            
            target_ids_query = session.query(WebhookEvent.id).filter(
                WebhookEvent.timestamp < threshold
            ).filter(
                (WebhookEvent.importance != 'high') | (WebhookEvent.is_duplicate == 1)
            ).filter(
                WebhookEvent.forward_status != 'failed'
            ).limit(5000) # 每次搬运 5000 条，避免事务过大
            
            target_ids = [r[0] for r in target_ids_query.all()]
            
            if not target_ids:
                logger.info("没有发现符合归档条件的记录。")
                return 0

            logger.info(f"发现 {len(target_ids)} 条记录待归档...")

            # 2. 批量搬迁数据 (Postgres 原生语法支持 INSERT INTO ... SELECT ...)
            # 为了保证模型一致性，这里使用 SQLAlchemy 批量插入
            for chunk_start in range(0, len(target_ids), 1000):
                chunk_ids = target_ids[chunk_start : chunk_start + 1000]
                
                # 获取数据
                events = session.query(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)).all()
                
                # 构造归档对象
                archived_records = []
                for e in events:
                    archived_records.append({
                        'id': e.id,
                        'source': e.source,
                        'client_ip': e.client_ip,
                        'timestamp': e.timestamp,
                        'raw_payload': e.raw_payload,
                        'headers': e.headers,
                        'parsed_data': e.parsed_data,
                        'alert_hash': e.alert_hash,
                        'ai_analysis': e.ai_analysis,
                        'importance': e.importance,
                        'forward_status': e.forward_status,
                        'is_duplicate': e.is_duplicate,
                        'duplicate_of': e.duplicate_of,
                        'duplicate_count': e.duplicate_count,
                        'beyond_window': e.beyond_window,
                        'last_notified_at': e.last_notified_at,
                        'created_at': e.created_at,
                        'updated_at': e.updated_at,
                        'archived_at': datetime.now()
                    })
                
                # 执行批量插入
                if archived_records:
                    session.execute(insert(ArchivedWebhookEvent), archived_records)
                    
                # 删除原记录
                session.query(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)).delete(synchronize_session=False)
                
                total_moved += len(chunk_ids)
                logger.info(f"已搬迁 {total_moved} 条记录...")

            session.commit()
            logger.info(f"归档任务完成！共处理 {total_moved} 条记录。")
            
            # 3. 碎片整理 (可选，生产环境大表删除后建议执行)
            # session.execute(text("VACUUM ANALYZE webhook_events"))
            
    except Exception as e:
        logger.error(f"归档任务失败: {e}", exc_info=True)
        return total_moved

if __name__ == "__main__":
    import sys
    import os
    # 确保能找到 core 模块
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    
    # 获取归档天数参数
    archive_days = 30
    if len(sys.argv) > 1:
        try:
            archive_days = int(sys.argv[1])
        except ValueError:
            pass
            
    archive_old_data(archive_days)
