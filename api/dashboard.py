"""Non-versioned dashboard page routes."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

dashboard_router = APIRouter()

# The dashboard HTML is the cache-busting entry point: it references every JS/CSS
# asset with a ?v=... query string, so the assets themselves can be cached hard.
# But if the browser also heuristically caches the HTML (which it does when no
# Cache-Control is set), a stale HTML keeps pointing at the old ?v= and the
# user never picks up a new bundle until a manual hard-refresh. Force the
# document to always revalidate (cheap: it's a small file) so a redeploy is
# reflected on the next normal navigation.
_DASHBOARD_HEADERS = {"Cache-Control": "no-cache"}


@dashboard_router.get("/")
@dashboard_router.get("/dashboard")
async def dashboard() -> FileResponse:
    """Return the Dashboard page."""
    return FileResponse("templates/dashboard.html", headers=_DASHBOARD_HEADERS)
