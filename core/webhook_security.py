import hmac
import time

from fastapi import HTTPException, Request

from api import InvalidSignatureError
from core.config import Config
from core.logger import logger
from core.redis_client import get_redis
from core.utils import verify_signature
from services.webhook_orchestrator import get_client_ip

_INCR_EXPIRE_IF_FIRST_LUA = """
local c = redis.call("incr", KEYS[1])
if c == 1 then
    redis.call("expire", KEYS[1], tonumber(ARGV[1]))
end
return c
"""


def extract_token(headers: dict) -> str:
    token = headers.get("token", "")
    if not token and headers.get("authorization", "").startswith("Token "):
        token = headers.get("authorization", "")[6:].strip()
    return token


def ensure_webhook_auth(headers: dict, raw_body: bytes) -> None:
    signature = headers.get("x-webhook-signature", "")
    token = extract_token(headers)

    if signature:
        if not Config.security.WEBHOOK_SECRET:
            raise InvalidSignatureError()
        if not verify_signature(raw_body, signature):
            raise InvalidSignatureError()
        return

    if Config.security.WEBHOOK_SECRET:
        if not token:
            raise InvalidSignatureError()
        if not hmac.compare_digest(token, Config.security.WEBHOOK_SECRET):
            raise InvalidSignatureError()


async def enforce_webhook_rate_limit(request: Request) -> str | None:
    if not Config.security.WEBHOOK_RATE_LIMIT_PER_MINUTE or Config.security.WEBHOOK_RATE_LIMIT_PER_MINUTE <= 0:
        return None

    client_ip = get_client_ip(request)
    redis = get_redis()
    window = int(time.time() // 60)
    key = f"rl:webhook:{client_ip}:{window}"
    current = int(await redis.eval(_INCR_EXPIRE_IF_FIRST_LUA, 1, key, 70))
    if current > Config.security.WEBHOOK_RATE_LIMIT_PER_MINUTE:
        return client_ip
    return None


# ── FastAPI Depends ────────────────────────────────────────────────────────────


async def verify_webhook_auth_dep(request: Request):
    """FastAPI Depends：校验 webhook 认证（含 Content-Length 前置 DoS 防御）"""
    # 1. Content-Length 前置检查（在读取 body 之前拦截超大请求）
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
            if length > Config.security.MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request body too large: {length} bytes (max {Config.security.MAX_WEBHOOK_BODY_BYTES})",
                )
        except ValueError:
            logger.debug("无效的 Content-Length 头: %s", content_length)

    # 2. 读取 body 并验证签名
    raw_body = await request.body()
    headers = dict(request.headers)
    try:
        ensure_webhook_auth(headers, raw_body)
    except InvalidSignatureError:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    except ValueError as e:
        logger.warning("Webhook 签名验证参数异常: %s", e)
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    except Exception as e:
        logger.error("Webhook 认证内部错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from None


async def check_rate_limit_dep(request: Request):
    """FastAPI Depends：检查速率限制"""
    try:
        limited_ip = await enforce_webhook_rate_limit(request)
        if limited_ip:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("限流检查异常（降级放行）: %s", e, exc_info=True)
