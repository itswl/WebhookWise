from fastapi import Security, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from core.config import Config
from core.logger import logger

security = HTTPBearer(auto_error=False)

async def verify_api_key(auth: HTTPAuthorizationCredentials = Security(security)):
    """
    验证 API Key (Bearer Token)
    如果 Config.API_KEY 未配置，则跳过验证（兼容模式）
    """
    if not Config.API_KEY:
        return True
    
    if not auth or auth.credentials != Config.API_KEY:
        logger.warning(f"[Auth] 未授权的 API 访问尝试")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return True
