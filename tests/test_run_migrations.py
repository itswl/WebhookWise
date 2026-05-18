import ast
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


def test_initial_schema_revision_is_idempotent_for_historical_tables() -> None:
    revision_path = migrations.PROJECT_ROOT / "alembic/versions/f8894c5c7e15_initial_schema_from_existing_models.py"
    tree = ast.parse(revision_path.read_text())

    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"create_table", "create_index"}:
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "op":
            continue
        calls.append(node)

    assert calls
    missing = [
        node.lineno
        for node in calls
        if not any(
            kw.arg == "if_not_exists" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
    ]
    assert missing == []


def test_legacy_partial_revision_patch_only_updates_matching_partial_state(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[tuple[str, tuple[str, ...] | None]] = []
    responses = [
        ("alembic_version",),
        ("9c0b7c3e2a11",),
        ("processing_locks",),
        (1,),
    ]

    class FakeCursor:
        def execute(self, sql: str, params: tuple[str, ...] | None = None) -> None:
            executed.append((sql, params))

        def fetchone(self) -> tuple[object, ...] | None:
            return responses.pop(0)

    class FakeConnection:
        autocommit = False

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def close(self) -> None:
            executed.append(("close", None))

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setattr("scripts.run_migrations.psycopg2.connect", lambda url: FakeConnection())

    migrations._advance_legacy_partial_revision()

    assert (
        "update public.alembic_version set version_num=%s",
        ("6a7b8c9d0e1f",),
    ) in executed
    assert executed[-1] == ("close", None)
