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
