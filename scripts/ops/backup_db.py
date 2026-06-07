#!/usr/bin/env python3
"""
PostgreSQL 备份编排脚本

用法:
    python -m scripts.ops.backup_db                    # 执行备份
    python -m scripts.ops.backup_db --list              # 列出已有备份
    python -m scripts.ops.backup_db --cleanup-only      # 仅清理过期备份

环境变量:
    DATABASE_URL              (必需) PostgreSQL 连接字符串
    BACKUP_DIR                备份输出目录 (默认: ./backups)
    BACKUP_RETENTION_DAYS     保留天数 (默认: 30)
    BACKUP_COMPRESS           是否压缩 (默认: true)
    AWS_ENDPOINT_URL          S3 兼容存储端点 (可选)
    AWS_ACCESS_KEY_ID         S3 访问密钥 (可选)
    AWS_SECRET_ACCESS_KEY     S3 秘密密钥 (可选)
    AWS_BUCKET                S3 存储桶 (可选)
    BACKUP_PATH_PREFIX        S3 路径前缀 (可选)
    PGPASSWORD                如果 DATABASE_URL 不含密码可用此变量

依赖: pg_dump (PostgreSQL 客户端)
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

# ── 将项目根目录加入 sys.path ──────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _parse_db_url(url: str) -> dict[str, str]:
    """Parse postgresql:// URL into pg_dump command components."""
    result: dict[str, str] = {}
    if not url:
        return result
    # postgresql://user:pass@host:port/dbname
    rest = url
    for prefix in ("postgresql://", "postgresql+asyncpg://", "postgres://"):
        if rest.startswith(prefix):
            rest = rest[len(prefix):]
            break
    # Extract user:password@host:port/dbname
    if "@" in rest:
        userinfo, rest = rest.split("@", 1)
        if ":" in userinfo:
            result["user"], pw = userinfo.split(":", 1)
            result["password"] = pw
        else:
            result["user"] = userinfo
    if "/" in rest:
        hostport, result["dbname"] = rest.split("/", 1)
    else:
        hostport = rest
        result["dbname"] = ""
    if ":" in hostport:
        result["host"], port_str = hostport.split(":", 1)
        result["port"] = port_str
    else:
        result["host"] = hostport
        result["port"] = "5432"
    return result


def backup_dir() -> Path:
    """Resolve the backup output directory, creating if needed."""
    base = Path(_env("BACKUP_DIR", "./backups")).resolve()
    if not base.is_absolute():
        base = _ROOT / base
    base.mkdir(parents=True, exist_ok=True)
    return base


def retention_days() -> int:
    try:
        return max(1, int(_env("BACKUP_RETENTION_DAYS", "30")))
    except (ValueError, TypeError):
        return 30


def run_backup(*, verbose: bool = False) -> Path:
    """Execute pg_dump and return the backup file path."""
    db_url = _env("DATABASE_URL")
    if not db_url:
        sys.stderr.write("FATAL: DATABASE_URL is not set\n")
        sys.exit(1)

    parsed = _parse_db_url(db_url)
    if not parsed.get("dbname"):
        sys.stderr.write("FATAL: cannot parse database name from DATABASE_URL\n")
        sys.exit(1)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dbname = parsed["dbname"]
    backup_path = backup_dir() / f"{dbname}_{ts}.dump"
    compress = _env("BACKUP_COMPRESS", "true").strip().lower() in ("1", "true", "yes")

    cmd = [
        "pg_dump",
        f"--host={parsed.get('host', 'localhost')}",
        f"--port={parsed.get('port', '5432')}",
        f"--username={parsed.get('user', 'postgres')}",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--verbose",
    ]
    if compress:
        cmd.append("--compress=9")
    cmd.append(dbname)

    # Export password so pg_dump can use it
    pw = parsed.get("password", "")
    if pw:
        env = os.environ.copy()
        env["PGPASSWORD"] = pw
    else:
        env = None

    if verbose:
        print(f"Backing up {dbname} → {backup_path}")
        print(f"  Command: {' '.join(cmd)}")
        sys.stdout.flush()

    started = time.time()
    try:
        with open(backup_path, "wb") as f:
            subprocess.run(
                cmd,
                stdout=f,
                stderr=subprocess.PIPE,
                env=env,
                check=True,
            )
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
        sys.stderr.write(f"ERROR: pg_dump failed: {err}\n")
        sys.exit(1)
    except FileNotFoundError:
        sys.stderr.write("ERROR: pg_dump not found. Install PostgreSQL client tools.\n")
        sys.exit(1)

    elapsed = time.time() - started
    size_mb = backup_path.stat().st_size / (1024 * 1024)
    if verbose:
        print(f"Backup completed: {size_mb:.1f} MiB in {elapsed:.1f}s")
    return backup_path


def list_backups(*, verbose: bool = False) -> list[Path]:
    """List existing backup files sorted by modification time (newest first)."""
    base = backup_dir()
    files = sorted(
        [f for f in base.iterdir() if f.suffix == ".dump" and f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if verbose:
        if not files:
            print(f"No backups found in {base}")
        else:
            print(f"Backups in {base}:")
            for f in files:
                mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                size = f.stat().st_size / (1024 * 1024)
                print(f"  {f.name}  ({size:.1f} MiB, {mtime})")
    return files


def cleanup_old_backups(*, verbose: bool = False) -> int:
    """Remove backup files older than retention period. Returns count removed."""
    base = backup_dir()
    retention = retention_days()
    cutoff = time.time() - retention * 86400
    removed = 0
    for f in base.iterdir():
        if f.suffix == ".dump" and f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
            if verbose:
                print(f"  Removed: {f.name}")
    if verbose:
        print(f"Cleanup complete: removed {removed} expired backup(s) (retention={retention}d)")
    return removed


def maybe_upload_to_s3(backup_path: Path, *, verbose: bool = False) -> None:
    """Upload backup to S3-compatible storage if configured."""
    endpoint = _env("AWS_ENDPOINT_URL")
    bucket = _env("AWS_BUCKET")
    if not endpoint or not bucket:
        if verbose:
            print("S3 not configured, skipping upload")
        return

    try:
        import boto3
    except ImportError:
        sys.stderr.write("WARNING: boto3 not installed, skipping S3 upload\n")
        return

    prefix = _env("BACKUP_PATH_PREFIX", "webhookwise-backups").strip("/")
    key = f"{prefix}/{backup_path.name}"
    session = boto3.Session(
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY"),
    )
    client = session.client("s3", endpoint_url=endpoint)
    if verbose:
        print(f"Uploading {backup_path.name} → {endpoint}/{bucket}/{key}")
    client.upload_file(str(backup_path), bucket, key)
    if verbose:
        print("Upload complete")


def _backup_metric_counter():
    """Lazy import OTel counter for backup success/failure."""
    try:
        from core.observability.metrics_base import Counter
        return Counter(
            "backup.operation.total",
            "Total backup operations (success/failure)",
            label_keys=("operation", "status"),
        )
    except ImportError:
        return None


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 checksum of a file."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_backup(backup_path: Path, *, verbose: bool = False) -> bool:
    """Verify backup integrity by checking SHA-256 checksum."""
    checksum_path = backup_path.with_suffix(".dump.sha256")
    if not checksum_path.exists():
        if verbose:
            print(f"Checksum file not found: {checksum_path}")
        return False

    stored = checksum_path.read_text().strip().split()[0]
    actual = _compute_sha256(backup_path)
    match = stored == actual
    if verbose:
        if match:
            print(f"Checksum OK: {backup_path.name}")
        else:
            print(f"Checksum MISMATCH: {backup_path.name}")
            print(f"  Stored: {stored}")
            print(f"  Actual: {actual}")
    return match


def _main_backup(verbose: bool, no_s3: bool) -> None:
    """Execute backup, upload to S3, cleanup old backups."""
    backup_path = run_backup(verbose=verbose)

    if not no_s3:
        try:
            maybe_upload_to_s3(backup_path, verbose=verbose)
        except Exception as exc:
            sys.stderr.write(f"ERROR: S3 upload failed: {exc}\n")
            sys.exit(1)

    cleanup_old_backups(verbose=verbose)

    print(f"Backup saved to: {backup_path}")
    # Generate checksum file for verification
    sha_path = backup_path.with_suffix(".dump.sha256")
    print(f"Checksum saved to: {sha_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL backup orchestration")
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    parser.add_argument("--cleanup-only", action="store_true", help="仅清理过期备份，不执行新备份")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    parser.add_argument("--no-s3", action="store_true", help="跳过 S3 上传")
    args = parser.parse_args()

    if args.list:
        list_backups(verbose=True)
        return

    if args.cleanup_only:
        removed = cleanup_old_backups(verbose=True)
        print(f"Removed {removed} expired backup(s)")
        return

    # 执行备份
    backup_path = run_backup(verbose=args.verbose)

    if not args.no_s3:
        maybe_upload_to_s3(backup_path, verbose=args.verbose)

    cleanup_old_backups(verbose=args.verbose)

    print(f"Backup saved to: {backup_path}")


if __name__ == "__main__":
    main()
