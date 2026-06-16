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
        raise RuntimeError("DATABASE_URL is still the local default connection string; please configure a production database connection")
    if not config.security.API_KEY:
        raise RuntimeError("API_KEY is not configured; please set API_KEY")
    if not config.security.ADMIN_WRITE_KEY:
        raise RuntimeError("ADMIN_WRITE_KEY is not configured; please set ADMIN_WRITE_KEY")
    if env == "production" and looks_like_placeholder_secret(config.security.API_KEY):
        raise RuntimeError("API_KEY is still an example placeholder value; please replace it with a real random key")
    if env == "production" and looks_like_placeholder_secret(config.security.ADMIN_WRITE_KEY):
        raise RuntimeError("ADMIN_WRITE_KEY is still an example placeholder value; please replace it with a real random key")
    if env == "production":
        _warn_if_short_secret("API_KEY", config.security.API_KEY)
        _warn_if_short_secret("ADMIN_WRITE_KEY", config.security.ADMIN_WRITE_KEY)
    if env == "production" and not config.security.REQUIRE_WEBHOOK_AUTH:
        raise RuntimeError("Webhook authentication is not enabled in production. Please set REQUIRE_WEBHOOK_AUTH=true and WEBHOOK_SECRET")
    if env == "production" and config.security.REQUIRE_WEBHOOK_AUTH:
        webhook_secret = config.security.WEBHOOK_SECRET.strip()
        if not webhook_secret:
            raise RuntimeError("Webhook authentication is enabled but WEBHOOK_SECRET is empty; please set WEBHOOK_SECRET")
        if looks_like_placeholder_secret(config.security.WEBHOOK_SECRET):
            raise RuntimeError("WEBHOOK_SECRET is still an example placeholder value; please replace it with a real random key")
        _warn_if_short_secret("WEBHOOK_SECRET", webhook_secret)


def _warn_if_short_secret(name: str, value: str) -> None:
    """Warn (not fail) when a production secret is shorter than recommended.

    Kept non-fatal so an existing deployment with legacy short secrets is not
    blocked from starting; the log line is a persistent prompt to rotate.
    """
    if 0 < len(value.strip()) < _MIN_SECRET_LENGTH:
        logger.warning(
            "[Security] %s is only %d characters, below the recommended %d; consider rotating to a stronger random key",
            name,
            len(value.strip()),
            _MIN_SECRET_LENGTH,
        )
