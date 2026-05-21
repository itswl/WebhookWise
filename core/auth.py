import hashlib
import hmac

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import UnifiedConfigManager
from core.dependencies import get_config_manager
from core.logger import logger

security = HTTPBearer(auto_error=False)
_AUTH_DEPENDENCY = Security(security)
_CONFIG_DEPENDENCY = Depends(get_config_manager)


def _redact_headers(headers: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for k, v in headers.items():
        lk = str(k).lower()
        if lk in {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}:
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return redacted


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
    config: UnifiedConfigManager = _CONFIG_DEPENDENCY,
) -> bool:
    """验证管理 API Bearer Token。"""
    api_key = config.security.API_KEY
    if not api_key:
        if config.security.ALLOW_UNAUTHENTICATED_ADMIN:
            return True
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API_KEY is not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not auth or not _matches_any_configured_token(auth.credentials, api_key, config.security.ADMIN_WRITE_KEY):
        client_ip = request.client.host if request.client else "unknown"

        try:
            body_bytes = await request.body()
        except Exception:
            body_bytes = b""

        logger.warning(
            f"[Auth] 未授权的 API 访问尝试: IP={client_ip}, URL={request.url.path}, "
            f"Method={request.method}, Headers={_redact_headers(dict(request.headers))}, Body={_body_meta(body_bytes)}"
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
    config: UnifiedConfigManager = _CONFIG_DEPENDENCY,
) -> bool:
    """验证 Admin 写操作 Bearer Token。"""
    admin_write_key = config.security.ADMIN_WRITE_KEY
    if not admin_write_key:
        if config.security.ALLOW_UNAUTHENTICATED_ADMIN:
            return True
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN_WRITE_KEY is not configured",
        )

    if not auth or not _matches_any_configured_token(auth.credentials, admin_write_key):
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
