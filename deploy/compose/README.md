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

Every host port these files publish binds to `127.0.0.1` by default (`API_BIND_ADDRESS` and `OBSERVABILITY_BIND_ADDRESS` in `.env`): the reference host fronts the API with a reverse proxy and the containers talk to each other over the Compose networks, so set a variable to `0.0.0.0` only on a bare host whose firewall you own.

## Data persistence

Redis and PostgreSQL persist to named volumes (`webhookwise_redis_data`,
`webhookwise_postgres_data`) that survive `docker compose down` (use `-v` to delete
them). Redis runs with AOF enabled (`--appendonly yes --appendfsync everysec`), so a
crash loses at most ~1s of writes; set `--appendfsync always` for a per-write fsync.
Redis is capped at `--maxmemory 192mb` with `--maxmemory-policy noeviction` (below the
256MB container limit) so writes fail loudly rather than the container being OOM-killed
or silently evicting queued Stream entries.

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

The default stack keeps only Alloy, Prometheus, Alertmanager, Loki, and Grafana running:

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml up -d --build
```

Enable the diagnostics profile temporarily when traces, continuous profiles, or eBPF signals are needed. It adds Tempo, Pyroscope, and Beyla:
Beyla attaches to the app container's PID namespace — after a deploy recreates the app container, restart Beyla (`docker compose --profile diagnostics restart beyla`) or it silently stops observing.

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml --profile diagnostics up -d --build
```

Check the observability stack status:

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml ps -a
```

The `Dockerfile` remains in the repository root to avoid breaking the default `docker build .` and the image build platform conventions.
