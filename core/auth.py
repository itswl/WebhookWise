from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.app_context import get_config_manager
from core.config import AppConfig
from core.logger import get_logger
from core.sensitive_data import redact_headers

logger = get_logger("auth")

security = HTTPBearer(auto_error=False)
_AUTH_DEPENDENCY = Security(security)
_CONFIG_DEPENDENCY = Depends(get_config_manager)


def _first_token(request: Request, auth: HTTPAuthorizationCredentials | None, *header_keys: str) -> str | None:
    if auth is not None:
        token = str(auth.credentials).strip() if auth.credentials else ""
        if token:
            return token

    if auth and auth.scheme:
        token = str(auth.credentials).strip() if auth.credentials else ""
        if token:
            return token

    header_value = request.headers.get("authorization")
    if header_value:
        token = header_value.strip()
        if token.lower().startswith("bearer "):
            return token[7:].strip()
        if token:
            return token

    for key in header_keys:
        candidate = request.headers.get(key)
        if candidate:
            token = str(candidate).strip()
            if token:
                return token

    query = getattr(request, "query_params", None)
    if query is not None:
        for key in (
            "admin_key",
            "admin-write-key",
            "admin_write_key",
            "api_key",
            "api-key",
            "token",
        ):
            candidate = query.get(key)
            if isinstance(candidate, str):
                token = candidate.strip()
                if token:
                    return token
    return None


def _body_meta(body: bytes) -> dict[str, object]:
    if not body:
        return {"size": 0, "sha256": None}
    digest = hashlib.sha256(body).hexdigest()
    return {"size": len(body), "sha256": digest}


def _matches_any_configured_token(credentials: str | None, *tokens: str) -> bool:
    if credentials is None:
        return False
    return any(token and hmac.compare_digest(credentials, token) for token in tokens)


async def verify_api_key(
    request: Request,
    auth: HTTPAuthorizationCredentials | None = _AUTH_DEPENDENCY,
    config: AppConfig = _CONFIG_DEPENDENCY,
) -> bool:
    """验证管理 API Bearer Token。"""
    api_key = config.security.API_KEY
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API_KEY is not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    credential = _first_token(request, auth, "x-api-key", "x-admin-key")
    if not credential or not _matches_any_configured_token(credential, api_key, config.security.ADMIN_WRITE_KEY):
        client_ip = request.client.host if request.client else "unknown"

        if logger.isEnabledFor(logging.WARNING):
            try:
                body_bytes = await request.body()
            except RuntimeError:
                body_bytes = b""

            logger.warning(
                "[Auth] 未授权的 API 访问尝试: IP=%s, URL=%s, Method=%s, Headers=%s, Body=%s",
                client_ip,
                request.url.path,
                request.method,
                redact_headers(dict(request.headers)),
                _body_meta(body_bytes),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return True


async def verify_admin_write(
    request: Request,
    auth: HTTPAuthorizationCredentials | None = _AUTH_DEPENDENCY,
    config: AppConfig = _CONFIG_DEPENDENCY,
) -> bool:
    """验证 Admin 写操作 Bearer Token。"""
    admin_write_key = config.security.ADMIN_WRITE_KEY
    api_key = config.security.API_KEY
    if not admin_write_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN_WRITE_KEY is not configured",
        )

    credential = _first_token(
        request, auth, "x-admin-write-key", "x-admin-key", "x-api-key"
    )

    if not credential:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "[Auth] Admin write operation requires permission: missing token, IP=%s, URL=%s",
            client_ip,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin write permission required. Missing ADMIN_WRITE_KEY.",
        )

    if _matches_any_configured_token(credential, admin_write_key) is False and _matches_any_configured_token(
        credential,
        api_key,
    ):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "[Auth] Admin write operation rejected with API key: IP=%s, URL=%s",
            client_ip,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin write token required. API key is insufficient for this endpoint.",
        )

    if not _matches_any_configured_token(credential, admin_write_key):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "[Auth] Admin 写操作权限不足: IP=%s, URL=%s",
            client_ip,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin write permission required",
        )
    return True
