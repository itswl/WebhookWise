"""Non-versioned dashboard page routes."""

from __future__ import annotations

import hashlib
import json
import re
from functools import cache, lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

dashboard_router = APIRouter()

_TEMPLATES_DIR = Path("templates")
_DASHBOARD_HTML = _TEMPLATES_DIR / "dashboard.html"

# Short content hash length. Long enough to make collisions on a redeploy
# effectively impossible while keeping URLs readable.
_HASH_LEN = 12

# Assets loaded only at runtime (injected by JS, so they are not present as
# <script>/<link> tags the HTML rewrite can see). Their versions are published
# to the page so the client can build the same content-hashed URLs.
_RUNTIME_VERSIONED_ASSETS = ("i18n.en.js", "i18n.zh.js")

# A /static/... asset reference with an optional pre-existing ?v=... suffix.
_STATIC_REF_RE = re.compile(r'(/static/[^"?\s]+)(?:\?v=[^"\s]*)?')

# The dashboard HTML is the cache-busting entry point: it references every JS/CSS
# asset with a ?v=<content-hash> query string (injected here at render time), so
# the assets themselves are served immutable and cached hard (see api/app.py).
# The document itself must always revalidate — otherwise a heuristically-cached
# stale HTML keeps pointing at old hashes and a redeploy never reaches the user —
# so it is served no-cache (cheap: it is a small file).
_DASHBOARD_HEADERS = {"Cache-Control": "no-cache"}


@cache
def _asset_version(static_path: str) -> str:
    """Return a short content hash for a "/static/..." asset, cached per process."""
    file_path = _TEMPLATES_DIR / static_path.lstrip("/")
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return digest[:_HASH_LEN]


def _version_static_refs(html: str) -> str:
    """Rewrite every /static asset reference to carry a ?v=<content-hash>."""

    def repl(match: re.Match[str]) -> str:
        path = match.group(1)
        try:
            return f"{path}?v={_asset_version(path)}"
        except OSError:
            # Leave references to non-existent files untouched rather than fail
            # the whole page render.
            return match.group(0)

    return _STATIC_REF_RE.sub(repl, html)


def _asset_versions_attr() -> str:
    """Build the <body> data attribute publishing runtime-loaded asset versions."""
    versions = {name: _asset_version(f"/static/js/{name}") for name in _RUNTIME_VERSIONED_ASSETS}
    # Single-quoted attribute wrapping JSON (double quotes); hashes/filenames
    # contain no quotes or HTML metacharacters, so this needs no escaping.
    return f"data-asset-versions='{json.dumps(versions, separators=(',', ':'))}'"


@lru_cache(maxsize=1)
def _rendered_dashboard() -> str:
    """Render the dashboard HTML with content-hash-versioned asset references.

    Cached per process: assets are immutable within a running deploy, and a
    redeploy restarts the process (re-reading the files and recomputing hashes).
    """
    html = _DASHBOARD_HTML.read_text(encoding="utf-8")
    html = html.replace("<body>", f"<body {_asset_versions_attr()}>", 1)
    return _version_static_refs(html)


@dashboard_router.get("/")
@dashboard_router.get("/dashboard")
async def dashboard() -> HTMLResponse:
    """Return the dashboard page with content-hash-versioned asset references."""
    return HTMLResponse(content=_rendered_dashboard(), headers=_DASHBOARD_HEADERS)


class ImmutableStaticFiles(StaticFiles):
    """StaticFiles that lets browsers cache assets hard.

    Every asset is referenced with a ?v=<content-hash> query string (injected by
    the dashboard render above), so a changed file gets a new URL. That makes it
    safe to serve the bytes as immutable for a year: browsers reuse them from
    cache with no revalidation round-trip until the hash — and thus the URL —
    changes. Applied only to successful file/not-modified responses so errors are
    not cached.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
