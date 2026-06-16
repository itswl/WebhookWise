# Database Backup and Restore

WebhookWise uses PostgreSQL custom-format backups (`pg_dump -Fc`), writing a
matching `.dump.sha256` checksum file next to each `.dump`. Both the backup and
restore scripts depend on the `postgresql-client` (pg_dump/pg_restore) inside the
image.

## Backup

### Manual one-off backup

```bash
python -m scripts.ops.backup_db --verbose          # Create a backup and clean up expired backups
python -m scripts.ops.backup_db --verify backups   # Verify all backups in the directory
python -m scripts.ops.backup_db --cleanup-only     # Only clean up by retention period
```

For configuration, see the DB Backup section of `.env.example.all` (`BACKUP_DIR` / `BACKUP_RETENTION_DAYS`
/ `BACKUP_COMPRESS`; setting `AWS_BUCKET` uploads to S3-compatible storage).

### Scheduled backups (Docker Compose)

`deploy/compose/docker-compose.yml` provides a `backup` service (profile=backup)
that runs backups in a loop on `BACKUP_INTERVAL_SECONDS` (default 86400 = daily),
writing to the host `BACKUP_HOST_DIR` (default `./backups`, mounted into the
container at `/backups`).

```bash
# Enable the scheduled backup service (not started by default; requires the profile explicitly)
docker compose --profile backup up -d backup
docker compose logs -f backup        # View the backup run logs
ls -lh backups/                      # Backup files on the host
```

> For Kubernetes deployments, use a CronJob to schedule `python -m scripts.ops.backup_db`
> (the image already includes the pg client); for single-host compose, the
> long-running service above is enough.

## Restore

The restore will **DROP and recreate** the target database objects
(`pg_restore --clean --if-exists`), so it requires explicit confirmation
(interactive y/N, or `--yes`).

```bash
# In an environment that can connect to the database (DATABASE_URL points at the target database)
python -m scripts.ops.restore_db --file backups/<name>.dump --verbose
# Skip the interactive confirmation (for automation):
python -m scripts.ops.restore_db --file backups/<name>.dump --yes
# Skip the checksum verification (not recommended): --no-verify
```

In a compose production environment, you can run it inside any application container:

```bash
docker compose exec webhook-service python3 -m scripts.ops.restore_db \
  --file /backups/<name>.dump --yes --verbose
```

## Restore Drill (recommended quarterly)

Only regular drills can guarantee that a backup is truly recoverable. Recommended
process (on a **non-production** database):

1. Take the latest backup and check the checksum:
   `python -m scripts.ops.backup_db --verify backups`
2. Prepare an empty drill database (a separate `DATABASE_URL`, **never point it at production**).
3. Restore: `python -m scripts.ops.restore_db --file backups/<name>.dump --yes --verbose`
4. Verify: connect to the drill database and confirm that key table row counts are
   reasonable, `alembic_version` is at the latest revision, and
   `SELECT count(*) FROM webhook_events;` and similar return the expected counts.
5. Record the drill time and results (restore duration, data integrity) as the
   basis for RTO/RPO.

## Notes

- Backups are custom-format and can only be restored with `pg_restore` (not `psql < file`).
- Restore is a destructive operation: always confirm that `DATABASE_URL` points at the correct target database.
- Before a production restore, stop writes first (stop api/worker) or perform it during a maintenance window to avoid write conflicts during the restore.
- When backups are not in the cloud, a host disk failure loses both the database and the backups; configuring `AWS_BUCKET`
  to send backups out to object storage is true disaster recovery.
