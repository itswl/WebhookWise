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


def test_every_destination_targets_a_real_content_panel() -> None:
    """Navigation moved into the command palette, so the old tab buttons are
    gone. What still has to hold is that every destination the registry can
    reach lands on a panel that actually exists in the markup."""
    html = _dashboard_html()
    panels = set(re.findall(r'id="([^"]+Tab)"', html))
    assert {"alertsTab", "decisionTraceTab", "routingTab", "operationsTab"} <= panels

    registry = re.search(r"const DESTINATIONS = \{(.*?)\n\};", _static_js("dashboard.js"), re.S)
    assert registry
    for tab in set(re.findall(r"tab: '([a-z-]+)'", registry.group(1))):
        camel = tab.split("-")[0] + "".join(part.title() for part in tab.split("-")[1:])
        assert f"{camel}Tab" in panels, f"destination tab {tab} has no panel"


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


def test_incident_intelligence_is_wired() -> None:
    incidents = _static_js("incidents.js")

    assert "'/v1/incidents/' + id + '/intelligence'" in incidents
    assert "'/v1/incidents/' + id + '/intelligence/feedback'" in incidents
    assert "renderIntelligence" in incidents
    assert "similar_incidents" in incidents
    assert "related_changes" in incidents
    assert "recommended_runbooks" in incidents
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'incidents.intelligence.title'" in js
        assert "'incidents.intelligence.similar'" in js
        assert "'incidents.intelligence.changes'" in js
        assert "'incidents.intelligence.runbooks'" in js


def test_incident_timeline_includes_suspected_change_markers() -> None:
    incidents = _static_js("incidents.js")
    css = _static_css("components.css")

    assert "buildIncidentTimeline" in incidents
    assert "renderChangeTimelineNode" in incidents
    assert "data.intelligence.related_changes" in incidents
    assert "safeHttpUrl" in incidents
    assert "timelineItem.kind === 'change'" in incidents
    assert ".incident-timeline-change" in css
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'incidents.timeline.counts'" in js
        assert "'incidents.timeline.changeMarker'" in js
        assert "'incidents.timeline.viewChange'" in js


def test_incident_command_summary_is_wired() -> None:
    incidents = _static_js("incidents.js")
    css = _static_css("components.css")

    assert "renderCommandSummary" in incidents
    assert "command_summary" in incidents
    assert "service_profile" in incidents
    assert "incident-command-grid" in incidents
    assert ".incident-command-summary" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'incidents.command.whatHappened'" in js
        assert "'incidents.command.likelyCause'" in js
        assert "'incidents.command.recentChange'" in js
        assert "'incidents.command.nextAction'" in js
        assert "'incidents.serviceProfile.title'" in js


def test_incident_secondary_actions_are_collapsed() -> None:
    incidents = _static_js("incidents.js")
    css = _static_css("components.css")

    assert "renderIncidentToolbar" in incidents
    assert "secondaryActions" in incidents
    assert "alert-action-menu incident-action-menu" in incidents
    assert "alert-action-count" in incidents
    assert ".incident-action-menu" in css
    assert "IncidentsModule.silenceIncidentSources" in incidents


def test_incident_change_impact_is_rendered() -> None:
    incidents = _static_js("incidents.js")
    css = _static_css("components.css")

    assert "renderChangeImpact" in incidents
    assert "impact_assessment" in incidents
    assert "before_alert_count" in incidents
    assert "new_identity_count" in incidents
    assert ".incident-impact-badge.impact-high" in css
    assert ".incident-change-impact-metrics" in css
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'incidents.changeImpact.level.high'" in js
        assert "'incidents.changeImpact.beforeAfter'" in js
        assert "'incidents.changeImpact.rollbackRecovered'" in js


def test_incident_runbook_execution_is_wired() -> None:
    incidents = _static_js("incidents.js")
    css = _static_css("components.css")

    assert "'/v1/incidents/' + id + '/runbook-executions'" in incidents
    assert "'/v1/incidents/' + id + '/runbook-executions/' + executionId" in incidents
    assert "startRunbookExecution" in incidents
    assert "toggleRunbookStep" in incidents
    assert "completeRunbookExecution" in incidents
    assert "step_index" in incidents
    assert "step_completed" in incidents
    assert ".incident-runbook-progress" in css
    assert ".incident-runbook-step" in css
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        js = _static_js(dict_name)
        assert "'incidents.runbook.start'" in js
        assert "'incidents.runbook.progress'" in js
        assert "'incidents.runbook.manualOnly'" in js


def test_incident_detail_interactions_do_not_toggle_parent_card() -> None:
    incidents = _static_js("incidents.js")
    css = _static_css("components.css")

    assert incidents.count('class="incident-detail"') >= 2
    assert incidents.count('onclick="event.stopPropagation()"') >= 2
    assert ".incident-detail" in css
    assert "cursor: default" in css


def test_incident_static_i18n_keys_exist_in_both_dictionaries() -> None:
    incidents = _static_js("incidents.js")
    keys = set(re.findall(r"\bt\(\s*'([^']+)'", incidents))
    incident_keys = {key for key in keys if key.startswith("incidents.") and not key.endswith(".")}

    assert incident_keys
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        dictionary = _static_js(dict_name)
        missing = [key for key in sorted(incident_keys) if f"'{key}':" not in dictionary]
        assert missing == []


def test_action_center_open_details_covers_every_backend_view() -> None:
    """Open-details must handle every `view` the backend can emit.

    The previous if/else chain silently had no branch for "overview", so the
    critical queue-backlog item's button did nothing at all. Pinned against the
    backend rather than against the JS so the two cannot drift apart.
    """
    import re

    backend = (PROJECT_ROOT / "services/operations/action_center.py").read_text()
    emitted = set(re.findall(r'view="([a-z-]+)"', backend))
    assert emitted, "no view= emitted; this guard would be vacuous"

    action_center = _static_js("action-center.js")
    mapping = re.search(r"DESTINATION_FOR_VIEW = \{(.*?)\};", action_center, re.S)
    assert mapping, "view→destination mapping not found"
    handled = set(re.findall(r"'?([a-z-]+)'?:\s*'", mapping.group(1)))

    assert emitted <= handled, f"Open details does nothing for: {sorted(emitted - handled)}"


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

    assert "slug: 'kb'" in _static_js("command-palette.js"), "kb unreachable from the palette"
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


def test_runtime_settings_admin_panel_is_wired() -> None:
    # Fifth Operations sub-view (mirrors the kb/gaps toggle): button, panel,
    # module script, setOperationsView routing, API surface (key path-encoded,
    # PUT body {"value": ...}, DELETE clears the override), and the propagation
    # note explaining changes reach all processes without a restart.
    html = _dashboard_html()
    api_js = _static_js("api.js")
    dashboard = _static_js("dashboard.js")
    module = _static_js("runtime-settings.js")

    assert "slug: 'settings'" in _static_js("command-palette.js"), "settings unreachable from the palette"
    assert 'id="runtimeSettingsTab"' in html
    assert 'id="runtimeSettingsList"' in html
    assert 'data-i18n="rs.propagationNote"' in html
    assert "/static/js/runtime-settings.js" in html
    assert "runtimeSettingsTab" in dashboard  # setOperationsView shows/hides the panel
    assert "RuntimeSettingsModule" in dashboard  # ...and loads the module

    assert "getRuntimeSettings" in api_js
    assert "updateRuntimeSetting" in api_js
    assert "clearRuntimeSetting" in api_js
    assert "'/v1/runtime-settings/' + encodeURIComponent(key)" in api_js
    assert "JSON.stringify({ value: value })" in api_js

    # Inline edit keeps the operator in the row: widget picked from the env
    # default (boolean select / number / text), clear-override only when an
    # override exists, and the backend's 400 message shown verbatim (escaped).
    assert "function valueKind" in module
    assert "data-rs-save" in module
    assert "data-rs-clear" in module
    assert "hasOverride(setting)" in module
    assert "escapeHtml(rowError.message)" in module


def test_runtime_settings_i18n_keys_exist_in_both_dictionaries() -> None:
    # Every literal t('rs.*') key in the module resolves in BOTH dictionaries.
    # Domain labels are looked up dynamically ('rs.domain.' + domain), so the
    # full domain set — including the zh ops vocabulary — is asserted explicitly.
    module = _static_js("runtime-settings.js")
    keys = set(re.findall(r"\bt\(\s*'([^']+)'", module))
    rs_keys = {key for key in keys if key.startswith("rs.") and not key.endswith(".")}

    assert rs_keys
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        dictionary = _static_js(dict_name)
        assert "'operations.view.settings'" in dictionary
        missing = [key for key in sorted(rs_keys) if f"'{key}':" not in dictionary]
        assert missing == []
        for domain in ("flapping", "escalation", "backpressure", "kb", "noise", "cadence", "retention"):
            assert f"'rs.domain.{domain}'" in dictionary

    zh = _static_js("i18n.zh.js")
    assert "'operations.view.settings': '运行时设置'" in zh
    assert "'rs.col.override': '覆盖'" in zh
    assert "'rs.col.effective': '生效值'" in zh
    assert "'rs.col.envDefault': '环境默认'" in zh
    assert "'rs.action.clearOverride': '清除覆盖'" in zh
    assert "'rs.domain.flapping': '抖动'" in zh
    assert "'rs.domain.escalation': '升级'" in zh
    assert "'rs.domain.backpressure': '背压'" in zh
    assert "'rs.domain.kb': '知识库'" in zh
    assert "'rs.domain.noise': '降噪'" in zh
    assert "'rs.domain.cadence': '通知节奏'" in zh
    assert "'rs.domain.retention': '保留策略'" in zh


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


def test_silence_duration_presets_exist_as_options() -> None:
    """Assigning an unknown value to a <select> leaves it blank, and a blank
    duration used to parse to a PERMANENT silence — quick-silence shipped
    setting '1h' when the option value is '1', so every "mute for an hour"
    created a mute that never expired."""
    import re

    html = _dashboard_html()
    select = re.search(r'<select[^>]*id="silenceFormDuration".*?</select>', html, re.S)
    assert select, "silenceFormDuration select not found"
    options = set(re.findall(r'value="([^"]*)"', select.group(0)))

    js = _static_js("silences.js")
    assigned = set(re.findall(r"getElementById\('silenceFormDuration'\)\.value\s*=\s*'([^']*)'", js))
    assert assigned, "no duration preset is assigned anywhere — the guard would be vacuous"
    assert assigned <= options, f"presets not offered by the select: {sorted(assigned - options)}"


def test_only_an_explicit_zero_means_a_permanent_silence() -> None:
    """ "Unreadable duration" and "until manually lifted" must not share a
    result: that collapse is what made a broken default silence alerts forever."""
    js = _static_js("silences.js")
    assert "if (String(durationValue).trim() === '0') return null;" in js
    assert "SILENCE_FALLBACK_HOURS" in js


def test_every_destination_is_reachable_by_url() -> None:
    """19 destinations used to sit behind one URL: a refresh dumped you back on
    Overview and nothing could be bookmarked or shared."""
    import re

    js = _static_js("dashboard.js")
    registry = re.search(r"const DESTINATIONS = \{(.*?)\n\};", js, re.S)
    assert registry, "destination registry not found"
    slugs = set(re.findall(r"^\s+'?([a-z-]+)'?:\s*\{ tab:", registry.group(1), re.M))

    # The palette is now the ONLY way to reach a destination without a link,
    # so a destination it omits is a feature nobody can find. Checked both
    # ways: the map must not lie in either direction.
    palette = _static_js("command-palette.js")
    listed = set(re.findall(r"slug: '([a-z-]+)'", palette))
    assert listed == slugs, f"palette missing {sorted(slugs - listed)}; palette lists unknown {sorted(listed - slugs)}"

    assert "window.addEventListener('hashchange', applyHashRoute)" in js, "Back/Forward not handled"
    assert "function navigateTo(" in js and "function recordDestination(" in js


def test_summary_counters_drill_into_their_detail_view() -> None:
    """.stat-card:hover lifts the card, promising a click. The Action Center and
    Overview headline numbers made that promise without keeping it."""
    action_center = _static_js("action-center.js")
    assert "statCard(t('action.summary.deadLetters')" in action_center
    assert "'alerts')" in action_center and "'work-queue')" in action_center

    overview = _static_js("overview.js")
    assert "drillToOutcome" in overview
    assert "navigateTo('cost')" in overview


def test_alert_and_incident_references_are_links() -> None:
    """Identifiers the reader obviously wants to open were inert text, while
    AlertsModule.focusAlertById sat fully built and called from nowhere."""
    assert "function openAlert(" in _static_js("dashboard.js")
    assert "function openIncident(" in _static_js("dashboard.js")
    assert "focusAlertById(focus)" in _static_js("dashboard.js")

    for module in ("decision-trace.js", "deep-analyses.js"):
        assert "openAlert(" in _static_js(module), f"{module} still renders inert alert ids"
    assert "openIncident(" in _static_js("incidents.js")
    assert "openIncident(" in _static_js("overview.js")


def test_routing_pill_bar_keeps_a_parent_highlighted() -> None:
    """Sandbox and Audit have no pill of their own; the bar used to go blank."""
    js = _static_js("routing.js")
    assert "PILL_PARENT" in js
    assert "sandbox: 'rules'" in js and "audit: 'rules'" in js


def test_command_palette_is_reachable_without_a_keyboard() -> None:
    """The palette replaced persistent navigation. If it were keyboard-only,
    a touch or mouse-only operator would have no way to move at all."""
    html = _dashboard_html()
    palette = _static_js("command-palette.js")

    assert "data-palette-open" in html, "no visible trigger"
    assert "data-palette-open" in palette, "trigger not bound"
    assert 'id="commandPalette"' in html and 'id="paletteInput"' in html
    # Keyboard paths still exist alongside it.
    assert "'k'" in palette and "event.key === '/'" in palette
    assert "'Escape'" in palette and "'ArrowDown'" in palette and "'Enter'" in palette


def test_empty_palette_shows_the_whole_map_not_a_blank_panel() -> None:
    """With no persistent nav, an empty query must list every destination —
    otherwise opening the palette teaches the operator nothing about what
    the product can do."""
    palette = _static_js("command-palette.js")
    assert "if (!normalized)" in palette
    assert "...ALL.filter(" in palette, "empty state does not fall back to the full map"


def test_dark_is_the_base_theme_not_an_override() -> None:
    """Dark-first has to be structural: if light lives in :root, an unstyled
    first paint flashes white and every new rule defaults to the light value."""
    css = _static_css("dashboard.css")
    assert ".theme-light {" in css, "light should be the override"
    assert ".theme-dark" not in css, "dark should be the base, not a class"

    js = _static_js("dashboard.js")
    assert "classList.toggle('theme-light'" in js
    assert "prefers-color-scheme: light" in js
