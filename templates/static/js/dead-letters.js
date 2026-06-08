/**
 * 死信队列模块
 * 支持筛选、详情查看、单条和批量重放。
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
        retry_exhausted: { label: '重试耗尽', cls: 'badge-high' },
        fat_err: { label: '致命错误', cls: 'badge-high' },
        processing_error: { label: '处理错误', cls: 'badge-medium' }
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
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>加载中...</p></div>';

        API.getDeadLetters({
            page: currentPage,
            page_size: pageSize,
            source: filters.source,
            search: filters.search,
            time_from: filters.time_from,
            time_to: filters.time_to
        }).then(function(res) {
            if (!res.success) {
                renderError(res.error || '加载失败');
                return;
            }
            loadedEvents = res.data || [];
            totalEvents = res.pagination?.total || loadedEvents.length;
            totalPages = Math.max(1, Math.ceil(totalEvents / pageSize));
            render();
            renderPagination();
            updateReplaySelectedButton();
        }).catch(function(error) {
            renderError(error.message || '请求失败');
        });
    }

    function renderError(message) {
        var container = document.getElementById('deadLettersList');
        if (!container) return;
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">!</div><div class="empty-title">加载失败</div><div class="empty-text">' + escapeHtml(message) + '</div></div>';
    }

    function render() {
        var container = document.getElementById('deadLettersList');
        if (!container) return;

        if (!loadedEvents.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-title">暂无死信</div><div class="empty-text">当前筛选条件下没有 dead-letter 事件</div></div>';
            return;
        }

        var allSelected = loadedEvents.every(function(item) { return selectedIds[item.id]; });
        var html = '<div class="outbox-table-wrap"><table class="outbox-table dead-letters-table"><thead><tr>' +
            '<th class="col-check"><input type="checkbox" ' + (allSelected ? 'checked' : '') + ' onchange="DeadLettersModule.toggleSelectAll(this.checked)"></th>' +
            '<th>ID</th><th>来源</th><th>失败原因</th><th>重要性</th><th>重试</th><th>时间</th><th></th></tr></thead><tbody>';

        loadedEvents.forEach(function(item) {
            var failure = failureLabels[item.failure_reason] || { label: item.failure_reason || '未知', cls: 'badge-new' };
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
            html += '<td><button class="btn btn-sm" onclick="DeadLettersModule.showDetail(' + item.id + ')">详情</button> ';
            html += '<button class="btn btn-sm" onclick="DeadLettersModule.replay(' + item.id + ')">重放</button></td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        container.innerHTML = html;
    }

    function renderPagination() {
        var container = document.getElementById('deadLettersPagination');
        if (!container) return;
        if (totalEvents <= pageSize) {
            container.innerHTML = '<div class="pagination-info">共 <strong>' + totalEvents + '</strong> 条</div>';
            return;
        }
        var html = '<div class="pagination"><div class="pagination-info">第 <strong>' + currentPage + '</strong> / <strong>' + totalPages + '</strong> 页，共 <strong>' + totalEvents + '</strong> 条</div>';
        html += '<div class="pagination-buttons">';
        html += '<button ' + (currentPage <= 1 ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(1)">首页</button>';
        html += '<button ' + (currentPage <= 1 ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(' + (currentPage - 1) + ')">上一页</button>';
        html += '<button ' + (currentPage >= totalPages ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(' + (currentPage + 1) + ')">下一页</button>';
        html += '<button ' + (currentPage >= totalPages ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(' + totalPages + ')">末页</button>';
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
        btn.textContent = count ? '🔄 重放已选 ' + count + ' 条' : '🔄 重放已选';
    }

    function replay(id) {
        if (!confirm('确认重放 dead-letter 事件 #' + id + '？')) return;
        API.replayDeadLetter(id).then(function(res) {
            if (res.success) {
                showToast(res.message || '已重放入队', 'success');
                load();
            } else {
                showToast(res.error || '重放失败', 'error');
            }
        }).catch(function(error) {
            showToast('请求失败: ' + error.message, 'error');
        });
    }

    function replaySelected() {
        var ids = selectedEventIds();
        if (!ids.length) return;
        if (!confirm('确认重放已选的 ' + ids.length + ' 条 dead-letter 事件？')) return;
        API.replayDeadLettersByIds(ids).then(function(res) {
            if (res.success) {
                showToast(res.message || '已批量重放', 'success');
                load();
            } else {
                showToast(res.error || '批量重放失败', 'error');
            }
        }).catch(function(error) {
            showToast('请求失败: ' + error.message, 'error');
        });
    }

    function showDetail(id) {
        ensureDetailModal();
        var modal = document.getElementById('deadLetterDetailModal');
        var body = document.getElementById('deadLetterDetailBody');
        if (!modal || !body) return;
        body.innerHTML = '<div class="loading"><div class="spinner"></div><p>加载中...</p></div>';
        modal.classList.add('active');
        API.getDeadLetterDetail(id).then(function(res) {
            if (!res.success || !res.data) {
                body.innerHTML = '<div class="empty-state"><div class="empty-title">加载失败</div><div class="empty-text">' + escapeHtml(res.error || '未知错误') + '</div></div>';
                return;
            }
            body.innerHTML = renderDetail(res.data);
        }).catch(function(error) {
            body.innerHTML = '<div class="empty-state"><div class="empty-title">加载异常</div><div class="empty-text">' + escapeHtml(error.message) + '</div></div>';
        });
    }

    function renderDetail(item) {
        var html = '<div class="detail-table-wrap"><table class="detail-table"><tbody>';
        [
            ['事件 ID', '#' + item.id],
            ['来源', item.source || '-'],
            ['request_id', item.request_id || '-'],
            ['client_ip', item.client_ip || '-'],
            ['状态', item.processing_status || '-'],
            ['失败原因', item.failure_reason || '-'],
            ['重试次数', item.retry_count != null ? String(item.retry_count) : '0'],
            ['重要性', item.importance || '-'],
            ['时间', item.timestamp ? new Date(item.timestamp).toLocaleString('zh-CN') : '-']
        ].forEach(function(row) {
            html += '<tr><th>' + escapeHtml(row[0]) + '</th><td>' + escapeHtml(row[1]) + '</td></tr>';
        });
        if (item.error_message) {
            html += '<tr><th>错误详情</th><td><pre class="json-preview">' + escapeHtml(item.error_message) + '</pre></td></tr>';
        }
        html += '</tbody></table></div>';
        if (item.parsed_data) {
            html += '<h4>解析后数据</h4><pre class="json-preview">' + escapeHtml(JSON.stringify(item.parsed_data, null, 2)) + '</pre>';
        }
        if (item.raw_body) {
            html += '<h4>原始 Payload</h4><pre class="json-preview">' + escapeHtml(item.raw_body) + '</pre>';
        }
        html += '<div class="modal-footer"><button class="btn btn-primary" onclick="DeadLettersModule.replay(' + item.id + ')">重放此事件</button><button class="btn" onclick="DeadLettersModule.closeDetail()">关闭</button></div>';
        return html;
    }

    function ensureDetailModal() {
        if (document.getElementById('deadLetterDetailModal')) return;
        var modal = document.createElement('div');
        modal.id = 'deadLetterDetailModal';
        modal.className = 'modal';
        modal.innerHTML = '<div class="modal-content modal-content-wide"><div class="modal-header"><h2 class="modal-title">死信详情</h2></div><div class="modal-body" id="deadLetterDetailBody"></div></div>';
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
