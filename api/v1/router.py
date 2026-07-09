"""Router aggregation for v1 business APIs."""

from fastapi import APIRouter, Depends

from api.v1.admin import admin_router
from api.v1.ai_usage import ai_usage_router
from api.v1.decision_trace import decision_trace_router
from api.v1.deep_analysis import deep_analysis_router
from api.v1.forwarding import forwarding_router
from api.v1.incidents import incidents_router
from api.v1.reanalysis import reanalysis_router
from api.v1.sandbox import sandbox_router
from api.v1.silences import silences_router
from api.v1.webhook import webhook_router
from core.auth import verify_api_key
from core.webhook_security import check_admin_rate_limit_dep

v1_router = APIRouter(prefix="/v1")

# Order matters: the per-IP admin rate limit runs BEFORE verify_api_key so
# failed-auth (brute-force) attempts are counted, not rejected before the
# limiter sees them. Opt-in via ADMIN_API_RATE_LIMIT_PER_MINUTE.
_admin_api_deps = [Depends(check_admin_rate_limit_dep), Depends(verify_api_key)]

v1_router.include_router(deep_analysis_router, dependencies=_admin_api_deps)
v1_router.include_router(reanalysis_router, dependencies=_admin_api_deps)
v1_router.include_router(ai_usage_router, dependencies=_admin_api_deps)
v1_router.include_router(decision_trace_router, dependencies=_admin_api_deps)
v1_router.include_router(forwarding_router, dependencies=_admin_api_deps)
v1_router.include_router(silences_router, dependencies=_admin_api_deps)
v1_router.include_router(sandbox_router, dependencies=_admin_api_deps)
v1_router.include_router(incidents_router, dependencies=_admin_api_deps)
v1_router.include_router(admin_router, dependencies=_admin_api_deps)
v1_router.include_router(webhook_router)
