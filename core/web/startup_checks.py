from core.config import AppConfig
from core.logger import get_logger

logger = get_logger("startup_checks")

_PLACEHOLDER_SECRETS = {"change-me", "changeme", "replace-me", "please-change", "please-change-me"}
# Recommended minimum secret length in production. 16 chars ~ 96 bits if random.
# Short secrets are warned about (not fatal) so an existing deployment with
# legacy short keys is not blocked from starting; rotate to >=16 when possible.
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
    if env == "production":
        _warn_if_short_secret("API_KEY", config.security.API_KEY)
        _warn_if_short_secret("ADMIN_WRITE_KEY", config.security.ADMIN_WRITE_KEY)
    if env == "production" and not config.security.REQUIRE_WEBHOOK_AUTH:
        raise RuntimeError("生产环境未开启 Webhook 鉴权。请设置 REQUIRE_WEBHOOK_AUTH=true 和 WEBHOOK_SECRET")
    if env == "production" and config.security.REQUIRE_WEBHOOK_AUTH:
        webhook_secret = config.security.WEBHOOK_SECRET.strip()
        if not webhook_secret:
            raise RuntimeError("已开启 Webhook 鉴权但 WEBHOOK_SECRET 为空，请设置 WEBHOOK_SECRET")
        if looks_like_placeholder_secret(config.security.WEBHOOK_SECRET):
            raise RuntimeError("WEBHOOK_SECRET 仍是示例占位值，请替换为真实随机密钥")
        _warn_if_short_secret("WEBHOOK_SECRET", webhook_secret)


def _warn_if_short_secret(name: str, value: str) -> None:
    """Warn (not fail) when a production secret is shorter than recommended.

    Kept non-fatal so an existing deployment with legacy short secrets is not
    blocked from starting; the log line is a persistent prompt to rotate.
    """
    if 0 < len(value.strip()) < _MIN_SECRET_LENGTH:
        logger.warning(
            "[Security] %s 长度仅 %d，低于推荐的 %d 字符，建议轮换为更强的随机密钥",
            name,
            len(value.strip()),
            _MIN_SECRET_LENGTH,
        )
