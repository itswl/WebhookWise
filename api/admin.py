"""
Admin and Management API Routes.
Handles system configuration, prompt management, and incident recovery (Dead Letter / Stuck Events).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api import _fail, _ok
from core.auth import verify_admin_write
from core.config import Config
from core.logger import logger
from db.session import get_db_session
from schemas import (
    ConfigResponse,
    ConfigSourcesResponse,
    ConfigUpdateResponse,
    DeadLetterListResponse,
    PromptGetResponse,
    PromptReloadResponse,
    ReplayAllResponse,
    ReplayResponse,
    StuckEventListResponse,
    StuckEventRequeueResponse,
)
from services.ai_analyzer import (
    load_user_prompt_template,
    reload_user_prompt_template,
)
from services.config_service import (
    build_prompt_source,
    collect_config_updates,
    get_config_sources,
    get_current_config,
)
from services.tasks import process_webhook_task
from services.webhook_orchestrator import (
    count_dead_letters,
    list_dead_letters,
    list_stuck_events,
    replay_dead_letter,
    requeue_stuck_event,
)

admin_router = APIRouter()


@admin_router.get("/api/config", response_model=ConfigResponse)
async def get_config():
    try:
        return _ok(get_current_config(), 200)
    except Exception as e:
        logger.error(f"获取配置失败: {e!s}")
        return _fail(str(e), 500)


@admin_router.get("/api/config/sources", response_model=ConfigSourcesResponse)
async def get_config_sources_endpoint():
    try:
        items_data = get_config_sources()
        return _ok(items_data, 200)
    except Exception as e:
        logger.error(f"获取配置来源失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post("/api/config", response_model=ConfigUpdateResponse, dependencies=[Depends(verify_admin_write)])
async def update_config(payload: dict | None = None):
    try:
        if not payload:
            return _fail("请求体为空", 400)

        updates, errors = collect_config_updates(payload)
        if errors:
            return _fail("; ".join(errors), 400)

        if not updates:
            return _ok(status=200, message="无需更新")

        runtime_updates = {var_name: typed_value for var_name, (_str_val, typed_value) in updates.items()}
        await Config.save_batch(runtime_updates)

        logger.info(f"配置已更新: {list(updates.keys())}")
        return _ok(status=200, message=f"配置更新成功，已保存 {len(runtime_updates)} 项")

    except Exception as e:
        logger.error(f"更新配置失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post("/api/prompt/reload", response_model=PromptReloadResponse)
def reload_prompt():
    try:
        new_template = reload_user_prompt_template()
        preview = new_template[:200] + ("..." if len(new_template) > 200 else "")
        return _ok(status=200, message="Prompt 模板已重新加载", template_length=len(new_template), preview=preview)
    except Exception as e:
        logger.error(f"重新加载 prompt 模板失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.get("/api/prompt", response_model=PromptGetResponse)
def get_prompt():
    try:
        template = load_user_prompt_template()
        return _ok(status=200, template=template, source=build_prompt_source())
    except Exception as e:
        logger.error(f"获取 prompt 模板失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


# ── Dead Letter & Stuck Events ──────────────────────────────────────────────────────────


@admin_router.get("/api/admin/dead-letters", response_model=DeadLetterListResponse)
async def get_dead_letters_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        items = await list_dead_letters(session, page=page, page_size=page_size)
        total = await count_dead_letters(session)
        return _ok(data=items, http_status=200, pagination={"page": page, "page_size": page_size, "total": total})
    except Exception as e:
        logger.error(f"查询 dead_letter 列表失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/admin/dead-letters/{event_id}/replay",
    response_model=ReplayResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def replay_single_dead_letter(event_id: int, session: AsyncSession = Depends(get_db_session)):
    try:
        updated = await replay_dead_letter(session, event_id)
        if not updated:
            return _fail(f"事件 {event_id} 不存在或状态非 dead_letter", 404)
        await session.commit()
        await process_webhook_task.kiq(event_id=event_id)
        return _ok(http_status=200, message=f"事件 {event_id} 已重放", event_id=event_id)
    except Exception as e:
        logger.error(f"重放 dead_letter 失败: {event_id=}, error={e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.get("/api/admin/stuck-events", response_model=StuckEventListResponse)
async def get_stuck_events_endpoint(
    status: str = Query("", alias="status"),
    older_than_seconds: int = Query(300, ge=0),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
):
    statuses = [s for s in (status or "").split(",") if s.strip()]
    try:
        items = await list_stuck_events(
            session, statuses=statuses or None, older_than_seconds=older_than_seconds, limit=limit
        )
        return _ok(items, 200)
    except Exception as e:
        logger.error(f"查询 stuck-events 失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/admin/stuck-events/{event_id}/requeue",
    response_model=StuckEventRequeueResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def requeue_single_stuck_event(event_id: int, session: AsyncSession = Depends(get_db_session)):
    try:
        updated = await requeue_stuck_event(session, event_id)
        if not updated:
            return _fail(f"事件 {event_id} 不存在或状态不可重放", 404)
        await session.commit()
        await process_webhook_task.kiq(event_id=event_id, client_ip="admin-requeue")
        return _ok(http_status=200, message=f"事件 {event_id} 已重新入队", event_id=event_id)
    except Exception as e:
        logger.error(f"重放 stuck-event 失败: {event_id=}, error={e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/admin/dead-letters/replay-all", response_model=ReplayAllResponse, dependencies=[Depends(verify_admin_write)]
)
async def replay_all_dead_letters(
    batch_size: int = Query(50, ge=1, le=500), session: AsyncSession = Depends(get_db_session)
):
    try:
        items = await list_dead_letters(session, page=1, page_size=batch_size)
        if not items:
            return _ok(http_status=200, message="无 dead_letter 需要重放", replayed=0)
        replayed_ids = []
        for item in items:
            eid = item["id"]
            if await replay_dead_letter(session, eid):
                replayed_ids.append(eid)
        await session.commit()
        for eid in replayed_ids:
            await process_webhook_task.kiq(event_id=eid)
        return _ok(
            http_status=200,
            message=f"已重放 {len(replayed_ids)} 条 dead_letter",
            replayed=len(replayed_ids),
            event_ids=replayed_ids,
        )
    except Exception as e:
        logger.error(f"批量重放 dead_letter 失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)
