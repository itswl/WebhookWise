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

# 启动 OpenClaw 轮询后台任务（仅启动一个实例）
_poller_lock_file = None  # 全局变量，防止 GC 关闭文件描述符释放锁

def _start_poller_once():
    """使用文件锁确保只有一个 worker 启动轮询"""
    global _poller_lock_file
    import fcntl
    lock_path = Path(Config.DATA_DIR) / 'openclaw_poller.lock'
    try:
        _poller_lock_file = open(lock_path, 'w')
        fcntl.flock(_poller_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 获得锁，启动轮询
        from services.openclaw_poller import start_poller
        start_poller(interval=30)
        logger.info("当前 worker 启动了 OpenClaw 轮询任务")
        # lock_file 保存在全局变量中，防止函数返回后被 GC 释放锁
    except (IOError, OSError):
        # 其他 worker 已经持有锁，跳过
        logger.info("其他 worker 已启动轮询任务，跳过")

_start_poller_once()


if __name__ == '__main__':
    # 启动前验证
    Config.validate()
    if not test_db_connection():
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)
    
    logger.info(f"启动 Webhook 服务: http://{Config.HOST}:{Config.PORT}")
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.DEBUG
    )
