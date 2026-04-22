#!/usr/bin/env python3
"""
Webhook AI分析服务主入口
"""
import sys
import os
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from core.app import app
from core.config import Config
from core.models import test_db_connection
from core.logger import logger

# 启动 OpenClaw 轮询后台任务
# 在分布式/多 worker 模式下，允许每个进程启动轮询线程，
# 真正的互斥和状态共享在 services/openclaw_poller.py 中由 Redis 锁和缓存来接管
def _start_poller():
    from services.openclaw_poller import start_poller
    start_poller(interval=30)
    from services.maintenance_poller import start_maintenance_poller
    start_maintenance_poller()

_start_poller()


if __name__ == '__main__':
    # 启动前验证
    Config.validate()
    if not test_db_connection():
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)
    
    logger.info(f"启动 Webhook 服务: http://{Config.HOST}:{Config.PORT}")
    import uvicorn
    uvicorn.run(
        "main:app",
        host=Config.HOST,
        port=Config.PORT,
        log_level="debug" if Config.DEBUG else "info"
    )
