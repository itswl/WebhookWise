"""Runtime configuration manager — DB overrides, Redis pub/sub sync."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from typing import TypedDict, TypeVar

from pydantic_settings import BaseSettings

from core.config.defaults import (
    AIConfig,
    CircuitBreakerConfig,
    DBConfig,
    MaintenanceConfig,
    OpenClawConfig,
    RedisConfig,
    RetryConfig,
    RuntimeType,
    RuntimeValue,
    SecurityConfig,
    ServerConfig,
    get_settings,
)

_config_logger = logging.getLogger("config")

_TSubSettings = TypeVar("_TSubSettings", bound=BaseSettings)


class _RuntimeKeyMeta(TypedDict):
    type: RuntimeType
    sub: str


class UnifiedConfigManager:
    """统一配置管理器：合并静态配置、动态覆盖、DB 加载与 Redis 同步"""

    RUNTIME_CONFIG_CHANNEL = "webhook:config:updated"
    RESTART_REQUIRED_RUNTIME_KEYS = frozenset(
        {
            "OPENAI_API_KEY",
            "OPENAI_API_URL",
            "OPENAI_MODEL",
            "OPENCLAW_GATEWAY_URL",
            "OPENCLAW_GATEWAY_TOKEN",
            "OPENCLAW_HOOKS_TOKEN",
            "OPENCLAW_HTTP_API_URL",
        }
    )

    RUNTIME_KEYS: dict[str, _RuntimeKeyMeta] = {
        "FORWARD_URL": {"type": "str", "sub": "ai"},
        "ENABLE_FORWARD": {"type": "bool", "sub": "ai"},
        "ENABLE_AI_ANALYSIS": {"type": "bool", "sub": "ai"},
        "OPENAI_API_KEY": {"type": "str", "sub": "ai"},
        "OPENAI_API_URL": {"type": "str", "sub": "ai"},
        "OPENAI_MODEL": {"type": "str", "sub": "ai"},
        "AI_SYSTEM_PROMPT": {"type": "str", "sub": "ai"},
        "LOG_LEVEL": {"type": "str", "sub": "server"},
        "DUPLICATE_ALERT_TIME_WINDOW": {"type": "int", "sub": "retry"},
        "FORWARD_DUPLICATE_ALERTS": {"type": "bool", "sub": "retry"},
        "REANALYZE_AFTER_TIME_WINDOW": {"type": "bool", "sub": "retry"},
        "FORWARD_AFTER_TIME_WINDOW": {"type": "bool", "sub": "retry"},
        "ENABLE_ALERT_NOISE_REDUCTION": {"type": "bool", "sub": "ai"},
        "NOISE_REDUCTION_WINDOW_MINUTES": {"type": "int", "sub": "ai"},
        "ROOT_CAUSE_MIN_CONFIDENCE": {"type": "float", "sub": "ai"},
        "NOISE_RELATED_MIN_CONFIDENCE": {"type": "float", "sub": "ai"},
        "NOISE_SOURCE_WEIGHT": {"type": "float", "sub": "ai"},
        "NOISE_RESOURCE_WEIGHT": {"type": "float", "sub": "ai"},
        "NOISE_SEMANTIC_WEIGHT": {"type": "float", "sub": "ai"},
        "NOISE_SEVERITY_WEIGHT": {"type": "float", "sub": "ai"},
        "NOISE_TIME_WEIGHT": {"type": "float", "sub": "ai"},
        "NOISE_SEVERITY_DOWNGRADE_SCORE": {"type": "float", "sub": "ai"},
        "SUPPRESS_DERIVED_ALERT_FORWARD": {"type": "bool", "sub": "ai"},
        "RULE_HIGH_KEYWORDS": {"type": "str", "sub": "ai"},
        "RULE_WARN_KEYWORDS": {"type": "str", "sub": "ai"},
        "RULE_METRIC_KEYWORDS": {"type": "str", "sub": "ai"},
        "RULE_THRESHOLD_MULTIPLIER": {"type": "float", "sub": "ai"},
        "AI_PAYLOAD_MAX_BYTES": {"type": "int", "sub": "ai"},
        "AI_PAYLOAD_STRIP_KEYS": {"type": "str", "sub": "ai"},
        "NOTIFICATION_COOLDOWN_SECONDS": {"type": "int", "sub": "retry"},
        "ENABLE_PERIODIC_REMINDER": {"type": "bool", "sub": "retry"},
        "REMINDER_INTERVAL_HOURS": {"type": "int", "sub": "retry"},
        "WEBHOOK_RATE_LIMIT_PER_MINUTE": {"type": "int", "sub": "security"},
        "WEBHOOK_RATE_LIMIT_BURST": {"type": "int", "sub": "security"},
        "WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE": {"type": "int", "sub": "security"},
        "OPENCLAW_ENABLED": {"type": "bool", "sub": "openclaw"},
        "OPENCLAW_GATEWAY_URL": {"type": "str", "sub": "openclaw"},
        "OPENCLAW_GATEWAY_TOKEN": {"type": "str", "sub": "openclaw"},
        "OPENCLAW_HOOKS_TOKEN": {"type": "str", "sub": "openclaw"},
        "OPENCLAW_HTTP_API_URL": {"type": "str", "sub": "openclaw"},
        "OPENCLAW_TIMEOUT_SECONDS": {"type": "int", "sub": "openclaw"},
        "OPENCLAW_STABILITY_REQUIRED_HITS": {"type": "int", "sub": "openclaw"},
        "OPENCLAW_POLL_INITIAL_DELAY_SECONDS": {"type": "int", "sub": "openclaw"},
        "OPENCLAW_POLL_MAX_DELAY_SECONDS": {"type": "int", "sub": "openclaw"},
        "OPENCLAW_POLL_BACKOFF_MULTIPLIER": {"type": "float", "sub": "openclaw"},
        "OPENCLAW_MAX_CONSECUTIVE_ERRORS": {"type": "int", "sub": "openclaw"},
        "OPENCLAW_ENABLE_DEGRADATION": {"type": "bool", "sub": "openclaw"},
        "OPENCLAW_CONNECT_TIMEOUT": {"type": "int", "sub": "openclaw"},
        "OPENCLAW_HANDSHAKE_TIMEOUT": {"type": "int", "sub": "openclaw"},
        "OPENCLAW_NONCE_TIMEOUT": {"type": "float", "sub": "openclaw"},
        "OPENCLAW_POLL_TIMEOUT": {"type": "int", "sub": "openclaw"},
    }

    def __init__(self) -> None:
        self._overrides: dict[str, RuntimeValue] = {}
        self._meta: dict[str, dict[str, object]] = {}
        self._subscriber_task: asyncio.Task[object] | None = None
        self._running = False
        self._db_loaded_once = False
        self._last_restart_required_skipped_keys: frozenset[str] = frozenset()

    def _merged_sub(self, sub_name: str, base: _TSubSettings) -> _TSubSettings:
        updates: dict[str, RuntimeValue] = {}
        for key, value in self._overrides.items():
            meta = self.RUNTIME_KEYS.get(key)
            if not meta or meta["sub"] != sub_name:
                continue
            if not hasattr(base, key):
                continue
            if value == "" and getattr(base, key, None):
                continue
            updates[key] = value
        if not updates:
            return base
        updated = base.model_copy(update=updates)
        return updated

    @property
    def server(self) -> ServerConfig:
        return self._merged_sub("server", get_settings().server)

    @property
    def security(self) -> SecurityConfig:
        return self._merged_sub("security", get_settings().security)

    @property
    def db(self) -> DBConfig:
        return self._merged_sub("db", get_settings().db)

    @property
    def redis(self) -> RedisConfig:
        return self._merged_sub("redis", get_settings().redis)

    @property
    def ai(self) -> AIConfig:
        return self._merged_sub("ai", get_settings().ai)

    @property
    def openclaw(self) -> OpenClawConfig:
        return self._merged_sub("openclaw", get_settings().openclaw)

    @property
    def circuit_breaker(self) -> CircuitBreakerConfig:
        return self._merged_sub("circuit_breaker", get_settings().circuit_breaker)

    @property
    def retry(self) -> RetryConfig:
        return self._merged_sub("retry", get_settings().retry)

    @property
    def maintenance(self) -> MaintenanceConfig:
        return self._merged_sub("maintenance", get_settings().maintenance)

    # ── 动态管理接口 ──

    def set_override(
        self,
        key: str,
        value: RuntimeValue | None,
        source: str = "db",
        updated_by: str | None = None,
    ) -> None:
        if value is None:
            self._overrides.pop(key, None)
            self._meta.pop(key, None)
        else:
            self._overrides[key] = value
            self._meta[key] = {"source": source, "updated_at": datetime.now(), "updated_by": updated_by}
        if key == "LOG_LEVEL":
            import logging

            level = getattr(logging, str(value or "INFO").upper(), logging.INFO)
            logging.getLogger("webhook_service").setLevel(level)

    def get_meta(self, key: str) -> dict[str, object]:
        return self._meta.get(key, {})

    def runtime_key_requires_restart(self, key: str) -> bool:
        return key in self.RESTART_REQUIRED_RUNTIME_KEYS and not self.server.ALLOW_RUNTIME_CONNECTION_CONFIG

    def _ensure_runtime_key_mutable(self, key: str, str_value: str) -> None:
        if str_value == "":
            return
        if self.runtime_key_requires_restart(key):
            raise ValueError(
                f"{key} 属于连接/密钥类配置，默认禁止热更新；请通过环境变量或 ConfigMap 修改并滚动重启。"
                "如确认要承担多进程配置短暂不一致风险，可设置 ALLOW_RUNTIME_CONNECTION_CONFIG=true。"
            )

    # ── 运行时持久化与同步 ──

    async def load_from_db(self) -> bool:
        """从数据库加载热更新配置"""
        from sqlalchemy import select

        from db.session import session_scope
        from models import SystemConfig

        try:
            async with session_scope() as session:
                result = await session.execute(select(SystemConfig))
                configs = {row.key: row for row in result.scalars().all()}

            count = 0
            skipped_restart_required_keys: set[str] = set()
            for key, row in configs.items():
                if key in self.RUNTIME_KEYS:
                    if self.runtime_key_requires_restart(key):
                        self.set_override(key, None, source="db", updated_by=row.updated_by)
                        skipped_restart_required_keys.add(key)
                        continue
                    val = self._deserialize(row.value, self.RUNTIME_KEYS[key]["type"])
                    self.set_override(key, val, source="db", updated_by=row.updated_by)
                    count += 1
            self._db_loaded_once = True
            skipped_keys = frozenset(skipped_restart_required_keys)
            if skipped_keys and skipped_keys != self._last_restart_required_skipped_keys:
                _config_logger.warning(
                    "[Config] 已忽略 %d 个需重启生效的 DB 配置: %s",
                    len(skipped_keys),
                    ",".join(sorted(skipped_keys)),
                )
            elif skipped_keys:
                _config_logger.debug("[Config] 已忽略 %d 个需重启生效的 DB 配置", len(skipped_keys))
            self._last_restart_required_skipped_keys = skipped_keys
            _config_logger.info("[Config] 从数据库加载 %d 个热更新配置", count)
            return True
        except Exception as e:
            _config_logger.warning("[Config] 数据库加载失败: %s", e)
            return False

    async def save_runtime_config(self, key: str, value: RuntimeValue, updated_by: str = "api") -> None:
        if key not in self.RUNTIME_KEYS:
            raise ValueError(f"不支持热更新的配置: {key}")

        v_type = self.RUNTIME_KEYS[key]["type"]
        str_val = self._serialize(value, v_type)
        self._ensure_runtime_key_mutable(key, str_val)

        from sqlalchemy import delete, select

        from db.session import session_scope
        from models import SystemConfig

        async with session_scope() as session:
            if str_val == "" and v_type == "str":
                await session.execute(delete(SystemConfig).where(SystemConfig.key == key))
                self.set_override(key, None, source="db", updated_by=updated_by)
            else:
                existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
                config = existing.scalar_one_or_none()
                if config:
                    config.value, config.value_type, config.updated_by = str_val, v_type, updated_by
                else:
                    config = SystemConfig(key=key, value=str_val, value_type=v_type, updated_by=updated_by)
                    session.add(config)

                typed_val = self._deserialize(str_val, v_type)
                self.set_override(key, typed_val, source="db", updated_by=updated_by)

        await self._publish_change([key])

    async def save_batch(self, updates: dict[str, RuntimeValue], updated_by: str = "api") -> None:
        for key, value in updates.items():
            if key in self.RUNTIME_KEYS:
                v_type = self.RUNTIME_KEYS[key]["type"]
                self._ensure_runtime_key_mutable(key, self._serialize(value, v_type))

        changed_keys = []
        for key, value in updates.items():
            if key in self.RUNTIME_KEYS:
                await self.save_runtime_config(key, value, updated_by)
                changed_keys.append(key)
        _config_logger.info("[Config] 批量更新完成: %s", changed_keys)

    async def _publish_change(self, keys: list[str]) -> None:
        try:
            from core.redis_client import redis_publish

            msg = json.dumps({"worker_id": self.server.WORKER_ID, "keys": keys, "ts": time.time()})
            await redis_publish(self.RUNTIME_CONFIG_CHANNEL, msg)
        except Exception as e:
            _config_logger.warning("[Config] Redis 发布失败: %s", e)

    async def start_subscriber(self) -> None:
        if self._running:
            return
        self._running = True
        self._subscriber_task = asyncio.create_task(self._subscribe_loop())
        _config_logger.info("[Config] 实时同步已启动")

    async def stop_subscriber(self) -> None:
        self._running = False
        if self._subscriber_task:
            self._subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscriber_task
        _config_logger.info("[Config] 实时同步已停止")

    async def _subscribe_loop(self) -> None:
        last_sync_time = time.time()
        while self._running:
            if not self._db_loaded_once:
                ok = await self.load_from_db()
                if not ok:
                    await asyncio.sleep(5)
                    continue
                last_sync_time = time.time()

            # 每 2 分钟进行一次强制全量同步，防止任何 Worker 状态永久脱节
            now = time.time()
            if now - last_sync_time > 120:
                await self.load_from_db()
                last_sync_time = now

            await self._run_subscription()

    async def _run_subscription(self) -> None:
        from core.redis_client import redis_pubsub

        try:
            pubsub = redis_pubsub()
            await pubsub.subscribe(self.RUNTIME_CONFIG_CHANNEL)

            for _ in range(24):
                if not self._running:
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                if isinstance(msg, dict) and msg.get("type") == "message":
                    loaded = json.loads(msg.get("data", ""))
                    if isinstance(loaded, dict) and loaded.get("worker_id") != self.server.WORKER_ID:
                        await self.load_from_db()
            await pubsub.close()
        except Exception as e:
            _config_logger.warning("[Config] 同步异常，5s后重连: %s", e)
            await asyncio.sleep(5)

    def _serialize(self, v: RuntimeValue, t: RuntimeType) -> str:
        if t == "bool":
            return str(bool(v)).lower()
        return str(v)

    def _deserialize(self, v: str, t: RuntimeType) -> RuntimeValue:
        if t == "bool":
            return v.lower() in ("true", "1", "yes")
        if t == "int":
            return int(v)
        if t == "float":
            return float(v)
        return v
