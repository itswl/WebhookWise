# Docker Compose

通用 Compose 编排文件放在这里；命令仍从仓库根目录执行。

## 本地完整栈

```bash
docker compose -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml up -d --build
```

## 仅应用服务

适用于 `.env` 中的 `DATABASE_URL` / `REDIS_URL` 指向云数据库或托管 Redis 的场景。

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
```

## 本地观测栈

```bash
docker compose -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml up -d --build
```

`Dockerfile` 仍保留在仓库根目录，避免破坏默认的 `docker build .` 和镜像构建平台约定。
