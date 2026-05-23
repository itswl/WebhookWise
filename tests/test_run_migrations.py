import re
from pathlib import Path
from typing import Any

import pytest

import scripts.run_migrations as migrations


@pytest.mark.asyncio
async def test_wait_for_database_disposes_engine_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_init_engine(config: Any | None = None) -> None:
        calls.append("init")

    async def fake_test_db_connection() -> bool:
        calls.append("test")
        return True

    async def fake_dispose_engine() -> None:
        calls.append("dispose")

    monkeypatch.setattr(migrations, "init_engine", fake_init_engine)
    monkeypatch.setattr(migrations, "test_db_connection", fake_test_db_connection)
    monkeypatch.setattr(migrations, "dispose_engine", fake_dispose_engine)

    await migrations._wait_for_database(max_retries=3, interval_seconds=0)

    assert calls == ["init", "test", "dispose"]


def test_run_alembic_upgrade_uses_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> None:
        captured["command"] = command
        captured["cwd"] = cwd
        captured["check"] = check

    monkeypatch.setattr("scripts.run_migrations.subprocess.run", fake_run)

    migrations._run_alembic_upgrade()

    assert captured == {
        "command": ["alembic", "upgrade", "head"],
        "cwd": migrations.PROJECT_ROOT,
        "check": True,
    }


def test_alembic_history_is_a_current_schema_baseline() -> None:
    revision_paths = sorted((migrations.PROJECT_ROOT / "alembic/versions").glob("*.py"))

    assert [path.name for path in revision_paths] == [
        "0001_current_schema.py",
        "0002_pluralize_tables.py",
        "0003_drop_system_configs.py",
        "0004_drop_archived_webhook_events.py",
        "0005_create_archived_webhook_events.py",
        "0006_channel_outbox_refactor.py",
        "0007_forward_rule_match_payload.py",
        "0008_create_suppressed_records.py",
        "0009_add_dedup_key.py",
    ]
    by_name = {path.name: path for path in revision_paths}
    for path in revision_paths:
        revision = re.search(r'^revision: str = "([^"]+)"', path.read_text(), re.MULTILINE)
        assert revision is not None
        assert len(revision.group(1)) <= 32
    source = by_name["0001_current_schema.py"].read_text()
    assert "Base.metadata.create_all" in source
    assert "Base.metadata.drop_all" in source
    migration_source = by_name["0002_pluralize_tables.py"].read_text()
    assert 'op.rename_table("ai_usage_log", "ai_usage_logs")' in migration_source
    assert 'op.rename_table("forward_outbox", "forward_outboxes")' in migration_source
    cleanup_source = by_name["0003_drop_system_configs.py"].read_text()
    assert 'op.drop_table("system_configs")' in cleanup_source
    archive_cleanup_source = by_name["0004_drop_archived_webhook_events.py"].read_text()
    assert 'op.drop_table("archived_webhook_events")' in archive_cleanup_source
    archive_create_source = by_name["0005_create_archived_webhook_events.py"].read_text()
    assert "op.create_table(" in archive_create_source
    assert '"archived_webhook_events"' in archive_create_source
    assert "processing_status" in archive_create_source
    assert "request_id" in archive_create_source
