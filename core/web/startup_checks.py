from core.config import AppConfig

_PLACEHOLDER_SECRETS = {"change-me", "changeme", "replace-me", "please-change", "please-change-me"}
# Minimum secret length enforced in production. 16 chars ~ 96 bits if random.
_MIN_SECRET_LENGTH = 16
_DEFAULT_DATABASE_URL_MARKERS = (
    "://postgres:postgres@localhost",
    "://postgres:postgres@127.0.0.1",
    "://postgres:postgres@[::1]",
)


def looks_like_placeholder_secret(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _PLACEHOLDER_SECRETS or normalized.startswith(("please-change-", "replace-me-"))


def looks_like_default_database_url(value: str) -> bool:
    normalized = value.strip().lower()
    return any(marker in normalized for marker in _DEFAULT_DATABASE_URL_MARKERS)


def validate_startup_security(config: AppConfig, *, app_env: str | None = None) -> None:
    env = app_env or config.server.APP_ENV
    if env == "production" and looks_like_default_database_url(config.db.DATABASE_URL):
        raise RuntimeError("DATABASE_URL 仍是本地默认连接串，请配置生产数据库连接")
    if not config.security.API_KEY:
        raise RuntimeError("API_KEY 未配置，请设置 API_KEY")
    if not config.security.ADMIN_WRITE_KEY:
        raise RuntimeError("ADMIN_WRITE_KEY 未配置，请设置 ADMIN_WRITE_KEY")
    if env == "production" and looks_like_placeholder_secret(config.security.API_KEY):
        raise RuntimeError("API_KEY 仍是示例占位值，请替换为真实随机密钥")
    if env == "production" and looks_like_placeholder_secret(config.security.ADMIN_WRITE_KEY):
        raise RuntimeError("ADMIN_WRITE_KEY 仍是示例占位值，请替换为真实随机密钥")
    if env == "production" and len(config.security.API_KEY.strip()) < _MIN_SECRET_LENGTH:
        raise RuntimeError(f"API_KEY 太短，生产环境至少需要 {_MIN_SECRET_LENGTH} 个字符")
    if env == "production" and len(config.security.ADMIN_WRITE_KEY.strip()) < _MIN_SECRET_LENGTH:
        raise RuntimeError(f"ADMIN_WRITE_KEY 太短，生产环境至少需要 {_MIN_SECRET_LENGTH} 个字符")
    if env == "production" and not config.security.REQUIRE_WEBHOOK_AUTH:
        raise RuntimeError("生产环境未开启 Webhook 鉴权。请设置 REQUIRE_WEBHOOK_AUTH=true 和 WEBHOOK_SECRET")
    if env == "production" and config.security.REQUIRE_WEBHOOK_AUTH:
        webhook_secret = config.security.WEBHOOK_SECRET.strip()
        if not webhook_secret:
            raise RuntimeError("已开启 Webhook 鉴权但 WEBHOOK_SECRET 为空，请设置 WEBHOOK_SECRET")
        if looks_like_placeholder_secret(config.security.WEBHOOK_SECRET):
            raise RuntimeError("WEBHOOK_SECRET 仍是示例占位值，请替换为真实随机密钥")
        if len(webhook_secret) < _MIN_SECRET_LENGTH:
            raise RuntimeError(f"WEBHOOK_SECRET 太短，生产环境至少需要 {_MIN_SECRET_LENGTH} 个字符")
