import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

# 必须导入任务以注册到 broker
import services.tasks  # noqa: F401
from adapters.plugins.local_engine import LocalAnalysisEngine
from adapters.plugins.openclaw_engine import OpenClawAnalysisEngine
from adapters.registry import register_engine
from api.admin import admin_router
from api.analysis import analysis_router
from api.forwarding import forwarding_router
from api.webhook import webhook_router
from core.auth import verify_api_key
from core.config import Config
from core.http_client import close_http_client, get_http_client
from core.logger import logger, stop_log_listener
from core.metrics import setup_metrics
from core.otel import setup_otel
from core.redis_client import dispose_redis
from core.taskiq_broker import broker
from core.trace import build_traceparent, extract_trace_id_from_headers, generate_trace_id, set_trace_id, trace_id_var
from db.session import dispose_engine, init_engine
from services.ai_analyzer import reset_openai_client
from services.pipeline import get_running_tasks


def _create_supervised_task(
    name: str, coro_factory: Callable[[], Awaitable[None]], *, leader: bool
) -> asyncio.Task[object]:
    async def _runner() -> None:
        from core.metrics import (
            BACKGROUND_POLLER_CRASHES_TOTAL,
            BACKGROUND_POLLER_LEADER,
            BACKGROUND_POLLER_RESTARTS_TOTAL,
            BACKGROUND_POLLER_UP,
        )

        async def _run_with_leader_election() -> None:
            from core.redis_client import redis_eval_int, redis_set_nx_ex

            lock_key = f"webhook:poller:leader:{name}"
            token = Config.server.WORKER_ID
            ttl_seconds = 60
            release_script = (
                "if redis.call('GET', KEYS[1]) == ARGV[1] then " "return redis.call('DEL', KEYS[1]) else return 0 end"
            )
            renew_script = (
                "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                "return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end"
            )

            while True:
                try:
                    acquired = await redis_set_nx_ex(lock_key, token, ttl_seconds)
                except Exception:
                    BACKGROUND_POLLER_LEADER.labels(name=name).set(0)
                    logger.exception("[Poller] %s leader election unavailable; running without lock", name)
                    await coro_factory()
                    return

                if not acquired:
                    BACKGROUND_POLLER_LEADER.labels(name=name).set(0)
                    await asyncio.sleep(5)
                    continue

                BACKGROUND_POLLER_LEADER.labels(name=name).set(1)
                lost_event = asyncio.Event()

                async def _renew_loop(lost: asyncio.Event) -> None:
                    try:
                        while True:
                            await asyncio.sleep(ttl_seconds / 2)
                            try:
                                renewed = await redis_eval_int(renew_script, 1, lock_key, token, ttl_seconds)
                            except Exception:
                                logger.exception("[Poller] %s leader lock renew error", name)
                                continue
                            if renewed == 0:
                                lost.set()
                                return
                    except asyncio.CancelledError:
                        raise

                poller_task = asyncio.create_task(coro_factory())
                renew_task = asyncio.create_task(_renew_loop(lost_event))
                lost_task = asyncio.create_task(lost_event.wait())
                try:
                    done, pending = await asyncio.wait({poller_task, lost_task}, return_when=asyncio.FIRST_COMPLETED)
                    if lost_task in done and lost_event.is_set():
                        poller_task.cancel()
                        await asyncio.gather(poller_task, return_exceptions=True)
                        raise RuntimeError("lost leader lock")
                    await poller_task
                    return
                finally:
                    lost_task.cancel()
                    renew_task.cancel()
                    await asyncio.gather(lost_task, renew_task, return_exceptions=True)
                    with contextlib.suppress(Exception):
                        await redis_eval_int(release_script, 1, lock_key, token)
                    BACKGROUND_POLLER_LEADER.labels(name=name).set(0)

        async def _run_once() -> None:
            if leader and Config.server.ENABLE_POLLER_LEADER_ELECTION:
                await _run_with_leader_election()
                return
            BACKGROUND_POLLER_LEADER.labels(name=name).set(0)
            await coro_factory()

        BACKGROUND_POLLER_UP.labels(name=name).set(1)
        restarted = False
        try:
            while True:
                if restarted:
                    BACKGROUND_POLLER_RESTARTS_TOTAL.labels(name=name).inc()
                try:
                    await _run_once()
                    BACKGROUND_POLLER_CRASHES_TOTAL.labels(name=name).inc()
                    logger.error("[Poller] %s exited unexpectedly, restarting", name)
                    restarted = True
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    BACKGROUND_POLLER_CRASHES_TOTAL.labels(name=name).inc()
                    logger.exception("[Poller] %s crashed, restarting", name)
                    restarted = True
                    await asyncio.sleep(5)
        finally:
            BACKGROUND_POLLER_UP.labels(name=name).set(0)

    return asyncio.create_task(_runner(), name=f"poller:{name}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 注册深度分析引擎
    register_engine(LocalAnalysisEngine())
    register_engine(OpenClawAnalysisEngine())
    if not Config.security.API_KEY and not (Config.server.DEBUG or Config.security.ALLOW_UNAUTHENTICATED_ADMIN):
        raise RuntimeError(
            "API_KEY 未配置且未允许公开管理接口，请设置 API_KEY 或在本地启用 ALLOW_UNAUTHENTICATED_ADMIN=true"
        )
    get_http_client()
    await init_engine()
    if Config.server.ENABLE_RUNTIME_CONFIG:
        await Config.load_from_db()
        await Config.start_subscriber()

    # 启动 TaskIQ Broker (API 侧只需 startup)
    await broker.startup()

    # 启动 Recovery + Metrics 轮询（API 进程内兜底，worker 侧由 TaskIQ 驱动）
    _poller_tasks = []
    if Config.server.ENABLE_POLLERS:
        from services.metrics_poller import refresh_all_metrics
        from services.recovery_poller import run_recovery_scan

        async def _recovery_loop() -> None:
            # 启动时立即执行一次，捞起重启前遗留的僵尸事件
            try:
                await run_recovery_scan(stuck_threshold_seconds=0)
            except Exception as e:
                logger.warning("[App] startup recovery scan error: %s", e)
            while True:
                try:
                    await run_recovery_scan()
                except Exception as e:
                    logger.warning("[App] recovery scan error: %s", e)
                await asyncio.sleep(Config.server.RECOVERY_POLLER_INTERVAL_SECONDS)

        async def _metrics_loop() -> None:
            while True:
                try:
                    await refresh_all_metrics()
                except Exception as e:
                    logger.warning("[App] metrics refresh error: %s", e)
                await asyncio.sleep(15)

        async def _openclaw_poll_loop() -> None:
            from services.openclaw_poller import poll_pending_analyses

            while True:
                try:
                    await poll_pending_analyses()
                except Exception as e:
                    logger.warning("[App] openclaw poller error: %s", e)
                await asyncio.sleep(30)

        async def _forward_retry_loop() -> None:
            from services.forward_retry_poller import poll_pending_retries

            while True:
                try:
                    await poll_pending_retries()
                except Exception as e:
                    logger.warning("[App] forward retry poller error: %s", e)
                await asyncio.sleep(Config.retry.FORWARD_RETRY_POLL_INTERVAL)

        async def _maintenance_loop() -> None:
            from services.data_maintenance import archive_old_data_by_policy

            while True:
                now = datetime.now()
                # 计算到下一个 MAINTENANCE_HOUR 点的秒数
                target = now.replace(hour=Config.maintenance.MAINTENANCE_HOUR, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                try:
                    moved = await archive_old_data_by_policy()
                    logger.info("[App] 数据维护完成，归档 %d 条记录", moved)
                except Exception as e:
                    logger.warning("[App] 数据维护失败: %s", e)

        _poller_tasks.append(_create_supervised_task("recovery", _recovery_loop, leader=True))
        _poller_tasks.append(_create_supervised_task("metrics_refresh", _metrics_loop, leader=True))
        _poller_tasks.append(_create_supervised_task("openclaw_poll", _openclaw_poll_loop, leader=True))
        if Config.retry.ENABLE_FORWARD_RETRY:
            _poller_tasks.append(_create_supervised_task("forward_retry", _forward_retry_loop, leader=True))
        _poller_tasks.append(_create_supervised_task("maintenance", _maintenance_loop, leader=True))
        logger.info("[App] 所有后台轮询任务已启动")

    yield

    for t in _poller_tasks:
        t.cancel()

    await Config.stop_subscriber()
    await broker.shutdown()

    # 优雅等待正在运行的任务
    running = get_running_tasks()
    if running:
        grace_timeout = Config.server.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
        logger.info(
            "优雅停机：等待 %d 个正在运行的任务完成 (超时 %ds)",
            len(running),
            grace_timeout,
        )
        await asyncio.wait(running, timeout=grace_timeout)

    await dispose_engine()
    await dispose_redis()
    await reset_openai_client()
    await close_http_client()
    stop_log_listener()


app = FastAPI(title="Webhook AI Assistant", lifespan=lifespan)


setup_metrics(app)
setup_otel(app)
app.mount("/static", StaticFiles(directory="templates/static"), name="static")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware – avoids BaseHTTPMiddleware's TaskGroup isolation
    that breaks asyncpg connections across tasks."""

    _EXTRA_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    ]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing_names = {h[0].lower() for h in headers}
                for name, value in self._EXTRA_HEADERS:
                    if name.lower() not in existing_names:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


class TraceContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers") or []}
        incoming = extract_trace_id_from_headers(headers)
        if incoming and "traceparent" not in headers:
            raw_headers = list(scope.get("headers") or [])
            raw_headers.append((b"traceparent", build_traceparent(incoming).encode("latin1")))
            scope["headers"] = raw_headers

        # 优先使用 OTEL 当前 span 的 trace_id 保证日志与 APM 双向关联；
        # OTEL 未启用时回退到请求头携带的 trace_id 或生成新 id
        from core.otel import get_otel_trace_id

        otel_tid = get_otel_trace_id()
        token = set_trace_id(otel_tid or incoming or generate_trace_id())
        try:
            await self.app(scope, receive, send)
            # 请求处理完成后，OTEL span 已激活，同步 trace_id 到日志上下文
            otel_tid = get_otel_trace_id()
            if otel_tid:
                set_trace_id(otel_tid)
        finally:
            trace_id_var.reset(token)


app.add_middleware(TraceContextMiddleware)


_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
logger.debug(f"worker_id={_WORKER_ID}")


app.include_router(analysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(forwarding_router, dependencies=[Depends(verify_api_key)])
app.include_router(admin_router, dependencies=[Depends(verify_api_key)])
app.include_router(webhook_router)
