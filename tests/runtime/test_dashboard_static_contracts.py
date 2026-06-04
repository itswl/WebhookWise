from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

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
    assert static_assets.index("/static/js/utils.js") < static_assets.index("/static/js/api.js")
    assert static_assets.index("/static/js/api.js") < static_assets.index("/static/js/alerts.js")

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

    assert {"alerts", "ai-cost", "deep-analyses", "outbox", "forward-rules"} <= tabs
    assert {"alertsTab", "aiCostTab", "deepAnalysesTab", "outboxTab", "forwardRulesTab"} <= panels


def test_dashboard_auto_refresh_intervals_are_operator_friendly() -> None:
    assert "DASHBOARD_AUTO_REFRESH_INTERVAL_MS = 60000" in _static_js("dashboard.js")
    assert "DEEP_ANALYSES_AUTO_REFRESH_INTERVAL_MS = 60000" in _static_js("deep-analyses.js")


def test_deep_analysis_formats_json_like_reports_as_structured_content() -> None:
    js = _static_js("deep-analyses.js")
    css = _static_css("components.css")

    assert "decodeEscapedJsonText" in js
    assert "renderStructuredValue(parsed)" in js
    assert "renderKeyValueGrid(value) || renderJsonBlock(value)" in js
    assert "da-json-block" in js
    assert ".da-json-block" in css
    assert ".da-inline-list" in css
