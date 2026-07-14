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
    let bound = false;

    const VIEWS = {
        rules: 'routingViewRules',
        silences: 'routingViewSilences',
        sandbox: 'routingViewSandbox',
        audit: 'routingViewAudit',
        integrations: 'routingViewIntegrations'
    };

    function loadView(view) {
        if (view === 'rules') {
            if (typeof loadForwardRules === 'function') loadForwardRules();
        } else if (view === 'silences') {
            if (typeof loadSilences === 'function') loadSilences();
        } else if (view === 'sandbox') {
            // Sandbox form is static; SandboxModule.load() is a no-op but kept for symmetry.
            if (typeof SandboxModule !== 'undefined') SandboxModule.load();
        } else if (view === 'audit') {
            if (typeof RuleAuditModule !== 'undefined') RuleAuditModule.load();
        } else if (view === 'integrations') {
            if (typeof IntegrationsModule !== 'undefined') IntegrationsModule.load();
        }
    }

    function setView(view) {
        currentView = VIEWS[view] ? view : 'rules';
        Object.keys(VIEWS).forEach(function (key) {
            const el = document.getElementById(VIEWS[key]);
            if (el) el.style.display = key === currentView ? 'block' : 'none';
        });
        document.querySelectorAll('[data-routing-view]').forEach(function (btn) {
            btn.classList.toggle('active', btn.getAttribute('data-routing-view') === currentView);
        });
        loadView(currentView);
    }

    function bindEvents() {
        if (bound) return;
        document.querySelectorAll('[data-routing-view]').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                const button = e.target.closest('[data-routing-view]');
                const view = button ? button.getAttribute('data-routing-view') : null;
                if (view) setView(view);
            });
        });
        bound = true;
    }

    return {
        init: function () {
            bindEvents();
        },
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
