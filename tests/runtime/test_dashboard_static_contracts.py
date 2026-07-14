from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest

from tests.helpers.paths import PROJECT_ROOT


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "script" and attr_map.get("src"):
            self.assets.append(attr_map["src"] or "")
        if tag == "link" and attr_map.get("href"):
            self.assets.append(attr_map["href"] or "")


def _dashboard_html() -> str:
    return (PROJECT_ROOT / "templates/dashboard.html").read_text()


def _static_js(name: str) -> str:
    return (PROJECT_ROOT / "templates/static/js" / name).read_text()


def _static_css(name: str) -> str:
    return (PROJECT_ROOT / "templates/static/css" / name).read_text()


def test_dashboard_references_existing_static_assets_in_order() -> None:
    parser = _AssetParser()
    parser.feed(_dashboard_html())

    static_assets = [asset for asset in parser.assets if asset.startswith("/static/")]
    assert static_assets
    # Compare on the path component so cache-busting query strings (?v=...) don't
    # break the load-order contract.
    asset_paths = [urlsplit(asset).path for asset in static_assets]
    assert asset_paths.index("/static/js/utils.js") < asset_paths.index("/static/js/api.js")
    assert asset_paths.index("/static/js/api.js") < asset_paths.index("/static/js/alerts.js")

    missing = []
    for asset in static_assets:
        path = urlsplit(asset).path.removeprefix("/")
        if not (PROJECT_ROOT / "templates" / path).is_file():
            missing.append(asset)
    assert missing == []


def test_dashboard_tabs_have_matching_content_panels() -> None:
    html = _dashboard_html()
    tabs = set(re.findall(r'data-tab="([^"]+)"', html))
    panels = set(re.findall(r'id="([^"]+Tab)"', html))

    # The navbar is down to 4 tabs. Forwarding analytics (Overview / Decision
    # Trace / AI Cost) are sub-views of the landing tab (data-tab="decision-trace",
    # labelled "Overview"); Forward Rules / Silences / Sandbox are sub-views of the
    # Routing tab. So the standalone overview / ai-cost / outbox / forward-rules /
    # silences / sandbox tabs no longer exist.
    assert {"alerts", "decision-trace", "routing", "operations"} <= tabs
    assert {
        "alertsTab",
        "decisionTraceTab",
        "routingTab",
        "operationsTab",
        "noiseCenterTab",
        "actionCenterTab",
    } <= panels
    assert {
        "overview",
        "ai-cost",
        "outbox",
        "forward-rules",
        "silences",
        "sandbox",
        "incidents",
        "deep-analyses",
    }.isdisjoint(tabs)
    assert {"overviewTab", "aiCostTab", "outboxTab", "forwardRulesTab", "silencesTab", "sandboxTab"}.isdisjoint(panels)
    # The landing tab's forwarding-analytics sub-views (Overview | Decision Trace | AI Cost).
    dt_views = set(re.findall(r'data-dt-view="([^"]+)"', html))
    assert {"overview", "trace", "cost"} <= dt_views
    inbox_views = set(re.findall(r'data-inbox-view="([^"]+)"', html))
    assert {"alerts", "incidents", "investigations"} <= inbox_views
    # Sandbox and Audit remain secondary tools opened from the Rules view.
    routing_views = set(re.findall(r'data-routing-view="([^"]+)"', html))
    assert {"rules", "silences"} <= routing_views
    operations_views = set(re.findall(r'data-operations-view="([^"]+)"', html))
    assert {"actions", "noise"} <= operations_views


@pytest.mark.asyncio
async def test_static_assets_are_content_hash_versioned() -> None:
    # Cache-busting versions are content hashes injected at render time (not
    # hand-edited date strings). Every /static reference in the rendered page
    # must carry ?v=<hash> matching the file's actual content hash, so a changed
    # asset always gets a fresh URL under the immutable cache policy and no
    # manual version bump (or matching test edit) is ever needed again.
    import json

    import httpx

    from api.app import app
    from api.dashboard import _asset_version

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")
        assert response.status_code == 200
        html = response.text

    refs = re.findall(r'/static/[^"?\s]+\?v=[0-9a-f]+', html)
    assert refs, "expected content-hash-versioned /static references in the rendered dashboard"
    for ref in refs:
        path, _, version = ref.partition("?v=")
        assert version == _asset_version(path), f"version for {path} does not match its content hash"

    # faro.js previously shipped with no ?v=; it must now be versioned like the rest.
    assert any(ref.startswith("/static/js/faro.js?v=") for ref in refs)

    # Runtime-loaded dictionaries are versioned via the <body> data attribute
    # (a CSP-safe manifest the i18n loader reads).
    manifest_match = re.search(r"data-asset-versions='([^']+)'", html)
    assert manifest_match, "expected a data-asset-versions manifest on <body>"
    manifest = json.loads(manifest_match.group(1))
    assert manifest["i18n.en.js"] == _asset_version("/static/js/i18n.en.js")
    assert manifest["i18n.zh.js"] == _asset_version("/static/js/i18n.zh.js")


@pytest.mark.asyncio
async def test_static_assets_are_served_immutable() -> None:
    # The ?v=<hash> scheme makes it safe to cache asset bytes hard; without a
    # long-lived immutable Cache-Control the query strings buy nothing (every
    # navigation revalidates). Lock the immutable policy on the /static mount.
    import httpx

    from api.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/static/js/i18n.js")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_i18n_dictionaries_are_split_per_language() -> None:
    # The dictionaries live in per-language files so the dashboard downloads only
    # the active language on first paint; the core reads the shared global they
    # register onto and lazy-loads the other on toggle.
    core = _static_js("i18n.js")
    en = _static_js("i18n.en.js")
    zh = _static_js("i18n.zh.js")

    assert "var DICT = (window.__WW_I18N_DICT__" in core
    assert "function ensureDict(" in core
    assert "ready: ready" in core
    # The core must no longer inline the dictionaries.
    assert "\n        en: {" not in core
    assert "\n        zh: {" not in core

    assert "DICT.en = {" in en
    assert "DICT.zh = {" in zh
    # Representative keys survive the split byte-identically in each language.
    assert "'nav.title': 'Webhook Monitor'" in en
    assert "'nav.title': 'Webhook 监控'" in zh


def test_dashboard_auto_refresh_intervals_are_operator_friendly() -> None:
    assert "DASHBOARD_AUTO_REFRESH_INTERVAL_MS = 60000" in _static_js("dashboard.js")
    assert "DEEP_ANALYSES_AUTO_REFRESH_INTERVAL_MS = 60000" in _static_js("deep-analyses.js")


def test_alert_cards_prioritize_summary_and_protect_action_controls() -> None:
    html = _dashboard_html()
    alerts_js = _static_js("alerts.js")
    api_js = _static_js("api.js")
    css = _static_css("components.css")

    assert "alert-card-top" in alerts_js
    assert "alerts.summaryUnavailable" in alerts_js
    assert "alert-action-menu" in alerts_js
    assert "alert-secondary-actions" in alerts_js
    assert "_pendingActions: new Set()" in alerts_js
    assert "button.classList.add('is-busy')" in alerts_js
    assert "parseJsonResponse(response)" in api_js
    assert ".alert-card-top" in css
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert ".alert-action-menu[open]" in css
    assert 'id="confirmForwardBtn"' in html


@pytest.mark.asyncio
async def test_dashboard_html_is_served_with_no_cache() -> None:
    # The HTML is the cache-busting entry point (it carries the ?v= asset refs),
    # so it must always revalidate; otherwise a heuristically-cached stale HTML
    # keeps pointing at an old bundle and a redeploy never reaches the user.
    import httpx

    from api.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for path in ("/", "/dashboard"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers.get("cache-control") == "no-cache"


def test_deep_analysis_formats_json_like_reports_as_structured_content() -> None:
    js = _static_js("deep-analyses.js")
    css = _static_css("components.css")

    assert "DEEP_ANALYSIS_REPORT_SCHEMA = 'deep_analysis_report.v1'" in js
    assert "record.normalized_report" in js
    assert "renderNormalizedReport(report)" in js
    assert "parseJsonLikeText" not in js
    assert "stripMarkdownJsonFence" not in js
    assert ".da-report-strip" in css
    assert ".da-empty-report" in css
    assert ".da-json-block" not in css
