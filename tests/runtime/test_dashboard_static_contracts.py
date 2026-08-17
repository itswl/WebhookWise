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

    # faro.js was removed in 3.7.x: its SDK loaded from a CDN the strict CSP
    # blocks (it never initialized in production) and its default collector
    # pointed at a port nothing serves. RUM returns only with a real,
    # explicitly configured consumer.
    assert not any("faro" in ref for ref in refs)

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
    # The trigger stays quiet: no count badge (an item count on "more actions"
    # reads as an unread indicator and earns none of that attention).
    assert "alert-action-count" not in incidents
    assert "alert-action-count" not in _static_js("alerts.js")
    assert ".incident-action-menu" in css
    # Dispatched by name through the delegated handler since the CSP burn-down.
    assert 'data-ic-act="silenceIncidentSources"' in incidents


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
    # Propagation is stopped by the delegated dispatcher via data-ic-stop
    # markers (inline handlers are gone), so detail controls still never
    # collapse the card they live in.
    assert incidents.count('data-ic-stop="1"') >= 2
    assert "data-ic-stop" in incidents and "event.stopPropagation();" in incidents
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


def test_desktop_notifications_are_opt_in_and_quiet() -> None:
    """A monitoring tool should be able to speak while you are elsewhere —
    but only on opt-in, only for high importance, only while the tab is
    hidden, and never re-announcing history (a baseline is primed on
    enable)."""
    dashboard = _static_js("dashboard.js")
    assert 'id="notifyToggleBtn"' in _dashboard_html()
    assert "Notification.requestPermission" in dashboard
    assert "if (!document.hidden) return;" in dashboard
    assert "importance: 'high'" in dashboard
    assert "_primeNotifyBaseline" in dashboard
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        assert "'notify.title'" in _static_js(dict_name)


def test_cache_hit_definition_has_one_source() -> None:
    """ "Cache hit" on the AI-cost view means "answered without calling the LLM".
    That route list used to be hand-spelled in the aggregate query AND again in
    the dashboard renderer, so a future reuse route added to one and not the
    other would silently under-report the hit rate with nothing failing.

    One definition now (NO_LLM_REUSE_ROUTE_TYPES), and the renderer takes the
    backend's total instead of re-summing. Verified against production: the
    constant's routes covered exactly the rows marked cache_hit=true
    (redis_reuse 128 + rechain 8 = 136)."""
    from services.webhooks.types import ALLOWED_ANALYSIS_ROUTE_TYPES, NO_LLM_REUSE_ROUTE_TYPES

    # Every no-LLM route must be a real route ("reuse" is the legacy alias kept
    # for historical rows and is deliberately not in the allowed set).
    assert NO_LLM_REUSE_ROUTE_TYPES - {"reuse"} <= ALLOWED_ANALYSIS_ROUTE_TYPES
    # Routes that skipped the LLM by policy or degradation are NOT cache hits.
    assert {"rule", "rule_routed", "ai"}.isdisjoint(NO_LLM_REUSE_ROUTE_TYPES)

    queries = (PROJECT_ROOT / "services/analysis/analysis_queries.py").read_text()
    assert "NO_LLM_REUSE_ROUTE_TYPES" in queries
    assert 'route_breakdown.get("redis_reuse"' not in queries, "route list re-spelled in the aggregator"

    ai_cost = _static_js("ai-cost.js")
    assert "cache_statistics.saved_calls" in ai_cost
    assert "'redis_reuse'" not in ai_cost, "route list re-spelled in the renderer"


def test_operator_reach_features_are_wired() -> None:
    """Three reach features the dashboard lacked, each pinned end to end:
    bulk workflow actions, filter state in the URL, and palette record jumps."""
    alerts = _static_js("alerts.js")
    html = _dashboard_html()
    dashboard = _static_js("dashboard.js")
    palette = _static_js("command-palette.js")

    # Bulk: checkbox per row, a bar, and sequential execution.
    assert 'id="alertsBulkBar"' in html
    assert "alert-bulk-check" in alerts
    assert "_runBulk" in alerts
    assert ".alerts-bulk-bar" in _static_css("components.css")

    # Filters in the URL, with the router understanding ?query.
    assert "function hashFilters(" in dashboard
    assert "function writeHashFilters(" in dashboard
    assert "_applyFiltersFromHash" in alerts and "_syncFiltersToHash" in alerts
    # Destination recording must preserve an existing ?query.
    assert "keptQuery" in dashboard

    # Palette jumps to a record id.
    assert "jumpEntries" in palette
    assert "openIncident(item.jump.id)" in palette
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        assert "'palette.jump.alert'" in _static_js(dict_name)


def test_time_rendering_uses_one_family() -> None:
    """Four time dialects coexisted (slashed locale dates, dashed ISO slices,
    truncated ISO, relative-only). All absolute times go through formatTime
    (lists) / formatTimeFull (detail); toLocaleTimeString stays legal for
    clock-only refresh stamps. Number formatting keeps its own locale call."""
    utils = _static_js("utils.js")
    assert "function formatTime(" in utils
    assert "function formatTimeFull(" in utils
    for name in sorted(p.name for p in (PROJECT_ROOT / "templates/static/js").glob("*.js")):
        if name == "utils.js":
            continue
        src = _static_js(name)
        assert ".toLocaleString(" not in src, f"{name}: date dialect outside the family"
        assert ".slice(0, 16).replace('T'" not in src, f"{name}: raw ISO truncation"


def test_mono_blocks_share_one_design() -> None:
    """Every preformatted block is either the full code component
    (.code-wrapper, JSON viewer with copy button) or the plain .ww-pre —
    both drawn from the shared --code-* tokens in BOTH themes. Ad-hoc
    inline-styled <pre> is exactly how the raw-data and AI views drifted
    apart."""
    for name in sorted(p.name for p in (PROJECT_ROOT / "templates/static/js").glob("*.js")):
        assert "<pre style=" not in _static_js(name), f"{name}: inline-styled <pre>"
    css = _static_css("components.css")
    assert ".ww-pre {" in css
    assert "var(--code-bg)" in css and "var(--code-fg)" in css
    dashboard_css = _static_css("dashboard.css")
    # Both themes define the code tokens; the component may not carry its own
    # hex palette island again.
    assert dashboard_css.count("--code-bg:") == 2
    for hexish in ("#0f172a", "#1e293b", "#334155", "#93c5fd"):
        assert hexish not in css, f"code component regrew its slate palette ({hexish})"


def test_html_tagged_template_is_the_new_renderer_path() -> None:
    """html`` escapes every interpolation unless wrapped in htmlRaw() — the
    replacement for the quoting dance that produced the splice-defect class.
    The sidebar item renderer is the living exemplar."""
    utils = _static_js("utils.js")
    assert "function html(strings)" in utils
    assert "function htmlRaw(" in utils
    assert "__wwHtml" in utils
    assert "return html`" in _static_js("dashboard.js")


def test_inline_handlers_static_zero_and_generated_ratchet() -> None:
    """CSP end-state is script-src-attr 'none': markup carries no code.

    The static page is already there — every on*= attribute was codemodded
    into static-bindings.js (one binding per original handler, selectors that
    must exist in the markup). Generated HTML still emits inline onclick=;
    each file's count is RATCHETED: touching a page may only hold or reduce
    it. When the table reaches zero everywhere, tighten the CSP and collapse
    this test to the zero assertion."""
    html = _dashboard_html()
    for attr in ("onclick=", "onchange=", "oninput=", "onsubmit="):
        assert attr not in html, f"static markup must not carry {attr}"

    bindings = _static_js("static-bindings.js")
    selectors = re.findall(r"bind\('([^']+)'", bindings)
    # 57 codemodded + hand-written additions since; every one must resolve.
    assert len(selectors) >= 57
    for selector in set(selectors):
        if selector.startswith("#"):
            assert f'id="{selector[1:]}"' in html, f"binding target {selector} missing"
        else:
            attr_pair = selector[1:-1].replace('"', '"')
            assert attr_pair in html, f"binding target {selector} missing"

    ratchet = dict.fromkeys(
        [
            "action-center.js",
            "alerts.js",
            "dashboard.js",
            "decision-trace.js",
            "deep-analyses.js",
            "forward-rules.js",
            "incidents.js",
            "overview.js",
            "runtime-settings.js",
            "silences.js",
            "utils.js",
        ],
        0,
    )
    for name, ceiling in ratchet.items():
        count = _static_js(name).count("onclick=")
        assert count <= ceiling, (
            f"{name}: {count} inline onclick= (ratchet is {ceiling}) — "
            "convert handlers to delegated listeners instead of adding more"
        )


def test_knowledge_gap_cards_can_be_left() -> None:
    """A gap names a pattern; its evidence is the incidents/alerts that formed
    it. The card must offer both drills, landing pre-filtered (the incidents
    list applies a pre-filled search box after load)."""
    rc = _static_js("response-center.js")
    incidents = _static_js("incidents.js")

    assert "data-gap-incidents" in rc
    assert "data-gap-alerts" in rc
    assert "drillFromGap" in rc
    assert "navigateTo(slug)" in rc
    # The suggested next step is executable: create-runbook opens the inline
    # authoring form, and publishing carries the tags the gap detector matches
    # deterministically (kind=runbook + this group's alert_pattern).
    assert "data-gap-create" in rc
    assert "'runbook:' + pattern" in rc
    assert "kind: 'runbook'" in rc
    assert "alert_pattern: pattern" in rc
    assert "ingestKbDocument" in _static_js("api.js")
    # The landing side honours the pre-filled term.
    assert "incidentSearchInput" in incidents
    assert ".trim()) search();" in incidents
    assert ".knowledge-gap-drills" in _static_css("components.css")


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
        # The KB destination's live label (the old operations.view.* pill
        # labels died with the tab bar and were purged in 3.7.0).
        assert "'nav.dest.kb'" in js
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
        assert "'nav.dest.settings'" in dictionary
        missing = [key for key in sorted(rs_keys) if f"'{key}':" not in dictionary]
        assert missing == []
        for domain in ("flapping", "escalation", "backpressure", "kb", "noise", "cadence", "retention"):
            assert f"'rs.domain.{domain}'" in dictionary

    zh = _static_js("i18n.zh.js")
    assert "'nav.dest.settings': '运行时设置'" in zh
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
    assert "act: 'navigateTo', args: 'cost'" in overview


def test_alert_and_incident_references_are_links() -> None:
    """Identifiers the reader obviously wants to open were inert text, while
    AlertsModule.focusAlertById sat fully built and called from nowhere."""
    assert "function openAlert(" in _static_js("dashboard.js")
    assert "function openIncident(" in _static_js("dashboard.js")
    assert "focusAlertById(focus)" in _static_js("dashboard.js")

    for module in ("decision-trace.js", "deep-analyses.js"):
        src = _static_js(module)
        assert 'data-act="openAlert"' in src or 'data-ic-act="openAlert"' in src, (
            f"{module} still renders inert alert ids"
        )
    # incidents.js dispatches through its own module-local handler.
    assert 'data-ic-act="openIncident"' in _static_js("incidents.js")
    assert 'data-act="openIncident"' in _static_js("overview.js")


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
    first paint flashes white and every new rule defaults to the light value.

    It also has to be the default by decision, not by OS preference: following
    prefers-color-scheme silently served every light-mode OS the second-class
    theme — four rounds of dark polish that a light-system operator never saw.
    Only an explicit saved toggle may select light."""
    css = _static_css("dashboard.css")
    assert ".theme-light {" in css, "light should be the override"
    assert ".theme-dark" not in css, "dark should be the base, not a class"

    js = _static_js("dashboard.js")
    assert "classList.toggle('theme-light'" in js
    assert "matchMedia('(prefers-color-scheme" not in js, (
        "theme must not follow the OS: dark is the default, the toggle the opt-out"
    )
    assert "saved === 'light' ? 'light' : 'dark'" in js


def test_sidebar_renders_the_same_map_as_the_palette() -> None:
    """The rail and the palette must be one navigation model, not two: the
    sidebar renders from CommandPalette._groups, so a destination added to
    the palette appears in the rail for free — and cannot appear in only one."""
    html = _dashboard_html()
    dashboard = _static_js("dashboard.js")

    assert 'id="sidebar"' in html
    assert "CommandPalette._groups" in dashboard, "sidebar must render from the palette's groups"
    assert "renderSidebar" in dashboard and "updateSidebarActive" in dashboard
    # Language switches re-render the rail; navigation updates its highlight.
    assert "renderSidebar();\n            updateAuthButtonState" in dashboard.replace("\r", "")
    assert "updateSidebarActive(slug)" in dashboard


def test_sidebar_works_on_mobile_and_remembers_collapse() -> None:
    css = _static_css("components.css")
    assert "body.sidebar-collapsed .container" in css, "content must reflow when collapsed"
    assert "translateX(-100%)" in css, "mobile must get the off-canvas drawer"
    assert 'id="sidebarMobileBtn"' in _dashboard_html(), "no way to open the drawer on touch"
    assert "SIDEBAR_COLLAPSED_KEY" in _static_js("dashboard.js")


def test_module_lookup_never_goes_through_window_indexing() -> None:
    """window[name] returns undefined for every const-declared module (a
    top-level const creates a global binding, not a window property) — that
    silently killed all seven Routing destinations while var-declared modules
    kept working. Bare-identifier typeof guards are the only safe lookup."""
    dashboard = _static_js("dashboard.js")
    assert "window[moduleName]" not in dashboard
    assert "typeof RoutingModule !== 'undefined'" in dashboard
    assert "typeof DecisionTraceModule !== 'undefined'" in dashboard


def _emoji_count(text: str) -> int:
    """Count emoji-as-iconography glyphs, after decoding HTML entities.

    The original range [U+1F300–U+1FAFF, U+2600–U+27BF] had four documented
    escapes: 🆔 (U+1F194, below the 1F300 floor), ⏱/⌛ (U+23Fx/U+231B, below
    the 2600 floor), ⬆ (U+2B06, above the 27BF ceiling), and 🔍 written as an
    HTML entity, invisible to a source-text scan. Hence: wider blocks plus
    html.unescape first. ⌘ (U+2318) is allowed — a keyboard glyph in shortcut
    hints is typography, not iconography.
    """
    import html as _html
    import re as _re

    decoded = _html.unescape(text)
    allowed = {"⌘"}
    return sum(1 for ch in _re.findall(r"[\U0001F000-\U0001FAFF⌀-⏿☀-➿⬀-⯿]", decoded) if ch not in allowed)


def test_dashboard_uses_the_icon_system_not_emoji() -> None:
    """310 emoji \"icons\" were the single loudest taste problem: each glyph
    ships its own palette (ignoring the design tokens), renders differently
    per OS, and varies in visual weight. The sprite gives every icon one
    stroke voice and currentColor. i18n dictionaries are exempt (their few
    emoji live in translated copy, not iconography); Feishu card emoji live
    in services/ — product output, deliberately untouched."""
    assert _emoji_count(_dashboard_html()) == 0, "emoji left in dashboard.html"

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    offenders = {}
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        if path.name.startswith("i18n."):
            continue
        count = _emoji_count(path.read_text())
        if count:
            offenders[path.name] = count
    assert offenders == {}, f"emoji icons remain: {offenders}"


def test_every_icon_reference_resolves_to_a_sprite_symbol() -> None:
    """A dangling <use href=\"#i-x\"> renders as blank space — worse than the
    emoji it replaced. Checked against every wwIcon() call and every literal
    use href in HTML and JS."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    html = _dashboard_html()
    defined = set(_re.findall(r'<symbol id="i-([a-z-]+)"', html))
    assert len(defined) >= 40

    used = set(_re.findall(r'href="#i-([a-z-]+)"', html))
    for path in (_ROOT / "templates/static/js").glob("*.js"):
        text = path.read_text()
        used |= set(_re.findall(r"wwIcon\('([a-z-]+)'", text))
        used |= set(_re.findall(r'href="#i-([a-z-]+)"', text))

    dangling = used - defined
    assert dangling == set(), f"icon references with no sprite symbol: {sorted(dangling)}"


def test_font_sizes_stay_on_the_scale_ratchet() -> None:
    """67 distinct font sizes is what \"designed by nobody\" looks like. The
    scale tokens plus this ratchet stop the number growing back; tighten the
    bound as the long tail is migrated."""
    import re as _re

    values = set()
    for source in (_static_css("components.css"), _static_css("dashboard.css"), _dashboard_html()):
        values |= set(_re.findall(r"font-size:\s*([0-9.]+(?:rem|px|em))", source))
    assert len(values) <= 24, f"font-size sprawl is growing again: {len(values)} distinct raw values"


def test_js_modules_never_call_methods_they_do_not_define() -> None:
    """Deleting a \"dead\" method while a `this.` call to it survives passes
    node --check and every static grep for external callers — then throws at
    runtime the first time the view loads. That is exactly how the AI Cost
    view shipped broken: loadStats still called this.updatePeriodButtons after
    the method was removed as orphaned. Self-references are checkable per
    file, so check them."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    offenders = {}
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        if path.name.startswith("i18n."):
            continue
        src = path.read_text()
        calls = set(_re.findall(r"\bthis\.([A-Za-z_]\w*)\s*\(", src))
        defs = set(_re.findall(r"^\s{4}(?:async\s+)?([A-Za-z_]\w*)\s*\([^)]*\)\s*\{", src, _re.M))
        defs |= set(_re.findall(r"\b([A-Za-z_]\w*)\s*:\s*(?:async\s+)?function\b", src))
        defs |= set(_re.findall(r"\bfunction\s+([A-Za-z_]\w*)\s*\(", src))
        missing = calls - defs
        if missing:
            offenders[path.name] = sorted(missing)
    assert offenders == {}, f"this.-calls with no same-file definition: {offenders}"


def test_one_click_navigation_invariants() -> None:
    """The Overview-tab destinations needed two clicks: the destination
    reporter had been spliced into load() (which fires on tab entry with the
    STALE sub-view) instead of setView() (which knows the view just entered).
    Three invariants pin the repaired shape:
    1. setView reports the destination it enters;
    2. load() — also the auto-refresh path — never reports, so refresh cannot
       rewrite the URL;
    3. navigateTo suppresses the tab's default loader (skipInit), because
       enter() loads the real target — the default was a wasted fetch of the
       old sub-view on every cross-tab navigation."""
    import re as _re

    dt = _static_js("decision-trace.js")
    set_view = _re.search(r"function setView\(view\) \{.*?\n    \}", dt, _re.S)
    assert set_view and "recordDestination(currentView)" in set_view.group(0)
    load_fn = _re.search(r"function load\(\) \{.*?\n    \}", dt, _re.S)
    assert load_fn and "recordDestination" not in load_fn.group(0)

    dashboard = _static_js("dashboard.js")
    assert "switchMainTab(destination.tab, { skipInit: true })" in dashboard
    assert "if (skipInit) return;" in dashboard


def test_sidebar_never_shows_raw_i18n_keys() -> None:
    """The dictionary loads async and startup deliberately does not block on
    it. The sidebar bakes t() output into strings at render time, so on a
    cold load it painted \"nav.dest.investigations\" until a language toggle
    forced a re-render. Two halves: labels degrade to slugs pre-ready, and
    the dictionary-settled hook repaints the baked chrome once."""
    dashboard = _static_js("dashboard.js")
    assert "translated === item.label ? item.slug : translated" in dashboard
    # The ready hook must repaint sidebar AND breadcrumb before the landing tab.
    import re as _re

    ready_block = _re.search(r"I18N\.ready\.finally\(\(\) => \{(.*?)\}\);", dashboard, _re.S)
    assert ready_block, "dictionary-settled hook missing"
    assert "renderSidebar()" in ready_block.group(1)
    assert "renderBreadcrumb(currentDestination)" in ready_block.group(1)


def test_js_renderers_carry_no_hardcoded_colors() -> None:
    """121 hex literals baked into renderers is how the dashboard froze
    mid-redesign: inline colours never follow the tokens, so every theme
    change strands them (the light-amber periodic-reminder badge on the dark
    theme was the reported symptom). Colour comes from var(--...) or a class.
    Six-digit hex is banned outright; three-digit hex is allowed only as the
    &#039; HTML entity and inside comment prose (#123 as an example id)."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    offenders = {}
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        if path.name.startswith("i18n."):
            continue
        found = _re.findall(r"(?<!&)#[0-9a-fA-F]{6}\b", path.read_text())
        if found:
            offenders[path.name] = sorted(set(found))
    assert offenders == {}, f"hardcoded colours crept back: {offenders}"


def test_js_renderers_do_not_reuse_the_retired_palette() -> None:
    """showToast froze the PREVIOUS palette in rgba() literals, which the hex
    ban cannot see — its toasts stayed emerald/indigo through a full retheme.
    rgba() in general is still tolerated (documented debt), but these five
    retired triplets are banned outright so the old palette cannot creep back
    through the alpha channel."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    retired = r"rgba\(\s*(?:16\s*,\s*185\s*,\s*129|239\s*,\s*68\s*,\s*68|245\s*,\s*158\s*,\s*11|99\s*,\s*102\s*,\s*241|102\s*,\s*126\s*,\s*234)\b"
    offenders = {}
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        if path.name.startswith("i18n."):
            continue
        found = _re.findall(retired, path.read_text())
        if found:
            offenders[path.name] = sorted(set(found))
    assert offenders == {}, f"retired-palette rgba() literals crept back: {offenders}"


def test_every_action_root_registers_itself() -> None:
    """const modules create global lexical bindings with NO window property, so
    the dispatcher's window[name] lookup silently returned null for every
    "Module.method" data-act — measured in a real browser, all of them were
    dead. Each allowlisted root must therefore call wwRegisterActionRoot in
    some module file, and nothing may register a name the allowlist doesn't
    carry."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    utils = _static_js("utils.js")
    roots_src = utils[utils.index("WW_ACTION_ROOTS = [") : utils.index("];", utils.index("WW_ACTION_ROOTS = ["))]
    allowlisted = set(_re.findall(r"'([A-Za-z]+)'", roots_src))
    assert allowlisted, "could not parse WW_ACTION_ROOTS"

    registered = set()
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        registered.update(_re.findall(r"wwRegisterActionRoot\('([A-Za-z]+)',", path.read_text()))

    assert allowlisted - registered == set(), f"allowlisted roots never register: {allowlisted - registered}"
    assert registered - allowlisted == set(), f"registrations outside the allowlist: {registered - allowlisted}"


_GHOST_CLASS_ALLOWLIST = frozenset(
    {
        # Frozen 2026-08-17: classes referenced by markup with no stylesheet
        # rule. Everything here is either a structural/JS hook (operations-view,
        # mw-day-checkbox) or a legacy family whose layout lives in inline
        # styles (ai-*, silence-*, rule-*). Shrink this set opportunistically;
        # never grow it silently — a NEW entry means a page just shipped with
        # browser-default styling, which is exactly how the inbound-rule form,
        # the audit list, and the handoff window buttons broke.
        "ai-analysis",
        "ai-content",
        "ai-details",
        "ai-header",
        "ai-item",
        "ai-label",
        "ai-meta",
        "ai-value",
        "alert-more-trigger",
        "badge-drill",
        "decision-trace-section",
        "detail-section",
        "dt-list-filters",
        "dt-period-selector",
        "event-type-grid",
        "impact-unknown",
        "inbound-rules-section",
        "incident-card",
        "incident-intelligence-excerpt",
        "incident-row",
        "incident-tree",
        "incidents-section",
        "integration-card",
        "last-refreshed",
        "mw-day-checkbox",
        "operations-view",
        "pipeline-flow",
        "pipeline-step",
        "raw-data",
        "response-queue-service",
        "routing-section",
        "rule-audit-section",
        "rule-conditions",
        "rule-target",
        "rules-list",
        "sandbox-grid",
        "sandbox-section",
        "silence-actions",
        "silence-card",
        "silence-conditions",
        "silence-header",
        "silences-list",
        "silences-section",
        "status-",
        "step-indicator",
        "tree-indicator",
        "tree-node",
        "ww-icon",
    }
)


def test_no_new_ghost_classes() -> None:
    """Every class the markup references must exist in a stylesheet — or sit
    on the frozen allowlist above. Four pages shipped on browser-default
    styling because their classes (.input, primary, .mem-bar, .audit-row,
    btn-danger/btn-warn/btn-ghost) existed nowhere; this ratchet catches the
    next one at commit time."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    token = _re.compile(r"^[a-z][a-z0-9-]*$")
    used: dict[str, set[str]] = {}
    paths = [_ROOT / "templates/dashboard.html", *sorted((_ROOT / "templates/static/js").glob("*.js"))]
    for path in paths:
        if path.name.startswith("i18n."):
            continue
        text = path.read_text()
        for match in _re.finditer(r"""class=\\?["']([^"'\\$<>{}]+)["'\\]""", text):
            for cls in match.group(1).split():
                if token.match(cls) and not cls.endswith("-"):
                    used.setdefault(cls, set()).add(path.name)

    defined: set[str] = set()
    for css in sorted((_ROOT / "templates/static/css").glob("*.css")):
        defined.update(_re.findall(r"\.([a-z][a-z0-9-]*)", css.read_text()))

    ghosts = {
        cls: sorted(files) for cls, files in used.items() if cls not in defined and cls not in _GHOST_CLASS_ALLOWLIST
    }
    assert ghosts == {}, f"classes with no stylesheet rule (define them or consciously allowlist): {ghosts}"


def test_native_dialogs_stay_replaced() -> None:
    """window.confirm/prompt are unthemeable, block the event loop, and put
    the browser's name above your text. The dashboard replaced them with
    wwConfirm/wwPrompt (ESC + focus trap for free); seven native calls crept
    back anyway, one with a hardcoded English string. utils.js is exempt —
    it owns the replacements and documents what they replace."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    pattern = _re.compile(r"(?<![\w.])(?:window\.)?(?:confirm|prompt)\(")
    offenders = {}
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        if path.name in ("utils.js",) or path.name.startswith("i18n."):
            continue
        hits = [
            lineno
            for lineno, line in enumerate(path.read_text().splitlines(), start=1)
            if pattern.search(line) and not line.lstrip().startswith(("//", "*"))
        ]
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"native dialogs crept back: {offenders}"


def test_js_never_guards_on_the_empty_string() -> None:
    """`includes('')` and `startsWith('')` are always true. An emoji purge
    rewrote showToast's comparison literals to '' and every toast became a
    green success missing its first two characters — the guard could no longer
    fail, so no branch below it ever ran. Empty-string membership checks are
    always a bug."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    pattern = _re.compile(r"\.(?:includes|startsWith|endsWith)\(\s*(?:''|\"\")\s*\)")
    offenders = {}
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        found = pattern.findall(path.read_text())
        if found:
            offenders[path.name] = len(found)
    assert offenders == {}, f"always-true empty-string guards: {offenders}"


def test_colour_stays_in_points_not_surfaces() -> None:
    """The restraint pass: status badges carry colour in an icon or dot, never
    a filled surface; card accents are 3px state signals (failing delivery,
    active silence, queue backlog), never 4px decoration on every card. Both
    regressed easily before because each new badge copied the nearest one."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    offenders = {}
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        if path.name.startswith("i18n."):
            continue
        src = path.read_text()
        bad_badges = _re.findall(r'class="badge[^"]*"[^>]*style="[^"]*background', src)
        bad_borders = _re.findall(r"border-left:\s*4px", src)
        if bad_badges or bad_borders:
            offenders[path.name] = {"inline_bg_badges": len(bad_badges), "4px_accents": len(bad_borders)}
    assert offenders == {}, f"colour crept back onto surfaces: {offenders}"

    css = _static_css("components.css")
    assert ".badge-success {\n  background: transparent;" in css.replace(
        ".badge-high, .badge-medium, .badge-low, .badge-success, .badge-danger,\n.badge-warning, .badge-info, .badge-duplicate {\n  background: transparent;",
        ".badge-success {\n  background: transparent;",
    ), "cold badge treatment missing"

    # Toggle controls too. The rule was written for badges and accents, so a
    # toolbar button that fills itself blue when active slipped straight past
    # it — an operator had to notice and say the box was too loud. A toggled
    # control states itself with its glyph, the way a badge does.
    for selector in (".btn.is-active", ".btn-icon.is-active"):
        block = _re.search(_re.escape(selector) + r"\s*\{([^}]*)\}", css)
        if not block:
            continue
        declarations = _re.sub(r"/\*.*?\*/", "", block.group(1), flags=_re.S)
        for banned in ("background", "border-color", "box-shadow"):
            assert banned not in declarations, (
                f"{selector} paints a surface ({banned}); colour belongs in the glyph — see design-language.md #3"
            )


def _js_string_contexts(text: str) -> dict[int, str]:
    """Innermost string context per character, comment-aware.

    Comment-awareness is the hard-won part: a backtick inside a // comment
    flipped the migration scanner's template parity, which is how fourteen
    `' + wwIcon() + '` splices shipped as LITERAL TEXT on the Rules and
    Silences pages."""
    marks: dict[int, str] = {}
    stack: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        top = stack[-1] if stack else None
        if top == "//":
            if ch == "\n":
                stack.pop()
            i += 1
            continue
        if top == "/*":
            if ch == "*" and text[i : i + 2] == "*/":
                stack.pop()
                i += 2
                continue
            i += 1
            continue
        if top in ("'", '"'):
            if ch == "\\":
                i += 2
                continue
            if ch == top:
                stack.pop()
            else:
                marks[i] = top
            i += 1
            continue
        if top == "`":
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                stack.pop()
            elif ch == "$" and text[i : i + 2] == "${":
                stack.append("${")
                i += 2
                continue
            else:
                marks[i] = "`"
            i += 1
            continue
        if top == "${" and ch == "}":
            stack.pop()
            i += 1
            continue
        if ch == "/" and text[i : i + 2] == "//":
            stack.append("//")
            i += 2
            continue
        if ch == "/" and text[i : i + 2] == "/*":
            stack.append("/*")
            i += 2
            continue
        if ch in ("'", '"', "`"):
            stack.append(ch)
        i += 1
    return marks


def test_icon_splices_match_their_string_context() -> None:
    """Concatenation syntax inside a template literal (or ${} inside a
    single-quoted string) renders as literal source code on screen."""
    import re as _re

    from tests.helpers.paths import PROJECT_ROOT as _ROOT

    offenders = []
    for path in sorted((_ROOT / "templates/static/js").glob("*.js")):
        if path.name.startswith("i18n."):
            continue
        src = path.read_text()
        contexts = _js_string_contexts(src)
        offenders.extend(
            f"{path.name}: concat splice inside a template literal"
            for match in _re.finditer(r"' \+ wwIcon\('[a-z-]+'\) \+ '", src)
            if contexts.get(match.start()) == "`"
        )
        offenders.extend(
            f"{path.name}: ${{}} splice inside a single-quoted string"
            for match in _re.finditer(r"\$\{wwIcon\('[a-z-]+'\)\}", src)
            if contexts.get(match.start()) == "'"
        )
    assert offenders == [], offenders


def test_open_details_carries_context_and_names_its_destination() -> None:
    """\"Open details\" teleported to an unfiltered page: a dead-letter item
    landed on the plain alert inbox with nothing connecting the landing to
    the number just clicked. Two invariants: the button labels WHERE it goes
    (destinationLabel), and context-bearing kinds arrive filtered — dead
    letters pre-select the dead_letter status, exhausted deliveries apply the
    failed result filter via the exported setResult."""
    action_center = _static_js("action-center.js")
    assert "function destinationLabel(view)" in action_center
    assert "btn-dest" in action_center
    assert "KIND_ARRIVAL" in action_center
    assert "dead_letter: function" in action_center
    assert "delivery_exhausted: function" in action_center
    assert "setResult: setResult," in _static_js("decision-trace.js")

    css = _static_css("components.css")
    assert ".stat-card[onclick]::after" in css, "clickable cards need a pre-hover mark"


def test_roi_badges_drill_into_the_trace() -> None:
    """The audit's headline example: a silence saying \"suppressed 42\" and a
    rule saying \"matched 87\" were inert text — the one question they raise
    (WHICH alerts?) had no answer. The trace list now accepts silence_id and
    matched_rule filters end to end, and both badges use them."""
    assert "filterBySilence" in _static_js("decision-trace.js")
    assert "filterByRule" in _static_js("decision-trace.js")
    assert "silence_id" in _static_js("api.js") and "matched_rule" in _static_js("api.js")
    assert "filterBySilence(" in _static_js("silences.js")
    assert "data-drill-rule" in _static_js("forward-rules.js")

    backend = (PROJECT_ROOT / "services/webhooks/decision_trace_queries.py").read_text()
    assert "silence_id: int | None = None" in backend
    assert "matched_rule: str" in backend


def test_scroll_to_alert_never_silently_noops() -> None:
    """Clicking a timeline reference to an off-page alert did nothing at all;
    the robust reveal (clear filters, paginate, fetch) sat one function away."""
    alerts = _static_js("alerts.js")
    import re as _re

    body = _re.search(r"_scrollToAlert\(eventId\) \{(.*?)\n    \}", alerts, _re.S)
    assert body and "focusAlertById(eventId)" in body.group(1)


def test_every_api_path_the_dashboard_calls_exists() -> None:
    """A page can call a route that was never registered, and nothing notices.

    The whole gate is blind to this: the JS is not type-checked against the API,
    a wrong path answers 404 at runtime, and a feature written to degrade
    gracefully — like the prompt-provenance comparison — then does nothing
    forever without an error anywhere. Shipped exactly that way once
    ('/v1/admin/prompt/versions', which is registered as '/v1/prompt/versions').
    """
    import json

    contract = json.loads((PROJECT_ROOT / "build/openapi/openapi.json").read_text())
    known = set(contract.get("paths", {}))

    # Only COMPLETE literals: a path built by concatenation ('/v1/webhooks/' + id)
    # is a prefix, not a route, and checking it here would flag every one of them.
    # The closing quote must be followed by , or ) for the literal to be whole.
    literal_call = re.compile(r"authenticatedFetch\(\s*'(/v1/[A-Za-z0-9/_.-]+)'\s*[,)]")
    js_dir = PROJECT_ROOT / "templates/static/js"
    missing = [
        f"{path.name}: {called}"
        for path in sorted(js_dir.glob("*.js"))
        for called in literal_call.findall(path.read_text())
        if called not in known
    ]

    assert missing == [], f"dashboard calls paths the API does not serve: {missing}"


def test_every_translation_key_the_dashboard_asks_for_exists() -> None:
    """A missing key renders as the key itself, and nothing fails.

    Shipped exactly that: an inbound-rules row showed COMMON.ENABLED,
    common.edit and common.delete to the operator, in both languages, because
    those keys were invented at the call site and never added. Only a
    screenshot caught it.
    """
    js_dir = PROJECT_ROOT / "templates/static/js"
    dictionaries = {}
    for language in ("zh", "en"):
        text = (js_dir / f"i18n.{language}.js").read_text()
        dictionaries[language] = set(re.findall(r"^\s*'([a-zA-Z0-9_.]+)':", text, re.M))

    # Literal single-argument t('...') calls. Keys built by concatenation
    # (t('inbound.action.' + rule.action)) cannot be checked statically.
    asked = re.compile(r"\bt\(\s*'([a-zA-Z0-9_.]+)'\s*[),]")
    missing: list[str] = []
    for path in sorted(js_dir.glob("*.js")):
        if path.name.startswith("i18n."):
            continue
        for key in asked.findall(path.read_text()):
            for language, known in dictionaries.items():
                if key not in known:
                    missing.append(f"{path.name}: {key} ({language})")

    assert missing == [], f"translation keys used but never defined: {sorted(set(missing))[:20]}"


def test_every_data_act_the_dashboard_renders_can_be_dispatched() -> None:
    """A button whose action is not on the allowlist does nothing, silently.

    wwResolveAction is an allowlist by design — a click must not be able to call
    an arbitrary global. The cost is that forgetting to register a new handler
    produces a button that looks right, clicks, and does nothing: shipped
    exactly that on the inbound-rules page, where add, edit and delete were all
    inert until a user reported it.
    """
    js_dir = PROJECT_ROOT / "templates/static/js"
    utils = (js_dir / "utils.js").read_text()

    def _listed(name: str) -> set[str]:
        block = re.search(rf"var {name} = \[(.*?)\];", utils, re.S)
        assert block, f"{name} not found in utils.js"
        return set(re.findall(r"'([A-Za-z0-9_.]+)'", block.group(1)))

    globals_allowed = _listed("WW_ACTION_GLOBALS")
    roots_allowed = _listed("WW_ACTION_ROOTS")

    used: set[str] = set()
    for path in sorted(js_dir.glob("*.js")):
        used.update(re.findall(r'data-act="([A-Za-z0-9_.]+)"', path.read_text()))
    for path in sorted(PROJECT_ROOT.glob("templates/*.html")):
        used.update(re.findall(r'data-act="([A-Za-z0-9_.]+)"', path.read_text()))

    def _dispatchable(name: str) -> bool:
        if "." in name:
            root, _, method = name.partition(".")
            return bool(method) and root in roots_allowed
        return name in globals_allowed

    unroutable = sorted(name for name in used if not _dispatchable(name))

    assert unroutable == [], f"data-act names that cannot be dispatched: {unroutable}"


def test_static_binding_ids_are_unique() -> None:
    """Every data-sb id must belong to exactly one element: the bind helper
    attaches to ALL matches, so a reused id cross-wires handlers (a reused
    sb23 once made clicking the rules search box clear the auth keys)."""
    import collections
    import re

    html = _dashboard_html()
    counts = collections.Counter(re.findall(r'data-sb="(sb\d+)"', html))
    duplicated = {sb: n for sb, n in counts.items() if n > 1}
    assert duplicated == {}, f"data-sb ids used by more than one element: {duplicated}"
