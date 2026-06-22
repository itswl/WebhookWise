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

    # AI Cost was merged into the Decision Trace tab (data-dt-view="cost"), and the
    # Forward Queue (outbox) tab was retired — the Decision Trace delivery detail
    # now covers it (per-target status + manual re-enqueue). Forward Rules,
    # Silences, and Sandbox were merged into one "Routing" tab with sub-views
    # (data-routing-view), so the three standalone tabs no longer exist.
    assert {"overview", "alerts", "deep-analyses", "decision-trace", "routing"} <= tabs
    assert {
        "overviewTab",
        "alertsTab",
        "deepAnalysesTab",
        "decisionTraceTab",
        "routingTab",
    } <= panels
    assert "ai-cost" not in tabs and "aiCostTab" not in panels
    assert "outbox" not in tabs and "outboxTab" not in panels
    # The merged-away standalone tabs are gone (their content lives as sub-views).
    assert {"forward-rules", "silences", "sandbox"}.isdisjoint(tabs)
    assert {"forwardRulesTab", "silencesTab", "sandboxTab"}.isdisjoint(panels)
    # The three Routing sub-views are present.
    routing_views = set(re.findall(r'data-routing-view="([^"]+)"', html))
    assert {"rules", "silences", "sandbox"} <= routing_views


def test_dashboard_auto_refresh_intervals_are_operator_friendly() -> None:
    assert "DASHBOARD_AUTO_REFRESH_INTERVAL_MS = 60000" in _static_js("dashboard.js")
    assert "DEEP_ANALYSES_AUTO_REFRESH_INTERVAL_MS = 60000" in _static_js("deep-analyses.js")


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
