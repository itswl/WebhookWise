import sys
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
        "command": [sys.executable, "-m", "alembic", "upgrade", "head"],
        "cwd": migrations.PROJECT_ROOT,
        "check": True,
    }


def test_alembic_history_starts_from_consolidated_baseline() -> None:
    revision_paths = sorted((migrations.PROJECT_ROOT / "alembic/versions").glob("*.py"))
    names = [path.name for path in revision_paths]

    # The incremental chain was squashed into one baseline; new migrations chain
    # off it normally.
    assert "0001_baseline.py" in names
    by_name = {p.name: p for p in revision_paths}
    source = by_name["0001_baseline.py"].read_text()
    # Real DDL, not a metadata.create_all shortcut (keeps it explicit/inspectable).
    assert "Base.metadata.create_all" not in source
    assert "Base.metadata.drop_all" not in source
    assert "op.create_table" in source
    assert "op.create_index" in source
    assert 'revision: str = "0001_baseline"' in source
    assert "down_revision: str | Sequence[str] | None = None" in source
    # The baseline is the single root; every other migration descends from it.
    assert sum("down_revision: str | Sequence[str] | None = None" in p.read_text() for p in revision_paths) == 1


def test_reconcile_restamps_pre_squash_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB stamped on the old chain is re-stamped onto the baseline."""
    stamped: list[tuple[str, ...]] = []

    async def fake_current() -> str | None:
        return "0006_drop_duplicate_outbox_index"

    monkeypatch.setattr(migrations, "_current_alembic_revision", fake_current)
    monkeypatch.setattr(migrations, "_alembic", lambda *args: stamped.append(args))

    migrations._reconcile_squashed_history()

    assert stamped == [("stamp", "0001_baseline", "--purge")]


@pytest.mark.parametrize("current", [None, "0001_baseline"])
def test_reconcile_leaves_fresh_and_baseline_databases_untouched(
    monkeypatch: pytest.MonkeyPatch, current: str | None
) -> None:
    called: list[tuple[str, ...]] = []

    async def fake_current() -> str | None:
        return current

    monkeypatch.setattr(migrations, "_current_alembic_revision", fake_current)
    monkeypatch.setattr(migrations, "_alembic", lambda *args: called.append(args))

    migrations._reconcile_squashed_history()

    assert called == []
