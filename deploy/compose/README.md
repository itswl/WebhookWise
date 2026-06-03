# Docker Compose

通用 Compose 编排文件放在这里；命令仍从仓库根目录执行。

所有命令都显式带：

- `-p webhookwise`：固定 Compose project 名，避免文件移动后变成 `compose` project，也方便继续管理线上已有容器。
- `--env-file .env`：Compose 文件在 `deploy/compose/` 下，显式指定仓库根目录 `.env`，避免 `DATABASE_URL`、`REDIS_URL`、`API_KEY` 等变量被解析为空。

## 本地完整栈

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml up -d --build
```

## 仅应用服务

适用于 `.env` 中的 `DATABASE_URL` / `REDIS_URL` 指向云数据库或托管 Redis 的场景。

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.yml up -d --build
```

## 本地观测栈

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml up -d --build
```

查看线上完整栈状态：

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml ps -a
```

`Dockerfile` 仍保留在仓库根目录，避免破坏默认的 `docker build .` 和镜像构建平台约定。
