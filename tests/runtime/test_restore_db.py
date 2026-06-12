"""Tests for the DB restore script (scripts.ops.restore_db)."""

from __future__ import annotations

import pytest

from scripts.ops.backup_db import DatabaseTarget
from scripts.ops.restore_db import RestoreError, build_pg_restore_command, run_restore


def test_build_pg_restore_command_is_clean_and_targeted() -> None:
    target = DatabaseTarget(host="db", port=5432, user="u", password="p", database="webhooks")
    cmd = build_pg_restore_command(target, __import__("pathlib").Path("/backups/x.dump"))
    assert cmd[0] == "pg_restore"
    assert "--clean" in cmd and "--if-exists" in cmd
    assert "--dbname" in cmd and "webhooks" in cmd
    assert "--host" in cmd and "db" in cmd
    assert cmd[-1] == "/backups/x.dump"


@pytest.mark.asyncio
async def test_run_restore_missing_file_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/webhooks")
    with pytest.raises(RestoreError, match="not found"):
        run_restore("/nonexistent/path/x.dump", verify=False, assume_yes=True)


@pytest.mark.asyncio
async def test_run_restore_aborts_without_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import scripts.ops.restore_db as restore_db

    backup = tmp_path / "x.dump"
    backup.write_bytes(b"PGDMP-fake")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/webhooks")
    # No --yes and confirmation declined -> abort (return 1), pg_restore never runs.
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    ran: list[object] = []
    monkeypatch.setattr(restore_db.subprocess, "run", lambda *a, **k: ran.append(a))

    rc = run_restore(str(backup), verify=False, assume_yes=False)
    assert rc == 1
    assert ran == []  # destructive command must not have executed
