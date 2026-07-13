/** Operator action-center read model. */
const ActionCenterModule = (function () {
    function statCard(label, value, color) {
        return '<div class="stat-card"><div class="stat-label">' + escapeHtml(label) +
            '</div><div class="stat-value" style="color:' + color + ';">' +
            escapeHtml(String(value || 0)) + '</div></div>';
    }

    function render(data) {
        const summary = data.summary || {};
        const summaryEl = document.getElementById('actionCenterSummary');
        const listEl = document.getElementById('actionCenterList');
        if (!summaryEl || !listEl) return;

        summaryEl.innerHTML =
            statCard(t('action.summary.total'), summary.total, 'var(--text-main)') +
            statCard(t('action.summary.critical'), summary.critical, 'var(--danger)') +
            statCard(t('action.summary.warning'), summary.warning, 'var(--warning)') +
            statCard(t('action.summary.deadLetters'), summary.dead_letters, 'var(--primary)');

        const items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            listEl.innerHTML = '<div class="empty-state" style="text-align:center; padding:60px;">' +
                '<div style="font-size:48px; margin-bottom:16px;">✅</div>' +
                '<div class="empty-title">' + escapeHtml(t('action.empty.title')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('action.empty.text')) + '</div></div>';
            return;
        }

        listEl.innerHTML = '<div style="display:flex; flex-direction:column; gap:12px;">' + items.map(function (item) {
            const critical = item.severity === 'critical';
            const color = critical ? 'var(--danger)' : 'var(--warning)';
            const icon = critical ? '🚨' : '⚠️';
            const when = item.occurred_at && typeof formatTime === 'function' ? formatTime(item.occurred_at) : '';
            return '<button type="button" class="action-center-item" data-action-view="' +
                escapeHtml(item.view || '') + '" style="text-align:left; width:100%; background:var(--bg-surface);' +
                ' border:1px solid var(--border); border-left:4px solid ' + color + '; border-radius:var(--radius-lg);' +
                ' padding:16px; cursor:pointer; color:inherit;">' +
                '<div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start;">' +
                '<div><div style="font-weight:700; margin-bottom:6px;">' + icon + ' ' + escapeHtml(item.title || '') +
                (Number(item.count || 1) > 1 ? ' <span class="badge">×' + escapeHtml(String(item.count)) + '</span>' : '') +
                '</div><div style="font-size:0.85rem; color:var(--text-secondary); overflow-wrap:anywhere;">' +
                escapeHtml(item.detail || '') + '</div></div>' +
                '<span style="font-size:0.75rem; color:var(--text-muted); white-space:nowrap;">' + escapeHtml(when) +
                '</span></div></button>';
        }).join('') + '</div>';

        listEl.querySelectorAll('[data-action-view]').forEach(function (button) {
            button.addEventListener('click', function () {
                const view = button.getAttribute('data-action-view');
                if (view === 'routing') {
                    switchMainTab('routing');
                    if (typeof RoutingModule !== 'undefined') RoutingModule.setView('rules');
                } else if (view === 'inbox') {
                    switchMainTab('alerts');
                    if (typeof setInboxView === 'function') setInboxView('alerts');
                } else if (view === 'alerts') {
                    switchMainTab('alerts');
                    if (typeof setInboxView === 'function') setInboxView('alerts');
                } else if (view === 'incidents') {
                    switchMainTab('alerts');
                    if (typeof setInboxView === 'function') setInboxView('incidents');
                } else if (view === 'decision-trace') {
                    switchMainTab('decision-trace');
                    if (typeof DecisionTraceModule !== 'undefined') DecisionTraceModule.setView('trace');
                }
            });
        });
    }

    async function load() {
        const listEl = document.getElementById('actionCenterList');
        if (listEl) listEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        try {
            const response = await API.authenticatedFetch('/v1/action-center');
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            render(payload.data || {});
        } catch (error) {
            if (listEl) {
                listEl.innerHTML = '<div class="empty-state" style="color:var(--danger); padding:40px;">' +
                    escapeHtml(t('common.loadFailed')) + ': ' + escapeHtml(error.message || String(error)) + '</div>';
            }
        }
    }

    return { load: load };
})();
