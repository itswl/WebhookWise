/**
 * 死信队列模块 - 增强版
 * 展示所有处理失败进入 dead-letter 状态的 webhook 事件，
 * 支持筛选、多选重放、详情和 JSON payload 查看
 */

var DeadLettersModule = (function() {
    var currentPage = 1;
    var pageSize = 20;
    var currentSource = '';
    var currentSearch = '';
    var currentTimeFrom = '';
    var currentTimeTo = '';
    var loadedEvents = [];
    var totalEvents = 0;
    var totalPages = 1;
    var isLoading = false;
    var selectedIds = {};

    var statusMap = {
        'retry_exhausted': { label: '重试耗尽', cls: 'badge-high' },
        'fat_err': { label: '致命错误', cls: 'badge-high' },
        'processing_error': { label: '处理错误', cls: 'badge-medium' }
    };

    var emptyIcon = '🗂️';
    var sourceList = [];

    // ── 初始化 ──────────────────────────────────────────────────────────────

    function load() {
        currentPage = 1;
        loadedEvents = [];
        totalEvents = 0;
        totalPages = 1;
        selectedIds = {};
        fetchPage(true);
        loadSources();
    }

    function applyFilters() {
        currentSource = document.getElementById('dlSourceFilter')?.value || '';
        currentSearch = document.getElementById('dlSearchInput')?.value || '';
        currentTimeFrom = document.getElementById('dlTimeFrom')?.value || '';
        currentTimeTo = document.getElementById('dlTimeTo')?.value || '';
        load();
    }

    function resetFilters() {
        var sf = document.getElementById('dlSourceFilter');
        if (sf) sf.value = '';
        var si = document.getElementById('dlSearchInput');
        if (si) si.value = '';
        var tf = document.getElementById('dlTimeFrom');
        if (tf) tf.value = '';
        var tt = document.getElementById('dlTimeTo');
        if (tt) tt.value = '';
        currentSource = '';
        currentSearch = '';
        currentTimeFrom = '';
        currentTimeTo = '';
        load();
    }

    function loadSources() {
        API.getDeadLetters({ page: 1, page_size: 1 }).then(function(res) {
            // Sources are collected from the full list after load
        }).catch(function() {});
    }

    function collectSources(events) {
        var seen = {};
        events.forEach(function(ev) {
            if (ev.source && !seen[ev.source]) {
                seen[ev.source] = true;
                sourceList.push(ev.source);
            }
        });
        var sel = document.getElementById('dlSourceFilter');
        if (!sel) return;
        var currentVal = sel.value;
        sel.innerHTML = '<option value="">全部来源</option>';
        sourceList.sort().forEach(function(s) {
            sel.innerHTML += '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + '</option>';
        });
        sel.value = currentVal;
    }

    // ── 数据加载 ────────────────────────────────────────────────────────────

    function fetchPage(reset) {
        if (isLoading) return;
        isLoading = true;
        var container = document.getElementById('deadLettersList');
        if (reset && container) {
            container.innerHTML = '<div class="loading"><div class="spinner"></div><p>加载中...</p></div>';
        }

        API.getDeadLetters({
            page: currentPage,
            page_size: pageSize,
            source: currentSource,
            search: currentSearch,
            time_from: currentTimeFrom ? new Date(currentTimeFrom).toISOString() : '',
            time_to: currentTimeTo ? new Date(currentTimeTo + 'T23:59:59').toISOString() : ''
        }).then(function(res) {
            isLoading = false;
            if (!container) return;
            if (res.success) {
                var data = res.data || [];
                var pagination = res.pagination || {};
                totalEvents = pagination.total || data.length;
                totalPages = Math.max(1, Math.ceil(totalEvents / pageSize));
                if (reset) {
                    loadedEvents = data;
                } else {
                    loadedEvents = loadedEvents.concat(data);
                }
                collectSources(data);
                render();
                renderPagination();
                updateBatchButton();
            } else {
                container.innerHTML = '<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">加载失败</div><div class="empty-text">' + escapeHtml(res.error || '未知错误') + '</div></div>';
            }
        }).catch(function(e) {
            isLoading = false;
            if (container) {
                container.innerHTML = '<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">加载异常</div><div class="empty-text">' + escapeHtml(e.message) + '</div></div>';
            }
        });
    }

    // ── 渲染 ────────────────────────────────────────────────────────────────

    function render() {
        var container = document.getElementById('deadLettersList');
        if (!container) return;

        if (loadedEvents.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + emptyIcon + '</div><div class="empty-title">暂无死信</div><div class="empty-text">所有告警处理正常，无 dead-letter 事件</div></div>';
            return;
        }

        var allSelected = loadedEvents.every(function(ev) { return selectedIds[ev.id]; });
        var html = '<div class="dead-letters-table-wrap"><table class="outbox-table"><thead><tr>' +
            '<th class="col-check"><input type="checkbox" id="dlSelectAll" ' + (allSelected ? 'checked' : '') + ' onchange="DeadLettersModule.toggleSelectAll(this.checked)"></th>' +
            '<th>ID</th><th>来源</th><th>状态</th><th>失败原因</th><th>重试次数</th><th>时间</th><th></th></tr></thead><tbody>';

        loadedEvents.forEach(function(ev) {
            var st = statusMap[ev.failure_reason] || { label: ev.failure_reason || ev.processing_status || '未知', cls: 'badge-new' };
            var time = ev.timestamp ? new Date(ev.timestamp).toLocaleString('zh-CN') : '-';
            var errorMsg = ev.error_message ? ev.error_message.substring(0, 80) + (ev.error_message.length > 80 ? '…' : '') : '-';
            var source = ev.source || '-';
            var checked = selectedIds[ev.id] ? 'checked' : '';

            html += '<tr class="outbox-row" data-id="' + ev.id + '">';
            html += '<td class="col-check"><input type="checkbox" ' + checked + ' onchange="DeadLettersModule.toggleSelect(' + ev.id + ', this.checked)"></td>';
            html += '<td class="outbox-id">#' + ev.id + '</td>';
            html += '<td>' + escapeHtml(source) + '</td>';
            html += '<td><span class="badge ' + st.cls + '">' + st.label + '</span></td>';
            html += '<td title="' + escapeHtml(ev.error_message || '') + '">' + escapeHtml(errorMsg) + '</td>';
            html += '<td>' + (ev.retry_count != null ? ev.retry_count : '0') + '</td>';
            html += '<td class="text-sm">' + time + '</td>';
            html += '<td>';
            html += '<button class="btn btn-sm" onclick="event.stopPropagation();DeadLettersModule.showDetail(' + ev.id + ')" title="查看详情">📋 详情</button> ';
            html += '<button class="btn btn-sm" onclick="event.stopPropagation();DeadLettersModule.replay(' + ev.id + ')" title="重放此告警">🔄 重放</button>';
            html += '</td></tr>';
        });

        html += '</tbody></table></div>';
        container.innerHTML = html;
    }

    function renderPagination() {
        var container = document.getElementById('deadLettersPagination');
        if (!container) return;
        if (totalEvents <= 0) {
            container.innerHTML = '';
            return;
        }

        var html = '<div class="pagination"><div class="pagination-info">第 <strong>' + currentPage + '</strong> / <strong>' + totalPages + '</strong> 页，共 <strong>' + totalEvents + '</strong> 条</div>';
        html += '<div class="pagination-buttons">';
        html += '<button ' + (currentPage <= 1 ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(1)">首页</button>';
        html += '<button ' + (currentPage <= 1 ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(\'prev\')">上一页</button>';
        html += '<button ' + (currentPage >= totalPages ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(\'next\')">下一页</button>';
        html += '<button ' + (currentPage >= totalPages ? 'disabled' : '') + ' onclick="DeadLettersModule.goToPage(\'last\')">末页</button>';
        html += '</div></div>';
        container.innerHTML = html;
    }

    function updateBatchButton() {
        var btn = document.getElementById('dlBatchReplayBtn');
        if (!btn) return;
        var count = Object.keys(selectedIds).length;
        if (count > 0) {
            btn.disabled = false;
            btn.textContent = '🔄 重放已选 ' + count + ' 条';
        } else {
            btn.disabled = true;
            btn.textContent = '🔄 重放已选';
        }
    }

    // ── 交互 ────────────────────────────────────────────────────────────────

    function goToPage(target) {
        if (isLoading) return;
        if (target === 'prev' && currentPage > 1) currentPage--;
        else if (target === 'next' && currentPage < totalPages) currentPage++;
        else if (target === 'last') currentPage = totalPages;
        else if (target === 1) currentPage = 1;
        else if (typeof target === 'number' && target >= 1 && target <= totalPages) currentPage = target;
        else return;
        fetchPage(true);
    }

    function toggleSelect(id, checked) {
        if (checked) {
            selectedIds[id] = true;
        } else {
            delete selectedIds[id];
        }
        updateBatchButton();
    }

    function toggleSelectAll(checked) {
        if (checked) {
            loadedEvents.forEach(function(ev) { selectedIds[ev.id] = true; });
        } else {
            selectedIds = {};
        }
        render();
        updateBatchButton();
    }

    // ── 详情 ────────────────────────────────────────────────────────────────

    function showDetail(eventId) {
        var modal = document.getElementById('dlDetailModal');
        var content = document.getElementById('dlDetailContent');
        if (!modal || !content) return;

        content.innerHTML = '<div class="loading"><div class="spinner"></div><p>加载中...</p></div>';
        modal.style.display = 'flex';

        API.getDeadLetterDetail(eventId).then(function(res) {
            if (!res.success || !res.data) {
                content.innerHTML = '<div class="empty-state"><div class="empty-title">加载失败</div><div class="empty-text">' + escapeHtml(res.error || '未知错误') + '</div></div>';
                return;
            }
            var ev = res.data;
            var st = statusMap[ev.failure_reason] || { label: ev.failure_reason || ev.processing_status || '未知', cls: 'badge-new' };
            var html = '<div class="dl-detail">';
            html += '<div class="dl-detail-section"><h4>基本信息</h4>';
            html += '<table class="detail-table"><tbody>';
            html += '<tr><td>事件 ID</td><td>#' + ev.id + '</td></tr>';
            html += '<tr><td>来源</td><td>' + escapeHtml(ev.source || '-') + '</td></tr>';
            html += '<tr><td>request_id</td><td><code>' + escapeHtml(ev.request_id || '-') + '</code></td></tr>';
            html += '<tr><td>client_ip</td><td>' + escapeHtml(ev.client_ip || '-') + '</td></tr>';
            html += '<tr><td>时间</td><td>' + (ev.timestamp ? new Date(ev.timestamp).toLocaleString('zh-CN') : '-') + '</td></tr>';
            html += '<tr><td>处理状态</td><td><span class="badge ' + st.cls + '">' + st.label + '</span></td></tr>';
            html += '<tr><td>重要性</td><td>' + escapeHtml(ev.importance || '-') + '</td></tr>';
            html += '<tr><td>重试次数</td><td>' + (ev.retry_count != null ? ev.retry_count : '0') + '</td></tr>';
            if (ev.failure_reason) html += '<tr><td>失败原因</td><td style="color:var(--danger)">' + escapeHtml(ev.failure_reason) + '</td></tr>';
            if (ev.error_message) html += '<tr><td>错误详情</td><td style="color:var(--danger)"><pre style="max-height:120px;overflow:auto;white-space:pre-wrap">' + escapeHtml(ev.error_message) + '</pre></td></tr>';
            html += '</tbody></table></div>';

            // Payload display
            if (ev.parsed_data) {
                html += '<div class="dl-detail-section"><h4>解析后数据</h4>';
                html += '<pre class="json-preview">' + escapeHtml(JSON.stringify(ev.parsed_data, null, 2)) + '</pre></div>';
            }
            if (ev.raw_payload) {
                html += '<div class="dl-detail-section"><h4>原始 Payload</h4>';
                html += '<pre class="json-preview">' + escapeHtml(JSON.stringify(ev.raw_payload, null, 2)) + '</pre></div>';
            }

            html += '<div class="dl-detail-actions">';
            html += '<button class="btn btn-primary" onclick="DeadLettersModule.replay(' + ev.id + ');closeDetailModal();">🔄 重放此事件</button>';
            html += '<button class="btn" onclick="closeDetailModal()">关闭</button>';
            html += '</div></div>';
            content.innerHTML = html;
        }).catch(function(e) {
            content.innerHTML = '<div class="empty-state"><div class="empty-title">加载异常</div><div class="empty-text">' + escapeHtml(e.message) + '</div></div>';
        });
    }

    function closeDetailModal() {
        var modal = document.getElementById('dlDetailModal');
        if (modal) modal.style.display = 'none';
    }

    // ── 重放 ────────────────────────────────────────────────────────────────

    function replay(eventId) {
        if (!confirm('确认重放 dead-letter 事件 #' + eventId + '？')) return;
        API.replayDeadLetter(eventId).then(function(res) {
            if (res.success) {
                showToast('事件 #' + eventId + ' 已重放入队', 'success');
                delete selectedIds[eventId];
                load();
            } else {
                showToast(res.error || '重放失败', 'error');
            }
        }).catch(function(e) {
            showToast('请求失败: ' + e.message, 'error');
        });
    }

    function replayAll() {
        var container = document.getElementById('deadLettersList');
        if (!loadedEvents.length) {
            showToast('没有可重放的死信', 'info');
            return;
        }
        if (!confirm('确认重放全部 ' + loadedEvents.length + ' 条 dead-letter 事件？')) return;
        API.replayAllDeadLetters().then(function(res) {
            if (res.success) {
                showToast(res.message || '已批量重放', 'success');
                selectedIds = {};
                load();
            } else {
                showToast(res.error || '批量重放失败', 'error');
            }
        }).catch(function(e) {
            showToast('请求失败: ' + e.message, 'error');
        });
    }

    function replaySelected() {
        var ids = Object.keys(selectedIds).map(Number);
        if (!ids.length) {
            showToast('请先勾选要重放的事件', 'info');
            return;
        }
        if (!confirm('确认重放已选的 ' + ids.length + ' 条 dead-letter 事件？')) return;

        API.replayDeadLettersByIds(ids).then(function(res) {
            if (res.success) {
                showToast(res.message || '已批量重放', 'success');
                selectedIds = {};
                load();
            } else {
                showToast(res.error || '批量重放失败', 'error');
            }
        }).catch(function(e) {
            showToast('请求失败: ' + e.message, 'error');
        });
    }

    return {
        load: load,
        applyFilters: applyFilters,
        resetFilters: resetFilters,
        goToPage: goToPage,
        toggleSelect: toggleSelect,
        toggleSelectAll: toggleSelectAll,
        showDetail: showDetail,
        closeDetailModal: closeDetailModal,
        replay: replay,
        replayAll: replayAll,
        replaySelected: replaySelected
    };
})();
