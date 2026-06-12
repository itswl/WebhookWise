from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response
from redis.exceptions import RedisError

from core.app_context import get_config_manager
from core.config import AppConfig, SecurityConfig
from core.logger import get_logger
from core.observability.metrics import REDIS_UNAVAILABLE_TOTAL, SECURITY_CHECKS_TOTAL
from core.redis_client import redis_eval_int_list
from core.redis_health import rate_limit_burst, rate_limit_global, rate_limit_sustained
from core.redis_lua import MULTI_TIER_RATE_LIMIT as _MULTI_TIER_LUA
from core.request_ip import get_client_ip

logger = get_logger("webhook_security")

_BURST_WINDOW_SECONDS = 10
_SUSTAINED_WINDOW_SECONDS = 60
_CONFIG_DEPENDENCY = Depends(get_config_manager)


class InvalidSignatureError(Exception):
    """签名校验失败。"""


def verify_signature(payload: bytes, signature: str, secret: str | None = None) -> bool:
    """验证 webhook 签名"""
    if secret is None:
        secret = get_config_manager().security.WEBHOOK_SECRET

    if not secret:
        return False

    expected_signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    result = hmac.compare_digest(expected_signature, signature)
    if not result:
        logger.warning("[Auth] 签名比对不匹配")
    else:
        logger.debug("[Auth] 签名验证通过")
    return result


def verify_timestamped_signature(timestamp: str, payload: bytes, signature: str, secret: str) -> bool:
    """Verify an HMAC computed over ``timestamp.body`` (replay-resistant form).

    Binding the timestamp into the signed material prevents an attacker from
    replaying a captured (timestamp, signature, body) tuple with a fresh
    timestamp, and lets the server reject stale timestamps.
    """
    if not secret or not timestamp:
        return False
    signed = timestamp.encode("utf-8") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class ReplayError(Exception):
    """Webhook replay protection rejected the request (stale or reused)."""


async def enforce_replay_protection(
    headers: Mapping[str, str], raw_body: bytes, *, security: SecurityConfig
) -> None:
    """Reject stale or replayed signed webhooks when replay protection is on.

    Backward compatible: only enforced when WEBHOOK_REPLAY_PROTECTION_ENABLED is
    true AND the request carries a signature. Requires the sender to send
    ``x-webhook-timestamp`` and to sign ``"<timestamp>.<body>"``.
    """
    signature = headers.get("x-webhook-signature", "")
    if not signature:
        # Token-auth path; nothing to replay-protect here.
        return

    timestamp = headers.get("x-webhook-timestamp", "").strip()
    if not timestamp:
        raise ReplayError("missing x-webhook-timestamp")

    try:
        ts_value = int(timestamp)
    except ValueError as exc:
        raise ReplayError("invalid x-webhook-timestamp") from exc

    max_skew = max(1, int(security.WEBHOOK_REPLAY_MAX_SKEW_SECONDS))
    now = int(time.time())
    if abs(now - ts_value) > max_skew:
        raise ReplayError("timestamp outside allowed skew window")

    if not verify_timestamped_signature(timestamp, raw_body, signature, security.WEBHOOK_SECRET):
        raise ReplayError("timestamped signature mismatch")

    # One-time use: the first request with this signature wins; later replays
    # (within the skew window) fail to claim the nonce. Fail closed on Redis
    # error to preserve the anti-replay guarantee.
    from core.redis_client import redis_set_nx_ex
    from core.redis_health import webhook_replay_nonce

    try:
        claimed = await redis_set_nx_ex(webhook_replay_nonce(signature), "1", max_skew * 2)
    except (RedisError, RuntimeError, TypeError, ValueError) as exc:
        raise ReplayError("replay nonce store unavailable") from exc
    if not claimed:
        raise ReplayError("signature already used (replay)")


def extract_token(headers: Mapping[str, str]) -> str:
    token = headers.get("token", "")
    if not token and headers.get("authorization", "").startswith("Token "):
        token = headers.get("authorization", "")[6:].strip()
    return token


def ensure_webhook_auth(headers: Mapping[str, str], raw_body: bytes, *, secret: str | None = None) -> None:
    signature = headers.get("x-webhook-signature", "")
    token = extract_token(headers)
    resolved_secret = get_config_manager().security.WEBHOOK_SECRET if secret is None else secret

    if signature:
        if not resolved_secret:
            raise InvalidSignatureError()
        if not verify_signature(raw_body, signature, resolved_secret):
            raise InvalidSignatureError()
        return

    if resolved_secret:
        if not token:
            raise InvalidSignatureError()
        if not hmac.compare_digest(token, resolved_secret):
            raise InvalidSignatureError()


@dataclass
class _TierResult:
    allowed: bool
    remaining: int
    limit: int
    reset_at: float


async def enforce_webhook_rate_limit(
    request: Request, *, security_config: SecurityConfig | None = None
) -> tuple[str | None, _TierResult | None]:
    sec = security_config or get_config_manager().security
    per_minute = sec.WEBHOOK_RATE_LIMIT_PER_MINUTE
    burst = sec.WEBHOOK_RATE_LIMIT_BURST
    global_per_minute = sec.WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE

    if per_minute <= 0 and burst <= 0 and global_per_minute <= 0:
        return None, None

    client_ip = get_client_ip(request)
    now = time.time()

    tiers: list[tuple[str, int, int]] = []
    if burst > 0:
        tiers.append((rate_limit_burst(client_ip), _BURST_WINDOW_SECONDS, burst))
    if per_minute > 0:
        tiers.append((rate_limit_sustained(client_ip), _SUSTAINED_WINDOW_SECONDS, per_minute))
    if global_per_minute > 0:
        tiers.append((rate_limit_global(), _SUSTAINED_WINDOW_SECONDS, global_per_minute))

    # Evaluate all tiers in a single Redis round-trip. The script checks every
    # tier before incrementing any, so an over-limit later tier no longer leaves
    # earlier tiers spuriously incremented.
    keys = [t[0] for t in tiers]
    argv: list[int | float] = [now]
    for _prefix, window, limit in tiers:
        argv.extend((window, limit))
    result = await redis_eval_int_list(_MULTI_TIER_LUA, len(keys), *keys, *argv)
    if not result:
        raise RuntimeError("rate limit script returned no result")

    failed_index = result[0]
    if failed_index > 0:
        _prefix, window, limit = tiers[failed_index - 1]
        reset_at = (math.floor(now / window) + 1) * window
        return client_ip, _TierResult(allowed=False, remaining=0, limit=limit, reset_at=reset_at)

    tightest: _TierResult | None = None
    remainings = result[1:]
    for idx, (_prefix, window, limit) in enumerate(tiers):
        remaining = remainings[idx] if idx < len(remainings) else 0
        reset_at = (math.floor(now / window) + 1) * window
        res = _TierResult(allowed=True, remaining=max(remaining, 0), limit=limit, reset_at=reset_at)
        if tightest is None or res.remaining < tightest.remaining:
            tightest = res
    return None, tightest


# ── FastAPI Depends ────────────────────────────────────────────────────────────


async def verify_webhook_auth_dep(
    request: Request,
    config: AppConfig = _CONFIG_DEPENDENCY,
) -> None:
    """FastAPI Depends：校验 webhook 认证（含 Content-Length 前置 DoS 防御）"""
    # 1. Content-Length 前置检查（在读取 body 之前拦截超大请求）
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
            max_body_bytes = config.security.MAX_WEBHOOK_BODY_BYTES
            if max_body_bytes and length > max_body_bytes:
                SECURITY_CHECKS_TOTAL.labels("body_size", "rejected").inc()
                raise HTTPException(
                    status_code=413,
                    detail=f"Request body too large: {length} bytes (max {max_body_bytes})",
                )
        except ValueError:
            logger.debug("无效的 Content-Length 头: %s", content_length)

    if not config.security.REQUIRE_WEBHOOK_AUTH:
        SECURITY_CHECKS_TOTAL.labels("webhook_auth", "disabled").inc()
        return

    if not config.security.WEBHOOK_SECRET:
        logger.warning("Webhook 鉴权已启用但 WEBHOOK_SECRET 为空")
        SECURITY_CHECKS_TOTAL.labels("webhook_auth", "misconfigured").inc()
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 2. 读取 body 并验证签名
    raw_body = await request.body()
    request.state.raw_body = raw_body
    headers: dict[str, str] = dict(request.headers)
    try:
        ensure_webhook_auth(headers, raw_body, secret=config.security.WEBHOOK_SECRET)
    except InvalidSignatureError:
        SECURITY_CHECKS_TOTAL.labels("webhook_auth", "rejected").inc()
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("Webhook 签名验证参数异常: %s", e)
        SECURITY_CHECKS_TOTAL.labels("webhook_auth", "invalid").inc()
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    except RuntimeError:
        logger.exception("Webhook 认证内部错误")
        SECURITY_CHECKS_TOTAL.labels("webhook_auth", "error").inc()
        raise HTTPException(status_code=500, detail="Internal server error") from None

    if config.security.WEBHOOK_REPLAY_PROTECTION_ENABLED:
        try:
            await enforce_replay_protection(headers, raw_body, security=config.security)
        except ReplayError as e:
            logger.warning("Webhook 重放保护拒绝请求: %s", e)
            SECURITY_CHECKS_TOTAL.labels("webhook_replay", "rejected").inc()
            raise HTTPException(status_code=401, detail="Unauthorized") from None

    SECURITY_CHECKS_TOTAL.labels("webhook_auth", "allowed").inc()


async def check_rate_limit_dep(
    request: Request,
    response: Response,
    config: AppConfig = _CONFIG_DEPENDENCY,
) -> None:
    """FastAPI Depends：检查速率限制（滑动窗口，三级限流）"""
    try:
        from core.redis_health import ensure_redis_available

        if not await ensure_redis_available("webhook_security:rate_limit"):
            if config.security.RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR:
                REDIS_UNAVAILABLE_TOTAL.labels("rate_limit", "allowed").inc()
                SECURITY_CHECKS_TOTAL.labels("rate_limit", "redis_unavailable_allowed").inc()
                return
            REDIS_UNAVAILABLE_TOTAL.labels("rate_limit", "rejected").inc()
            SECURITY_CHECKS_TOTAL.labels("rate_limit", "redis_unavailable_rejected").inc()
            raise HTTPException(status_code=503, detail="Rate limit backend unavailable")

        limited_ip, tier = await enforce_webhook_rate_limit(request, security_config=config.security)
        if tier:
            response.headers["X-RateLimit-Limit"] = str(tier.limit)
            response.headers["X-RateLimit-Remaining"] = str(tier.remaining)
            response.headers["X-RateLimit-Reset"] = str(int(tier.reset_at))
        if limited_ip:
            from core.observability.metrics import WEBHOOK_RECEIVED_TOTAL, sanitize_source

            src = sanitize_source(request.path_params.get("source", request.query_params.get("source", "unknown")))
            WEBHOOK_RECEIVED_TOTAL.labels(source=src, status="rate_limited").inc()
            SECURITY_CHECKS_TOTAL.labels("rate_limit", "rejected").inc()
            retry_after = int(tier.reset_at - time.time()) if tier else 60
            response.headers["Retry-After"] = str(max(retry_after, 1))
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        SECURITY_CHECKS_TOTAL.labels("rate_limit", "allowed" if tier else "disabled").inc()
    except HTTPException:
        raise
    except (RuntimeError, TypeError, ValueError, RedisError) as e:
        from core.redis_health import mark_redis_failure

        mark_redis_failure("webhook_security:rate_limit", e)
        logger.error("限流检查异常（降级放行）: %s", e, exc_info=True)
        if config.security.RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR:
            REDIS_UNAVAILABLE_TOTAL.labels("rate_limit", "allowed").inc()
            SECURITY_CHECKS_TOTAL.labels("rate_limit", "error_allowed").inc()
            return
        REDIS_UNAVAILABLE_TOTAL.labels("rate_limit", "rejected").inc()
        SECURITY_CHECKS_TOTAL.labels("rate_limit", "error_rejected").inc()
        raise HTTPException(status_code=503, detail="Rate limit backend unavailable") from None
