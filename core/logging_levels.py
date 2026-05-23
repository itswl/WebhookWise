import logging

APP_LOGGER_NAMES = (
    "webhook_service",
    "config",
    "api",
    "adapters",
    "core",
    "db",
    "models",
    "services",
)

THIRD_PARTY_LOGGER_NAMES = (
    "asyncio",
    "asyncpg",
    "gunicorn",
    "gunicorn.access",
    "gunicorn.error",
    "httpcore",
    "httpx",
    "instructor",
    "openai",
    "redis",
    "sqlalchemy",
    "taskiq",
    "taskiq_redis",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)


def _resolve_level(value: str | int | None, default: int) -> int:
    if isinstance(value, int):
        return value
    if value:
        return getattr(logging, str(value).upper(), default)
    return default


def apply_log_levels(
    app_level: str | int | None = "INFO",
    third_party_level: str | int | None = "WARNING",
) -> None:
    """Apply project and dependency logger levels consistently."""
    app_level_no = _resolve_level(app_level, logging.INFO)
    third_party_level_no = _resolve_level(third_party_level, logging.WARNING)

    logging.getLogger().setLevel(third_party_level_no)
    for logger_name in THIRD_PARTY_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(third_party_level_no)

    for logger_name in APP_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(app_level_no)
