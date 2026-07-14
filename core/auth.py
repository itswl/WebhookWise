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


def _token_candidates(request: Request, auth: HTTPAuthorizationCredentials | None, *header_keys: str) -> list[str]:
    candidates: list[str] = []

    def add_token(value: object) -> None:
        token = str(value).strip() if value else ""
        if token and token not in candidates:
            candidates.append(token)

    if auth is not None:
        add_token(auth.credentials)

    header_value = request.headers.get("authorization")
    if header_value:
        token = header_value.strip()
        if token.lower().startswith("bearer "):
            add_token(token[7:])
        else:
            add_token(token)

    for key in header_keys:
        add_token(request.headers.get(key))

    return candidates


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
    """Verify the management API Bearer token."""
    api_key = config.security.API_KEY
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API_KEY is not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    credentials = _token_candidates(request, auth, "x-api-key", "x-admin-key", "x-admin-write-key")
    if not any(_matches_any_configured_token(credential, api_key) for credential in credentials):
        client_ip = request.client.host if request.client else "unknown"

        if logger.isEnabledFor(logging.WARNING):
            try:
                body_bytes = await request.body()
            except RuntimeError:
                body_bytes = b""

            logger.warning(
                "[Auth] Unauthorized API access attempt: IP=%s, URL=%s, Method=%s, Headers=%s, Body=%s",
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
    """Verify the Admin write-operation Bearer token."""
    admin_write_key = config.security.ADMIN_WRITE_KEY
    api_key = config.security.API_KEY
    if not admin_write_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN_WRITE_KEY is not configured",
        )

    credentials = _token_candidates(request, auth, "x-admin-write-key", "x-admin-key", "x-api-key")

    if not credentials:
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

    if any(_matches_any_configured_token(credential, admin_write_key) for credential in credentials):
        return True

    if any(
        _matches_any_configured_token(
            credential,
            api_key,
        )
        for credential in credentials
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

    client_ip = request.client.host if request.client else "unknown"
    logger.warning(
        "[Auth] Insufficient permission for Admin write operation: IP=%s, URL=%s",
        client_ip,
        request.url.path,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin write permission required",
    )
