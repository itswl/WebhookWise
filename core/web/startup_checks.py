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
        raise RuntimeError(
            "DATABASE_URL is still the local default connection string; please configure a production database connection"
        )
    if not config.security.API_KEY:
        raise RuntimeError("API_KEY is not configured; please set API_KEY")
    if not config.security.ADMIN_WRITE_KEY:
        raise RuntimeError("ADMIN_WRITE_KEY is not configured; please set ADMIN_WRITE_KEY")
    if env == "production" and looks_like_placeholder_secret(config.security.API_KEY):
        raise RuntimeError("API_KEY is still an example placeholder value; please replace it with a real random key")
    if env == "production" and looks_like_placeholder_secret(config.security.ADMIN_WRITE_KEY):
        raise RuntimeError(
            "ADMIN_WRITE_KEY is still an example placeholder value; please replace it with a real random key"
        )
    if env == "production":
        _warn_if_short_secret("API_KEY", config.security.API_KEY)
        _warn_if_short_secret("ADMIN_WRITE_KEY", config.security.ADMIN_WRITE_KEY)
        change_ingest_token = config.security.CHANGE_INGEST_TOKEN.strip()
        if change_ingest_token and looks_like_placeholder_secret(change_ingest_token):
            raise RuntimeError(
                "CHANGE_INGEST_TOKEN is still an example placeholder value; please replace it with a real random key"
            )
        if change_ingest_token:
            _warn_if_short_secret("CHANGE_INGEST_TOKEN", change_ingest_token)
        if config.notifications.FEISHU_CARD_ACTIONS_ENABLED:
            required_feishu_values = {
                "FEISHU_APP_ID": config.notifications.FEISHU_APP_ID,
                "FEISHU_APP_SECRET": config.notifications.FEISHU_APP_SECRET,
                "FEISHU_INCIDENT_CHAT_ID": config.notifications.FEISHU_INCIDENT_CHAT_ID,
                "FEISHU_CARD_VERIFICATION_TOKEN": config.security.FEISHU_CARD_VERIFICATION_TOKEN,
                "FEISHU_CARD_ACTION_SECRET": config.security.FEISHU_CARD_ACTION_SECRET,
            }
            missing = [name for name, value in required_feishu_values.items() if not value.strip()]
            if missing:
                raise RuntimeError(
                    "Feishu card actions are enabled but required settings are missing: " + ", ".join(missing)
                )
            for name in (
                "FEISHU_APP_SECRET",
                "FEISHU_CARD_VERIFICATION_TOKEN",
                "FEISHU_CARD_ACTION_SECRET",
            ):
                value = required_feishu_values[name].strip()
                if looks_like_placeholder_secret(value):
                    raise RuntimeError(f"{name} is still an example placeholder value")
                _warn_if_short_secret(name, value)
            if not config.security.FEISHU_ALLOWED_TENANT_KEYS.strip():
                logger.warning(
                    "FEISHU_ALLOWED_TENANT_KEYS is empty: any tenant passing token verification may drive "
                    "incident card actions. Pin your tenant key unless cross-tenant access is intended."
                )
            if not config.security.FEISHU_ALLOWED_OPERATOR_OPEN_IDS.strip():
                logger.warning(
                    "FEISHU_ALLOWED_OPERATOR_OPEN_IDS is empty: any chat member may acknowledge/resolve "
                    "incidents from cards. Set an operator allowlist to restrict who can act."
                )
    if env == "production" and not config.security.REQUIRE_WEBHOOK_AUTH:
        raise RuntimeError(
            "Webhook authentication is not enabled in production. Please set REQUIRE_WEBHOOK_AUTH=true and WEBHOOK_SECRET"
        )
    if env == "production" and config.security.REQUIRE_WEBHOOK_AUTH:
        webhook_secret = config.security.WEBHOOK_SECRET.strip()
        if not webhook_secret:
            raise RuntimeError(
                "Webhook authentication is enabled but WEBHOOK_SECRET is empty; please set WEBHOOK_SECRET"
            )
        if looks_like_placeholder_secret(config.security.WEBHOOK_SECRET):
            raise RuntimeError(
                "WEBHOOK_SECRET is still an example placeholder value; please replace it with a real random key"
            )
        _warn_if_short_secret("WEBHOOK_SECRET", webhook_secret)
        previous_secret = config.security.WEBHOOK_SECRET_PREVIOUS.strip()
        if previous_secret:
            if looks_like_placeholder_secret(previous_secret):
                raise RuntimeError(
                    "WEBHOOK_SECRET_PREVIOUS is an example placeholder value; "
                    "set it to the real outgoing secret or remove it"
                )
            _warn_if_short_secret("WEBHOOK_SECRET_PREVIOUS", previous_secret)
            logger.warning(
                "Webhook secret rotation overlap is active: WEBHOOK_SECRET_PREVIOUS is still accepted. "
                "Remove it once the webhook_auth allowed_previous_secret counter stays at zero."
            )
    if env == "production":
        _warn_on_open_ingress_defaults(config)


def _warn_on_open_ingress_defaults(config: AppConfig) -> None:
    """Warn (not fail) about ship-disabled protections on the public surface.

    These stay non-fatal because both defaults are documented and an existing
    deployment must keep starting — but out of the box the UNAUTHENTICATED
    ingress has no per-IP ceiling while the authenticated admin API has one,
    and a captured signed request replays forever. The log line is the nudge.
    """
    security = config.security
    if (
        security.WEBHOOK_RATE_LIMIT_PER_MINUTE == 0
        and security.WEBHOOK_RATE_LIMIT_BURST == 0
        and security.WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE == 0
    ):
        logger.warning(
            "[Security] Webhook ingress rate limiting is disabled (all WEBHOOK_RATE_LIMIT_* are 0): "
            "the public ingress has no per-IP ceiling. Set WEBHOOK_RATE_LIMIT_PER_MINUTE to cap it."
        )
    if not security.WEBHOOK_REPLAY_PROTECTION_ENABLED:
        logger.warning(
            "[Security] Webhook replay protection is disabled: a captured signed request can be replayed "
            "indefinitely. Set WEBHOOK_REPLAY_PROTECTION_ENABLED=true once senders emit x-webhook-timestamp."
        )
    if config.kb.KB_ENABLED and not config.kb.KB_EMBEDDING_API_URL.strip():
        logger.warning(
            "[KB] KB_ENABLED is on but KB_EMBEDDING_API_URL is empty: retrieval runs on the deterministic "
            "placeholder embedding and returns near-noise. Configure an embeddings endpoint for real recall."
        )


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
