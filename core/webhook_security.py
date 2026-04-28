import hmac
import time

from fastapi import HTTPException, Request

from api import InvalidSignatureError
from core.config import Config
from core.logger import logger
from core.redis_client import get_redis
from core.utils import verify_signature
from crud.webhook import get_client_ip


def extract_token(headers: dict) -> str:
    token = headers.get("token", "")
    if not token and headers.get("authorization", "").startswith("Token "):
        token = headers.get("authorization", "")[6:].strip()
    return token


def ensure_webhook_auth(headers: dict, raw_body: bytes) -> None:
    signature = headers.get("x-webhook-signature", "")
    token = extract_token(headers)

    if signature:
        if not Config.WEBHOOK_SECRET:
            raise InvalidSignatureError()
        if not verify_signature(raw_body, signature):
            raise InvalidSignatureError()
        return

    if Config.WEBHOOK_SECRET:
        if not token:
            raise InvalidSignatureError()
        if not hmac.compare_digest(token, Config.WEBHOOK_SECRET):
            raise InvalidSignatureError()


async def enforce_webhook_rate_limit(request: Request) -> str | None:
    if not Config.WEBHOOK_RATE_LIMIT_PER_MINUTE or Config.WEBHOOK_RATE_LIMIT_PER_MINUTE <= 0:
        return None

    client_ip = get_client_ip(request)
    redis = get_redis()
    window = int(time.time() // 60)
    key = f"rl:webhook:{client_ip}:{window}"
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, 70)
    if current > Config.WEBHOOK_RATE_LIMIT_PER_MINUTE:
        return client_ip
    return None


# ── FastAPI Depends ────────────────────────────────────────────────────────────


async def verify_webhook_auth_dep(request: Request):
    """FastAPI Depends：校验 webhook 认证"""
    raw_body = await request.body()
    headers = dict(request.headers)
    try:
        ensure_webhook_auth(headers, raw_body)
    except (InvalidSignatureError, Exception):
        raise HTTPException(status_code=401, detail="Unauthorized") from None


async def check_rate_limit_dep(request: Request):
    """FastAPI Depends：检查速率限制"""
    try:
        limited_ip = await enforce_webhook_rate_limit(request)
        if limited_ip:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"限流检查失败: {e}")
