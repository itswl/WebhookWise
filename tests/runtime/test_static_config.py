import re
from pathlib import Path

import pytest

from tests.helpers.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT


def _env_keys(path: Path) -> set[str]:
    return set(re.findall(r"^(?:#\s*)?([A-Z][A-Z0-9_]+)=", path.read_text(), re.MULTILINE))


def test_config_keys_are_derived_from_config_models() -> None:
    from core.config.manager import get_config_keys

    keys = get_config_keys()
    assert keys["AI_ERROR_NOTIFICATION_COOLDOWN_SECONDS"] == {
        "type": "int",
        "sub": "notifications",
    }
    assert keys["WEBHOOK_MQ_QUEUE"] == {"type": "str", "sub": "mq"}
    assert keys["BACKGROUND_SCAN_INTERVAL_SECONDS"] == {"type": "int", "sub": "tasks"}
    assert keys["CIRCUIT_BREAKER_FEISHU_THRESHOLD"] == {
        "type": "int",
        "sub": "circuit_breaker",
    }
    assert keys["DATA_RETENTION_DAYS_DEFAULT"] == {"type": "int", "sub": "maintenance"}
    assert keys["DATABASE_URL"] == {"type": "str", "sub": "db"}
    assert keys["OPENAI_API_KEY"] == {"type": "str", "sub": "ai"}


def test_full_env_example_covers_config_model_keys() -> None:
    from core.config.manager import get_config_keys

    env_keys = _env_keys(ROOT / ".env.example.all")

    assert sorted(set(get_config_keys()) - env_keys) == []


def test_full_env_example_covers_direct_environment_reads() -> None:
    env_keys = _env_keys(ROOT / ".env.example.all")
    source_paths = [
        ROOT / "scripts/run_migrations.py",
        ROOT / "scripts/healthcheck.py",
        ROOT / "core/observability/profiling.py",
        ROOT / "scripts/observability/query_lib.py",
        ROOT / "core/observability/exporters.py",
        ROOT / "core/observability/resource.py",
        ROOT / "core/observability/tracing.py",
        ROOT / "core/observability/logging.py",
        ROOT / "core/observability/metrics_base.py",
        ROOT / "core/observability/metrics.py",
    ]
    direct_env_keys: set[str] = set()
    for path in source_paths:
        direct_env_keys.update(
            re.findall(
                r"(?:os\.getenv|env_flag|env_int|env_float)\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']",
                path.read_text(),
            )
        )

    # Signal-specific OTLP endpoints are read dynamically by signal name.
    direct_env_keys.update(
        {
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        }
    )

    assert sorted(direct_env_keys - env_keys) == []


def test_full_env_example_covers_compose_variable_references() -> None:
    env_keys = _env_keys(ROOT / ".env.example.all")
    compose_files = [ROOT / "compose.yaml", *(ROOT / "deploy/compose").glob("docker-compose*.yml")]
    compose_text = "\n".join(path.read_text() for path in compose_files)
    compose_vars = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)(?::[-?][^}]*)?\}", compose_text))

    assert sorted(compose_vars - env_keys) == []


def test_minimal_env_example_stays_small() -> None:
    assert sum(1 for _ in (ROOT / ".env.example").open()) <= 130


def test_removed_dynamic_config_switches_are_not_config_fields() -> None:
    from core.config.manager import get_config_keys

    keys = get_config_keys()
    assert "ENABLE_RUNTIME_CONFIG" not in keys
    assert "ALLOW_RUNTIME_CONNECTION_CONFIG" not in keys


def test_database_sync_commit_defaults_to_durable() -> None:
    from core.config.defaults import DBConfig

    assert DBConfig.model_fields["DB_SYNC_COMMIT"].default == "on"


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from core.config.defaults import DBConfig

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        DBConfig(_env_file=None)


def test_production_rejects_local_default_database_url(temp_config) -> None:
    from core.web.startup_checks import validate_startup_security

    temp_config.server.APP_ENV = "production"
    temp_config.security.API_KEY = "real-api-key"
    temp_config.security.ADMIN_WRITE_KEY = "real-admin-write-key"
    temp_config.security.REQUIRE_WEBHOOK_AUTH = True
    temp_config.security.WEBHOOK_SECRET = "real-webhook-secret"
    temp_config.db.DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/webhooks"

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_startup_security(temp_config)


def test_production_rejects_k8s_placeholder_secret_values(temp_config) -> None:
    from core.web.startup_checks import looks_like_placeholder_secret, validate_startup_security

    assert looks_like_placeholder_secret("replace-me-api-key")
    assert looks_like_placeholder_secret("replace-me-webhook-secret")

    temp_config.server.APP_ENV = "production"
    temp_config.security.API_KEY = "replace-me-api-key"
    temp_config.security.ADMIN_WRITE_KEY = "real-admin-write-key"
    temp_config.security.REQUIRE_WEBHOOK_AUTH = True
    temp_config.security.WEBHOOK_SECRET = "real-webhook-secret"

    with pytest.raises(RuntimeError, match="API_KEY"):
        validate_startup_security(temp_config)
