import hashlib

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import Config
from core.logger import logger

security = HTTPBearer(auto_error=False)
_AUTH_DEPENDENCY = Security(security)


def _redact_headers(headers: dict) -> dict:
    redacted = {}
    for k, v in headers.items():
        lk = str(k).lower()
        if lk in {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}:
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return redacted


def _body_meta(body: bytes) -> dict:
    if not body:
        return {"size": 0, "sha256": None}
    digest = hashlib.sha256(body).hexdigest()
    return {"size": len(body), "sha256": digest}


async def verify_api_key(request: Request, auth: HTTPAuthorizationCredentials = _AUTH_DEPENDENCY):
    """
    验证 API Key (Bearer Token)
    如果 Config.security.API_KEY 未配置，则跳过验证（兼容模式）
    """
    if not Config.security.API_KEY:
        return True

    if not auth or auth.credentials != Config.security.API_KEY:
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
    auth: HTTPAuthorizationCredentials = _AUTH_DEPENDENCY,
):
    """
    验证 Admin 写操作权限。
    如果配置了 ADMIN_WRITE_KEY，则要求 Bearer token 必须匹配此 key；
    否则退回到普通 verify_api_key 行为（向后兼容）。
    """
    admin_write_key = Config.security.ADMIN_WRITE_KEY

    # 情况 1：配置了单独的 ADMIN_WRITE_KEY → 只校验它
    if admin_write_key:
        if not auth or auth.credentials != admin_write_key:
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

    # 情况 2：未配置 ADMIN_WRITE_KEY → 回退到普通 API_KEY 逻辑
    return await verify_api_key(request, auth)
