# 数据库备份与恢复

WebhookWise 用 PostgreSQL custom-format 备份(`pg_dump -Fc`),每个 `.dump` 旁
写一个同名 `.dump.sha256` 校验文件。备份与恢复脚本都依赖镜像内的
`postgresql-client`(pg_dump/pg_restore)。

## 备份

### 手动一次性备份

```bash
python -m scripts.ops.backup_db --verbose          # 生成一份备份并清理过期备份
python -m scripts.ops.backup_db --verify backups   # 校验目录内所有备份
python -m scripts.ops.backup_db --cleanup-only     # 只按保留期清理
```

配置见 `.env.example.all` 的 DB Backup 区段(`BACKUP_DIR` / `BACKUP_RETENTION_DAYS`
/ `BACKUP_COMPRESS`;配置 `AWS_BUCKET` 后会上传到 S3 兼容存储)。

### 定时备份(Docker Compose)

`deploy/compose/docker-compose.yml` 提供一个 `backup` 服务(profile=backup),
按 `BACKUP_INTERVAL_SECONDS`(默认 86400=每天)循环跑备份,写到宿主机
`BACKUP_HOST_DIR`(默认 `./backups`,挂载到容器 `/backups`)。

```bash
# 启用定时备份服务(默认不启动,需显式带 profile)
docker compose --profile backup up -d backup
docker compose logs -f backup        # 看备份运行日志
ls -lh backups/                      # 宿主机上的备份文件
```

> Kubernetes 部署请改用 CronJob 调度 `python -m scripts.ops.backup_db`(镜像已含
> pg client);单机 compose 用上面的常驻服务即可。

## 恢复

恢复会 **DROP 并重建** 目标库对象(`pg_restore --clean --if-exists`),因此需要
显式确认(交互式 y/N,或 `--yes`)。

```bash
# 在能连到数据库的环境里(DATABASE_URL 指向目标库)
python -m scripts.ops.restore_db --file backups/<name>.dump --verbose
# 跳过交互确认(自动化场景):
python -m scripts.ops.restore_db --file backups/<name>.dump --yes
# 跳过 checksum 校验(不建议):--no-verify
```

在 compose 线上环境里可在任一应用容器内执行:

```bash
docker compose exec webhook-service python3 -m scripts.ops.restore_db \
  --file /backups/<name>.dump --yes --verbose
```

## 恢复演练(建议每季度做一次)

定期演练才能保证备份真的可恢复。推荐流程(在**非生产**库上做):

1. 取最新备份并核对 checksum:
   `python -m scripts.ops.backup_db --verify backups`
2. 准备一个空的演练库(单独的 `DATABASE_URL`,**绝不要指向生产**)。
3. 恢复:`python -m scripts.ops.restore_db --file backups/<name>.dump --yes --verbose`
4. 验证:连上演练库,确认关键表行数合理、`alembic_version` 为最新版本、
   `SELECT count(*) FROM webhook_events;` 等返回预期数量。
5. 记录演练时间与结果(恢复耗时、数据完整性),作为 RTO/RPO 依据。

## 注意事项

- 备份是 custom-format,只能用 `pg_restore`(不是 `psql < file`)恢复。
- 恢复是破坏性操作:务必确认 `DATABASE_URL` 指向正确的目标库。
- 生产恢复前先停写(停 api/worker)或在维护窗口进行,避免恢复期间的写入冲突。
- 备份未上云时,宿主机磁盘故障会同时丢失数据库和备份;配置 `AWS_BUCKET`
  把备份外发到对象存储是真正的容灾。
