import logging
from datetime import datetime, timedelta
from sqlalchemy import text, insert
from core.models import session_scope, WebhookEvent, ArchivedWebhookEvent
from core.config import Config

logger = logging.getLogger("webhook_service.maintenance")

def archive_old_data(days: int = 30):
    """
    归档旧数据：
    1. 将 30 天前、状态为已完成且非高风险的告警搬迁到归档表
    2. 删除活跃表中的对应记录
    """
    threshold = datetime.now() - timedelta(days=days)
    logger.info(f"[Maintenance] 开始执行归档任务，清理 {threshold.strftime('%Y-%m-%d %H:%M:%S')} 之前的记录...")
    
    total_moved = 0
    
    try:
        with session_scope() as session:
            # 1. 查找符合条件的记录 IDs
            target_ids_query = session.query(WebhookEvent.id).filter(
                WebhookEvent.timestamp < threshold
            ).filter(
                (WebhookEvent.importance != 'high') | (WebhookEvent.is_duplicate == 1)
            ).filter(
                WebhookEvent.forward_status != 'failed'
            ).limit(5000)
            
            target_ids = [r[0] for r in target_ids_query.all()]
            
            if not target_ids:
                logger.info("[Maintenance] 没有发现符合归档条件的记录。")
                return 0

            logger.info(f"[Maintenance] 发现 {len(target_ids)} 条记录待归档...")

            # 2. 批量搬迁数据
            for chunk_start in range(0, len(target_ids), 1000):
                chunk_ids = target_ids[chunk_start : chunk_start + 1000]
                
                events = session.query(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)).all()
                
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
                
                if archived_records:
                    session.execute(insert(ArchivedWebhookEvent), archived_records)
                    
                session.query(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)).delete(synchronize_session=False)
                
                total_moved += len(chunk_ids)
                logger.info(f"[Maintenance] 已搬迁 {total_moved} 条记录...")

            logger.info(f"[Maintenance] 归档任务完成！共处理 {total_moved} 条记录。")
            return total_moved
            
    except Exception as e:
        logger.error(f"[Maintenance] 归档任务失败: {e}", exc_info=True)
        return total_moved
