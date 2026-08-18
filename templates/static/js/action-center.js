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
            // Server titles/labels are English by contract; title_key +
            // title_params (and the action name) let us render a localized
            // equivalent, falling back to the server string for unknown keys.
            let title = item.title || '';
            if (item.title_key) {
                const key = 'action.t.' + item.title_key;
                const localized = t(key, item.title_params || {});
                if (localized !== key) title = localized;
            }
            const color = critical ? 'var(--danger)' : 'var(--warning)';
            const icon = critical ? wwIcon('alert-triangle') : wwIcon('alert-triangle');
            const when = item.occurred_at && typeof formatTime === 'function' ? formatTime(item.occurred_at) : '';
            const actionButtons = (item.actions || []).map(function (action) {
                const btnKey = 'action.btn.' + (action.action || '');
                const btnLocalized = t(btnKey);
                const btnLabel = btnLocalized !== btnKey ? btnLocalized : (action.label || 'Run');
                return '<button type="button" class="btn btn-sm btn-primary" data-remediation="' +
                    escapeHtml(action.action || '') + '" data-resource-id="' +
                    escapeHtml(String(action.resource_id || '')) + '" data-resource-type="' +
                    escapeHtml(action.resource_type || '') + '">' + escapeHtml(btnLabel) + '</button>';
            }).join('');
            return '<div class="action-center-item" data-action-view-target="' +
                escapeHtml(item.view || '') + '" data-action-kind="' + escapeHtml(item.kind || '') + '" style="text-align:left; width:100%; background:var(--bg-surface);' +
                ' border:1px solid var(--border); border-left:3px solid ' + color + '; border-radius:var(--radius-lg);' +
                ' padding:16px; color:inherit;">' +
                '<div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start;">' +
                '<div><div style="font-weight:700; margin-bottom:6px;">' + icon + ' ' + escapeHtml(title) +
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

    // ── Proposed commands awaiting a human decision ───────────────────────
    //
    // An agent can propose one of the Action Center's own commands
    // (propose_remediation over MCP, or the admin API). A proposal executes
    // NOTHING until somebody approves it here, and approving runs the same
    // endpoint the buttons above run. This block is that decision surface --
    // without it the feature exists only for whoever is willing to write curl.

    function proposalRow(item) {
        const resource = item.resource_id
            ? escapeHtml(String(item.resource_type || '') + '#' + String(item.resource_id))
            : '';
        const expires = item.expires_at && typeof formatTime === 'function' ? formatTime(item.expires_at) : '';
        // The reason is prose written by the proposer and stored verbatim, so it
        // is escaped like any untrusted string -- an agent that read it off an
        // alert payload must not be able to inject markup through it.
        // The same action names are already localized for the buttons below
        // (action.btn.*); showing a raw enum here would make one page speak two
        // languages about the same command. The identifier stays, muted, because
        // whoever audits the decision needs the exact command that ran.
        const actionKey = 'action.btn.' + String(item.action || '');
        const actionLabel = t(actionKey) !== actionKey ? t(actionKey) : String(item.action || '');
        return '<div class="action-center-item" data-proposal-id="' + escapeHtml(String(item.id)) +
            '" style="text-align:left; width:100%; background:var(--bg-surface);' +
            ' border:1px solid var(--border); border-left:3px solid var(--primary);' +
            ' border-radius:var(--radius-lg); padding:16px; color:inherit;">' +
            '<div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start;">' +
            '<div><div style="font-weight:700; margin-bottom:6px;">' +
            wwIcon('lightbulb') + ' ' + escapeHtml(actionLabel) +
            ' <code style="font-weight:400; font-size:0.75rem; color:var(--text-muted);">' +
            escapeHtml(String(item.action || '')) + '</code>' +
            (resource ? ' <span class="badge">' + resource + '</span>' : '') +
            '</div><div style="font-size:0.85rem; color:var(--text-secondary); overflow-wrap:anywhere;">' +
            escapeHtml(String(item.reason || '')) + '</div></div>' +
            '<span style="font-size:0.75rem; color:var(--text-muted); white-space:nowrap;">' +
            escapeHtml(String(item.proposed_by || '')) + '</span></div>' +
            '<div style="display:flex; gap:8px; margin-top:12px; align-items:center;">' +
            '<button type="button" class="btn btn-sm btn-primary" data-proposal-decision="approve">' +
            wwIcon('check', 'icon-sm') + ' ' + escapeHtml(t('action.proposals.approve')) + '</button>' +
            '<button type="button" class="btn btn-sm" data-proposal-decision="reject">' +
            wwIcon('x', 'icon-sm') + ' ' + escapeHtml(t('action.proposals.reject')) + '</button>' +
            (expires
                ? '<span style="font-size:0.75rem; color:var(--text-muted); margin-left:auto;">' +
                  wwIcon('clock', 'icon-sm') + ' ' + escapeHtml(t('action.proposals.expiresAt')) + ' ' +
                  escapeHtml(expires) + '</span>'
                : '') +
            '</div></div>';
    }

    function renderProposals(items) {
        const el = document.getElementById('actionCenterProposals');
        if (!el) return;
        const pending = (Array.isArray(items) ? items : []).filter(function (item) {
            // The server marks an expired-but-undecided row as expired on read;
            // trust that rather than re-deriving the deadline in the browser.
            return item && item.status === 'pending';
        });
        if (!pending.length) {
            el.innerHTML = '';
            return;
        }
        el.innerHTML = '<div class="section-title" style="font-size:var(--fs-sm); margin-bottom:4px;">' +
            escapeHtml(t('action.proposals.title')) + ' <span class="badge">' + escapeHtml(String(pending.length)) +
            '</span></div>' +
            // Said before the click, not only in the confirm dialog: nothing here
            // has run yet, and approving is what runs it.
            '<p style="margin:0 0 12px; color:var(--text-secondary); font-size:var(--fs-sm);">' +
            escapeHtml(t('action.proposals.subtitle')) + '</p>' +
            '<div style="display:flex; flex-direction:column; gap:12px; margin-bottom:24px;">' +
            pending.map(proposalRow).join('') + '</div>';

        el.querySelectorAll('[data-proposal-decision]').forEach(function (button) {
            button.addEventListener('click', async function () {
                await decideProposal(button);
            });
        });
    }

    async function decideProposal(button) {
        const decision = button.getAttribute('data-proposal-decision');
        const row = button.closest('[data-proposal-id]');
        if (!row) return;
        const id = row.getAttribute('data-proposal-id');
        // Approving runs a real command against production, so it asks first.
        // Rejecting only closes the row and needs no ceremony.
        if (decision === 'approve' && !(await wwConfirm(t('action.proposals.confirmApprove')))) return;

        row.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
        try {
            const response = await API.authenticatedFetch(
                '/v1/action-center/proposals/' + encodeURIComponent(id) + '/' + decision,
                { method: 'POST' }
            );
            const result = await response.json();
            if (response.status === 502) {
                // Approved, and the execution failed. Not a rejection and not a
                // refusal to approve -- the operator has to learn the difference.
                showToast(t('action.proposals.executionFailed') + ': ' + (result.error || ''), 'error');
            } else if (!response.ok || !result.success) {
                throw new Error(result.error || 'HTTP ' + response.status);
            } else if (decision === 'reject') {
                showToast(t('action.proposals.rejected'), 'success');
            } else if (result.data && result.data.result && result.data.result.changed === false) {
                showToast(t('action.proposals.ranNothing'), 'warning');
            } else {
                showToast(t('action.proposals.approved'), 'success');
            }
            await load();
        } catch (error) {
            showToast(t('action.proposals.decideFailed') + ': ' + (error.message || String(error)), 'error');
            row.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
        }
    }

    async function loadProposals() {
        const el = document.getElementById('actionCenterProposals');
        if (!el) return;
        try {
            const response = await API.authenticatedFetch('/v1/action-center/proposals?status=pending');
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            renderProposals((payload.data || {}).items);
        } catch (error) {
            // Supplementary to the board: a failure here must not take the
            // Action Center down with it, but it must not be silent either.
            el.innerHTML = '<div style="color:var(--danger); font-size:var(--fs-sm); margin-bottom:16px;">' +
                escapeHtml(t('action.proposals.loadFailed')) + ': ' +
                escapeHtml(error.message || String(error)) + '</div>';
        }
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
            if (undo && (await wwConfirm(t('action.msg.undoPrompt')))) {
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
            await loadProposals();
        } catch (error) {
            if (listEl) {
                listEl.innerHTML = '<div class="empty-state" style="color:var(--danger); padding:40px;">' +
                    escapeHtml(t('common.loadFailed')) + ': ' + escapeHtml(error.message || String(error)) + '</div>';
            }
        }
    }

    // _decideProposal is exposed for the headless harness: approving executes a
    // real command, so the confirm-first / 502-is-not-success behaviour has to be
    // testable without a browser.
    return { load: load, _decideProposal: decideProposal };
})();

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="ActionCenterModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('ActionCenterModule', ActionCenterModule);
