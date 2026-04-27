#!/usr/bin/env python3
"""
Webhook AI分析服务主入口
"""
import sys
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from core.config import Config
from core.logger import logger
from db.session import test_db_connection

if __name__ == '__main__':
    # 启动前验证
    Config.validate()
    if not test_db_connection():
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)
    
    logger.info(f"启动 Webhook 服务: http://{Config.HOST}:{Config.PORT}")
    import uvicorn
    uvicorn.run(
        "core.app:app",
        host=Config.HOST,
        port=Config.PORT,
        log_level="debug" if Config.DEBUG else "info"
    )
