#!/usr/bin/env python3
"""
Webhook AI分析服务主入口
"""

from __future__ import annotations

import asyncio

import uvloop

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from core.dependencies import get_config_manager
from core.logger import get_logger
from core.service_lifecycle import check_database_ready

logger = get_logger("main")

if __name__ == "__main__":
    config = get_config_manager()
    # 启动前验证（model_validator 在 AppConfig 实例化时已自动执行）
    if not asyncio.run(check_database_ready()):
        logger.error("数据库连接失败，请检查配置")
        raise SystemExit(1)

    logger.info("启动 Webhook 服务: http://%s:%s", config.server.HOST, config.server.PORT)
    import uvicorn

    uvicorn.run(
        "core.app:app",
        host=config.server.HOST,
        port=config.server.PORT,
        log_level="debug" if config.server.DEBUG else "info",
        loop="uvloop",
        http="httptools",
    )
