from fastapi import Security, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from core.config import Config
from core.logger import logger

security = HTTPBearer(auto_error=False)

async def verify_api_key(request: Request, auth: HTTPAuthorizationCredentials = Security(security)):
    """
    验证 API Key (Bearer Token)
    如果 Config.API_KEY 未配置，则跳过验证（兼容模式）
    """
    if not Config.API_KEY:
        return True
    
    if not auth or auth.credentials != Config.API_KEY:
        client_ip = request.client.host if request.client else 'unknown'
        
        # 尝试读取请求体 (由于 request.body() 是异步的，在 depends 里直接 await 可能影响性能，但这是异常分支，影响不大)
        try:
            body_bytes = await request.body()
            body_preview = body_bytes.decode('utf-8', errors='ignore')[:200]
        except Exception:
            body_preview = "无法读取"
            
        logger.warning(
            f"[Auth] 未授权的 API 访问尝试: IP={client_ip}, URL={request.url.path}, "
            f"Method={request.method}, Headers={dict(request.headers)}, Body Preview={body_preview}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return True
