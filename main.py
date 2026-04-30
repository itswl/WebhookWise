#!/usr/bin/env python3
"""
Webhook AI分析服务主入口
"""

import asyncio

import uvloop

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

import sys
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from core.config import Config
from core.logger import logger
from db.session import init_engine, test_db_connection


async def _check_db():
    await init_engine()
    return await test_db_connection()


if __name__ == "__main__":
    # 启动前验证（model_validator 在 Config 实例化时已自动执行）
    if not asyncio.run(_check_db()):
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)

    logger.info(f"启动 Webhook 服务: http://{Config.server.HOST}:{Config.server.PORT}")
    import uvicorn

    uvicorn.run(
        "core.app:app",
        host=Config.server.HOST,
        port=Config.server.PORT,
        log_level="debug" if Config.server.DEBUG else "info",
        loop="uvloop",
        http="httptools",
    )
