from core.config import UnifiedConfigManager

_PLACEHOLDER_SECRETS = {"change-me", "changeme", "replace-me", "please-change", "please-change-me"}


def looks_like_placeholder_secret(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _PLACEHOLDER_SECRETS or normalized.startswith("please-change-")


def validate_startup_security(config: UnifiedConfigManager, *, app_env: str | None = None) -> None:
    env = app_env or config.server.APP_ENV
    if not config.security.API_KEY and not (config.server.DEBUG or config.security.ALLOW_UNAUTHENTICATED_ADMIN):
        raise RuntimeError(
            "API_KEY 未配置且未允许公开管理接口，请设置 API_KEY 或在本地启用 ALLOW_UNAUTHENTICATED_ADMIN=true"
        )
    if not config.security.ADMIN_WRITE_KEY and not (config.server.DEBUG or config.security.ALLOW_UNAUTHENTICATED_ADMIN):
        raise RuntimeError(
            "ADMIN_WRITE_KEY 未配置且未允许公开管理接口，请设置 ADMIN_WRITE_KEY "
            "或在本地启用 ALLOW_UNAUTHENTICATED_ADMIN=true"
        )
    if env == "production" and looks_like_placeholder_secret(config.security.API_KEY):
        raise RuntimeError("API_KEY 仍是示例占位值，请替换为真实随机密钥")
    if env == "production" and looks_like_placeholder_secret(config.security.ADMIN_WRITE_KEY):
        raise RuntimeError("ADMIN_WRITE_KEY 仍是示例占位值，请替换为真实随机密钥")
    if (
        env == "production"
        and not config.security.REQUIRE_WEBHOOK_AUTH
        and not config.security.ALLOW_UNAUTHENTICATED_WEBHOOK
    ):
        raise RuntimeError(
            "生产环境未开启 Webhook 鉴权。请设置 REQUIRE_WEBHOOK_AUTH=true 和 WEBHOOK_SECRET，"
            "或显式设置 ALLOW_UNAUTHENTICATED_WEBHOOK=true 承担公开接收风险"
        )
    if (
        env == "production"
        and config.security.REQUIRE_WEBHOOK_AUTH
        and looks_like_placeholder_secret(config.security.WEBHOOK_SECRET)
    ):
        raise RuntimeError("WEBHOOK_SECRET 仍是示例占位值，请替换为真实随机密钥")
