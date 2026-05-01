"""CRUD 层通用辅助函数。"""

import contextlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def count_with_timeout(
    session: AsyncSession,
    stmt,
    timeout_ms: int = 2000,
) -> int | None:
    """带 statement_timeout 兜底的 COUNT 查询（PostgreSQL-only, skip for SQLite）。

    超时返回 None，调用方应适配 None 场景。
    """
    try:
        # Try to set statement_timeout (PostgreSQL-specific)
        with contextlib.suppress(Exception):
            await session.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}'"))

        result = await session.execute(stmt)
        return result.scalar() or 0
    except Exception as e:
        logger.warning("COUNT query timeout (%dms): %s", timeout_ms, e)
        # 关键：清理已中止的事务，避免后续查询连带失败
        with contextlib.suppress(Exception):
            await session.rollback()
        return None
    finally:
        # Try to reset statement_timeout (PostgreSQL-specific)
        with contextlib.suppress(Exception):
            await session.execute(text("RESET statement_timeout"))
