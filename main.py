#!/usr/bin/env python3
"""
Webhook AI分析服务主入口
"""
import sys
import threading
import time
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from core.app import app
from core.config import Config
from core.models import test_db_connection, get_session
from core.logger import logger


def start_prediction_scheduler():
    """启动预测引擎的后台调度器"""
    def _scheduler_loop():
        from services.predictor import alert_predictor
        
        interval = Config.PREDICTION_INTERVAL
        # 启动后等待一段时间，让应用完全启动
        time.sleep(30)
        logger.info(f"Prediction scheduler started (interval: {interval}s)")
        
        while True:
            try:
                session = get_session()
                try:
                    alert_predictor.run_prediction_cycle(session)
                finally:
                    session.close()
            except Exception as e:
                logger.error(f"Prediction scheduler error: {e}")
            
            time.sleep(interval)
    
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
    logger.info("Prediction scheduler thread initialized")


if __name__ == '__main__':
    # 启动前验证
    Config.validate()
    if not test_db_connection():
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)
    
    # 启动后台预测调度器
    start_prediction_scheduler()
    
    logger.info(f"启动 Webhook 服务: http://{Config.HOST}:{Config.PORT}")
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.DEBUG
    )
