/** Operator action-center read model. */
const ActionCenterModule = (function () {
    // Backend view names -> navigation destinations. Module-scoped because the
    // renderer also uses it to LABEL each Open-details button with where it
    // goes -- a jump button that hides its destination reads as a teleport.
    const DESTINATION_FOR_VIEW = {
        routing: 'rules',
        inbox: 'alerts',
        alerts: 'alerts',
        incidents: 'incidents',
        'decision-trace': 'trace',
        noise: 'noise',
        overview: 'overview',
    };

    // Per-kind arrival: carry the item's CONTEXT into the destination, so
    // "5 dead letters -> Open details" lands on the alert list already
    // filtered to dead letters instead of teleporting to an unfiltered page.
    const KIND_ARRIVAL = {
        dead_letter: function () {
            const sel = document.getElementById('processingStatusFilter');
            if (sel) sel.value = 'dead_letter';
            navigateTo('alerts');
        },
        stuck_processing: function () {
            // "stuck" is a virtual status the list API expands to the same
            // non-terminal + age predicate the card counted (single-select
            // could not express two statuses plus an age bound).
            const sel = document.getElementById('processingStatusFilter');
            if (sel) sel.value = 'stuck';
            navigateTo('alerts');
        },
        delivery_exhausted: function () {
            navigateTo('trace');
            if (typeof DecisionTraceModule !== 'undefined' && typeof DecisionTraceModule.setResult === 'function') {
                DecisionTraceModule.setResult('failed');
            }
        },
    };

    function destinationLabel(view) {
        const slug = DESTINATION_FOR_VIEW[view];
        if (!slug) return '';
        const key = 'nav.dest.' + (slug === 'work-queue' ? 'workQueue' : slug);
        const translated = t(key);
        return translated === key ? slug : translated;
    }

    // The cards already lift on hover (.stat-card:hover), promising a click
    // that never existed. Each counter now goes where its number came from.
    function statCard(label, value, color, destination) {
        const clickable = destination
            ? ' data-act="navigateTo" data-args="\'' + destination + '\'" style="cursor:pointer;"'
            : '';
        return '<div class="stat-card"' + clickable + '><div class="stat-label">' + escapeHtml(label) +
            '</div><div class="stat-value"' + (color ? ' style="color:' + color + ';"' : '') + '>' +
            escapeHtml(String(value || 0)) + '</div></div>';
    }

    function render(data) {
        const summary = data.summary || {};
        const summaryEl = document.getElementById('actionCenterSummary');
        const listEl = document.getElementById('actionCenterList');
        if (!summaryEl || !listEl) return;

        summaryEl.innerHTML =
            // A zero is quiet: alarm colours are earned by a non-zero count,
            // otherwise a healthy board reads like a wall of warnings.
            statCard(t('action.summary.total'), summary.total, '') +
            statCard(t('action.summary.critical'), summary.critical, summary.critical > 0 ? 'var(--danger)' : '') +
            statCard(t('action.summary.warning'), summary.warning, summary.warning > 0 ? 'var(--warning)' : '') +
            statCard(t('action.summary.deadLetters'), summary.dead_letters, summary.dead_letters > 0 ? 'var(--primary)' : '', 'alerts') +
            statCard(t('action.summary.sla'), summary.sla_breaches, summary.sla_breaches > 0 ? 'var(--danger)' : '', 'work-queue');

        const items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            listEl.innerHTML = '<div class="empty-state" style="text-align:center; padding:60px;">' +
                '<div style="font-size:48px; margin-bottom:16px;">' + wwIcon('check') + '</div>' +
                '<div class="empty-title">' + escapeHtml(t('action.empty.title')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('action.empty.text')) + '</div></div>';
            return;
        }

        listEl.innerHTML = '<div style="display:flex; flex-direction:column; gap:12px;">' + items.map(function (item) {
            const critical = item.severity === 'critical';
            const color = critical ? 'var(--danger)' : 'var(--warning)';
            const icon = critical ? wwIcon('alert-triangle') : wwIcon('alert-triangle');
            const when = item.occurred_at && typeof formatTime === 'function' ? formatTime(item.occurred_at) : '';
            const actionButtons = (item.actions || []).map(function (action) {
                return '<button type="button" class="btn btn-sm btn-primary" data-remediation="' +
                    escapeHtml(action.action || '') + '" data-resource-id="' +
                    escapeHtml(String(action.resource_id || '')) + '" data-resource-type="' +
                    escapeHtml(action.resource_type || '') + '">' + escapeHtml(action.label || 'Run') + '</button>';
            }).join('');
            return '<div class="action-center-item" data-action-view-target="' +
                escapeHtml(item.view || '') + '" data-action-kind="' + escapeHtml(item.kind || '') + '" style="text-align:left; width:100%; background:var(--bg-surface);' +
                ' border:1px solid var(--border); border-left:3px solid ' + color + '; border-radius:var(--radius-lg);' +
                ' padding:16px; color:inherit;">' +
                '<div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start;">' +
                '<div><div style="font-weight:700; margin-bottom:6px;">' + icon + ' ' + escapeHtml(item.title || '') +
                (Number(item.count || 1) > 1 ? ' <span class="badge">×' + escapeHtml(String(item.count)) + '</span>' : '') +
                '</div><div style="font-size:0.85rem; color:var(--text-secondary); overflow-wrap:anywhere;">' +
                escapeHtml(item.detail || '') + '</div></div>' +
                '<span style="font-size:0.75rem; color:var(--text-muted); white-space:nowrap;">' + escapeHtml(when) +
                '</span></div>' +
                '<div style="display:flex; gap:8px; margin-top:12px; align-items:center;">' + actionButtons +
                '<button type="button" class="btn btn-sm" data-open-action-view>' + escapeHtml(t('action.openDetails')) +
                (destinationLabel(item.view) ? ' <span class="btn-dest">' + wwIcon('external-link', 'icon-sm') + ' ' + escapeHtml(destinationLabel(item.view)) + '</span>' : '') +
                '</button></div></div>';
        }).join('') + '</div>';

        listEl.querySelectorAll('[data-open-action-view]').forEach(function (button) {
            button.addEventListener('click', function () {
                const container = button.closest('[data-action-view-target]');
                const view = container.getAttribute('data-action-view-target');
                const kind = container.getAttribute('data-action-kind');
                if (KIND_ARRIVAL[kind]) {
                    KIND_ARRIVAL[kind]();
                    return;
                }
                navigateTo(DESTINATION_FOR_VIEW[view] || view);
            });
        });
        listEl.querySelectorAll('[data-remediation]').forEach(function (button) {
            button.addEventListener('click', async function () {
                await remediate(button);
            });
        });
    }

    async function remediate(button) {
        const payload = {
            action: button.getAttribute('data-remediation'),
            batch_size: 50
        };
        const resourceId = button.getAttribute('data-resource-id');
        const resourceType = button.getAttribute('data-resource-type');
        if (resourceId) payload.resource_id = Number(resourceId);
        if (resourceType) payload.resource_type = resourceType;
        button.disabled = true;
        try {
            const response = await API.authenticatedFetch('/v1/action-center/actions', {
                method: 'POST', body: JSON.stringify(payload)
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || 'HTTP ' + response.status);
            const undo = result.data && result.data.undo;
            if (undo && window.confirm('Action completed. Undo it now?')) {
                await API.authenticatedFetch('/v1/action-center/actions', {
                    method: 'POST', body: JSON.stringify({ ...undo, batch_size: 50 })
                });
            }
            await load();
        } catch (error) {
            showToast(t('action.msg.failed') + ': ' + (error.message || String(error)), 'error');
            button.disabled = false;
        }
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

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="ActionCenterModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('ActionCenterModule', ActionCenterModule);
