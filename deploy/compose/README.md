# Docker Compose

The general Compose orchestration files live here; commands are still run from the repository root.

The everyday business stack uses the root `compose.yaml`:

```bash
docker compose up -d --build
docker compose ps
docker compose logs worker -f
```

The root `compose.yaml` fixes the project name to `webhookwise` and includes the infra + app orchestration files in this directory. By default it only manages the business containers such as PostgreSQL, Redis, API, Worker, and Scheduler.

When using the business Compose files in this directory directly, the command needs to explicitly include:

- `-p webhookwise`: fixes the Compose project name to avoid it becoming the `compose` project after the files are moved, and makes it easy to keep managing existing production containers.
- `--env-file .env`: since the Compose files are under `deploy/compose/`, explicitly specify the repository root `.env` to avoid variables such as `DATABASE_URL`, `REDIS_URL`, and `API_KEY` being resolved as empty.

The observability stack uses the separate project `webhookwise-observability` and joins the `webhookwise_webhook_net` network created by the business stack. This way `docker compose ps` from the repository root only shows the business containers.

## Alternative: Start the Business Stack Bypassing the Root Entry Point

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml up -d --build
```

## Application Services Only

Suitable for scenarios where `DATABASE_URL` / `REDIS_URL` in `.env` point at a cloud database or managed Redis.

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.yml up -d --build
```

## Local Observability Stack

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml up -d --build
```

Check the observability stack status:

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml ps -a
```

The `Dockerfile` remains in the repository root to avoid breaking the default `docker build .` and the image build platform conventions.
