"""WebhookWise Lite — the core idea in one process, one file's worth of routes.

Receive an alert, decide whether it deserves a human, deliver it if so, and
record why either way. Everything else the full edition does is an elaboration
of that sentence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from lite import channels, pipeline
from lite.dashboard import DASHBOARD_HTML
from lite.settings import settings
from lite.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("webhookwise.lite")

store = Store(settings.db_path)


async def _deliver_once(client: httpx.AsyncClient) -> None:
    for record in await store.due_deliveries():
        payload = json.loads(record["payload"])
        try:
            await channels.send(client, str(record["target_kind"]), str(record["target_url"]), payload)
        except Exception as e:  # noqa: BLE001 - a bad target must never kill the loop
            status = await store.mark_failed(
                int(record["id"]), f"{type(e).__name__}: {e}", settings.outbox_backoff_seconds
            )
            logger.warning(
                "delivery failed id=%s rule=%s status=%s error=%s", record["id"], record["rule_name"], status, e
            )
        else:
            await store.mark_sent(int(record["id"]))
            logger.info("delivered id=%s rule=%s", record["id"], record["rule_name"])


async def _outbox_loop(client: httpx.AsyncClient) -> None:
    while True:
        try:
            await _deliver_once(client)
        except Exception as e:  # noqa: BLE001 - the loop outlives any single failure
            logger.warning("outbox loop error: %s", e)
        await asyncio.sleep(settings.outbox_poll_seconds)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await store.open()
    client = httpx.AsyncClient(follow_redirects=False)
    app.state.client = client
    task = asyncio.create_task(_outbox_loop(client), name="outbox")
    logger.info("WebhookWise Lite ready db=%s ai=%s", settings.db_path, bool(settings.openai_api_key))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await client.aclose()
        await store.close()


# With a read token set this is no longer a laptop toy: hide the API map too.
app = FastAPI(
    title="WebhookWise Lite",
    lifespan=lifespan,
    docs_url=None if settings.read_token else "/docs",
    redoc_url=None,
    openapi_url=None if settings.read_token else "/openapi.json",
)


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")


def require_read(x_read_token: str = Header(default="")) -> None:
    """Reads carry alert content verbatim; token-gate them when configured.

    /health stays open on purpose: the container healthcheck curls it, and its
    status/outbox counters carry no alert content. The admin token also passes,
    so an operator with write power is never locked out of reading.
    """
    if not settings.read_token:
        return
    if x_read_token in (settings.read_token, settings.admin_token) and x_read_token:
        return
    raise HTTPException(status_code=401, detail="invalid read token")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "outbox": await store.outbox_summary()}


@app.post("/webhook/{source}")
async def ingest(
    source: str,
    request: Request,
    x_ingest_token: str = Header(default=""),
    x_hook_correlation_id: str = Header(default=""),
) -> JSONResponse:
    if settings.ingest_token and x_ingest_token != settings.ingest_token:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - accept non-JSON senders rather than dropping them
        payload = {"body": (await request.body()).decode("utf-8", "replace")}

    # A relay in front of us stamps this so the work we send back can be
    # gathered under the alert it came from. Quoting it back is the whole
    # contract — three lines here buy an end-to-end view over there.
    result = await pipeline.process(
        store,
        request.app.state.client,
        settings,
        source[:100],
        payload,
        correlation_id=x_hook_correlation_id.strip()[:120],
    )
    logger.info("ingest source=%s outcome=%s skip=%s", source, result["outcome"], result["skip_code"])
    return JSONResponse(result, status_code=202)


@app.get("/api/decisions", dependencies=[Depends(require_read)])
async def api_decisions(limit: int = 50) -> dict[str, Any]:
    return {"decisions": await store.list_decisions(min(max(limit, 1), 200))}


@app.get("/api/stats", dependencies=[Depends(require_read)])
async def api_stats() -> dict[str, Any]:
    return {"last24h": await store.decision_stats(), "outbox": await store.outbox_summary()}


@app.get("/api/rules", dependencies=[Depends(require_read)])
async def api_rules() -> dict[str, Any]:
    return {"rules": await store.active_rules()}


@app.post("/api/rules", dependencies=[Depends(require_admin)])
async def api_add_rule(rule: dict[str, Any]) -> dict[str, Any]:
    if not rule.get("name") or not rule.get("target_url"):
        raise HTTPException(status_code=400, detail="name and target_url are required")
    if str(rule.get("target_kind", "feishu")) not in ("feishu", "generic"):
        raise HTTPException(status_code=400, detail="target_kind must be feishu or generic")
    try:
        int(rule.get("priority", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="priority must be an integer") from None
    return {"id": await store.add_rule(rule)}


@app.delete("/api/rules/{rule_id}", dependencies=[Depends(require_admin)])
async def api_delete_rule(rule_id: int) -> dict[str, Any]:
    return {"deleted": await store.delete_rule(rule_id)}


@app.get("/api/silences", dependencies=[Depends(require_read)])
async def api_silences() -> dict[str, Any]:
    return {"silences": await store.active_silences()}


@app.post("/api/silences", dependencies=[Depends(require_admin)])
async def api_add_silence(silence: dict[str, Any]) -> dict[str, Any]:
    pattern = str(silence.get("pattern") or "").strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    minutes = int(silence.get("minutes") or 60)
    return {"id": await store.add_silence(pattern, minutes, str(silence.get("reason") or ""))}


@app.delete("/api/silences/{silence_id}", dependencies=[Depends(require_admin)])
async def api_delete_silence(silence_id: int) -> dict[str, Any]:
    return {"deleted": await store.delete_silence(silence_id)}


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)
