"""Router aggregation for v1 business APIs."""

from fastapi import APIRouter, Depends

from api.v1.activity import activity_router
from api.v1.admin import admin_router
from api.v1.admin_config import admin_config_router
from api.v1.ai_usage import ai_usage_router
from api.v1.alert_quality import alert_quality_router
from api.v1.changes import changes_router
from api.v1.decision_trace import decision_trace_router
from api.v1.deep_analysis import deep_analysis_router
from api.v1.feishu_actions import feishu_actions_router
from api.v1.forwarding import forwarding_router
from api.v1.inbound_rules import inbound_rules_router
from api.v1.incidents import incidents_router
from api.v1.onboarding import onboarding_router, source_ingress_router
from api.v1.operations import operations_router
from api.v1.reanalysis import reanalysis_router
from api.v1.response_center import response_center_router
from api.v1.runtime_settings import runtime_settings_router
from api.v1.sandbox import sandbox_router
from api.v1.services import services_router
from api.v1.silences import silences_router
from api.v1.webhook import webhook_router
from core.auth import verify_api_key
from core.webhook_security import check_admin_rate_limit_dep

v1_router = APIRouter(prefix="/v1")

# Order matters: the per-IP admin rate limit runs BEFORE verify_api_key so
# failed-auth (brute-force) attempts are counted, not rejected before the
# limiter sees them. Set ADMIN_API_RATE_LIMIT_PER_MINUTE=0 to disable it.
_admin_api_deps = [Depends(check_admin_rate_limit_dep), Depends(verify_api_key)]

# Tags group the ~120 operations in Swagger UI / ReDoc / generated clients;
# without them every consumer saw one flat undifferentiated list.
v1_router.include_router(deep_analysis_router, dependencies=_admin_api_deps, tags=["Deep Analysis"])
v1_router.include_router(reanalysis_router, dependencies=_admin_api_deps, tags=["Re-analysis"])
v1_router.include_router(ai_usage_router, dependencies=_admin_api_deps, tags=["AI Usage"])
v1_router.include_router(decision_trace_router, dependencies=_admin_api_deps, tags=["Decision Traces"])
v1_router.include_router(forwarding_router, dependencies=_admin_api_deps, tags=["Forward Rules"])
v1_router.include_router(inbound_rules_router, dependencies=_admin_api_deps, tags=["Inbound Rules"])
v1_router.include_router(silences_router, dependencies=_admin_api_deps, tags=["Silences"])
v1_router.include_router(sandbox_router, dependencies=_admin_api_deps, tags=["Sandbox"])
v1_router.include_router(incidents_router, dependencies=_admin_api_deps, tags=["Incidents"])
v1_router.include_router(response_center_router, dependencies=_admin_api_deps, tags=["Response Center"])
v1_router.include_router(alert_quality_router, dependencies=_admin_api_deps, tags=["Alert Quality"])
v1_router.include_router(services_router, dependencies=_admin_api_deps, tags=["Service Profiles"])
v1_router.include_router(feishu_actions_router, tags=["Feishu Integration"])
v1_router.include_router(changes_router, tags=["Change Events"])
v1_router.include_router(activity_router, dependencies=_admin_api_deps, tags=["Activity"])
v1_router.include_router(operations_router, dependencies=_admin_api_deps, tags=["Operations"])
v1_router.include_router(admin_router, dependencies=_admin_api_deps, tags=["Admin"])
v1_router.include_router(admin_config_router, dependencies=_admin_api_deps, tags=["Admin Config"])
v1_router.include_router(runtime_settings_router, dependencies=_admin_api_deps, tags=["Runtime Settings"])
v1_router.include_router(onboarding_router, dependencies=_admin_api_deps, tags=["Source Onboarding"])
v1_router.include_router(source_ingress_router, tags=["Source Ingress"])
v1_router.include_router(webhook_router, tags=["Webhooks"])
