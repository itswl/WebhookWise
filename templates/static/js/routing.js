/**
 * Routing tab — groups the three "how alerts get routed/muted/tested" views
 * (Forward Rules | Silences | Sandbox) under one tab with a sub-view toggle.
 *
 * Each sub-view keeps its original markup, element IDs, and loaders
 * (loadForwardRules / loadSilences / SandboxModule), so the underlying modules
 * are unchanged — this is purely a navigation wrapper, mirroring the Decision
 * Trace tab's Trace|AI-Cost switch.
 */
const RoutingModule = (function () {
    let currentView = 'rules';

    const VIEWS = {
        rules: 'routingViewRules',
        handoff: 'routingViewHandoff',
        inbound: 'routingViewInbound',
        silences: 'routingViewSilences',
        sandbox: 'routingViewSandbox',
        audit: 'routingViewAudit',
        ingress: 'routingViewIngress',
        quality: 'routingViewQuality',
        integrations: 'routingViewIntegrations'
    };

    function loadView(view) {
        if (view === 'rules') {
            if (typeof loadForwardRules === 'function') loadForwardRules();
        } else if (view === 'handoff') {
            if (typeof HandoffModule !== 'undefined') HandoffModule.load();
        } else if (view === 'inbound') {
            if (typeof InboundRulesModule !== 'undefined') InboundRulesModule.load();
        } else if (view === 'silences') {
            if (typeof loadSilences === 'function') loadSilences();
        } else if (view === 'sandbox') {
            // Sandbox form is static; SandboxModule.load() is a no-op but kept for symmetry.
            if (typeof SandboxModule !== 'undefined') SandboxModule.load();
        } else if (view === 'audit') {
            if (typeof RuleAuditModule !== 'undefined') RuleAuditModule.load();
        } else if (view === 'ingress') {
            if (typeof IngressSetupModule !== 'undefined') IngressSetupModule.load();
        } else if (view === 'quality') {
            if (typeof AlertQualityModule !== 'undefined') AlertQualityModule.load();
        } else if (view === 'integrations') {
            if (typeof IntegrationsModule !== 'undefined') IntegrationsModule.load();
        }
    }

    function setView(view) {
        const nextView = VIEWS[view] ? view : 'rules';
        if (currentView === 'ingress' && nextView !== 'ingress' &&
                typeof IngressSetupModule !== 'undefined') {
            IngressSetupModule.deactivate();
        }
        currentView = nextView;
        Object.keys(VIEWS).forEach(function (key) {
            const el = document.getElementById(VIEWS[key]);
            if (el) el.style.display = key === currentView ? 'block' : 'none';
        });
        if (typeof recordDestination === 'function') recordDestination(currentView);
        loadView(currentView);
    }

    return {
        // Sub-view switching arrives via navigateTo/setView (sidebar and
        // palette); there is no pill bar left to bind.
        init: function () {},
        // Called when the Routing tab is opened: show + load the active sub-view.
        load: function () {
            setView(currentView);
        },
        // Refresh button / auto-refresh: reload only the active sub-view.
        refresh: function () {
            loadView(currentView);
        },
        setView: setView
    };
})();

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="RoutingModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('RoutingModule', RoutingModule);
