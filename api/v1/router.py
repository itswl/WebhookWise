"""Router aggregation for v1 business APIs."""

from fastapi import APIRouter, Depends

from api.v1.admin import admin_router
from api.v1.ai_usage import ai_usage_router
from api.v1.deep_analysis import deep_analysis_router
from api.v1.forwarding import forwarding_router
from api.v1.reanalysis import reanalysis_router
from api.v1.webhook import webhook_router
from core.auth import verify_api_key

v1_router = APIRouter(prefix="/v1")

v1_router.include_router(deep_analysis_router, dependencies=[Depends(verify_api_key)])
v1_router.include_router(reanalysis_router, dependencies=[Depends(verify_api_key)])
v1_router.include_router(ai_usage_router, dependencies=[Depends(verify_api_key)])
v1_router.include_router(forwarding_router, dependencies=[Depends(verify_api_key)])
v1_router.include_router(admin_router, dependencies=[Depends(verify_api_key)])
v1_router.include_router(webhook_router)
