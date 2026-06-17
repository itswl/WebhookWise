/**
 * Dashboard core logic module
 * Handles initializing all modules, global event binding, and auto-refresh
 */

// Global variables
let autoRefreshInterval = null;
let currentTab = 'alerts';
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
    console.log('🚀 Initializing Dashboard...');

    // Apply static translations to the markup and re-render the active tab on
    // language change so dynamically-rendered content also switches language.
    if (typeof I18N !== 'undefined') {
        I18N.apply();
        I18N.onChange(() => {
            updateAuthButtonState();
            updateAutoRefreshLabel();
            refreshCurrentTab();
        });
    }

    if (typeof API !== 'undefined') {
        await API.initAuthStorage();
    }
    updateAuthButtonState();

    // Initialize each module
    if (typeof AlertsModule !== 'undefined') {
        AlertsModule.init();
        // Set a global reference for use by onclick callbacks
        window.alertsModule = AlertsModule;
    }
    if (typeof AICostModule !== 'undefined') {
        AICostModule.init();
    }
    if (typeof ForwardRulesModule !== 'undefined') {
        ForwardRulesModule.init();
    }
    if (typeof SilencesModule !== 'undefined') {
        SilencesModule.init();
    }

    // Bind global events
    bindGlobalEvents();

    // Start auto-refresh
    startAutoRefresh();

    // Force-clear the search box (to prevent browser autofill)
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.value = '';
        // Clear again after a delay to also catch browser autofill
        setTimeout(() => {
            searchInput.value = '';
        }, 100);
    }

    console.log('✅ Dashboard initialization complete');
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
    console.log('Switching tab:', tabId);
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
        'ai-cost': 'aiCostTab',
        'deep-analyses': 'deepAnalysesTab',
        'outbox': 'outboxTab',
        'forward-rules': 'forwardRulesTab',
        'silences': 'silencesTab'
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
            // Stop deep-analysis auto-refresh when switching to the Alerts Tab
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            break;
        case 'ai-cost':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof AICostModule !== 'undefined') {
                AICostModule.loadStats(AICostModule.currentPeriod || 'day');
            }
            break;
        case 'deep-analyses':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.load();
            }
            break;
        case 'outbox':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof OutboxModule !== 'undefined') {
                OutboxModule.load();
            }
            break;
        case 'forward-rules':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof loadForwardRules === 'function') {
                loadForwardRules();
            }
            break;
        case 'silences':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof loadSilences === 'function') {
                loadSilences();
            }
            break;
    }
}

function refreshCurrentTab() {
    switch (currentTab) {
        case 'ai-cost':
            if (typeof AICostModule !== 'undefined') {
                AICostModule.loadStats(AICostModule.currentPeriod || 'day');
            }
            break;
        case 'deep-analyses':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.load();
            }
            break;
        case 'outbox':
            if (typeof OutboxModule !== 'undefined') {
                OutboxModule.load();
            }
            break;
        case 'forward-rules':
            if (typeof loadForwardRules === 'function') {
                loadForwardRules();
            }
            break;
        case 'silences':
            if (typeof loadSilences === 'function') {
                loadSilences();
            }
            break;
        case 'alerts':
        default:
            if (typeof AlertsModule !== 'undefined') {
                AlertsModule.loadAlerts();
            }
            break;
    }
}

/**
 * Start auto-refresh
 */
function startAutoRefresh() {
    // Auto-refresh is off by default, waiting for the user to enable it manually
    console.log('⏸️ Auto-refresh ready (click the refresh button to start)');
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
        console.log('⏸️ Auto-refresh stopped');
    } else {
        autoRefreshInterval = setInterval(() => {
            refreshCurrentTab();
        }, DASHBOARD_AUTO_REFRESH_INTERVAL_MS);
        console.log('⏵️ Auto-refresh started (every 1 minute)');
    }
    updateAutoRefreshLabel();
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
        console.error('Failed to encrypt and save credentials', error);
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
