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
            e.target.classList.remove('active');
        }
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        // ESC closes the modal
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal.active').forEach(modal => {
                modal.classList.remove('active');
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

    if (currentInboxView === 'incidents' && typeof IncidentsModule !== 'undefined') {
        IncidentsModule.load();
    } else if (currentInboxView === 'investigations' && typeof DeepAnalysesModule !== 'undefined') {
        DeepAnalysesModule.load();
    } else {
        if (typeof DeepAnalysesModule !== 'undefined') DeepAnalysesModule.stopAutoRefresh();
        if (typeof AlertsModule !== 'undefined') AlertsModule.loadAlerts();
    }
}

function openInboxIncidents() {
    switchMainTab('alerts');
    setInboxView('incidents');
}

function setOperationsView(view) {
    const views = { actions: 'actionCenterTab', noise: 'noiseCenterTab', kb: 'kbDraftsTab' };
    currentOperationsView = views[view] ? view : 'actions';
    Object.keys(views).forEach(function (key) {
        const element = document.getElementById(views[key]);
        if (element) element.style.display = key === currentOperationsView ? 'block' : 'none';
    });
    document.querySelectorAll('[data-operations-view]').forEach(function (button) {
        button.classList.toggle('active', button.getAttribute('data-operations-view') === currentOperationsView);
    });
    if (currentOperationsView === 'noise') {
        if (typeof NoiseCenterModule !== 'undefined') NoiseCenterModule.load();
    } else if (currentOperationsView === 'kb') {
        if (typeof KbDraftsModule !== 'undefined') KbDraftsModule.load();
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

function openAuthModal() {
    const apiKeyInput = document.getElementById('authApiKey');
    const adminWriteKeyInput = document.getElementById('authAdminWriteKey');
    if (apiKeyInput) apiKeyInput.value = '';
    if (adminWriteKeyInput) adminWriteKeyInput.value = '';
    updateAuthButtonState();
    document.getElementById('authModal').classList.add('active');
}

function closeAuthModal() {
    document.getElementById('authModal').classList.remove('active');
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
    
    // Also re-render chart if Overview tab is active
    if (typeof OverviewModule !== 'undefined' && currentTab === 'decision-trace') {
        const ctx = document.getElementById('overviewTrendChart');
        if (ctx && window.ovTrendChartInstance) {
            // Re-fetch overview data or just trigger chart refresh
            OverviewModule.load(OverviewModule.currentPeriod);
        }
    }
}
