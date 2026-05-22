import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_keys(path: Path) -> set[str]:
    return set(re.findall(r"^(?:#\s*)?([A-Z][A-Z0-9_]+)=", path.read_text(), re.MULTILINE))


def test_config_keys_are_derived_from_config_models() -> None:
    from core.config import UnifiedConfigManager

    assert UnifiedConfigManager.CONFIG_KEYS["AI_ERROR_NOTIFICATION_COOLDOWN_SECONDS"] == {
        "type": "int",
        "sub": "notifications",
    }
    assert UnifiedConfigManager.CONFIG_KEYS["DEFAULT_FORWARD_TARGET_URL"] == {"type": "str", "sub": "forwarding"}
    assert UnifiedConfigManager.CONFIG_KEYS["WEBHOOK_MQ_QUEUE"] == {"type": "str", "sub": "mq"}
    assert UnifiedConfigManager.CONFIG_KEYS["BACKGROUND_SCAN_INTERVAL_SECONDS"] == {"type": "int", "sub": "tasks"}
    assert UnifiedConfigManager.CONFIG_KEYS["MAX_CONCURRENT_WEBHOOK_TASKS"] == {"type": "int", "sub": "tasks"}
    assert UnifiedConfigManager.CONFIG_KEYS["CIRCUIT_BREAKER_FEISHU_THRESHOLD"] == {
        "type": "int",
        "sub": "circuit_breaker",
    }
    assert UnifiedConfigManager.CONFIG_KEYS["DATA_RETENTION_DAYS_DEFAULT"] == {"type": "int", "sub": "maintenance"}
    assert UnifiedConfigManager.CONFIG_KEYS["DATABASE_URL"] == {"type": "str", "sub": "db"}
    assert UnifiedConfigManager.CONFIG_KEYS["OPENAI_API_KEY"] == {"type": "str", "sub": "ai"}


def test_full_env_example_covers_config_model_keys() -> None:
    from core.config import UnifiedConfigManager

    env_keys = _env_keys(ROOT / ".env.example.all")

    assert sorted(set(UnifiedConfigManager.CONFIG_KEYS) - env_keys) == []


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
        ROOT / "core/observability/metrics/base.py",
        ROOT / "core/observability/metrics/source.py",
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
    compose_text = "\n".join(path.read_text() for path in ROOT.glob("docker-compose*.yml"))
    compose_vars = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)(?::[-?][^}]*)?\}", compose_text))

    assert sorted(compose_vars - env_keys) == []


def test_minimal_env_example_stays_small() -> None:
    assert sum(1 for _ in (ROOT / ".env.example").open()) <= 130


def test_removed_dynamic_config_switches_are_not_config_fields() -> None:
    from core.config import UnifiedConfigManager

    assert "ENABLE_RUNTIME_CONFIG" not in UnifiedConfigManager.CONFIG_KEYS
    assert "ALLOW_RUNTIME_CONNECTION_CONFIG" not in UnifiedConfigManager.CONFIG_KEYS


def test_config_sources_are_static_and_restart_required(monkeypatch) -> None:
    from services.configuration.config_service import get_config_sources

    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    by_key = {item["key"]: item for item in get_config_sources()}

    assert by_key["OPENAI_MODEL"]["source"] == "file_or_environment"
    assert by_key["OPENAI_MODEL"]["requires_restart"] is True

    if os.getenv("NOISE_SEMANTIC_WEIGHT") is None:
        assert by_key["NOISE_SEMANTIC_WEIGHT"]["source"] == "default"


def test_database_sync_commit_defaults_to_durable() -> None:
    from core.config.defaults import DBConfig

    assert DBConfig.model_fields["DB_SYNC_COMMIT"].default == "on"
