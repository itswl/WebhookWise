"""
运行时配置管理器 — 业务策略热更新

配置优先级（从低到高）：
1. core/config.py 中的 Pydantic 默认值
2. .env 文件中的环境变量覆盖
3. 数据库 system_configs 表中的值（启动时 load_from_db 加载）
4. Web 管理界面 /api/config 修改后通过 Redis Pub/Sub 实时同步

RUNTIME_KEYS 中定义的配置项支持热更新，修改后无需重启即可生效。
其余配置（DATABASE_URL、REDIS_URL、连接池参数等）为静态基础设施配置，
修改后必须重启服务才能生效。
"""

import asyncio
import contextlib
import json
import time
from datetime import datetime

from core.config import Config
from core.config_provider import policies
from core.logger import get_logger
from core.redis_client import get_redis
from crud.webhook import get_all_runtime_configs, upsert_runtime_config

logger = get_logger("runtime_config")

RUNTIME_CONFIG_CHANNEL = "webhook:config:updated"

# 运行时 key → 子配置属性名映射
_KEY_TO_SUBCONFIG: dict[str, str] = {
    "FORWARD_URL": "ai",
    "ENABLE_FORWARD": "ai",
    "ENABLE_AI_ANALYSIS": "ai",
    "OPENAI_API_KEY": "ai",
    "OPENAI_API_URL": "ai",
    "OPENAI_MODEL": "ai",
    "AI_SYSTEM_PROMPT": "ai",
    "LOG_LEVEL": "server",
    "DUPLICATE_ALERT_TIME_WINDOW": "retry",
    "FORWARD_DUPLICATE_ALERTS": "retry",
    "REANALYZE_AFTER_TIME_WINDOW": "retry",
    "FORWARD_AFTER_TIME_WINDOW": "retry",
    "ENABLE_PERIODIC_REMINDER": "retry",
    "REMINDER_INTERVAL_HOURS": "retry",
    "NOTIFICATION_COOLDOWN_SECONDS": "retry",
    "ENABLE_ALERT_NOISE_REDUCTION": "ai",
    "NOISE_REDUCTION_WINDOW_MINUTES": "ai",
    "ROOT_CAUSE_MIN_CONFIDENCE": "ai",
    "SUPPRESS_DERIVED_ALERT_FORWARD": "ai",
    "RULE_HIGH_KEYWORDS": "ai",
    "RULE_WARN_KEYWORDS": "ai",
    "RULE_METRIC_KEYWORDS": "ai",
    "RULE_THRESHOLD_MULTIPLIER": "ai",
    "AI_PAYLOAD_MAX_BYTES": "ai",
    "AI_PAYLOAD_STRIP_KEYS": "ai",
}


def _set_nested(key: str, value, *, source: str | None = None, updated_at=None, updated_by: str | None = None) -> None:
    policies.set(key, value, source=source, updated_at=updated_at, updated_by=updated_by)


# 16 个运行时可变配置定义（从 admin.py _CONFIG_SCHEMA 对齐）
# key: 环境变量名, type: 值类型, desc: 描述
RUNTIME_KEYS = {
    "FORWARD_URL": {"type": "str", "desc": "告警转发目标 URL"},
    "ENABLE_FORWARD": {"type": "bool", "desc": "启用告警转发"},
    "ENABLE_AI_ANALYSIS": {"type": "bool", "desc": "启用 AI 分析"},
    "OPENAI_API_KEY": {"type": "str", "desc": "OpenAI API 密钥"},
    "OPENAI_API_URL": {"type": "str", "desc": "OpenAI API 地址"},
    "OPENAI_MODEL": {"type": "str", "desc": "AI 模型名称"},
    "AI_SYSTEM_PROMPT": {"type": "str", "desc": "AI 系统提示词"},
    "LOG_LEVEL": {"type": "str", "desc": "日志级别"},
    "DUPLICATE_ALERT_TIME_WINDOW": {"type": "int", "desc": "告警去重时间窗口（小时）"},
    "FORWARD_DUPLICATE_ALERTS": {"type": "bool", "desc": "转发重复告警"},
    "REANALYZE_AFTER_TIME_WINDOW": {"type": "bool", "desc": "超窗口后重新分析"},
    "FORWARD_AFTER_TIME_WINDOW": {"type": "bool", "desc": "超窗口后转发"},
    "ENABLE_ALERT_NOISE_REDUCTION": {"type": "bool", "desc": "启用智能降噪"},
    "NOISE_REDUCTION_WINDOW_MINUTES": {"type": "int", "desc": "降噪时间窗口（分钟）"},
    "ROOT_CAUSE_MIN_CONFIDENCE": {"type": "float", "desc": "根因关联最小置信度"},
    "SUPPRESS_DERIVED_ALERT_FORWARD": {"type": "bool", "desc": "抑制衍生告警转发"},
    "RULE_HIGH_KEYWORDS": {"type": "str", "desc": "规则降级：高优先级关键字"},
    "RULE_WARN_KEYWORDS": {"type": "str", "desc": "规则降级：警告级别关键字"},
    "RULE_METRIC_KEYWORDS": {"type": "str", "desc": "规则降级：指标名称关键字"},
    "RULE_THRESHOLD_MULTIPLIER": {"type": "float", "desc": "规则降级：超阈值倍数提升为 high"},
    "AI_PAYLOAD_MAX_BYTES": {"type": "int", "desc": "AI 分析输入 payload 最大字节数"},
    "AI_PAYLOAD_STRIP_KEYS": {"type": "str", "desc": "AI 分析前移除的噪音字段名"},
}


def _serialize_value(value, value_type: str) -> str:
    """将值序列化为字符串存储"""
    if value_type == "bool":
        if isinstance(value, str):
            return value.lower()
        return str(bool(value)).lower()
    return str(value)


def _deserialize_value(value: str, value_type: str):
    """从字符串反序列化为正确类型"""
    if value_type == "bool":
        return value.lower() in ("true", "1", "yes")
    elif value_type == "int":
        return int(value)
    elif value_type == "float":
        return float(value)
    return value


class RuntimeConfigManager:
    def __init__(self):
        self._subscriber_task: asyncio.Task | None = None
        self._running = False

    async def load_from_db(self):
        """启动时调用：从 system_configs 批量加载，setattr 到 Config 单例"""
        try:
            configs = await get_all_runtime_configs()
            count = 0
            for key, config_row in configs.items():
                if key in RUNTIME_KEYS:
                    typed_value = _deserialize_value(config_row.value, config_row.value_type)
                    _set_nested(
                        key,
                        typed_value,
                        source="db",
                        updated_at=config_row.updated_at,
                        updated_by=config_row.updated_by,
                    )
                    count += 1
            logger.info(f"[RuntimeConfig] 从数据库加载 {count} 个运行时配置")
        except Exception as e:
            logger.warning(f"[RuntimeConfig] 从数据库加载配置失败，使用默认值: {e}")

    async def save(self, key: str, value, updated_by: str = "api"):
        """写入单个配置：DB + setattr + Redis publish"""
        if key not in RUNTIME_KEYS:
            raise ValueError(f"不支持的运行时配置: {key}")
        value_type = RUNTIME_KEYS[key]["type"]
        str_value = _serialize_value(value, value_type)
        await upsert_runtime_config(key, str_value, value_type, updated_by)
        typed_value = _deserialize_value(str_value, value_type)
        _set_nested(key, typed_value, source="db", updated_at=datetime.now(), updated_by=updated_by)
        await self._publish_change([key])

    async def save_batch(self, updates: dict, updated_by: str = "api"):
        """批量保存（对应 POST /api/config 的批量更新）"""
        changed_keys = []
        for key, value in updates.items():
            if key not in RUNTIME_KEYS:
                continue
            value_type = RUNTIME_KEYS[key]["type"]
            str_value = _serialize_value(value, value_type)
            await upsert_runtime_config(key, str_value, value_type, updated_by)
            typed_value = _deserialize_value(str_value, value_type)
            _set_nested(key, typed_value, source="db", updated_at=datetime.now(), updated_by=updated_by)
            changed_keys.append(key)
        if changed_keys:
            await self._publish_change(changed_keys)
            logger.info(f"[RuntimeConfig] 批量保存 {len(changed_keys)} 个配置: {changed_keys}")

    async def _publish_change(self, keys: list[str]):
        """发布配置变更到 Redis"""
        try:
            r = get_redis()
            message = json.dumps(
                {
                    "worker_id": Config.server.WORKER_ID,
                    "keys": keys,
                    "timestamp": time.time(),
                }
            )
            await r.publish(RUNTIME_CONFIG_CHANNEL, message)
        except Exception as e:
            logger.warning(f"[RuntimeConfig] Redis 发布失败: {e}")

    async def start_subscriber(self):
        """启动 Redis Pub/Sub 监听"""
        if self._running:
            return
        self._running = True
        self._subscriber_task = asyncio.create_task(self._subscribe_loop())
        logger.info("[RuntimeConfig] Pub/Sub 订阅已启动")

    async def stop_subscriber(self):
        """停止监听"""
        self._running = False
        if self._subscriber_task:
            self._subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscriber_task
            self._subscriber_task = None
        logger.info("[RuntimeConfig] Pub/Sub 订阅已停止")

    async def _subscribe_loop(self):
        """Redis Pub/Sub 监听循环

        使用 get_message(timeout=) 轮询而非阻塞式 listen()，
        以兼容全局连接池的 socket_timeout 设置。
        get_message 内部以 timeout 参数控制单次等待时长，
        超时返回 None 而非抛异常，避免 Pub/Sub 长连接被误判断开。
        """
        while self._running:
            pubsub = None
            try:
                r = get_redis()
                pubsub = r.pubsub()
                await pubsub.subscribe(RUNTIME_CONFIG_CHANNEL)
                while self._running:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                    if message is None:
                        continue
                    if message["type"] == "message":
                        await self._on_config_updated(message["data"])
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[RuntimeConfig] Pub/Sub 监听异常，5秒后重连: {e}")
                await asyncio.sleep(5)
            finally:
                if pubsub:
                    try:
                        await pubsub.unsubscribe(RUNTIME_CONFIG_CHANNEL)
                        await pubsub.close()
                    except Exception as exc:
                        logger.debug(f"[RuntimeConfig] Pub/Sub 清理时忽略异常: {exc}")

    async def _on_config_updated(self, data):
        """收到变更通知，从 DB 重新加载"""
        try:
            payload = json.loads(data) if isinstance(data, str) else data
            keys = payload.get("keys", [])
            if not keys:
                return
            configs = await get_all_runtime_configs()
            for key in keys:
                if key in configs and key in RUNTIME_KEYS:
                    config_row = configs[key]
                    typed_value = _deserialize_value(config_row.value, config_row.value_type)
                    _set_nested(
                        key,
                        typed_value,
                        source="db",
                        updated_at=config_row.updated_at,
                        updated_by=config_row.updated_by,
                    )
            logger.info(f"[RuntimeConfig] 热更新 {len(keys)} 个配置: {keys}")

            # Prompt 模板缓存重载
            prompt_keys = {"AI_USER_PROMPT", "AI_SYSTEM_PROMPT"}
            if prompt_keys & set(keys):
                from services.ai_prompts import reload_user_prompt_template

                reload_user_prompt_template()
                logger.info("[RuntimeConfig] Prompt 模板缓存已重载")

            openai_keys = {"OPENAI_API_KEY", "OPENAI_API_URL"}
            if openai_keys & set(keys):
                from services.ai_client import reset_openai_client

                reset_openai_client()
        except Exception as e:
            logger.warning(f"[RuntimeConfig] 处理配置变更通知失败: {e}")


# 模块级单例
runtime_config = RuntimeConfigManager()
