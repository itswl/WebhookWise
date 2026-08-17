/**
 * Delivery queue: browse the forwarding outbox and triage dead letters.
 *
 * Both API surfaces existed for a long time with no UI reaching them — bulk
 * dead-letter replay was API-only, and the outbox could not be browsed at
 * all. This is deliberately a workbench, not a dashboard: filter, look at
 * the error, replay.
 */
const DeliveryQueueModule = (function () {
    let outboxCursor = null;
    let outboxRows = [];
    let dlPage = 1;
    let dlHasMore = false;
    let dlRows = [];
    const selectedDl = new Set();

    const STATUS_DOT = {
        sent: 'ww-dot-success',
        pending: 'ww-dot-muted',
        processing: 'ww-dot-muted',
        retrying: 'ww-dot-warning',
        exhausted: 'ww-dot-danger',
        expired: 'ww-dot-danger',
    };

    function statusBadge(status) {
        const dot = STATUS_DOT[status] || 'ww-dot-muted';
        return '<span class="badge badge-outline"><span class="ww-dot ' + dot + '"></span>' +
            escapeHtml(t('delivery.status.' + status, null, status)) + '</span>';
    }

    // ── Outbox ────────────────────────────────────────────────────────────

    async function loadOutbox(reset) {
        const container = document.getElementById('deliveryOutboxList');
        if (!container) return;
        if (reset) {
            outboxCursor = null;
            outboxRows = [];
            container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        }
        const statusSel = document.getElementById('deliveryOutboxStatus');
        try {
            const result = await API.getOutbox({
                page_size: 50,
                cursor: outboxCursor,
                status: statusSel ? statusSel.value : '',
            });
            if (!result.success) throw new Error(result.error || 'request failed');
            outboxRows = outboxRows.concat(result.data || []);
            const pg = result.pagination || {};
            outboxCursor = pg.has_more ? pg.next_cursor : null;
            renderOutbox(container, pg.total);
        } catch (error) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('alert-triangle') + '</div>' +
                '<div class="empty-title">' + t('common.loadFailed') + '</div>' +
                '<div class="empty-text">' + escapeHtml(error.message || String(error)) + '</div>' +
                '<button class="btn" data-act="DeliveryQueueModule.reloadOutbox">' + t('common.retry') + '</button></div>';
        }
    }

    function renderOutbox(container, total) {
        if (!outboxRows.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('send') + '</div>' +
                '<div class="empty-title">' + t('delivery.outbox.empty') + '</div></div>';
            return;
        }
        let html = '';
        if (typeof total === 'number') {
            html += '<div style="color: var(--text-muted); font-size: var(--fs-sm); margin-bottom: 0.5rem;">' +
                escapeHtml(t('delivery.outbox.total', { n: formatNumber(total) })) + '</div>';
        }
        const th = 'padding: 0.5rem 0.65rem; font-weight: 600; text-align: left; white-space: nowrap;';
        const td = 'padding: 0.5rem 0.65rem; border-top: 1px solid var(--border); vertical-align: top;';
        html += '<div style="overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface);">';
        html += '<table style="width: 100%; border-collapse: collapse; font-size: var(--fs-sm);">';
        html += '<thead><tr style="color: var(--text-muted);">' +
            '<th style="' + th + '">ID</th>' +
            '<th style="' + th + '">' + t('delivery.col.alert') + '</th>' +
            '<th style="' + th + '">' + t('delivery.col.target') + '</th>' +
            '<th style="' + th + '">' + t('delivery.col.status') + '</th>' +
            '<th style="' + th + '">' + t('delivery.col.attempts') + '</th>' +
            '<th style="' + th + '">' + t('delivery.col.lastError') + '</th>' +
            '<th style="' + th + '">' + t('delivery.col.time') + '</th>' +
            '<th style="' + th + '"></th>' +
            '</tr></thead><tbody>';
        outboxRows.forEach(function (row) {
            const target = (row.rule_name || row.target_name || row.target_type || '—');
            const alertRef = row.webhook_event_id
                ? '<a href="#/alerts/' + Number(row.webhook_event_id) + '">#' + Number(row.webhook_event_id) + '</a>'
                : '<span style="color: var(--text-muted);">' + escapeHtml(row.event_type || '—') + '</span>';
            const retryable = row.status === 'exhausted' || row.status === 'expired' || row.status === 'retrying';
            html += '<tr>' +
                '<td style="' + td + '">' + Number(row.id) + '</td>' +
                '<td style="' + td + '">' + alertRef + '</td>' +
                '<td style="' + td + ' max-width: 220px; overflow: hidden; text-overflow: ellipsis;" title="' + escapeHtml(String(target)) + '">' +
                    escapeHtml(String(target).slice(0, 60)) + '<div style="color: var(--text-muted); font-size: var(--fs-xs);">' + escapeHtml(row.target_type || '') + '</div></td>' +
                '<td style="' + td + '">' + statusBadge(String(row.status || '')) + '</td>' +
                '<td style="' + td + ' white-space: nowrap;">' + Number(row.attempts || 0) + '/' + Number(row.max_attempts || 0) + '</td>' +
                '<td style="' + td + ' max-width: 280px;" title="' + escapeHtml(String(row.last_error || '')) + '">' +
                    '<span style="color: var(--text-secondary);">' + escapeHtml(String(row.last_error || '—').slice(0, 90)) + '</span></td>' +
                '<td style="' + td + ' white-space: nowrap; color: var(--text-muted);">' + escapeHtml(formatTime(row.last_attempt_at || row.created_at)) + '</td>' +
                '<td style="' + td + '">' +
                    (retryable ? '<button class="btn btn-sm" data-act="DeliveryQueueModule.retryOutboxRow" data-args="' + Number(row.id) + '">' + wwIcon('refresh') + ' ' + t('delivery.action.retry') + '</button>' : '') +
                '</td>' +
                '</tr>';
        });
        html += '</tbody></table></div>';
        if (outboxCursor !== null) {
            html += '<div style="margin-top: 0.75rem;"><button class="btn" data-act="DeliveryQueueModule.loadMoreOutbox">' +
                t('utils.loadMore', { n: 50 }) + '</button></div>';
        }
        container.innerHTML = html;
    }

    async function retryOutboxRow(id) {
        try {
            const result = await API.retryOutbox(id);
            if (result.success) {
                showToast(t('delivery.msg.retryQueued', { id: id }), 'success');
                loadOutbox(true);
            } else {
                showToast(t('delivery.msg.retryFailed') + ': ' + (result.error || ''), 'error');
            }
        } catch (error) {
            showToast(t('delivery.msg.retryFailed') + ': ' + (error.message || String(error)), 'error');
        }
    }

    // ── Dead letters ──────────────────────────────────────────────────────

    async function loadDeadLetters(page) {
        const container = document.getElementById('deliveryDeadLetters');
        if (!container) return;
        dlPage = page || 1;
        if (dlPage === 1) selectedDl.clear();
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        try {
            const result = await API.getDeadLetters({ page: dlPage, page_size: 20 });
            if (!result.success) throw new Error(result.error || 'request failed');
            dlRows = result.data || [];
            const pg = result.pagination || {};
            dlHasMore = !!pg.has_more;
            renderDeadLetters(container, pg.total);
        } catch (error) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('alert-triangle') + '</div>' +
                '<div class="empty-title">' + t('common.loadFailed') + '</div>' +
                '<div class="empty-text">' + escapeHtml(error.message || String(error)) + '</div>' +
                '<button class="btn" data-act="DeliveryQueueModule.reloadDeadLetters">' + t('common.retry') + '</button></div>';
        }
    }

    function renderDeadLetters(container, total) {
        if (!dlRows.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('check') + '</div>' +
                '<div class="empty-title">' + t('delivery.dl.empty') + '</div>' +
                '<div class="empty-text">' + t('delivery.dl.emptyText') + '</div></div>';
            return;
        }
        let html = '<div style="display: flex; gap: 8px; align-items: center; margin-bottom: 0.75rem; flex-wrap: wrap;">';
        html += '<button class="btn btn-sm" data-act="DeliveryQueueModule.toggleSelectPage">' + t('delivery.dl.selectPage') + '</button>';
        html += '<button class="btn btn-sm" data-act="DeliveryQueueModule.replaySelected">' + wwIcon('play') + ' ' + t('delivery.dl.replaySelected') + '</button>';
        html += '<button class="btn btn-sm" data-act="DeliveryQueueModule.replayAll">' + wwIcon('skip-forward') + ' ' + t('delivery.dl.replayAll') + '</button>';
        if (typeof total === 'number') {
            html += '<span style="color: var(--text-muted); font-size: var(--fs-sm); margin-left: auto;">' +
                escapeHtml(t('delivery.dl.total', { n: formatNumber(total) })) + '</span>';
        }
        html += '</div>';

        const td = 'padding: 0.5rem 0.65rem; border-top: 1px solid var(--border); vertical-align: top;';
        html += '<div style="overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface);">';
        html += '<table style="width: 100%; border-collapse: collapse; font-size: var(--fs-sm);"><tbody>';
        dlRows.forEach(function (row) {
            const checked = selectedDl.has(Number(row.id)) ? ' checked' : '';
            html += '<tr>' +
                '<td style="' + td + ' width: 1%;"><input type="checkbox" data-dl-select="' + Number(row.id) + '"' + checked +
                    ' aria-label="' + escapeHtml(t('delivery.dl.selectOne', { id: row.id })) + '"></td>' +
                '<td style="' + td + ' white-space: nowrap;"><a href="#/alerts/' + Number(row.id) + '">#' + Number(row.id) + '</a></td>' +
                '<td style="' + td + '">' + escapeHtml(String(row.source || '—')) +
                    '<div style="color: var(--text-muted); font-size: var(--fs-xs);">' + escapeHtml(String(row.importance || '')) + '</div></td>' +
                '<td style="' + td + ' max-width: 340px;"><span style="color: var(--text-secondary);">' +
                    escapeHtml(String(row.failure_reason || row.error_message || '—').slice(0, 120)) + '</span>' +
                    '<div style="color: var(--text-muted); font-size: var(--fs-xs);">' + escapeHtml(t('delivery.dl.retries', { n: Number(row.retry_count || 0) })) + '</div></td>' +
                '<td style="' + td + ' white-space: nowrap; color: var(--text-muted);">' + escapeHtml(formatTime(row.created_at || row.timestamp)) + '</td>' +
                '<td style="' + td + ' white-space: nowrap;">' +
                    '<button class="btn btn-sm" data-act="DeliveryQueueModule.replayOne" data-args="' + Number(row.id) + '">' + wwIcon('play') + ' ' + t('delivery.dl.replay') + '</button>' +
                '</td>' +
                '</tr>';
        });
        html += '</tbody></table></div>';

        html += '<div style="display: flex; gap: 8px; margin-top: 0.75rem;">';
        if (dlPage > 1) {
            html += '<button class="btn btn-sm" data-act="DeliveryQueueModule.deadLetterPage" data-args="' + (dlPage - 1) + '">' + t('common.prevPage') + '</button>';
        }
        if (dlHasMore) {
            html += '<button class="btn btn-sm" data-act="DeliveryQueueModule.deadLetterPage" data-args="' + (dlPage + 1) + '">' + t('common.nextPage') + '</button>';
        }
        html += '</div>';
        container.innerHTML = html;

        container.querySelectorAll('[data-dl-select]').forEach(function (box) {
            box.addEventListener('change', function () {
                const id = Number(box.getAttribute('data-dl-select'));
                if (box.checked) selectedDl.add(id); else selectedDl.delete(id);
            });
        });
    }

    function toggleSelectPage() {
        const boxes = document.querySelectorAll('[data-dl-select]');
        const allSelected = Array.from(boxes).every(function (box) { return box.checked; });
        boxes.forEach(function (box) {
            box.checked = !allSelected;
            const id = Number(box.getAttribute('data-dl-select'));
            if (box.checked) selectedDl.add(id); else selectedDl.delete(id);
        });
    }

    async function replayOne(id) {
        try {
            const result = await API.replayDeadLetter(id);
            if (result.success) {
                showToast(t('delivery.msg.replayQueued', { n: 1 }), 'success');
                loadDeadLetters(dlPage);
            } else {
                showToast(t('delivery.msg.replayFailed') + ': ' + (result.error || ''), 'error');
            }
        } catch (error) {
            showToast(t('delivery.msg.replayFailed') + ': ' + (error.message || String(error)), 'error');
        }
    }

    async function replaySelected() {
        const ids = Array.from(selectedDl);
        if (!ids.length) {
            showToast(t('delivery.msg.nothingSelected'), 'warning');
            return;
        }
        if (!(await wwConfirm(t('delivery.confirm.replaySelected', { n: ids.length })))) return;
        try {
            const result = await API.replayDeadLettersByIds(ids);
            if (result.success) {
                const replayed = (result.data && result.data.replayed_count != null) ? result.data.replayed_count : ids.length;
                showToast(t('delivery.msg.replayQueued', { n: replayed }), 'success');
                loadDeadLetters(1);
            } else {
                showToast(t('delivery.msg.replayFailed') + ': ' + (result.error || ''), 'error');
            }
        } catch (error) {
            showToast(t('delivery.msg.replayFailed') + ': ' + (error.message || String(error)), 'error');
        }
    }

    async function replayAll() {
        if (!(await wwConfirm(t('delivery.confirm.replayAll'), { danger: true }))) return;
        try {
            const result = await API.replayAllDeadLetters(200);
            if (result.success) {
                const replayed = (result.data && result.data.replayed_count != null) ? result.data.replayed_count : '?';
                showToast(t('delivery.msg.replayQueued', { n: replayed }), 'success');
                loadDeadLetters(1);
            } else {
                showToast(t('delivery.msg.replayFailed') + ': ' + (result.error || ''), 'error');
            }
        } catch (error) {
            showToast(t('delivery.msg.replayFailed') + ': ' + (error.message || String(error)), 'error');
        }
    }

    return {
        load: function () {
            loadOutbox(true);
            loadDeadLetters(1);
        },
        reloadOutbox: function () { loadOutbox(true); },
        loadMoreOutbox: function () { loadOutbox(false); },
        retryOutboxRow: retryOutboxRow,
        reloadDeadLetters: function () { loadDeadLetters(dlPage); },
        deadLetterPage: function (page) { loadDeadLetters(Number(page) || 1); },
        toggleSelectPage: toggleSelectPage,
        replayOne: replayOne,
        replaySelected: replaySelected,
        replayAll: replayAll,
    };
})();

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="DeliveryQueueModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('DeliveryQueueModule', DeliveryQueueModule);
