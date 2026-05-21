import re
from pathlib import Path
from typing import Any

import pytest

import scripts.run_migrations as migrations


@pytest.mark.asyncio
async def test_wait_for_database_disposes_engine_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_init_engine() -> None:
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
    ]
    for path in revision_paths:
        revision = re.search(r'^revision: str = "([^"]+)"', path.read_text(), re.MULTILINE)
        assert revision is not None
        assert len(revision.group(1)) <= 32
    source = revision_paths[0].read_text()
    assert "Base.metadata.create_all" in source
    assert "Base.metadata.drop_all" in source
    migration_source = revision_paths[1].read_text()
    assert 'op.rename_table("ai_usage_log", "ai_usage_logs")' in migration_source
    assert 'op.rename_table("forward_outbox", "forward_outboxes")' in migration_source
