/**
 * Dead Letter queue module
 * Supports filtering, detail viewing, and single and batch replay.
 */

var DeadLettersModule = (function() {
    var currentPage = 1;
    var pageSize = 50;
    var loadedEvents = [];
    var totalEvents = 0;
    var totalPages = 1;
    var selectedIds = {};
    var filters = {
        source: '',
        search: '',
        time_from: '',
        time_to: ''
    };

    var failureLabels = {
        retry_exhausted: { label: 'Retries Exhausted', cls: 'badge-high' },
        fat_err: { label: 'Fatal Error', cls: 'badge-high' },
        processing_error: { label: 'Processing Error', cls: 'badge-medium' }
    };

    function load() {
        currentPage = 1;
        selectedIds = {};
        fetchPage();
    }

    function applyFilters() {
        filters.source = document.getElementById('dlSourceFilter')?.value.trim() || '';
        filters.search = document.getElementById('dlSearchInput')?.value.trim() || '';
        filters.time_from = dateInputToIsoStart(document.getElementById('dlTimeFrom')?.value || '');
        filters.time_to = dateInputToIsoEnd(document.getElementById('dlTimeTo')?.value || '');
        load();
    }

    function resetFilters() {
        ['dlSourceFilter', 'dlSearchInput', 'dlTimeFrom', 'dlTimeTo'].forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.value = '';
        });
        filters = { source: '', search: '', time_from: '', time_to: '' };
        load();
    }

    function dateInputToIsoStart(value) {
        if (!value) return '';
        return new Date(value + 'T00:00:00').toISOString();
    }

    function dateInputToIsoEnd(value) {
        if (!value) return '';
        return new Date(value + 'T23:59:59').toISOString();
    }

    function fetchPage() {
        var container = document.getElementById('deadLettersList');
        if (!container) return;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading...</p></div>';

        API.getDeadLetters({
            page: currentPage,
            page_size: pageSize,
            source: filters.source,
            search: filters.search,
            time_from: filters.time_from,
            time_to: filters.time_to
        }).then(function(res) {
            if (!res.success) {
                renderError(res.error || 'Load failed');
                return;
            }
            loadedEvents = res.data || [];
            totalEvents = res.pagination?.total || loadedEvents.length;
            totalPages = Math.max(1, Math.ceil(totalEvents / pageSize));
            render();
            renderPagination();
            updateReplaySelectedButton();
        }).catch(function(error) {
            renderError(error.message || 'Request failed');
        });
    }

    function renderError(message) {
        var container = document.getElementById('deadLettersList');
        if (!container) return;
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">!</div><div class="empty-title">Load failed</div><div class="empty-text">' + escapeHtml(message) + '</div></div>';
    }

    function render() {
        var container = document.getElementById('deadLettersList');
        if (!container) return;

        if (!loadedEvents.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-title">No dead letters</div><div class="empty-text">No dead-letter events match the current filters</div></div>';
            return;
        }

        var allSelected = loadedEvents.every(function(item) { return selectedIds[item.id]; });
        var html = '<div class="outbox-table-wrap"><table class="outbox-table dead-letters-table"><thead><tr>' +
            '<th class="col-check"><input type="checkbox" ' + (allSelected ? 'checked' : '') + ' onchange="DeadLettersModule.toggleSelectAll(this.checked)"></th>' +
            '<th>ID</th><th>Source</th><th>Failure Reason</th><th>Importance</th><th>Retries</th><th>Time</th><th></th></tr></thead><tbody>';

        loadedEvents.forEach(function(item) {
            var failure = failureLabels[item.failure_reason] || { label: item.failure_reason || 'Unknown', cls: 'badge-new' };
            var timestamp = item.timestamp ? new Date(item.timestamp).toLocaleString('zh-CN') : '-';
            var checked = selectedIds[item.id] ? 'checked' : '';
            var errorTitle = item.error_message || item.failure_reason || '';
            html += '<tr class="outbox-row" data-id="' + item.id + '">';
            html += '<td class="col-check"><input type="checkbox" ' + checked + ' onchange="DeadLettersModule.toggleSelect(' + item.id + ', this.checked)"></td>';
            html += '<td class="outbox-id">#' + item.id + '</td>';
            html += '<td>' + escapeHtml(item.source || '-') + '</td>';
            html += '<td title="' + escapeHtml(errorTitle) + '"><span class="badge ' + failure.cls + '">' + escapeHtml(failure.label) + '</span></td>';
            html += '<td>' + escapeHtml(item.importance || '-') + '</td>';
            html += '<td>' + (item.retry_count != null ? item.retry_count : 0) + '</td>';
            html += '<td class="text-sm">' + escapeHtml(timestamp) + '</td>';
            html += '<td><button class="btn btn-sm" onclick="DeadLettersModule.showDetail(' + item.id + ')">Details</button> ';
            html += '<button class="btn btn-sm" onclick="DeadLettersModule.replay(' + item.id + ')">Replay</button></td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        container.innerHTML = html;
    }

    function renderPagination() {
        var container = document.getElementById('deadLettersPagination');
        if (!container) return;
        if (totalEvents <= pageSize) {
            container.innerHTML = '<div class="pagination-info"><strong>' + totalEvents + '</strong> total</div>';
            return;
        }
        var html = '<div class="pagination"><div class="pagination-info">Page <strong>' + currentPage + '</strong> / <strong>' + totalPages + '</strong>, <strong>' + totalEvents + '</strong> total</div>';
        html += '<div class="pagination-buttons">';
        html += '<button ' + (currentPage <= 1 ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(1)">First</button>';
        html += '<button ' + (currentPage <= 1 ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(' + (currentPage - 1) + ')">Previous</button>';
        html += '<button ' + (currentPage >= totalPages ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(' + (currentPage + 1) + ')">Next</button>';
        html += '<button ' + (currentPage >= totalPages ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(' + totalPages + ')">Last</button>';
        html += '</div></div>';
        container.innerHTML = html;
    }

    function goToPage(page) {
        if (page < 1 || page > totalPages || page === currentPage) return;
        currentPage = page;
        selectedIds = {};
        fetchPage();
    }

    function toggleSelect(id, checked) {
        if (checked) selectedIds[id] = true;
        else delete selectedIds[id];
        updateReplaySelectedButton();
    }

    function toggleSelectAll(checked) {
        if (checked) {
            loadedEvents.forEach(function(item) { selectedIds[item.id] = true; });
        } else {
            selectedIds = {};
        }
        render();
        updateReplaySelectedButton();
    }

    function selectedEventIds() {
        return Object.keys(selectedIds).map(function(id) { return Number(id); });
    }

    function updateReplaySelectedButton() {
        var btn = document.getElementById('dlReplaySelectedBtn');
        if (!btn) return;
        var count = selectedEventIds().length;
        btn.disabled = count === 0;
        btn.textContent = count ? '🔄 Replay Selected (' + count + ')' : '🔄 Replay Selected';
    }

    function replay(id) {
        if (!confirm('Confirm replay of dead-letter event #' + id + '?')) return;
        API.replayDeadLetter(id).then(function(res) {
            if (res.success) {
                showToast(res.message || 'Replayed and re-enqueued', 'success');
                load();
            } else {
                showToast(res.error || 'Replay failed', 'error');
            }
        }).catch(function(error) {
            showToast('Request failed: ' + error.message, 'error');
        });
    }

    function replaySelected() {
        var ids = selectedEventIds();
        if (!ids.length) return;
        if (!confirm('Confirm replay of the ' + ids.length + ' selected dead-letter events?')) return;
        API.replayDeadLettersByIds(ids).then(function(res) {
            if (res.success) {
                showToast(res.message || 'Batch replay done', 'success');
                load();
            } else {
                showToast(res.error || 'Batch replay failed', 'error');
            }
        }).catch(function(error) {
            showToast('Request failed: ' + error.message, 'error');
        });
    }

    function showDetail(id) {
        ensureDetailModal();
        var modal = document.getElementById('deadLetterDetailModal');
        var body = document.getElementById('deadLetterDetailBody');
        if (!modal || !body) return;
        body.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading...</p></div>';
        modal.classList.add('active');
        API.getDeadLetterDetail(id).then(function(res) {
            if (!res.success || !res.data) {
                body.innerHTML = '<div class="empty-state"><div class="empty-title">Load failed</div><div class="empty-text">' + escapeHtml(res.error || 'Unknown error') + '</div></div>';
                return;
            }
            body.innerHTML = renderDetail(res.data);
        }).catch(function(error) {
            body.innerHTML = '<div class="empty-state"><div class="empty-title">Load error</div><div class="empty-text">' + escapeHtml(error.message) + '</div></div>';
        });
    }

    function renderDetail(item) {
        var html = '<div class="detail-table-wrap"><table class="detail-table"><tbody>';
        [
            ['Event ID', '#' + item.id],
            ['Source', item.source || '-'],
            ['request_id', item.request_id || '-'],
            ['client_ip', item.client_ip || '-'],
            ['Status', item.processing_status || '-'],
            ['Failure Reason', item.failure_reason || '-'],
            ['Retry Count', item.retry_count != null ? String(item.retry_count) : '0'],
            ['Importance', item.importance || '-'],
            ['Time', item.timestamp ? new Date(item.timestamp).toLocaleString('zh-CN') : '-']
        ].forEach(function(row) {
            html += '<tr><th>' + escapeHtml(row[0]) + '</th><td>' + escapeHtml(row[1]) + '</td></tr>';
        });
        if (item.error_message) {
            html += '<tr><th>Error Details</th><td><pre class="json-preview">' + escapeHtml(item.error_message) + '</pre></td></tr>';
        }
        html += '</tbody></table></div>';
        if (item.parsed_data) {
            html += '<h4>Parsed Data</h4><pre class="json-preview">' + escapeHtml(JSON.stringify(item.parsed_data, null, 2)) + '</pre>';
        }
        if (item.raw_body) {
            html += '<h4>Raw Payload</h4><pre class="json-preview">' + escapeHtml(item.raw_body) + '</pre>';
        }
        html += '<div class="modal-footer"><button class="btn btn-primary" onclick="DeadLettersModule.replay(' + item.id + ')">Replay This Event</button><button class="btn" onclick="DeadLettersModule.closeDetail()">Close</button></div>';
        return html;
    }

    function ensureDetailModal() {
        if (document.getElementById('deadLetterDetailModal')) return;
        var modal = document.createElement('div');
        modal.id = 'deadLetterDetailModal';
        modal.className = 'modal';
        modal.innerHTML = '<div class="modal-content modal-content-wide"><div class="modal-header"><h2 class="modal-title">Dead Letter Details</h2></div><div class="modal-body" id="deadLetterDetailBody"></div></div>';
        modal.addEventListener('click', function(event) {
            if (event.target === modal) closeDetail();
        });
        document.body.appendChild(modal);
    }

    function closeDetail() {
        var modal = document.getElementById('deadLetterDetailModal');
        if (modal) modal.classList.remove('active');
    }

    return {
        load: load,
        applyFilters: applyFilters,
        resetFilters: resetFilters,
        goToPage: goToPage,
        toggleSelect: toggleSelect,
        toggleSelectAll: toggleSelectAll,
        replay: replay,
        replaySelected: replaySelected,
        showDetail: showDetail,
        closeDetail: closeDetail
    };
})();
