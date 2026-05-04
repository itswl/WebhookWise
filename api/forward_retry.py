# api/forward_retry.py
"""转发重试管理 API"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api import _fail, _ok
from core.logger import logger
from services.forward import (
    delete_failed_forward,
    get_failed_forward_stats,
    get_failed_forwards,
    manual_retry_reset,
)
from db.session import get_db_session

forward_retry_router = APIRouter()


@forward_retry_router.get("/api/failed-forwards")
async def list_failed_forwards(
    status: str = Query(None),
    target_type: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    """获取失败转发列表"""
    try:
        records, total = await get_failed_forwards(
            status=status,
            target_type=target_type,
            limit=limit,
            offset=offset,
            session=session,
        )
        return _ok(data=records, total=total, limit=limit, offset=offset)
    except Exception as e:
        logger.error(f"获取失败转发列表失败: {e!s}")
        return _fail(str(e), 500)


@forward_retry_router.get("/api/failed-forwards/stats")
async def get_retry_stats(session: AsyncSession = Depends(get_db_session)):
    """获取转发重试统计"""
    try:
        stats = await get_failed_forward_stats(session=session)
        return _ok(data=stats)
    except Exception as e:
        logger.error(f"获取转发重试统计失败: {e!s}")
        return _fail(str(e), 500)


@forward_retry_router.post("/api/failed-forwards/{failed_forward_id}/retry")
async def retry_forward(failed_forward_id: int, session: AsyncSession = Depends(get_db_session)):
    """手动重试（重置 exhausted 为 pending）"""
    try:
        success = await manual_retry_reset(failed_forward_id, session=session)
        if success:
            return _ok(message="已重置为待重试")
        return _fail("记录不存在或状态不是 exhausted", 400)
    except Exception as e:
        logger.error(f"手动重试失败: {e!s}")
        return _fail(str(e), 500)


@forward_retry_router.delete("/api/failed-forwards/{failed_forward_id}")
async def delete_record(failed_forward_id: int, session: AsyncSession = Depends(get_db_session)):
    """删除失败转发记录"""
    try:
        success = await delete_failed_forward(failed_forward_id, session=session)
        if success:
            return _ok(message="记录已删除")
        return _fail("记录不存在", 404)
    except Exception as e:
        logger.error(f"删除失败转发记录失败: {e!s}")
        return _fail(str(e), 500)
