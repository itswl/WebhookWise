#!/usr/bin/env python3
"""PostgreSQL backup orchestration with checksum verification."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class BackupError(RuntimeError):
    """Raised when a backup operation cannot complete safely."""


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int
    user: str
    password: str
    database: str


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def parse_database_url(url: str) -> DatabaseTarget:
    """Parse a PostgreSQL URL into pg_dump connection arguments."""
    if not url:
        raise BackupError("DATABASE_URL is not set")

    parsed = urlparse(url)
    if parsed.scheme not in {"postgresql", "postgres", "postgresql+asyncpg"}:
        raise BackupError("DATABASE_URL must use postgresql://, postgres://, or postgresql+asyncpg://")

    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise BackupError("DATABASE_URL does not include a database name")

    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise BackupError("DATABASE_URL includes an invalid port") from exc

    return DatabaseTarget(
        host=parsed.hostname or "localhost",
        port=port,
        user=unquote(parsed.username or "postgres"),
        password=unquote(parsed.password or ""),
        database=database,
    )


def resolve_backup_dir() -> Path:
    """Resolve and create the backup output directory."""
    raw = Path(_env("BACKUP_DIR", "backups")).expanduser()
    base = raw if raw.is_absolute() else _ROOT / raw
    base.mkdir(parents=True, exist_ok=True)
    return base


def retention_days() -> int:
    try:
        return max(1, int(_env("BACKUP_RETENTION_DAYS", "30")))
    except (TypeError, ValueError):
        return 30


def compress_enabled() -> bool:
    return _env("BACKUP_COMPRESS", "true").strip().lower() in {"1", "true", "yes", "on"}


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "database"


def build_pg_dump_command(target: DatabaseTarget, backup_path: Path, *, compress: bool) -> list[str]:
    cmd = [
        "pg_dump",
        "--host",
        target.host,
        "--port",
        str(target.port),
        "--username",
        target.user,
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--file",
        str(backup_path),
    ]
    if compress:
        cmd.append("--compress=9")
    cmd.append(target.database)
    return cmd


def pg_dump_env(target: DatabaseTarget) -> dict[str, str]:
    env = os.environ.copy()
    if target.password:
        env["PGPASSWORD"] = target.password
    return env


def _stderr_text(exc: subprocess.CalledProcessError) -> str:
    output = exc.stderr if exc.stderr else exc.stdout
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace").strip()
    if isinstance(output, str):
        return output.strip()
    return str(exc)


def checksum_path_for(backup_path: Path) -> Path:
    return backup_path.with_suffix(backup_path.suffix + ".sha256")


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(backup_path: Path) -> Path:
    checksum_path = checksum_path_for(backup_path)
    checksum_path.write_text(f"{compute_sha256(backup_path)}  {backup_path.name}\n", encoding="utf-8")
    return checksum_path


def verify_backup(backup_path: Path, *, verbose: bool = False) -> bool:
    checksum_path = checksum_path_for(backup_path)
    if not backup_path.is_file():
        if verbose:
            print(f"Backup file not found: {backup_path}")
        return False
    if not checksum_path.is_file():
        if verbose:
            print(f"Checksum file not found: {checksum_path}")
        return False

    expected = checksum_path.read_text(encoding="utf-8").strip().split(maxsplit=1)[0]
    actual = compute_sha256(backup_path)
    ok = expected == actual
    if verbose:
        status = "OK" if ok else "MISMATCH"
        print(f"{status}: {backup_path.name}")
        if not ok:
            print(f"  expected={expected}")
            print(f"  actual={actual}")
    return ok


def verify_target(path: Path, *, verbose: bool = False) -> bool:
    if path.is_dir():
        backups = sorted(item for item in path.iterdir() if item.suffix == ".dump" and item.is_file())
        if not backups:
            if verbose:
                print(f"No .dump backups found in {path}")
            return False
        return all(verify_backup(backup, verbose=verbose) for backup in backups)
    return verify_backup(path, verbose=verbose)


def run_backup(*, verbose: bool = False) -> Path:
    target = parse_database_url(_env("DATABASE_URL"))
    now = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = resolve_backup_dir() / f"{_safe_filename(target.database)}_{now}.dump"
    cmd = build_pg_dump_command(target, backup_path, compress=compress_enabled())

    if verbose:
        print(f"Backing up database {target.database!r} to {backup_path}")
        print("Command:", " ".join(cmd))

    started = time.monotonic()
    try:
        subprocess.run(
            cmd,
            env=pg_dump_env(target),
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        backup_path.unlink(missing_ok=True)
        raise BackupError("pg_dump not found; install PostgreSQL client tools") from exc
    except subprocess.CalledProcessError as exc:
        backup_path.unlink(missing_ok=True)
        detail = _stderr_text(exc)
        message = f"pg_dump failed with exit code {exc.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise BackupError(message) from exc

    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        backup_path.unlink(missing_ok=True)
        raise BackupError("pg_dump completed but did not produce a non-empty backup file")

    checksum_path = write_checksum(backup_path)
    if verbose:
        elapsed = time.monotonic() - started
        size_mb = backup_path.stat().st_size / (1024 * 1024)
        print(f"Backup completed: {backup_path.name} ({size_mb:.1f} MiB, {elapsed:.1f}s)")
        print(f"Checksum saved to: {checksum_path}")
    return backup_path


def list_backups(*, verbose: bool = False) -> list[Path]:
    backups = sorted(
        [item for item in resolve_backup_dir().iterdir() if item.suffix == ".dump" and item.is_file()],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if verbose:
        if not backups:
            print(f"No backups found in {resolve_backup_dir()}")
        for backup in backups:
            mtime = dt.datetime.fromtimestamp(backup.stat().st_mtime, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%SZ")
            size_mb = backup.stat().st_size / (1024 * 1024)
            checksum_status = "checksum" if checksum_path_for(backup).is_file() else "missing-checksum"
            print(f"{backup.name}  {size_mb:.1f} MiB  {mtime}  {checksum_status}")
    return backups


def cleanup_old_backups(*, verbose: bool = False) -> int:
    cutoff = time.time() - retention_days() * 86400
    removed = 0
    for path in resolve_backup_dir().iterdir():
        if path.suffix == ".dump" and path.is_file() and path.stat().st_mtime < cutoff:
            checksum_path = checksum_path_for(path)
            path.unlink()
            removed += 1
            if checksum_path.exists():
                checksum_path.unlink()
                removed += 1
            if verbose:
                print(f"Removed expired backup: {path.name}")
        elif path.name.endswith(".dump.sha256") and path.is_file() and path.stat().st_mtime < cutoff:
            dump_path = path.with_suffix("")
            if not dump_path.exists():
                path.unlink()
                removed += 1
                if verbose:
                    print(f"Removed orphan checksum: {path.name}")
    if verbose:
        print(f"Cleanup complete: removed {removed} file(s), retention={retention_days()}d")
    return removed


def _s3_key(prefix: str, filename: str) -> str:
    clean_prefix = prefix.strip("/")
    return f"{clean_prefix}/{filename}" if clean_prefix else filename


def _load_boto3() -> Any:
    return importlib.import_module("boto3")


def maybe_upload_to_s3(backup_path: Path, *, verbose: bool = False) -> None:
    bucket = _env("AWS_BUCKET")
    endpoint = _env("AWS_ENDPOINT_URL")
    if not bucket:
        if endpoint:
            raise BackupError("AWS_ENDPOINT_URL is set but AWS_BUCKET is missing")
        if verbose:
            print("S3 upload not configured; skipping")
        return

    try:
        boto3 = _load_boto3()
    except ImportError as exc:
        raise BackupError("boto3 is required when AWS_BUCKET is configured") from exc

    session = boto3.Session(
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY") or None,
    )
    client_kwargs = {"endpoint_url": endpoint} if endpoint else {}
    client = session.client("s3", **client_kwargs)
    prefix = _env("BACKUP_PATH_PREFIX", "webhookwise-backups")

    for path in (backup_path, checksum_path_for(backup_path)):
        if not path.is_file():
            raise BackupError(f"cannot upload missing backup artifact: {path}")
        key = _s3_key(prefix, path.name)
        if verbose:
            print(f"Uploading {path.name} to s3://{bucket}/{key}")
        try:
            client.upload_file(str(path), bucket, key)
        except Exception as exc:
            raise BackupError(f"S3 upload failed for {path.name}: {exc}") from exc


def _resolve_cli_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL backup orchestration")
    parser.add_argument("--list", action="store_true", help="List existing backups")
    parser.add_argument("--cleanup-only", action="store_true", help="Only remove expired backups")
    parser.add_argument("--verify", metavar="PATH", help="Verify one .dump file or a directory of backups")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed output")
    parser.add_argument("--no-s3", action="store_true", help="Skip S3 upload even when AWS_BUCKET is configured")
    args = parser.parse_args(argv)

    try:
        if args.list:
            list_backups(verbose=True)
            return 0

        if args.cleanup_only:
            removed = cleanup_old_backups(verbose=True)
            print(f"Removed {removed} expired backup file(s)")
            return 0

        if args.verify:
            return 0 if verify_target(_resolve_cli_path(args.verify), verbose=True) else 1

        backup_path = run_backup(verbose=args.verbose)
        if not args.no_s3:
            maybe_upload_to_s3(backup_path, verbose=args.verbose)
        cleanup_old_backups(verbose=args.verbose)
        print(f"Backup saved to: {backup_path}")
        print(f"Checksum saved to: {checksum_path_for(backup_path)}")
        return 0
    except BackupError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
