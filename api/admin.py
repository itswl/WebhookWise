"""
Admin and Management API Routes.
Handles system configuration, prompt management, and dead-letter replay.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api import _fail, _ok
from core.auth import verify_admin_write
from core.logger import get_logger
from db.session import get_db_session
from models import WebhookEvent
from schemas import (
    ConfigResponse,
    ConfigSourcesResponse,
    ConfigUpdateResponse,
    DeadLetterListResponse,
    PromptGetResponse,
    PromptReloadResponse,
    ReplayAllResponse,
    ReplayResponse,
)
from services.analysis.ai_analyzer import (
    get_prompt_source,
    load_deep_analysis_prompt_template,
    load_user_prompt_template,
    reload_deep_analysis_prompt_template,
    reload_user_prompt_template,
)
from services.operations.tasks import process_webhook_task
from services.runtime_config.config_service import (
    collect_config_updates,
    get_config_sources,
    get_current_config,
    runtime_config_enabled,
    save_config_updates,
)
from services.webhooks.query_service import count_dead_letters, list_dead_letters
from services.webhooks.repository import load_event_payload

logger = get_logger("api.admin")

admin_router = APIRouter()
PromptKind = Literal["user", "deep_analysis"]


def _normalize_prompt_kind(kind: str) -> PromptKind:
    normalized = kind.strip().lower().replace("-", "_")
    if normalized in ("user", "ai"):
        return "user"
    if normalized in ("deep_analysis", "deep"):
        return "deep_analysis"
    raise ValueError("unsupported prompt kind")


async def _load_prompt_by_kind(kind: PromptKind) -> str:
    if kind == "deep_analysis":
        return await load_deep_analysis_prompt_template()
    return await load_user_prompt_template()


async def _reload_prompt_by_kind(kind: PromptKind) -> str:
    if kind == "deep_analysis":
        return await reload_deep_analysis_prompt_template()
    return await reload_user_prompt_template()


@admin_router.get("/api/config", response_model=ConfigResponse)
async def get_config() -> JSONResponse:
    try:
        return _ok(get_current_config(), 200)
    except Exception as e:
        logger.error("获取配置失败: %s", e)
        return _fail(str(e), 500)


@admin_router.get("/api/config/sources", response_model=ConfigSourcesResponse)
async def get_config_sources_endpoint() -> JSONResponse:
    try:
        items_data = get_config_sources()
        return _ok(items_data, 200)
    except Exception as e:
        logger.error("获取配置来源失败: %s", e, exc_info=True)
        return _fail(str(e), 500)


@admin_router.post("/api/config", response_model=ConfigUpdateResponse, dependencies=[Depends(verify_admin_write)])
async def update_config(payload: dict[str, Any] | None = None) -> JSONResponse:
    try:
        if not runtime_config_enabled():
            logger.warning("[Admin] 配置更新被拒绝，运行时动态配置未启用")
            return _fail("运行时动态配置已禁用，请通过环境变量/ConfigMap 配置并滚动重启生效", 403)
        if not payload:
            logger.warning("[Admin] 配置更新被拒绝，请求体为空")
            return _fail("请求体为空", 400)

        updates, errors = collect_config_updates(payload)
        if errors:
            logger.warning("[Admin] 配置更新校验失败 errors=%s", errors)
            return _fail("; ".join(errors), 400)

        if not updates:
            logger.info("[Admin] 配置更新无需变更")
            return _ok(status=200, message="无需更新")

        updated_count = await save_config_updates(updates)

        logger.info("[Admin] 配置已更新 keys=%s count=%s", list(updates.keys()), updated_count)
        return _ok(status=200, message=f"配置更新成功，已保存 {updated_count} 项")

    except ValueError as e:
        logger.warning("更新配置被拒绝: %s", e)
        return _fail(str(e), 400)
    except Exception as e:
        logger.error("更新配置失败: %s", e, exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/prompt/reload",
    response_model=PromptReloadResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def reload_prompt(kind: str = Query("user")) -> JSONResponse:
    try:
        prompt_kind = _normalize_prompt_kind(kind)
        new_template = await _reload_prompt_by_kind(prompt_kind)
        preview = new_template[:200] + ("..." if len(new_template) > 200 else "")
        logger.info("[Admin] Prompt 模板已重新加载 kind=%s length=%s", prompt_kind, len(new_template))
        return _ok(
            status=200,
            message="Prompt 模板已重新加载",
            kind=prompt_kind,
            source=get_prompt_source(prompt_kind),
            template_length=len(new_template),
            preview=preview,
        )
    except ValueError as e:
        return _fail(str(e), 400)
    except Exception as e:
        logger.error("重新加载 prompt 模板失败: %s", e, exc_info=True)
        return _fail(str(e), 500)


@admin_router.get("/api/prompt", response_model=PromptGetResponse)
async def get_prompt(kind: str = Query("user")) -> JSONResponse:
    try:
        prompt_kind = _normalize_prompt_kind(kind)
        template = await _load_prompt_by_kind(prompt_kind)
        return _ok(status=200, kind=prompt_kind, template=template, source=get_prompt_source(prompt_kind))
    except ValueError as e:
        return _fail(str(e), 400)
    except Exception as e:
        logger.error("获取 prompt 模板失败: %s", e, exc_info=True)
        return _fail(str(e), 500)


# ── Dead Letter ───────────────────────────────────────────────────────────────


async def _enqueue_dead_letter_event(event: WebhookEvent) -> None:
    headers = {str(k): str(v) for k, v in dict(event.headers or {}).items()}
    _, raw_body = await load_event_payload(event)
    await process_webhook_task.kiq(
        source_name=event.source or "unknown",
        raw_headers=headers,
        raw_body=raw_body,
        client_ip=event.client_ip or "admin-replay",
        request_id=event.request_id,
        received_at=event.timestamp.isoformat() if event.timestamp else None,
        ingest_retry_count=max(0, int(event.retry_count or 0)),
    )


@admin_router.get("/api/admin/dead-letters", response_model=DeadLetterListResponse)
async def get_dead_letters_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        items = await list_dead_letters(session, page=page, page_size=page_size)
        total = await count_dead_letters(session)
        return _ok(data=items, http_status=200, pagination={"page": page, "page_size": page_size, "total": total})
    except Exception as e:
        logger.error("查询 dead_letter 列表失败: %s", e, exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/admin/dead-letters/{event_id}/replay",
    response_model=ReplayResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def replay_single_dead_letter(event_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    try:
        event = await session.get(WebhookEvent, event_id)
        if not event or event.processing_status != "dead_letter":
            logger.warning("[Admin] dead_letter 重放失败，状态不匹配或事件不存在 event_id=%s", event_id)
            return _fail(f"事件 {event_id} 不存在或状态非 dead_letter", 404)
        await _enqueue_dead_letter_event(event)
        logger.info("[Admin] dead_letter 已重放 event_id=%s", event_id)
        return _ok(http_status=200, message=f"事件 {event_id} 已重放", event_id=event_id)
    except Exception as e:
        logger.error("重放 dead_letter 失败: event_id=%s, error=%s", event_id, e, exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/admin/dead-letters/replay-all", response_model=ReplayAllResponse, dependencies=[Depends(verify_admin_write)]
)
async def replay_all_dead_letters(
    batch_size: int = Query(50, ge=1, le=500), session: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    try:
        items = await list_dead_letters(session, page=1, page_size=batch_size)
        if not items:
            logger.info("[Admin] 批量重放 dead_letter：无待处理记录")
            return _ok(http_status=200, message="无 dead_letter 需要重放", replayed=0)
        replayed_ids = []
        for item in items:
            eid = item["id"]
            event = await session.get(WebhookEvent, eid)
            if event and event.processing_status == "dead_letter":
                replayed_ids.append(eid)
                await _enqueue_dead_letter_event(event)
        logger.info("[Admin] 批量重放 dead_letter 完成 replayed=%s event_ids=%s", len(replayed_ids), replayed_ids)
        return _ok(
            http_status=200,
            message=f"已重放 {len(replayed_ids)} 条 dead_letter",
            replayed=len(replayed_ids),
            event_ids=replayed_ids,
        )
    except Exception as e:
        logger.error("批量重放 dead_letter 失败: %s", e, exc_info=True)
        return _fail(str(e), 500)
