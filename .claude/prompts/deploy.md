# Deploy to Production

Push latest code and deploy to `imwl@138.2.25.190:/opt/docker-compose/WebhookWise`.

## Steps

1. **Run full CI gate locally first:**
   ```bash
   ruff check . && mypy && python -m compileall -q . -x .venv && pytest -q --ignore=tests/api/test_mcp_server.py --cov=core --cov=api --cov=services --cov=models --cov=adapters --cov=db --cov=contracts --cov-branch --cov-fail-under=85
   ```

2. **Commit and push:**
   ```bash
   git add -A && git commit -m "<message>" && git push origin main
   ```

3. **Wait for GitHub Actions CI** — check status at https://github.com/itswl/WebhookWise/actions or run:
   ```bash
   gh run watch $(gh run list -w ci -L 1 --json databaseId -q '.[].databaseId') --exit-status
   ```

4. **Deploy to production (only after CI passes):**
   ```bash
   ssh imwl@138.2.25.190 "cd /opt/docker-compose/WebhookWise && git pull origin main && cd /opt/docker-compose && sudo docker compose -f WebhookWise/compose.yaml build webhook-service worker && sudo docker compose -f WebhookWise/compose.yaml up -d --no-deps --force-recreate webhook-service worker && sleep 3 && sudo docker compose -f WebhookWise/compose.yaml exec -T webhook-service alembic upgrade head"
   ```

5. **Verify:**
   ```bash
   ssh imwl@138.2.25.190 "curl -s http://localhost:8000/ready"
   ```

## Key paths
- **Repo:** `itswl/WebhookWise` (main branch)
- **Server:** `imwl@138.2.25.190`
- **Deploy dir:** `/opt/docker-compose/WebhookWise`
- **Compose file:** `/opt/docker-compose/WebhookWise/compose.yaml` (includes `deploy/compose/*.yml`)
- **Containers:** `webhook-receiver`, `webhook-scheduler`, `webhookwise-worker-1`
- **DB:** PostgreSQL via `webhook-postgres` (user: `webhook_user`, db: `webhooks`)

## Notes
- `webhook-service` and `worker` containers need rebuild on code changes; `postgres` and `redis` do not
- The `migrate` container runs `alembic upgrade head` automatically on `up`; if it fails (e.g. DB already migrated manually), use `--no-deps --force-recreate` to skip it
- Use `sudo docker compose exec -T webhook-service alembic upgrade head` to run migrations manually if the migrate container fails
- To reset incidents and re-group: `sudo docker exec webhook-postgres psql -U webhook_user -d webhooks -h localhost -c "DELETE FROM incidents;"` then `sudo docker compose restart worker`
