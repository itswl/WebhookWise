"""Non-versioned dashboard page routes."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

dashboard_router = APIRouter()


@dashboard_router.get("/")
@dashboard_router.get("/dashboard")
async def dashboard() -> FileResponse:
    """Return the Dashboard page."""
    return FileResponse("templates/dashboard.html")
