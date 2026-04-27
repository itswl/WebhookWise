import hmac
import time
from typing import Optional

from fastapi import Request

from core.config import Config
from core.redis_client import get_redis
from core.routes import InvalidSignatureError
from core.utils import get_client_ip, verify_signature


def extract_token(headers: dict) -> str:
    token = headers.get('token', '')
    if not token and headers.get('authorization', '').startswith('Token '):
        token = headers.get('authorization', '')[6:].strip()
    return token


def ensure_webhook_auth(headers: dict, raw_body: bytes) -> None:
    signature = headers.get('x-webhook-signature', '')
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


def enforce_webhook_rate_limit(request: Request) -> str | None:
    if not Config.WEBHOOK_RATE_LIMIT_PER_MINUTE or Config.WEBHOOK_RATE_LIMIT_PER_MINUTE <= 0:
        return None

    client_ip = get_client_ip(request)
    redis = get_redis()
    window = int(time.time() // 60)
    key = f"rl:webhook:{client_ip}:{window}"
    current = redis.incr(key)
    if current == 1:
        redis.expire(key, 70)
    if current > Config.WEBHOOK_RATE_LIMIT_PER_MINUTE:
        return client_ip
    return None

