"""深度分析相关路由：触发分析、列表查询、转发、重试。"""

import contextlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.registry import get_default_engine, get_engine
from core.compression import decompress_payload_async
from core.config import Config
from core.config_provider import policies
from core.http_client import get_http_client
from core.logger import logger
from crud.analysis import get_deep_analyses_for_webhook, get_deep_analysis_list
from db.session import get_db_session
from models import DeepAnalysis, WebhookEvent
from schemas.analysis import DeepAnalysisListResponse
from services.event_payload import load_event_payload

deep_analysis_router = APIRouter()

MAX_PAGE = 500


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _resolve_engine(requested: str):
    """通过注册表解析引擎：优先按名称查找，回退到默认引擎。"""
    if requested and requested != "auto":
        engine = get_engine(requested)
        if engine and engine.is_available():
            return engine
        logger.warning(f"请求的引擎 '{requested}' 不可用，回退到默认引擎")
    return get_default_engine()


async def _local_ai_analysis(alert_data: dict, user_question: str) -> tuple[dict, float]:
    start_time = time.time()

    if not policies.ai.OPENAI_API_KEY:
        raise ValueError("AI 服务未配置")

    prompt_path = Path(__file__).parent.parent.parent / "prompts" / "deep_analysis.txt"
    try:
        with open(prompt_path, encoding="utf-8") as f:
            system_prompt = f.read()
    except FileNotFoundError:
        # 1. 根因分析
        # 2. 影响范围评估
        # 3. 修复建议

        pass

        # ## 告警数据
        # ```json
        # {alert_json}
        # ```
    if user_question:
        pass

        # 请返回 JSON 格式的分析报告
        # - root_cause:
        # - impact:
        # - recommendations:
        # - confidence:

    from services.ai_client import get_openai_client

    client = get_openai_client()
    response = await client.chat.completions.create(
        model=policies.ai.OPENAI_MODEL,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_question}],
        temperature=Config.ai.OPENAI_TEMPERATURE,
        max_tokens=Config.ai.OPENAI_MAX_TOKENS * 2,
    )

    ai_response = response.choices[0].message.content.strip()
    duration = time.time() - start_time

    try:
        json_match = re.search(r"```json\s*([\s\S]*?)```", ai_response)
        report = json.loads(json_match.group(1)) if json_match else json.loads(ai_response)
    except (json.JSONDecodeError, TypeError):
        report = {"root_cause": ai_response, "impact": "请查看上方分析", "recommendations": [], "confidence": 0.5}

    return report, duration


# ── 路由 ─────────────────────────────────────────────────────────────────────


@deep_analysis_router.post("/api/deep-analyze/{webhook_id}")
async def deep_analyze_webhook(webhook_id: int, payload: dict = None, session: AsyncSession = Depends(get_db_session)):
    """触发深度分析（支持多引擎）"""
    payload = payload or {}
    try:
        result = await session.execute(select(WebhookEvent).filter_by(id=webhook_id))
        event = result.scalars().first()
        if not event:
            return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

        alert_data, raw_text = await load_event_payload(event)
        if not isinstance(alert_data, dict):
            alert_data = {"raw": raw_text or ""}

        user_question = payload.get("user_question", "")
        engine_pref = payload.get("engine", "auto")

        # ===== 策略模式：通过注册表路由引擎 =====
        engine_impl = _resolve_engine(engine_pref)
        if engine_impl is None:
            return JSONResponse(status_code=503, content={"success": False, "error": "没有可用的分析引擎"})

        try:
            result = await engine_impl.analyze(
                alert_data,
                source=event.source or "unknown",
                headers=event.headers or {},
                user_question=user_question,
            )
        except Exception as e:
            return JSONResponse(status_code=500, content={"success": False, "error": f"深度分析失败: {e!s}"})

        engine_name = engine_impl.name
        # 降级回退时标记引擎名
        if result.get("_degraded") and engine_name == "openclaw":
            engine_name = "local (fallback)"

        run_id = result.get("_openclaw_run_id", "") if result.get("_pending") else ""

        deep_record = DeepAnalysis(
            webhook_event_id=webhook_id,
            engine=engine_name,
            user_question=user_question,
            analysis_result=result,
            status="pending" if result.get("_pending") else "completed",
            openclaw_run_id=run_id,
            openclaw_session_key=result.get("_openclaw_session_key", ""),
        )
        session.add(deep_record)
        await session.flush()

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": {"id": deep_record.id, "engine": engine_name, "status": deep_record.status, "result": result},
            },
        )

    except Exception as e:
        logger.error("深度分析失败: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@deep_analysis_router.get("/api/deep-analyses", response_model=DeepAnalysisListResponse)
async def list_all_deep_analyses(
    page: int = Query(1),
    per_page: int = Query(20),
    cursor: int | None = Query(None),
    status_filter: str = Query("", alias="status"),
    engine_filter: str = Query("", alias="engine"),
    session: AsyncSession = Depends(get_db_session),
):
    # Handle both FastAPI Query objects and direct calls
    if hasattr(page, "default"):
        page = page.default
    if hasattr(per_page, "default"):
        per_page = per_page.default
    if hasattr(cursor, "default"):
        cursor = cursor.default
    if hasattr(status_filter, "default"):
        status_filter = status_filter.default
    if hasattr(engine_filter, "default"):
        engine_filter = engine_filter.default

    try:
        data = await get_deep_analysis_list(
            session=session,
            page=page,
            per_page=per_page,
            cursor=cursor,
            status_filter=status_filter,
            engine_filter=engine_filter,
            max_page=MAX_PAGE,
        )
        return {"success": True, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@deep_analysis_router.get("/api/deep-analyses/{webhook_id}")
async def get_deep_analyses(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    data = await get_deep_analyses_for_webhook(session, webhook_id)
    return {"success": True, "data": data}


@deep_analysis_router.post("/api/deep-analyses/{analysis_id}/forward")
async def forward_deep_analysis(
    analysis_id: int, payload: dict | None = None, session: AsyncSession = Depends(get_db_session)
):
    payload = payload or {}
    try:
        target_url = (payload.get("target_url") or "").strip()
        if not target_url:
            return JSONResponse(status_code=400, content={"success": False, "error": "转发 URL 不能为空"})
        if not target_url.startswith(("http://", "https://")):
            return JSONResponse(status_code=400, content={"success": False, "error": "URL 格式无效"})

        analysis = await session.get(DeepAnalysis, analysis_id)
        if not analysis:
            return JSONResponse(status_code=404, content={"success": False, "error": "分析记录不存在"})
        if analysis.status != "completed":
            return JSONResponse(status_code=400, content={"success": False, "error": "分析尚未完成"})

        source = "unknown"
        if analysis.webhook_event_id:
            event = await session.get(WebhookEvent, analysis.webhook_event_id)
            if event:
                source = event.source or "unknown"

        is_feishu = "feishu.cn" in target_url or "larksuite.com" in target_url

        if is_feishu:
            from adapters.ecosystem_adapters import send_feishu_deep_analysis

            success = await send_feishu_deep_analysis(
                webhook_url=target_url,
                analysis_record={
                    "analysis_result": analysis.analysis_result,
                    "engine": analysis.engine,
                    "duration_seconds": analysis.duration_seconds,
                },
                source=source,
                webhook_event_id=analysis.webhook_event_id or 0,
            )
            if success:
                return {"success": True, "message": "已发送到飞书"}
            try:
                from crud.webhook import record_failed_forward

                await record_failed_forward(
                    webhook_event_id=analysis.webhook_event_id or 0,
                    forward_rule_id=None,
                    target_url=target_url,
                    target_type="feishu",
                    failure_reason="feishu_send_failed",
                    error_message="深度分析结果飞书发送失败",
                    forward_data={"analysis_id": analysis_id, "webhook_event_id": analysis.webhook_event_id},
                    session=session,
                )
            except Exception as rec_err:
                logger.warning("记录飞书发送失败异常: %s", rec_err)
            return JSONResponse(status_code=202, content={"success": True, "message": "分析已提交，飞书通知可能延迟"})
        else:
            fwd_payload = {
                "type": "deep_analysis",
                "analysis_id": analysis_id,
                "source": source,
                "engine": analysis.engine,
                "webhook_event_id": analysis.webhook_event_id,
                "analysis_result": analysis.analysis_result,
                "duration_seconds": analysis.duration_seconds,
                "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
            }
            client = get_http_client()
            resp = await client.post(target_url, json=fwd_payload, timeout=Config.ai.FORWARD_TIMEOUT)
            resp.raise_for_status()
            return {"success": True, "message": f"已转发 (HTTP {resp.status_code})"}
    except Exception as e:
        logger.error("转发深度分析失败: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@deep_analysis_router.post("/api/deep-analyses/{analysis_id}/retry")
async def retry_deep_analysis(analysis_id: int, session: AsyncSession = Depends(get_db_session)):
    """重新拉取深度分析结果"""

    # 策略：
    # - 配置了
    # - 未配置
    try:
        record = await session.get(DeepAnalysis, analysis_id)
        if not record:
            return JSONResponse(status_code=404, content={"success": False, "error": "分析记录不存在"})

        if record.status not in ("failed", "completed"):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": f"只能在失败或已完成状态下重新拉取，当前状态: {record.status}"},
            )

        if not record.openclaw_session_key:
            # 没有 session key，重新调用 OpenClaw 获取新的 session_key
            webhook_result = await session.execute(select(WebhookEvent).filter_by(id=record.webhook_event_id))
            webhook_event = webhook_result.scalars().first()

            if not webhook_event:
                return JSONResponse(status_code=404, content={"success": False, "error": "关联的 webhook 事件不存在"})

            # 重新构造 webhook_data
            alert_data = webhook_event.parsed_data or {}
            if not alert_data and webhook_event.raw_payload:
                try:
                    raw_text = await decompress_payload_async(webhook_event.raw_payload) or ""
                    alert_data = json.loads(raw_text)
                except Exception:
                    alert_data = {}

            webhook_data = {
                "source": webhook_event.source or "unknown",
                "headers": webhook_event.headers or {},
                "parsed_data": alert_data,
            }

            # 重新调用 OpenClaw
            from services.forward import analyze_with_openclaw

            new_result = await analyze_with_openclaw(webhook_data, user_question=record.user_question or "")

            if new_result.get("_pending"):
                record.status = "pending"
                record.created_at = datetime.now()
                record.analysis_result = new_result
                record.openclaw_run_id = new_result.get("_openclaw_run_id", "")
                record.openclaw_session_key = new_result.get("_openclaw_session_key", "")
                record.duration_seconds = 0
                await session.flush()
                logger.info(f"深度分析 #{analysis_id} 已重新触发 OpenClaw 分析")
                return {"success": True, "message": "已重新发起分析任务，请等待结果"}
            else:
                # 直接完成或降级
                record.status = "completed"
                record.analysis_result = new_result
                record.duration_seconds = 0
                await session.flush()
                return {"success": True, "message": "分析已完成"}

        # 检查是否配置了 HTTP API URL
        if Config.openclaw.OPENCLAW_HTTP_API_URL:
            # 直接通过 HTTP API 获取（复用轮询器的重试逻辑）
            from services.openclaw_poller import _poll_via_http

            logger.info(f"通过 HTTP API 重新获取分析结果: id={analysis_id}")
            result = await _poll_via_http(record.openclaw_session_key, retry_count=3)

            if result.get("status") == "error":
                return JSONResponse(
                    status_code=400, content={"success": False, "error": result.get("error", "获取失败")}
                )

            if result.get("status") != "completed":
                return JSONResponse(
                    status_code=400, content={"success": False, "error": f"获取未完成: {result.get('status')}"}
                )

            text = result.get("text", "")

            # 尝试解析 JSON
            parsed_result = None
            json_match = re.search(r"\{[\s\S]*\}", text)
            if json_match:
                with contextlib.suppress(json.JSONDecodeError):
                    parsed_result = json.loads(json_match.group())

            if parsed_result and isinstance(parsed_result, dict):
                parsed_result["_openclaw_run_id"] = record.openclaw_run_id
                parsed_result["_openclaw_text"] = text
                parsed_result["_fetched_via"] = "http-retry"
                record.analysis_result = parsed_result
            else:
                record.analysis_result = {
                    "root_cause": text,
                    "impact": "",
                    "recommendations": [],
                    "confidence": 0.5,
                    "_openclaw_run_id": record.openclaw_run_id,
                    "_openclaw_text": text,
                    "_fetched_via": "http-retry",
                }

            record.status = "completed"
            record.duration_seconds = (datetime.now() - record.created_at).total_seconds() if record.created_at else 0
            await session.flush()

            logger.info(f"HTTP API 获取成功: id={analysis_id}, text_len={len(text)}")

            # 发送飞书通知
            try:
                from adapters.ecosystem_adapters import send_feishu_deep_analysis

                if Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK:
                    result = await session.execute(select(WebhookEvent).filter_by(id=record.webhook_event_id))
                    event = result.scalars().first()
                    source = event.source if event else ""
                    analysis_data = {
                        "analysis_result": record.analysis_result,
                        "engine": record.engine,
                        "duration_seconds": record.duration_seconds,
                    }
                    success = await send_feishu_deep_analysis(
                        webhook_url=Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK,
                        analysis_record=analysis_data,
                        source=source,
                        webhook_event_id=record.webhook_event_id,
                    )
                    if not success:
                        try:
                            from crud.webhook import record_failed_forward

                            await record_failed_forward(
                                webhook_event_id=record.webhook_event_id,
                                forward_rule_id=None,
                                target_url=Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK,
                                target_type="feishu",
                                failure_reason="feishu_send_failed",
                                error_message="HTTP重试后飞书通知发送失败",
                                forward_data={"analysis_id": analysis_id, "webhook_event_id": record.webhook_event_id},
                                session=session,
                            )
                        except Exception as rec_err:
                            logger.warning(f"记录飞书发送失败异常: {rec_err}")
            except Exception as notify_err:
                logger.warning(f"飞书深度分析通知失败: {notify_err}")

            return {"success": True, "message": f"获取成功！通过 HTTP API 获取了 {len(text)} 字符的分析结果"}
        else:
            # 没有配置 HTTP API，重置为 pending 让轮询器处理
            record.status = "pending"
            record.created_at = datetime.now()
            record.analysis_result = None
            await session.flush()

            logger.info(f"深度分析 #{analysis_id} 已重置为 pending，等待轮询重新拉取")
            return {"success": True, "message": "已重新开始拉取，请等待结果"}

    except Exception as e:
        logger.error("重试深度分析失败: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})
