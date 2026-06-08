from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from scripts.ops import backup_db


def test_parse_database_url_decodes_special_credentials() -> None:
    target = backup_db.parse_database_url(
        "postgresql+asyncpg://webhook_user:p%40ss%2Fword@db.internal:5433/webhooks?sslmode=require"
    )

    assert target.host == "db.internal"
    assert target.port == 5433
    assert target.user == "webhook_user"
    assert target.password == "p@ss/word"
    assert target.database == "webhooks"


def test_run_backup_invokes_pg_dump_and_writes_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:p%40ss@localhost:5432/webhooks")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_COMPRESS", "false")

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs["env"]))
        backup_path = Path(cmd[cmd.index("--file") + 1])
        backup_path.write_bytes(b"custom pg dump bytes")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(backup_db.subprocess, "run", fake_run)

    backup_path = backup_db.run_backup()

    assert backup_path.parent == tmp_path
    assert backup_path.name.startswith("webhooks_")
    assert backup_path.suffix == ".dump"
    assert calls[0][0][:2] == ["pg_dump", "--host"]
    assert "--compress=9" not in calls[0][0]
    assert "p@ss" not in calls[0][0]
    assert calls[0][1]["PGPASSWORD"] == "p@ss"
    assert backup_db.checksum_path_for(backup_path).is_file()
    assert backup_db.verify_backup(backup_path)


def test_run_backup_removes_partial_file_on_pg_dump_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/webhooks")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        backup_path = Path(cmd[cmd.index("--file") + 1])
        backup_path.write_bytes(b"partial")
        raise subprocess.CalledProcessError(2, cmd, stderr="permission denied")

    monkeypatch.setattr(backup_db.subprocess, "run", fake_run)

    with pytest.raises(backup_db.BackupError, match="permission denied"):
        backup_db.run_backup()

    assert list(tmp_path.glob("*.dump")) == []


def test_verify_backup_detects_checksum_mismatch(tmp_path: Path) -> None:
    backup_path = tmp_path / "webhooks.dump"
    backup_path.write_bytes(b"original")
    backup_db.write_checksum(backup_path)

    assert backup_db.verify_backup(backup_path)

    backup_path.write_bytes(b"changed")

    assert not backup_db.verify_backup(backup_path)


def test_cleanup_removes_expired_backup_and_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "1")
    old_backup = tmp_path / "old.dump"
    old_checksum = tmp_path / "old.dump.sha256"
    new_backup = tmp_path / "new.dump"
    orphan_checksum = tmp_path / "orphan.dump.sha256"
    for path in (old_backup, old_checksum, new_backup, orphan_checksum):
        path.write_text("x", encoding="utf-8")

    old_time = time.time() - 3 * 86400
    os.utime(old_backup, (old_time, old_time))
    os.utime(old_checksum, (old_time, old_time))
    os.utime(orphan_checksum, (old_time, old_time))

    removed = backup_db.cleanup_old_backups()

    assert removed == 3
    assert not old_backup.exists()
    assert not old_checksum.exists()
    assert not orphan_checksum.exists()
    assert new_backup.exists()


def test_main_returns_nonzero_when_s3_upload_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "webhooks.dump"
    backup_path.write_bytes(b"backup")
    backup_db.write_checksum(backup_path)

    def fail_upload(_backup_path: Path, *, verbose: bool = False) -> None:
        raise backup_db.BackupError("S3 upload failed")

    monkeypatch.setattr(backup_db, "run_backup", lambda *, verbose=False: backup_path)
    monkeypatch.setattr(backup_db, "maybe_upload_to_s3", fail_upload)

    assert backup_db.main([]) == 1


def test_verify_cli_accepts_backup_directory(tmp_path: Path) -> None:
    backup_path = tmp_path / "webhooks.dump"
    backup_path.write_bytes(b"backup")
    backup_db.write_checksum(backup_path)

    assert backup_db.main(["--verify", str(tmp_path)]) == 0

    backup_path.write_bytes(b"corrupted")

    assert backup_db.main(["--verify", str(tmp_path)]) == 1
