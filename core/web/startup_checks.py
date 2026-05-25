from core.config import UnifiedConfigManager

_PLACEHOLDER_SECRETS = {"change-me", "changeme", "replace-me", "please-change", "please-change-me"}


def looks_like_placeholder_secret(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _PLACEHOLDER_SECRETS or normalized.startswith("please-change-")


def validate_startup_security(config: UnifiedConfigManager, *, app_env: str | None = None) -> None:
    env = app_env or config.server.APP_ENV
    if not config.security.API_KEY:
        raise RuntimeError("API_KEY 未配置，请设置 API_KEY")
    if not config.security.ADMIN_WRITE_KEY:
        raise RuntimeError("ADMIN_WRITE_KEY 未配置，请设置 ADMIN_WRITE_KEY")
    if env == "production" and looks_like_placeholder_secret(config.security.API_KEY):
        raise RuntimeError("API_KEY 仍是示例占位值，请替换为真实随机密钥")
    if env == "production" and looks_like_placeholder_secret(config.security.ADMIN_WRITE_KEY):
        raise RuntimeError("ADMIN_WRITE_KEY 仍是示例占位值，请替换为真实随机密钥")
    if env == "production" and not config.security.REQUIRE_WEBHOOK_AUTH:
        raise RuntimeError("生产环境未开启 Webhook 鉴权。请设置 REQUIRE_WEBHOOK_AUTH=true 和 WEBHOOK_SECRET")
    if (
        env == "production"
        and config.security.REQUIRE_WEBHOOK_AUTH
        and looks_like_placeholder_secret(config.security.WEBHOOK_SECRET)
    ):
        raise RuntimeError("WEBHOOK_SECRET 仍是示例占位值，请替换为真实随机密钥")
