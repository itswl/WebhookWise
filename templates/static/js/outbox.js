/**
 * Outbox / Forward Queue module
 * Displays outbound records such as pending / delivered / failed
 */

var OutboxModule = (function() {
    var currentPage = 0;
    var pageSize = 200;
    var currentStatus = '';
    var loadedRecords = [];
    var totalRecords = 0;
    var totalPages = 1;
    var nextCursor = null;
    var hasMoreRecords = false;
    var isLoadingMore = false;

    var statusMap = {
        'pending': { label: 'Pending', cls: 'badge-medium' },
        'processing': { label: 'Delivering', cls: 'badge-medium' },
        'retrying': { label: 'Retrying', cls: 'badge-medium' },
        'sent': { label: 'Delivered', cls: 'badge-low' },
        'expired': { label: 'Expired', cls: 'badge-new' },
        'exhausted': { label: 'Exhausted', cls: 'badge-high' }
    };

    var targetLabels = {
        'feishu': 'Feishu',
        'webhook': 'Webhook',
        'openclaw': 'OpenClaw'
    };

    var eventLabels = {
        'webhook_forward': 'Alert Forward',
        'manual_forward': 'Manual Forward',
        'rule_test': 'Rule Test',
        'deep_analysis': 'Deep Analysis',
        'ai_error': 'AI Error',
        'ai_degraded': 'AI Degraded',
        'outbox_exhausted': 'Forward Exhausted',
        'deep_analysis_manual': 'Deep Analysis Forward'
    };

    function hasMore() {
        return hasMoreRecords;
    }

    function load() {
        currentPage = 1;
        loadedRecords = [];
        totalRecords = 0;
        totalPages = 1;
        nextCursor = null;
        hasMoreRecords = false;
        var container = document.getElementById('outboxList');
        if (!container) return;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading...</p></div>';
        fetchPage(null, false);
    }

    function fetchPage(cursor, append) {
        var container = document.getElementById('outboxList');
        API.getOutbox({ page_size: pageSize, cursor: cursor, status: currentStatus })
            .then(function(res) {
                if (res.success) {
                    var data = res.data || {};
                    currentPage = append ? currentPage + 1 : 1;
                    totalRecords = data.total || 0;
                    totalPages = data.total_pages || Math.max(1, Math.ceil(totalRecords / pageSize));
                    nextCursor = data.next_cursor || null;
                    hasMoreRecords = !!data.has_more;
                    loadedRecords = append ? loadedRecords.concat(data.items || []) : (data.items || []);
                    if (append) isLoadingMore = false;
                    render();
                } else {
                    isLoadingMore = false;
                    if (append && typeof showToast === 'function') {
                        showToast('Failed to load more: ' + (res.error || 'Unknown error'), 'error');
                    } else if (container) {
                        container.innerHTML = '<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">Load failed</div><div class="empty-text">' + escapeHtml(res.error || 'Unknown error') + '</div></div>';
                    }
                    renderPagination();
                }
            })
            .catch(function(e) {
                isLoadingMore = false;
                if (append && typeof showToast === 'function') {
                    showToast('Failed to load more: ' + e.message, 'error');
                } else if (container) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">Load error</div><div class="empty-text">' + escapeHtml(e.message) + '</div></div>';
                }
                renderPagination();
            });
    }

    function loadMore() {
        if (!hasMore() || isLoadingMore) return;
        isLoadingMore = true;
        renderPagination();
        fetchPage(nextCursor, true);
    }

    function render() {
        var container = document.getElementById('outboxList');
        if (!container) return;

        var records = loadedRecords;
        if (records.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-title">No records</div><div class="empty-text">No forwarding records match the criteria</div></div>';
            renderPagination();
            return;
        }

        var html = '<div class="outbox-table-wrap"><table class="outbox-table"><thead><tr>' +
            '<th>ID</th><th>Event ID</th><th>Rule</th><th>Target</th><th>Event Type</th><th>Status</th><th>Attempts</th><th>Created</th><th></th></tr></thead><tbody>';

        records.forEach(function(r) {
            var st = statusMap[r.status] || { label: r.status || 'Unknown', cls: 'badge-new' };
            var targetType = targetLabels[r.target_type] || r.target_type || '-';
            var eventType = eventLabels[r.event_type] || r.event_type || '-';
            var time = r.created_at ? new Date(r.created_at).toLocaleString('zh-CN') : '-';
            var alertTargetId = r.original_event_id || r.webhook_event_id;
            var alertTitle = r.original_event_id ? 'View original alert #' + r.original_event_id : 'View alert';

            html += '<tr class="outbox-row" data-id="' + r.id + '" onclick="OutboxModule.toggleDetail(' + r.id + ')">';
            html += '<td class="outbox-id">#' + r.id + '</td>';
            html += '<td>' + (r.webhook_event_id ? '<a href="#" onclick="event.preventDefault();event.stopPropagation();OutboxModule.goToAlert(' + alertTargetId + ')" title="' + escapeHtml(alertTitle) + '">#' + r.webhook_event_id + '</a>' : '-') + '</td>';
            html += '<td title="' + escapeHtml(r.rule_name || '') + '">' + escapeHtml((r.rule_name || '') .substring(0, 20) || '-') + '</td>';
            html += '<td title="' + escapeHtml(r.target_url || '') + '">' + escapeHtml(targetType) + (r.target_name ? ' <span class="text-muted text-xs">' + escapeHtml(r.target_name) + '</span>' : '') + '</td>';
            html += '<td>' + escapeHtml(eventType) + (r.is_periodic_reminder ? ' <span class="badge" style="font-size:0.6rem;padding:1px 4px;">Recurring</span>' : '') + '</td>';
            html += '<td><span class="badge ' + st.cls + '">' + st.label + '</span></td>';
            html += '<td>' + r.attempts + '/' + r.max_attempts + '</td>';
            html += '<td class="text-sm">' + time + '</td>';
            html += '<td>' + (r.status === 'exhausted' || r.status === 'expired' || r.status === 'retrying' ? '<button class="btn btn-sm" onclick="event.stopPropagation();OutboxModule.retry(' + r.id + ')" title="Re-enqueue">🔄</button>' : '') + '</td>';
            html += '</tr>';

            // Detail row
            html += '<tr class="outbox-detail" id="outbox-detail-' + r.id + '" style="display:none;"><td colspan="9">';
            html += '<div class="outbox-detail-content">';
            if (r.target_url) html += '<div><strong>Target URL:</strong> <code>' + escapeHtml(r.target_url) + '</code></div>';
            if (r.last_error) html += '<div style="margin-top:0.5rem;color:var(--danger);"><strong>Last error:</strong> ' + escapeHtml(r.last_error) + '</div>';
            if (r.sent_at) html += '<div style="margin-top:0.25rem;"><strong>Delivered at:</strong> ' + escapeHtml(new Date(r.sent_at).toLocaleString('zh-CN')) + '</div>';
            if (r.next_attempt_at) html += '<div style="margin-top:0.25rem;"><strong>Next retry:</strong> ' + escapeHtml(new Date(r.next_attempt_at).toLocaleString('zh-CN')) + '</div>';
            html += '</div></td></tr>';
        });

        html += '</tbody></table></div>';
        container.innerHTML = html;
        renderPagination();
    }

    function renderPagination() {
        var container = document.getElementById('outboxPagination');
        if (!container) return;
        renderLoadMorePagination(container, {
            loaded: loadedRecords.length,
            total: totalRecords,
            batchSize: pageSize,
            hasMore: hasMore(),
            isLoading: isLoadingMore,
            onLoadMore: loadMore
        });
    }

    function toggleDetail(id) {
        var row = document.getElementById('outbox-detail-' + id);
        if (row) row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
    }

    function filterStatus(status) {
        currentStatus = status;
        load();
    }

    function retry(id) {
        if (!confirm('Re-enqueue forwarding record #' + id + '?')) return;
        API.retryOutbox(id).then(function(r) {
            if (r.success) showToast('Re-enqueued', 'success');
            else showToast(r.error || 'Retry failed', 'error');
            load();
        }).catch(function(e) {
            showToast('Request failed: ' + e.message, 'error');
        });
    }

    function goToAlert(webhookId) {
        // Switch to the Alerts tab and expand the corresponding alert
        if (typeof switchMainTab === 'function') switchMainTab('alerts');
        setTimeout(function() {
            if (typeof AlertsModule !== 'undefined' && typeof AlertsModule.focusAlertById === 'function') {
                AlertsModule.focusAlertById(webhookId);
                return;
            }
            var item = document.querySelector('.alert-item[data-id="' + webhookId + '"]');
            if (item) {
                item.scrollIntoView({ behavior: 'smooth', block: 'center' });
                if (!item.classList.contains('expanded')) {
                    item.querySelector('.alert-header').click();
                }
            }
        }, 500);
    }

    document.addEventListener('DOMContentLoaded', function() {
        var statusFilter = document.getElementById('outboxStatusFilter');
        if (statusFilter) statusFilter.addEventListener('change', function() { filterStatus(this.value); });
    });

    return {
        load: load,
        loadMore: loadMore,
        toggleDetail: toggleDetail,
        filterStatus: filterStatus,
        retry: retry,
        goToAlert: goToAlert
    };
})();
