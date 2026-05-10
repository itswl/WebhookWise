import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeVar, cast

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config import Config
from core.logger import mask_url

if TYPE_CHECKING:
    from pydantic import BaseModel

T = TypeVar("T", bound="BaseModel")


class SerializerMixin:
    """提供通用的序列化能力，减少 Models 与 Schemas 之间的重复代码。"""

    __table__: Any

    def to_schema(self, schema_cls: type[T]) -> T:
        """将 Model 实例转换为指定的 Pydantic Schema。"""
        return schema_cls.model_validate(self)

    def to_dict(self, schema_cls: type["BaseModel"] | None = None) -> dict[str, object]:
        """将 Model 实例转换为字典。如果提供 schema_cls，则通过 Schema 进行过滤和格式化。"""
        if schema_cls:
            return cast(dict[str, object], self.to_schema(schema_cls).model_dump(mode="json"))
        # 默认简单的 dict 转换（排除 bytes 等非 JSON 序列化字段，格式化 datetime）
        import datetime

        res: dict[str, object] = {}
        for c in self.__table__.columns:
            val = getattr(self, c.name)
            if isinstance(val, (bytes, memoryview)):
                continue
            if isinstance(val, datetime.datetime):
                val = val.isoformat()
            res[c.name] = val
        return res


_logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# ── 全局异步引擎（主事件循环使用） ──
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine_kwargs() -> dict[str, Any]:
    """返回连接池公共参数"""
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": Config.db.DB_POOL_SIZE,
        "max_overflow": Config.db.DB_MAX_OVERFLOW,
        "pool_recycle": Config.db.DB_POOL_RECYCLE,
        "pool_timeout": Config.db.DB_POOL_TIMEOUT,
        "connect_args": {
            "server_settings": {
                "statement_timeout": str(Config.db.DB_STATEMENT_TIMEOUT_MS),
                # 关闭同步提交可提升写入 3-5x，断电时可能丢失最近 1-2s 数据
                # Webhook 场景可接受（有 Redis MQ + RecoveryPoller 兜底）
                "synchronous_commit": Config.db.DB_SYNC_COMMIT,
            }
        },
    }


def _async_url() -> str:
    """将 DATABASE_URL 的 driver 前缀安全替换为 asyncpg。

    不使用 make_url 解析，避免密码含 @#%: 等特殊字符时被误判为 URL 分隔符。
    """
    url = Config.db.DATABASE_URL
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+asyncpg://", 1)
    return url


# ────────────────────────────────────────
# 公共 API
# ────────────────────────────────────────


async def init_engine() -> None:
    """创建全局 AsyncEngine 和 async_sessionmaker（应用启动时调用一次）"""
    global _engine, _session_factory
    if _session_factory is not None:
        return  # 已初始化，幂等
    _logger.info("[DB] 正在初始化异步数据库连接池: %s", mask_url(Config.db.DATABASE_URL))
    _engine = create_async_engine(_async_url(), **_build_engine_kwargs())
    _session_factory = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)
    assert _engine is not None
    try:
        from core.otel import instrument_sqlalchemy

        instrument_sqlalchemy(_engine.sync_engine)
    except Exception as e:
        _logger.debug("[DB] SQLAlchemy 自动探测失败（可能未安装 OTEL）: %s", e)
    _setup_pool_metrics(_engine)


def _setup_pool_metrics(engine: AsyncEngine) -> None:
    """注册连接池事件监听，通过回调更新 Prometheus Gauge。"""
    from core.metrics import DB_POOL_CHECKED_OUT, DB_POOL_SIZE

    pool = engine.sync_engine.pool

    def _on_checkout(dbapi_conn: Any, connection_record: Any, connection_proxy: Any) -> None:
        DB_POOL_CHECKED_OUT.inc()

    def _on_checkin(dbapi_conn: Any, connection_record: Any) -> None:
        DB_POOL_CHECKED_OUT.dec()

    event.listen(pool, "checkout", _on_checkout)
    event.listen(pool, "checkin", _on_checkin)

    # 初始化连接池容量
    DB_POOL_SIZE.set(int(get_db_pool_capacity(engine) or 0))
    _logger.info("[DB] Pool 事件监听已注册 (checkout/checkin → Prometheus Gauge)")


def get_db_pool_capacity(engine: AsyncEngine) -> int | None:
    pool = engine.sync_engine.pool
    size = getattr(pool, "size", None)
    overflow = getattr(pool, "overflow", None)
    if not callable(size) or not callable(overflow):
        return None
    try:
        return int(size() + overflow())
    except Exception:
        return None


async def dispose_engine() -> None:
    """关闭全局异步引擎（应用关闭时调用）"""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    _logger.info("[DB] 当前数据库引擎已关闭")


def get_engine() -> AsyncEngine | None:
    """返回全局异步引擎（向后兼容）"""
    return _engine


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI Depends 异步生成器：只管理 session 生命周期。

    HTTP 写接口需要显式提交。这样路由可以在提交成功后再触发 TaskIQ
    或外部通知，避免依赖退出时才提交导致的事务/副作用顺序不清。
    """
    if _session_factory is None:
        await init_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        yield session


@asynccontextmanager
async def session_scope(existing_session: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
    """异步数据库事务上下文管理器。

    新建 session 时使用 SQLAlchemy 2.0 的 ``async_sessionmaker.begin()``，
    由框架负责提交、回滚和关闭。传入 existing_session 时不接管事务边界，
    由外层调用方负责提交或回滚。
    """
    if existing_session:
        yield existing_session
    else:
        if _session_factory is None:
            await init_engine()
        assert _session_factory is not None
        async with _session_factory.begin() as session:
            yield session


async def count_with_timeout(
    session: AsyncSession,
    stmt: Any,
    timeout_ms: int = 2000,
) -> int | None:
    """带 statement_timeout 兜底的 COUNT 查询（PostgreSQL-only）。

    超时返回 None，调用方应适配 None 场景。
    使用 SAVEPOINT 隔离超时查询，避免 rollback 摧毁调用者事务。
    """
    try:
        async with session.begin_nested():
            with contextlib.suppress(Exception):
                await session.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}'"))
            result = await session.execute(stmt)
            return result.scalar() or 0
    except Exception as e:
        _logger.warning("COUNT query timeout (%dms): %s", timeout_ms, e)
        return None


# ────────────────────────────────────────
# 异步初始化 & 连接测试
# ────────────────────────────────────────


async def init_db() -> None:
    """初始化数据库表（异步版本）"""
    if _engine is None:
        await init_engine()
    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _logger.info("数据库表初始化完成")


async def test_db_connection() -> bool:
    """测试数据库连接（异步版本）"""
    try:
        if _engine is None:
            await init_engine()
        assert _engine is not None
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _logger.info("数据库连接测试成功")
        return True
    except Exception as e:
        _logger.error("数据库连接失败: %s", e)
        return False
