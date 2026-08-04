"""Admin content/config routes: KB corpus, config bundles, adoption ledger.

Split from api/v1/admin.py purely for file size; paths and auth dependencies
are unchanged.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.auth import verify_admin_write, verify_api_key
from core.logger import get_logger
from db.session import get_db_session
from schemas.admin import ConfigImportRequest, KBDocumentRequest
from services.operations.audit_logger import add_audit

logger = get_logger("api.v1.admin_config")

admin_config_router = APIRouter()

_ADMIN_RUNTIME_ERRORS = (OSError, RuntimeError, TimeoutError)


@admin_config_router.post(
    "/admin/kb/documents",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def ingest_kb_document_endpoint(
    request: KBDocumentRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Ingest one knowledge-base document: chunk + embed + upsert (idempotent)."""
    from services.kb.store import ingest_document

    try:
        result = await ingest_document(
            session,
            title=request.title,
            content=request.content,
            source_ref=request.source_ref,
            tags=request.tags,
        )
        await session.commit()
        logger.info(
            "[Admin] KB ingest title=%s chunks=%d model=%s", result.title, result.chunks, result.embedding_model
        )
        return ok_response(
            http_status=200,
            message="document ingested",
            data={"title": result.title, "chunks": result.chunks, "embedding_model": result.embedding_model},
        )
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("[Admin] KB ingest failed title=%s error=%s", request.title, e, exc_info=True)
        return internal_error_response()


@admin_config_router.get("/admin/kb/drafts", dependencies=[Depends(verify_api_key)])
async def list_kb_drafts_endpoint(
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """List KB drafts awaiting review (one row per sedimented document)."""
    from services.kb.incident_sediment import list_kb_drafts
    from services.operations.feature_adoption import record_feature_use

    await record_feature_use("view:kb_drafts")
    return ok_response(http_status=200, data=await list_kb_drafts(session))


@admin_config_router.get("/admin/kb/drafts/{source_ref:path}", dependencies=[Depends(verify_api_key)])
async def get_kb_draft_endpoint(
    source_ref: str,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Full ordered content of one KB draft — the material under review."""
    from services.kb.incident_sediment import get_kb_draft

    chunks = await get_kb_draft(session, source_ref)
    if not chunks:
        return fail_response("KB draft not found", 404)
    return ok_response(http_status=200, data={"source_ref": source_ref, "chunks": chunks})


class KbDraftUpdateRequest(BaseModel):
    """Operator-edited replacement text for a draft under review."""

    content: str = Field(min_length=1, max_length=200_000)


@admin_config_router.put("/admin/kb/drafts/{source_ref:path}", dependencies=[Depends(verify_admin_write)])
async def update_kb_draft_endpoint(
    source_ref: str,
    request: KbDraftUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Amend a draft before approval (re-chunked and re-embedded on save)."""
    from services.kb.incident_sediment import update_kb_draft

    chunks = await update_kb_draft(session, source_ref, request.content)
    if not chunks:
        return fail_response("KB draft not found", 404)
    await session.commit()
    from services.operations.feature_adoption import record_feature_use

    await record_feature_use("action:kb_draft_edited")
    logger.info("[Admin] KB draft edited source_ref=%s chunks=%d", source_ref, chunks)
    return ok_response(http_status=200, message="draft updated", data={"chunks": chunks})


@admin_config_router.post("/admin/kb/drafts/{source_ref:path}/publish", dependencies=[Depends(verify_admin_write)])
async def publish_kb_draft_endpoint(
    source_ref: str,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Publish a KB draft into the RAG corpus (operator approval)."""
    from services.kb.incident_sediment import publish_kb_draft

    published = await publish_kb_draft(session, source_ref)
    if not published:
        return fail_response("KB draft not found", 404)
    await session.commit()
    from services.operations.feature_adoption import record_feature_use

    await record_feature_use("action:kb_draft_published")
    logger.info("[Admin] KB draft published source_ref=%s chunks=%d", source_ref, published)
    return ok_response(http_status=200, message="draft published", data={"published_chunks": published})


@admin_config_router.delete("/admin/kb/drafts/{source_ref:path}", dependencies=[Depends(verify_admin_write)])
async def discard_kb_draft_endpoint(
    source_ref: str,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Discard a KB draft without publishing it."""
    from services.kb.incident_sediment import discard_kb_draft

    discarded = await discard_kb_draft(session, source_ref)
    if not discarded:
        return fail_response("KB draft not found", 404)
    await session.commit()
    from services.operations.feature_adoption import record_feature_use

    await record_feature_use("action:kb_draft_discarded")
    logger.info("[Admin] KB draft discarded source_ref=%s chunks=%d", source_ref, discarded)
    return ok_response(http_status=200, message="draft discarded", data={"discarded_chunks": discarded})


@admin_config_router.get("/admin/config/export", dependencies=[Depends(verify_admin_write)])
async def export_config_endpoint(session: AsyncSession = Depends(get_db_session)) -> Response:
    """Export forward rules + active silences + maintenance windows as YAML.

    Write-key-gated although it is a read: the bundle contains forwarding
    target URLs (bot tokens).
    """
    import yaml

    from services.operations.config_transfer import export_config
    from services.operations.feature_adoption import record_feature_use

    bundle = await export_config(session)
    await record_feature_use("action:config_exported")
    content = yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False)
    return Response(
        content=content,
        media_type="application/x-yaml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="webhookwise-config.yaml"'},
    )


@admin_config_router.post("/admin/config/import", dependencies=[Depends(verify_admin_write)])
async def import_config_endpoint(
    request: ConfigImportRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Upsert a previously exported YAML bundle (dry_run to preview).

    Additive only: creates or updates by natural key, never deletes. After a
    real import, the forward-rule and silence caches are invalidated and the
    maintenance-window sweep runs so an active window takes effect immediately.
    """
    import yaml

    from services.forwarding.rules import invalidate_forward_rules_cache, publish_rules_invalidation
    from services.operations.config_transfer import import_config
    from services.operations.feature_adoption import record_feature_use
    from services.silences.maintenance_windows import sweep_maintenance_windows
    from services.silences.store import invalidate_silences_cache, publish_silences_invalidation

    try:
        bundle = yaml.safe_load(request.content)
    except yaml.YAMLError as e:
        return fail_response(f"Invalid YAML: {e}", 400)
    from sqlalchemy.exc import DataError, IntegrityError

    try:
        report = await import_config(session, bundle, dry_run=request.dry_run)
    except ValueError as e:
        return fail_response(str(e), 400)
    except (DataError, IntegrityError) as e:
        # Entry validation should make this unreachable; if the DB still
        # rejects the bundle, reject the request rather than 500 — nothing was
        # applied (transaction rolls back).
        await session.rollback()
        logger.warning("[Admin] Config import rejected by the database: %s", e)
        return fail_response("bundle rejected by database constraints; nothing was applied", 400)

    if request.dry_run:
        await session.rollback()
        return ok_response(http_status=200, message="dry run — nothing applied", data=report)

    await sweep_maintenance_windows(session)
    add_audit(
        session,
        "config_bundle",
        0,
        "import",
        "imported",
        (
            f"Config import: rules +{report['forward_rules']['created']}/~{report['forward_rules']['updated']}, "
            f"windows +{report['maintenance_windows']['created']}/~{report['maintenance_windows']['updated']}, "
            f"silences +{report['silences']['created']}/~{report['silences']['updated']}"
        ),
    )
    await session.commit()
    invalidate_forward_rules_cache()
    await publish_rules_invalidation()
    invalidate_silences_cache()
    await publish_silences_invalidation()
    await record_feature_use("action:config_imported")
    logger.info("[Admin] Config bundle imported report=%s", report)
    return ok_response(http_status=200, message="config imported", data=report)


@admin_config_router.get("/admin/feature-adoption", dependencies=[Depends(verify_api_key)])
async def feature_adoption_endpoint() -> JSONResponse:
    """Monthly usage counters for recently shipped operator features.

    The observation-period instrument: after a release, this answers "which of
    the new features actually get used", so the next iteration can double down
    or delete. See services/operations/feature_adoption.py for semantics.
    """
    from services.operations.feature_adoption import get_feature_adoption

    return ok_response(http_status=200, data=await get_feature_adoption())
