"""Restore a PostgreSQL custom-format backup produced by scripts.ops.backup_db.

Usage:
    python -m scripts.ops.restore_db --file backups/webhooks-20260612.dump [--verify] [--yes]

Verifies the .sha256 checksum (unless --no-verify), then runs pg_restore with
--clean --if-exists into the database named in DATABASE_URL. Because this DROPs
and recreates objects, it requires an explicit confirmation (--yes or an
interactive y/N prompt) so it cannot be run by accident.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.ops.backup_db import (  # noqa: E402
    BackupError,
    DatabaseTarget,
    parse_database_url,
    pg_dump_env,
    verify_backup,
)


class RestoreError(RuntimeError):
    """Raised when a restore cannot be performed."""


def build_pg_restore_command(target: DatabaseTarget, backup_path: Path) -> list[str]:
    return [
        "pg_restore",
        "--host",
        target.host,
        "--port",
        str(target.port),
        "--username",
        target.user,
        "--dbname",
        target.database,
        # Drop existing objects before recreating so a restore over a populated
        # database is deterministic; --if-exists avoids errors on first restore.
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        str(backup_path),
    ]


def _confirm(target: DatabaseTarget, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    prompt = (
        f"This will DROP and recreate objects in database '{target.database}' "
        f"on {target.host}:{target.port}. Continue? [y/N] "
    )
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def run_restore(backup_file: str, *, verify: bool = True, assume_yes: bool = False, verbose: bool = False) -> int:
    backup_path = Path(backup_file).expanduser()
    if not backup_path.is_absolute():
        backup_path = (_ROOT / backup_path).resolve()
    if not backup_path.is_file():
        raise RestoreError(f"backup file not found: {backup_path}")

    if verify and not verify_backup(backup_path, verbose=verbose):
        raise RestoreError(f"checksum verification failed for {backup_path}")

    target = parse_database_url(_database_url())
    if not _confirm(target, assume_yes):
        print("Restore aborted.")
        return 1

    cmd = build_pg_restore_command(target, backup_path)
    if verbose:
        print("Running:", " ".join(cmd))
    try:
        # pg_restore is invoked with a fixed argv list; shell=False is the subprocess default.
        result = subprocess.run(  # nosec B603
            cmd,
            check=True,
            env=pg_dump_env(target),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RestoreError("pg_restore not found; install PostgreSQL client tools") from exc
    except subprocess.CalledProcessError as exc:
        # pg_restore exits non-zero on benign warnings too; surface stderr.
        raise RestoreError(f"pg_restore failed (exit {exc.returncode}):\n{exc.stderr}") from exc
    if verbose and result.stderr:
        print(result.stderr)
    print(f"Restore complete from {backup_path} into '{target.database}'.")
    return 0


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RestoreError("DATABASE_URL is not set")
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore a PostgreSQL custom-format backup.")
    parser.add_argument("--file", required=True, help="path to the .dump backup file")
    parser.add_argument("--no-verify", action="store_true", help="skip .sha256 checksum verification")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run_restore(args.file, verify=not args.no_verify, assume_yes=args.yes, verbose=args.verbose)
    except (RestoreError, BackupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
