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


def test_dashboard_startup_is_resilient_to_i18n_dictionary_stalls() -> None:
    # A slow or failed per-language dictionary fetch must not gate the shell.
    dashboard = _static_js("dashboard.js")
    i18n = _static_js("i18n.js")
    overview = _static_js("overview.js")

    # Init no longer blocks on the dictionary; the landing-tab load is gated on
    # it instead, and the readiness check is exposed for that decision.
    assert "await I18N.ready" not in dashboard
    assert "loadLandingTab" in dashboard
    assert "isReady" in i18n
    # setLang commits the switch only once the target dictionary populated
    # (ensureDict resolves even on load failure).
    assert "if (!DICT[norm])" in i18n
    # The trend stays dependency-free and renders even when external networks fail.
    assert "Native CSS bars" in overview


def test_forward_rule_hits_badge_reads_as_rolling_90_day_window() -> None:
    # The backend hit count is a rolling 90-day window; both dictionaries say so.
    assert "'rules.roi.hits': '{count} matched (90d)'" in _static_js("i18n.en.js")
    assert "'rules.roi.hits': '近 90 天命中 {count} 次'" in _static_js("i18n.zh.js")


def test_silence_debt_panel_is_wired() -> None:
    # Silence-debt surface on the Silences view: container, API call, renderer,
    # and i18n keys present in BOTH dictionaries.
    html = _dashboard_html()
    silences = _static_js("silences.js")
    api_js = _static_js("api.js")

    assert 'id="silenceDebtPanel"' in html
    assert "getSilenceDebt" in api_js
    assert "/v1/silences/debt" in api_js
    assert "function renderSilenceDebt" in silences
    assert "loadSilenceDebt()" in silences
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'silences.debt.title'" in js
        assert "'silences.debt.chronicBadge'" in js


def test_maintenance_windows_surface_is_wired() -> None:
    # Maintenance windows on the Silences view: list container, form modal with
    # weekday checkboxes, API surface (array days_of_week in requests), renderer,
    # the "[mw:" origin badge on materialized silences, and i18n in BOTH dicts.
    html = _dashboard_html()
    silences = _static_js("silences.js")
    api_js = _static_js("api.js")

    assert 'id="maintenanceWindowsList"' in html
    assert 'id="maintenanceWindowFormModal"' in html
    assert html.count('class="mw-day-checkbox"') == 7
    assert "getMaintenanceWindows" in api_js
    assert "createMaintenanceWindow" in api_js
    assert "updateMaintenanceWindow" in api_js
    assert "deleteMaintenanceWindow" in api_js
    assert "/v1/maintenance-windows" in api_js
    assert "function renderMaintenanceWindows" in silences
    assert "loadMaintenanceWindows()" in silences
    assert "days_of_week: days" in silences  # requests send an int array, not the CSV
    assert "startsWith('[mw:')" in silences  # origin badge on window-materialized silences
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'silences.mw.title'" in js
        assert "'silences.mw.originBadge'" in js
        for day in range(1, 8):
            assert f"'mw.day.{day}'" in js


def test_incident_postmortem_export_is_wired() -> None:
    # Export postmortem on the incident detail: authenticated fetch of the
    # markdown endpoint, blob download named like the backend attachment, and
    # i18n in BOTH dicts.
    incidents = _static_js("incidents.js")

    assert "'/v1/incidents/' + id + '/postmortem'" in incidents
    assert "exportPostmortem" in incidents
    assert "'postmortem-incident-' + id + '.md'" in incidents
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'incidents.action.postmortem'" in js
        assert "'incidents.action.postmortemFailed'" in js


def test_action_center_routes_noise_view_items() -> None:
    # Action Center items render generically off severity/view (no kind
    # whitelist), so flapping_identity (view="noise") only needs the open-details
    # navigation to know the noise view.
    action_center = _static_js("action-center.js")

    assert "view === 'noise'" in action_center
    assert "setOperationsView('noise')" in action_center


def test_ai_disagreements_review_surface_is_wired() -> None:
    # AI-vs-rules drill-down on the Decision Trace view: container, API call, the
    # exposed toggle, reuse of the by-event chain renderer, and i18n in both dicts.
    html = _dashboard_html()
    dt = _static_js("decision-trace.js")
    api_js = _static_js("api.js")

    assert 'id="decisionTraceDisagreements"' in html
    assert "getAiDisagreements" in api_js
    assert "/v1/decision-traces/ai-disagreements" in api_js
    assert "toggleDisagreement: toggleDisagreement" in dt
    assert "getDecisionTraceByEvent" in dt
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'dt.disagreements.title'" in js
        assert "'dt.disagreements.noTrace'" in js


def test_kb_drafts_review_subview_is_wired() -> None:
    # Third Operations sub-view (mirrors the actions/noise toggle): button,
    # panel, module script, setOperationsView routing, API surface, i18n in both.
    html = _dashboard_html()
    api_js = _static_js("api.js")
    dashboard = _static_js("dashboard.js")

    assert 'data-operations-view="kb"' in html
    assert 'id="kbDraftsTab"' in html
    assert "/static/js/kb-drafts.js" in html
    assert "kbDraftsTab" in dashboard  # setOperationsView shows/hides the panel
    assert "KbDraftsModule" in dashboard  # ...and loads the module
    assert "getKbDrafts" in api_js
    assert "/v1/admin/kb/drafts" in api_js
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'operations.view.kb'" in js
        assert "'kb.title'" in js
        assert "'kb.empty.text'" in js


def test_kb_draft_publish_discard_urls_encode_source_ref() -> None:
    # source_ref like "incident:123" is a path segment behind a :path route; the
    # colon must be percent-encoded (encodeURIComponent -> %3A). Assert both
    # admin-write calls encode it and use the right verb.
    api_js = _static_js("api.js")

    assert "'/v1/admin/kb/drafts/' + encodeURIComponent(sourceRef) + '/publish'" in api_js
    assert "'/v1/admin/kb/drafts/' + encodeURIComponent(sourceRef)" in api_js
    assert "method: 'POST'" in api_js  # publish
    assert "method: 'DELETE'" in api_js  # discard


def test_queue_health_tile_keys_on_backlog_not_fill() -> None:
    # Ingest-queue health tile on the Overview view (rendered dynamically by
    # overview.js). The gauge/tint must key on backlog_fraction, NOT fill_fraction:
    # a healthy busy stream sits at depth==maxlen permanently (Redis trims lazily,
    # not on ack), so fill would show a false 100%/red. depth/maxlen is only
    # informational retention.
    overview = _static_js("overview.js")
    api_js = _static_js("api.js")

    assert "getQueueHealth" in api_js
    assert "/v1/queue-health" in api_js
    assert "_renderQueueHealth" in overview
    assert "API.getQueueHealth()" in overview  # fetched alongside the overview stats
    assert "backlog_fraction" in overview  # the alarm signal
    assert "fill_fraction" not in overview  # the buggy signal is gone from the tile
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'overview.queue.title'" in js
        assert "'overview.queue.backlog'" in js
        assert "'overview.queue.retention'" in js  # depth/maxlen is informational
        assert "'overview.queue.backlogged'" in js


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
