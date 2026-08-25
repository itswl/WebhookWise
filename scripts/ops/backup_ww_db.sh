#!/usr/bin/env bash
# Nightly WebhookWise database backup.
#
# Install (host crontab, pinning the real root — the default below is a
# placeholder because this repository is public):
#   30 4 * * * WW_ROOT=/your/deploy/root /your/deploy/root/scripts/ops/backup_ww_db.sh >> /opt/backups/webhookwise/backup.log 2>&1
#
# Runs backup_db.py INSIDE the application image, which is the only place a
# pg_dump that can read this server lives — and, more importantly, the only
# place a pg_restore that can read the RESULT lives. The database container's
# own pg_restore refuses these archives outright ("unsupported version (1.16)
# in file header") because its client is older than the one that wrote them.
# See scripts/ops/restore_db.py.
set -euo pipefail

ROOT="${WW_ROOT:-/srv/WebhookWise}"
OUT="${WW_BACKUP_DIR:-/opt/backups/webhookwise}"
KEEP="${WW_BACKUP_RETENTION_DAYS:-30}"

[ -d "$ROOT" ] || { echo "no deploy root at $ROOT — set WW_ROOT" >&2; exit 1; }
# The container writes as its own uid; a directory it cannot write fails the
# dump AFTER pg_dump has already run, which is the expensive half.
mkdir -p "$OUT"

cd "$ROOT"
docker compose run --rm --no-deps \
  -e BACKUP_DIR=/backups \
  -e BACKUP_RETENTION_DAYS="$KEEP" \
  -v "$OUT":/backups \
  webhook-service python -m scripts.ops.backup_db --verbose
