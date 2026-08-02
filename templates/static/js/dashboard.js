/**
 * Dashboard core logic module
 * Handles initializing all modules, global event binding, and auto-refresh
 */

// Global variables
let autoRefreshInterval = null;
let currentTab = 'decision-trace';  // the Overview landing tab (hosts Overview|Decision Trace|AI Cost sub-views)
let currentInboxView = 'alerts';
let currentOperationsView = 'actions';
const DASHBOARD_AUTO_REFRESH_INTERVAL_MS = 60000;

/**
 * Navigation.
 *
 * The dashboard has 19 leaf destinations behind 4 tabs and four separate
 * sub-view mechanisms (data-dt-view / -inbox-view / -routing-view /
 * -operations-view). With no URL they were unreachable except by clicking:
 * a refresh always dumped you back on Overview, nothing could be bookmarked
 * or pasted to a colleague, and browser Back left the app entirely.
 *
 * Rather than rewrite the four mechanisms, each one REPORTS the destination it
 * just entered (recordDestination) and a single registry knows how to re-enter
 * one from a URL. Every existing call site therefore gains URL sync without
 * being touched, and navigateTo() gives new code one way to jump anywhere.
 */
// Module lookups stay late-bound and guarded, matching the rest of this file:
// one module failing to parse must not take navigation down with it.
function setSubView(moduleName, view) {
    const module = window[moduleName];
    if (module && typeof module.setView === 'function') {
        module.setView(view);
    } else {
        console.warn('Navigation target unavailable:', moduleName);
    }
}

const DESTINATIONS = {
    overview: { tab: 'decision-trace', enter: () => setSubView('DecisionTraceModule', 'overview') },
    trace: { tab: 'decision-trace', enter: () => setSubView('DecisionTraceModule', 'trace') },
    cost: { tab: 'decision-trace', enter: () => setSubView('DecisionTraceModule', 'cost') },

    alerts: { tab: 'alerts', enter: () => setInboxView('alerts') },
    'work-queue': { tab: 'alerts', enter: () => setInboxView('work-queue') },
    incidents: { tab: 'alerts', enter: () => setInboxView('incidents') },
    investigations: { tab: 'alerts', enter: () => setInboxView('investigations') },

    rules: { tab: 'routing', enter: () => setSubView('RoutingModule', 'rules') },
    silences: { tab: 'routing', enter: () => setSubView('RoutingModule', 'silences') },
    sandbox: { tab: 'routing', enter: () => setSubView('RoutingModule', 'sandbox') },
    audit: { tab: 'routing', enter: () => setSubView('RoutingModule', 'audit') },
    ingress: { tab: 'routing', enter: () => setSubView('RoutingModule', 'ingress') },
    quality: { tab: 'routing', enter: () => setSubView('RoutingModule', 'quality') },
    integrations: { tab: 'routing', enter: () => setSubView('RoutingModule', 'integrations') },

    actions: { tab: 'operations', enter: () => setOperationsView('actions') },
    noise: { tab: 'operations', enter: () => setOperationsView('noise') },
    kb: { tab: 'operations', enter: () => setOperationsView('kb') },
    gaps: { tab: 'operations', enter: () => setOperationsView('gaps') },
    settings: { tab: 'operations', enter: () => setOperationsView('settings') },
};
const DEFAULT_DESTINATION = 'overview';

let currentDestination = DEFAULT_DESTINATION;
let pendingFocus = null;

/** Jump anywhere. `opts.focus` is handed to the destination's module. */
function navigateTo(slug, opts) {
    const destination = DESTINATIONS[slug];
    if (!destination) {
        console.warn('Unknown navigation destination:', slug);
        return false;
    }
    pendingFocus = (opts && opts.focus) || null;
    switchMainTab(destination.tab);
    destination.enter();
    return true;
}

/** The id a destination should reveal on arrival; consumed once. */
function takePendingFocus() {
    const focus = pendingFocus;
    pendingFocus = null;
    return focus;
}

/**
 * Jump to one alert / one incident, from anywhere.
 *
 * Dozens of places rendered "#123" as inert text next to an alert or incident
 * the reader obviously wanted to open. The machinery to open them already
 * existed — AlertsModule.focusAlertById was fully built and called from
 * nowhere — it just had no entry point these renderers could reach.
 */
function openAlert(id) {
    if (!id) return;
    navigateTo('alerts', { focus: String(id) });
}

function openIncident(id) {
    if (!id) return;
    navigateTo('incidents', { focus: String(id) });
}

/** Called by each sub-view mechanism once it has entered a destination. */
function recordDestination(slug) {
    if (!DESTINATIONS[slug]) return;
    currentDestination = slug;
    const focus = pendingFocus ? '/' + encodeURIComponent(pendingFocus) : '';
    const next = '#/' + slug + focus;
    if (window.location.hash !== next) {
        // replaceState, not assignment: a sub-view switch is not a separate
        // history entry, otherwise Back would walk every pill the user touched.
        window.history.replaceState(null, '', next);
    }
}

function applyHashRoute() {
    const raw = String(window.location.hash || '').replace(/^#\/?/, '');
    const [slug, focus] = raw.split('/');
    if (!DESTINATIONS[slug]) {
        navigateTo(DEFAULT_DESTINATION);
        return;
    }
    if (slug === currentDestination && !focus) return; // already here; don't refetch
    navigateTo(slug, focus ? { focus: decodeURIComponent(focus) } : null);
}

/**
 * Initialize the Dashboard
 */
document.addEventListener('DOMContentLoaded', () => {
    initDashboard().catch((error) => {
        console.error('Dashboard initialization failed', error);
    });
});

/**
 * Dashboard initialization function
 */
async function initDashboard() {

    // Initialize theme settings
    initTheme();

    // Register the language re-render hook up front. Do NOT block startup on the
    // dictionary fetch: a slow or stalled dict must never gate module init, event
    // binding, or the first render — that would leave a dead, unclickable shell.
    // Static translations are applied now if the dictionary is already loaded,
    // otherwise once it settles (see the landing-tab load below); either way the
    // shell is fully interactive immediately.
    const i18nReadyAtStart = (typeof I18N === 'undefined')
        || (typeof I18N.isReady === 'function' && I18N.isReady());
    if (typeof I18N !== 'undefined') {
        I18N.onChange(() => {
            updateAuthButtonState();
            updateAutoRefreshLabel();
            refreshCurrentTab();
        });
        if (i18nReadyAtStart) {
            I18N.apply();
        }
    }

    if (typeof API !== 'undefined') {
        await API.initAuthStorage();
    }
    updateAuthButtonState();

    // Initialize each module
    if (typeof OverviewModule !== 'undefined') {
        // OverviewModule is now the default sub-view of the Decision Trace
        // ("Overview") landing tab; DecisionTraceModule.load() loads it. No eager load here.
        OverviewModule.init();
    }
    if (typeof AlertsModule !== 'undefined') {
        AlertsModule.init();
        // Set a global reference for use by onclick callbacks
        window.alertsModule = AlertsModule;
    }
    // The Overview landing tab (data-tab="decision-trace") is loaded further
    // below, gated on the active language dictionary, so its first (and only)
    // render is translated without a second re-render pass.
    // AICostModule is no longer eagerly initialized: the AI Cost view is now a
    // sub-view of the Decision Trace tab and is loaded on demand by
    // DecisionTraceModule.setView('cost'). Its renderer (loadStats) is reused.
    if (typeof ForwardRulesModule !== 'undefined') {
        ForwardRulesModule.init();
    }
    if (typeof SilencesModule !== 'undefined') {
        SilencesModule.init();
    }
    if (typeof SandboxModule !== 'undefined') {
        SandboxModule.init();
    }
    if (typeof RoutingModule !== 'undefined') {
        RoutingModule.init();
    }
    if (typeof ResponseCenterModule !== 'undefined') {
        ResponseCenterModule.init();
    }

    // Bind global events
    bindGlobalEvents();

    // Start auto-refresh
    startAutoRefresh();

    // Load the Overview landing tab. If the active dictionary is already loaded,
    // render now; otherwise wait for it to settle so the first (and only) render
    // is translated — the shell above is already interactive regardless. On a
    // dict load failure we still render (English/key fallbacks) rather than
    // leaving the landing tab stuck on its spinner. Single render = no race.
    const loadLandingTab = () => {
        // Honour a pasted/bookmarked URL. Only fall back to the landing tab
        // when the hash names nothing — otherwise a shared link would load,
        // then immediately bounce the reader to Overview.
        const slug = String(window.location.hash || '').replace(/^#\/?/, '').split('/')[0];
        if (DESTINATIONS[slug]) {
            applyHashRoute();
            return;
        }
        if (typeof DecisionTraceModule !== 'undefined') DecisionTraceModule.load();
    };
    if (typeof I18N === 'undefined' || i18nReadyAtStart) {
        loadLandingTab();
    } else if (I18N.ready && typeof I18N.ready.then === 'function') {
        I18N.ready.finally(() => {
            if (I18N.isReady()) I18N.apply();
            loadLandingTab();
        });
    } else {
        I18N.apply();
        loadLandingTab();
    }

    // Force-clear the search box (to prevent browser autofill)
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.value = '';
        // Clear again after a delay to also catch browser autofill
        setTimeout(() => {
            searchInput.value = '';
        }, 100);
    }

}

/**
 * Bind global events
 */
function bindGlobalEvents() {
    // Tab switching
    document.querySelectorAll('.nav-tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            const navTab = e.target.closest('.nav-tab');
            const tabId = navTab ? navTab.getAttribute('data-tab') : null;
            if (tabId) {
                switchMainTab(tabId);
            }
        });
    });

    document.querySelectorAll('[data-inbox-view]').forEach(button => {
        button.addEventListener('click', (e) => {
            const target = e.target.closest('[data-inbox-view]');
            const view = target ? target.getAttribute('data-inbox-view') : null;
            if (view) setInboxView(view);
        });
    });

    document.querySelectorAll('[data-operations-view]').forEach(button => {
        button.addEventListener('click', (e) => {
            const target = e.target.closest('[data-operations-view]');
            const view = target ? target.getAttribute('data-operations-view') : null;
            if (view) setOperationsView(view);
        });
    });

    // Browser Back/Forward and pasted links.
    window.addEventListener('hashchange', applyHashRoute);

    // Auto-refresh button
    const autoRefreshBtn = document.getElementById('autoRefreshBtn');
    if (autoRefreshBtn) {
        autoRefreshBtn.addEventListener('click', toggleAutoRefresh);
    }

    // Refresh button
    const refreshBtn = document.getElementById('refreshBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', refreshCurrentTab);
    }

    // Close modal when clicking outside it
    document.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal')) {
            if (e.target.id === 'authModal') {
                closeAuthModal();
            } else if (e.target.id === 'incidentResolutionModal' &&
                       typeof IncidentsModule !== 'undefined') {
                IncidentsModule.closeResolutionModal();
            } else if (e.target.id === 'runbookCompletionModal' &&
                       typeof IncidentsModule !== 'undefined') {
                IncidentsModule.closeRunbookCompletionModal();
            } else {
                e.target.classList.remove('active');
            }
        }
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        // ESC closes the modal
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal.active').forEach(modal => {
                if (modal.id === 'authModal') {
                    closeAuthModal();
                } else if (modal.id === 'incidentResolutionModal' &&
                           typeof IncidentsModule !== 'undefined') {
                    IncidentsModule.closeResolutionModal();
                } else if (modal.id === 'runbookCompletionModal' &&
                           typeof IncidentsModule !== 'undefined') {
                    IncidentsModule.closeRunbookCompletionModal();
                } else {
                    modal.classList.remove('active');
                }
            });
        }

        // Ctrl/Cmd + R to refresh
        if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
            e.preventDefault();
            if (typeof AlertsModule !== 'undefined') {
                AlertsModule.loadAlerts();
            }
        }
    });

    // Removed the duplicate pagination button listeners, since onclick events are already bound in the HTML
}

/**
 * Switch the main Tab
 * @param {string} tabId - Tab ID
 */
function switchMainTab(tabId) {
    if (currentTab === 'routing' && tabId !== 'routing' &&
            typeof IngressSetupModule !== 'undefined') {
        IngressSetupModule.deactivate();
    }
    currentTab = tabId;

    // Update the navbar active state
    document.querySelectorAll('.nav-tab').forEach(tab => {
        if (tab.getAttribute('data-tab') === tabId) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });

    // Show/hide content areas
    const tabContents = {
        'alerts': 'alertsTab',
        'decision-trace': 'decisionTraceTab',
        'routing': 'routingTab',
        'operations': 'operationsTab'
    };

    Object.entries(tabContents).forEach(([id, elementId]) => {
        const element = document.getElementById(elementId);
        if (element) {
            element.style.display = id === tabId ? 'block' : 'none';
        }
    });

    window.scrollTo({ top: 0, behavior: 'smooth' });

    // Trigger Tab-specific initialization
    switch (tabId) {
        case 'alerts':
            setInboxView(currentInboxView);
            break;
        case 'decision-trace':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof DecisionTraceModule !== 'undefined') {
                DecisionTraceModule.load();
            }
            break;
        case 'routing':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof RoutingModule !== 'undefined') {
                RoutingModule.load();
            }
            break;
        case 'operations':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            setOperationsView(currentOperationsView);
            break;
    }
}

function setInboxView(view) {
    const views = {
        alerts: 'inboxViewAlerts',
        'work-queue': 'inboxViewWorkQueue',
        incidents: 'inboxViewIncidents',
        investigations: 'inboxViewInvestigations'
    };
    currentInboxView = views[view] ? view : 'alerts';
    Object.keys(views).forEach(function (key) {
        const element = document.getElementById(views[key]);
        if (element) element.style.display = key === currentInboxView ? 'block' : 'none';
    });
    document.querySelectorAll('[data-inbox-view]').forEach(function (button) {
        button.classList.toggle('active', button.getAttribute('data-inbox-view') === currentInboxView);
    });

    recordDestination(currentInboxView);

    // Consumed here, after recordDestination has put it in the URL: the focus
    // can only be applied once the destination's list has actually loaded.
    const focus = takePendingFocus();

    if (currentInboxView === 'work-queue' && typeof ResponseCenterModule !== 'undefined') {
        ResponseCenterModule.loadWorkQueue();
    } else if (currentInboxView === 'incidents' && typeof IncidentsModule !== 'undefined') {
        if (focus && typeof IncidentsModule.focusIncident === 'function') {
            IncidentsModule.focusIncident(focus);
        }
        IncidentsModule.load();
    } else if (currentInboxView === 'investigations' && typeof DeepAnalysesModule !== 'undefined') {
        DeepAnalysesModule.load();
    } else {
        if (typeof DeepAnalysesModule !== 'undefined') DeepAnalysesModule.stopAutoRefresh();
        if (typeof AlertsModule !== 'undefined') {
            const loading = AlertsModule.loadAlerts();
            if (focus && typeof AlertsModule.focusAlertById === 'function') {
                // Focus after the list resolves, and never let a failed reveal
                // surface as an unhandled rejection over the loaded view.
                Promise.resolve(loading)
                    .then(() => AlertsModule.focusAlertById(focus))
                    .catch((error) => console.warn('Could not reveal alert', focus, error));
            }
        }
    }
}

function openInboxIncidents() {
    switchMainTab('alerts');
    setInboxView('incidents');
}

function setOperationsView(view) {
    const views = {
        actions: 'actionCenterTab',
        noise: 'noiseCenterTab',
        kb: 'kbDraftsTab',
        gaps: 'knowledgeGapsTab',
        settings: 'runtimeSettingsTab'
    };
    currentOperationsView = views[view] ? view : 'actions';
    Object.keys(views).forEach(function (key) {
        const element = document.getElementById(views[key]);
        if (element) element.style.display = key === currentOperationsView ? 'block' : 'none';
    });
    document.querySelectorAll('[data-operations-view]').forEach(function (button) {
        button.classList.toggle('active', button.getAttribute('data-operations-view') === currentOperationsView);
    });
    recordDestination(currentOperationsView);

    if (currentOperationsView === 'noise') {
        if (typeof NoiseCenterModule !== 'undefined') NoiseCenterModule.load();
    } else if (currentOperationsView === 'kb') {
        if (typeof KbDraftsModule !== 'undefined') KbDraftsModule.load();
    } else if (currentOperationsView === 'gaps') {
        if (typeof ResponseCenterModule !== 'undefined') ResponseCenterModule.loadKnowledgeGaps();
    } else if (currentOperationsView === 'settings') {
        if (typeof RuntimeSettingsModule !== 'undefined') RuntimeSettingsModule.load();
    } else if (typeof ActionCenterModule !== 'undefined') {
        ActionCenterModule.load();
    }
}

function refreshCurrentTab() {
    switch (currentTab) {
        case 'decision-trace':
            if (typeof DecisionTraceModule !== 'undefined') {
                DecisionTraceModule.load();
            }
            break;
        case 'routing':
            if (typeof RoutingModule !== 'undefined') {
                RoutingModule.refresh();
            }
            break;
        case 'operations':
            setOperationsView(currentOperationsView);
            break;
        case 'alerts':
        default:
            setInboxView(currentInboxView);
            break;
    }
}

/**
 * Start auto-refresh
 */
function startAutoRefresh() {
    // Auto-refresh is off by default, waiting for the user to enable it manually
}

/**
 * Update the auto-refresh button label to match the current state + language.
 */
function updateAutoRefreshLabel() {
    const icon = document.getElementById('autoRefreshIcon');
    const text = document.getElementById('autoRefreshText');
    const on = !!autoRefreshInterval;
    if (icon) icon.textContent = on ? '⏵️' : '⏸️';
    if (text) text.textContent = on ? t('nav.autoRefreshOn') : t('nav.autoRefresh');
}

/**
 * Toggle the auto-refresh state
 */
function toggleAutoRefresh() {
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    } else {
        autoRefreshInterval = setInterval(() => {
            refreshCurrentTab();
        }, DASHBOARD_AUTO_REFRESH_INTERVAL_MS);
    }
    updateAutoRefreshLabel();
}

var _incidentsBadgeTimer = null;
function updateIncidentsBadge() {
    var badge = document.getElementById('incidentsBadge');
    if (!badge) return;
    // Only update badge if API is authenticated
    if (typeof API === 'undefined' || !API.getReadToken()) return;
    API.getIncidents({ status: 'active', page_size: 1 }).then(function (res) {
        if (res && res.success && res.pagination && res.pagination.total != null) {
            var count = res.pagination.total;
            if (count > 0) {
                badge.textContent = count > 99 ? '99+' : String(count);
                badge.style.display = 'inline-block';
            } else {
                badge.style.display = 'none';
            }
        }
    }).catch(function () { /* badge is best-effort */ });
}
if (!_incidentsBadgeTimer) {
    // Poll every 2 minutes — cheap enough (one lightweight count query).
    _incidentsBadgeTimer = setInterval(updateIncidentsBadge, 120000);
    setTimeout(updateIncidentsBadge, 5000); // First update after API token loads
}

let authModalPromise = null;
let resolveAuthModal = null;

function openAuthModal(authMode = '') {
    if (authModalPromise) {
        return authModalPromise;
    }

    const apiKeyInput = document.getElementById('authApiKey');
    const adminWriteKeyInput = document.getElementById('authAdminWriteKey');
    if (apiKeyInput) apiKeyInput.value = '';
    if (adminWriteKeyInput) adminWriteKeyInput.value = '';
    updateAuthButtonState();
    document.getElementById('authModal').classList.add('active');

    authModalPromise = new Promise((resolve) => {
        resolveAuthModal = resolve;
    });

    const preferredInput = authMode === 'write' ? adminWriteKeyInput : apiKeyInput;
    if (preferredInput) {
        window.requestAnimationFrame(() => preferredInput.focus());
    }
    return authModalPromise;
}

function closeAuthModal() {
    document.getElementById('authModal').classList.remove('active');
    const resolver = resolveAuthModal;
    resolveAuthModal = null;
    authModalPromise = null;
    if (resolver) resolver();
}

async function saveAuthKeys() {
    const apiKey = document.getElementById('authApiKey')?.value.trim() || '';
    const adminWriteKey = document.getElementById('authAdminWriteKey')?.value.trim() || '';

    try {
        if (apiKey) {
            await API.setReadToken(apiKey);
        }
        if (adminWriteKey) {
            await API.setWriteToken(adminWriteKey);
        }
    } catch (error) {
        console.error('Failed to save credentials in the browser', error);
        alert(error.message || t('auth.saveFailed'));
        return;
    }

    updateAuthButtonState();
    closeAuthModal();
}

async function clearAuthKeys() {
    await API.clearTokens();
    const apiKeyInput = document.getElementById('authApiKey');
    const adminWriteKeyInput = document.getElementById('authAdminWriteKey');
    if (apiKeyInput) apiKeyInput.value = '';
    if (adminWriteKeyInput) adminWriteKeyInput.value = '';
    updateAuthButtonState();
}

function updateAuthButtonState() {
    if (typeof API === 'undefined') return;
    const status = API.getTokenStatus();
    const readStatus = document.getElementById('authReadStatus');
    const writeStatus = document.getElementById('authWriteStatus');
    const authBtnText = document.getElementById('authBtnText');

    if (readStatus) {
        readStatus.textContent = t(status.read ? 'auth.readSaved' : 'auth.readNotSaved');
    }
    if (writeStatus) {
        writeStatus.textContent = t(status.write ? 'auth.writeSaved' : 'auth.writeNotSaved');
    }
    if (authBtnText) {
        authBtnText.textContent = status.read && status.write ? t('nav.credentialsSaved') : t('nav.credentials');
    }
}

/**
 * Confirm forward (called by the forward modal)
 */
async function confirmForward() {
    if (typeof AlertsModule !== 'undefined') {
        await AlertsModule.confirmForward();
    }
}

/**
 * Close the forward modal
 */
function closeForwardModal() {
    if (typeof AlertsModule !== 'undefined') {
        AlertsModule.closeForwardModal();
    }
}

// ========== Global function wrappers ==========
// Used by HTML onclick events to call module methods

// Alerts module
function loadWebhooks() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.loadAlerts();
}

function loadMoreWebhooks() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.loadMoreAlerts();
}

function filterAlerts() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.filterAlerts();
}

function changePageSize() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.changePageSize();
}

function goToPage(page) {
    if (typeof AlertsModule !== 'undefined') {
        // Support special values
        if (page === 'prev') {
            AlertsModule.goToPage(AlertsModule.currentPage - 1);
        } else if (page === 'next') {
            AlertsModule.goToPage(AlertsModule.currentPage + 1);
        } else if (page === 'last') {
            const totalPages = Math.ceil(AlertsModule.filteredAlerts.length / AlertsModule.pageSize);
            AlertsModule.goToPage(totalPages);
        } else {
            AlertsModule.goToPage(page);
        }
    }
}

// ========== Dark Mode / Theme Toggle Logic ==========

function initTheme() {
    const savedTheme = localStorage.getItem('ww-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (savedTheme === 'dark' || (!savedTheme && prefersDark)) {
        document.documentElement.classList.add('theme-dark');
        const icon = document.getElementById('themeToggleIcon');
        if (icon) icon.textContent = '☀️';
    } else {
        document.documentElement.classList.remove('theme-dark');
        const icon = document.getElementById('themeToggleIcon');
        if (icon) icon.textContent = '🌙';
    }
}

function toggleTheme() {
    const isDark = document.documentElement.classList.contains('theme-dark');
    const icon = document.getElementById('themeToggleIcon');
    if (isDark) {
        document.documentElement.classList.remove('theme-dark');
        localStorage.setItem('ww-theme', 'light');
        if (icon) icon.textContent = '🌙';
    } else {
        document.documentElement.classList.add('theme-dark');
        localStorage.setItem('ww-theme', 'dark');
        if (icon) icon.textContent = '☀️';
    }
    
}
