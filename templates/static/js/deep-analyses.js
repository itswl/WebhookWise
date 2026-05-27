/**
 * 深度分析页面模块 (Modernized)
 */
var DeepAnalysesModule = (function() {
    var currentPage = 0;
    var perPage = 200;
    var loadedRecords = [];
    var totalRecords = 0;
    var totalPages = 1;
    var nextCursor = null;
    var hasMoreRecords = false;
    var isLoadingMore = false;
    var autoRefreshTimer = null;
    var expandedIds = new Set();

    function escapeHtml(unsafe) {
        if (!unsafe) return '';
        return String(unsafe)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function renderPlainMarkdown(text) {
        return `<pre style="white-space: pre-wrap; font-family: inherit; margin: 0;">${escapeHtml(text)}</pre>`;
    }

    function toggleExpand(id) {
        var card = document.getElementById('da-record-' + id);
        var details = document.getElementById('da-details-' + id);
        if (!card || !details) return;

        if (expandedIds.has(id)) {
            expandedIds.delete(id);
            card.className = 'da-card';
            details.style.display = 'none';
        } else {
            expandedIds.add(id);
            card.className = 'da-card da-card-expanded';
            details.style.display = 'block';
        }
    }

    function buildSummaryHtml(record) {
        var analysis = record.analysis_result || {};
        var time = record.created_at ? new Date(record.created_at).toLocaleString('zh-CN') : '-';
        var duration = record.duration_seconds ? record.duration_seconds.toFixed(2) + 's' : '-';
        var source = record.source || analysis.source || '未知来源';

        const engineMap = {
            'openclaw': { label: 'OpenClaw', class: 'badge-high', icon: '🧠', bg: '#dbeafe', color: '#4338ca' },
            'hermes': { label: 'Hermes', class: 'badge-medium', icon: '⚡', bg: '#fae8ff', color: '#4c1d95' },
            'local': { label: 'Local AI', class: 'badge-low', icon: '💻', bg: '#f3f4f6', color: '#4b5563' },
            'auto': { label: 'Auto', class: 'badge-outline', icon: '🤖', bg: '#fef3c7', color: '#b45309' }
        };
        const engine = engineMap[record.engine] || engineMap['local'];
        const engineLabel = `<span class="badge" style="background: ${engine.bg}; color: ${engine.color}; border: none; font-size: 0.7rem;">${engine.icon} ${engine.label}</span>`;

        const statusMap = {
            'pending': { label: '分析中', class: 'badge-warning', icon: '<div class="spinner" style="width: 10px; height: 10px; border-width: 2px; margin-right: 4px; display: inline-block;"></div>' },
            'completed': { label: '已完成', class: 'badge-success', icon: '✅' },
            'failed': { label: '失败', class: 'badge-danger', icon: '❌' }
        };
        const status = statusMap[record.status] || { label: '未知', class: 'badge-outline', icon: '❓' };

        var alertTypeTag = '';
        if (record.is_duplicate) {
            alertTypeTag = '<span class="badge" style="background: #e2e8f0; color: #334155; font-size: 0.7rem;">🔁 重复告警</span>';
        } else {
            alertTypeTag = '<span class="badge" style="background: #dcfce7; color: #059669; font-size: 0.7rem;">🆕 新告警</span>';
        }

        let html = `
            <div class="da-summary" onclick="DeepAnalysesModule.toggleExpand(${record.id})">
                <div style="display: flex; flex-direction: column; gap: 0.5rem; flex-grow: 1;">
                    <div style="display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;">
                        <span class="badge ${status.class}" style="display: flex; align-items: center; font-size: 0.7rem;">${status.icon} ${status.label}</span>
                        ${engineLabel}
                        <span style="font-weight: 600; color: var(--text-main); font-size: 0.95rem;">🔔 告警 #${record.webhook_event_id}</span>
                        <span style="color: var(--text-muted); font-size: 0.85rem;">📡 ${escapeHtml(source)}</span>
                        ${alertTypeTag}
                    </div>
        `;

        if (record.status === 'completed') {
            let textPreview = analysis.root_cause || analysis._openclaw_text || '';
            if (textPreview.length > 120) textPreview = textPreview.substring(0, 120) + '...';
            if (textPreview) {
                html += `<div style="color: var(--text-main); font-size: 0.95rem; font-weight: 500; margin-top: 0.25rem;">${escapeHtml(textPreview)}</div>`;
            }
        } else if (record.status === 'pending') {
            const runIdText = record.openclaw_run_id ? `(Run ID: <span style="font-family: monospace;">${escapeHtml(record.openclaw_run_id.substring(0,8))}</span>)` : '';
            var pollInfo = [];
            if (record.poll_attempts != null) pollInfo.push('轮询 ' + record.poll_attempts + ' 次');
            if (record.last_polled_at) pollInfo.push('上次 ' + new Date(record.last_polled_at).toLocaleTimeString('zh-CN'));
            const pollText = pollInfo.length > 0 ? `<div style="color: var(--text-muted); font-size: 0.75rem; margin-top: 0.2rem;">${pollInfo.join(' · ')}</div>` : '';
            html += `<div style="color: var(--warning); font-size: 0.95rem; font-weight: 500; margin-top: 0.25rem; display: flex; align-items: center; gap: 0.5rem;">正在等待 ${engine.label} 返回诊断报告... ${runIdText}</div>${pollText}`;
        } else if (record.status === 'failed') {
            let errorMsg = analysis.root_cause || analysis.error || analysis.failure_reason || '未知错误';
            if (errorMsg.length > 100) errorMsg = errorMsg.substring(0, 100) + '...';
            html += `<div style="color: var(--danger); font-size: 0.95rem; font-weight: 500; margin-top: 0.25rem;">❌ ${escapeHtml(errorMsg)}</div>`;
        }

        html += `
                </div>
                <div style="text-align: right; color: var(--text-muted); font-size: 0.85rem; font-family: 'Fira Code', monospace; min-width: 120px;">
                    <div>${time}</div>
                    <div style="margin-top: 0.25rem; color: var(--text-main);">⏱️ ${duration}</div>
                </div>
            </div>
        `;
        return html;
    }

    function buildDetailsHtml(record) {
        var analysis = record.analysis_result || {};
        const engineLabel = record.engine === 'openclaw' ? 'OpenClaw' : (record.engine === 'hermes' ? 'Hermes' : 'Local AI');

        let detailsHtml = '';

        if (record.user_question) {
            detailsHtml += `
                <div style="background: #f8fafc; padding: 1rem 1.25rem; border-radius: var(--radius-md); border-left: 4px solid var(--primary); margin-bottom: 1.5rem;">
                    <strong style="color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; display: block; margin-bottom: 0.5rem;">👤 用户补充提问</strong>
                    <div style="color: var(--text-main); font-size: 0.95rem;">${escapeHtml(record.user_question)}</div>
                </div>
            `;
        }

        if (record.status === 'pending') {
            detailsHtml += `
                <div style="text-align: center; padding: 3rem 1rem;">
                    <div class="spinner" style="width: 32px; height: 32px; margin: 0 auto 1rem auto; border-left-color: var(--primary);"></div>
                    <div style="color: var(--text-main); font-weight: 500; font-size: 1.1rem; margin-bottom: 0.5rem;">${engineLabel} 正在深层分析中，请耐心等待</div>
                    ${record.openclaw_run_id ? `<div style="color: var(--text-muted); font-family: monospace; font-size: 0.85rem; background: #f1f5f9; display: inline-block; padding: 0.25rem 0.5rem; border-radius: 4px; margin-top: 0.5rem;">Run ID: ${escapeHtml(record.openclaw_run_id)}</div>` : ''}
                </div>
            `;
        } else if (record.status === 'failed') {
            detailsHtml += `
                <div style="background: var(--danger-bg); border: 1px solid rgba(239,68,68,0.2); padding: 1.5rem; border-radius: var(--radius-md); color: var(--danger);">
                    <h4 style="margin-bottom: 0.5rem; font-weight: 600;">⚠️ 分析任务崩溃</h4>
                    <p style="margin: 0; font-size: 0.95rem;">${escapeHtml(analysis.root_cause || analysis.error || analysis.failure_reason || '未知异常')}</p>
                </div>
            `;
        } else {
            // Completed
            if (analysis._openclaw_text) {
                const mdHtml = renderPlainMarkdown(analysis._openclaw_text);
                detailsHtml += `
                    <div style="background: #ffffff; padding: 1.5rem; border-radius: var(--radius-md); border: 1px solid var(--border); font-size: 0.95rem; line-height: 1.6; color: var(--text-main);" class="markdown-body">
                        ${mdHtml}
                    </div>
                `;
            } else {
                // Structured output (Local Engine)
                detailsHtml += `<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; background: #ffffff; padding: 1.5rem; border-radius: var(--radius-md); border: 1px solid var(--border);">`;

                if (analysis.root_cause) {
                    detailsHtml += `
                        <div>
                            <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">🔍 根因定位</h4>
                            <p style="color: var(--text-main); font-size: 0.95rem; margin: 0; line-height: 1.6;">${escapeHtml(analysis.root_cause)}</p>
                        </div>
                    `;
                }

                if (analysis.impact) {
                    detailsHtml += `
                        <div>
                            <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">💥 影响评估</h4>
                            <p style="color: var(--text-main); font-size: 0.95rem; margin: 0; line-height: 1.6;">${escapeHtml(analysis.impact)}</p>
                        </div>
                    `;
                }

                if (analysis.recommendations && analysis.recommendations.length > 0) {
                    detailsHtml += `
                        <div style="grid-column: 1 / -1;">
                            <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">🛠️ 修复建议</h4>
                            <ul style="padding-left: 1.5rem; color: var(--text-main); font-size: 0.95rem; margin: 0; line-height: 1.6;">
                                ${analysis.recommendations.map(r => `<li style="margin-bottom: 0.5rem;">${escapeHtml(r)}</li>`).join('')}
                            </ul>
                        </div>
                    `;
                }
                detailsHtml += `</div>`;
            }
        }

        // Action Buttons Row
        detailsHtml += `<div style="margin-top: 1.5rem; padding-top: 1.5rem; border-top: 1px solid var(--border); display: flex; gap: 1rem; flex-wrap: wrap;">`;

        if (record.status === 'completed') {
            detailsHtml += `<button class="btn" style="background: #f1f5f9; border-color: #cbd5e1;" onclick="DeepAnalysesModule.forwardResult(${record.id})">📨 推送结果</button>`;
        }
        if (record.status === 'failed' || record.status === 'pending') {
            detailsHtml += `<button class="btn btn-primary" onclick="DeepAnalysesModule.retryAnalysis(${record.id})">🔄 重新拉取结果</button>`;
        }
        detailsHtml += `<button class="btn" style="border-color: var(--success); color: var(--success); background: var(--success-bg);" onclick="DeepAnalysesModule.reanalyzeFromDeepAnalysis(${record.webhook_event_id})">🔬 发起全新深研</button>`;

        // Raw Data Toggle
        const rawJsonId = `raw-da-${record.id}`;
        detailsHtml += `<button class="btn" onclick="document.getElementById('${rawJsonId}').style.display = document.getElementById('${rawJsonId}').style.display === 'none' ? 'block' : 'none'">💻 Debug 报文</button>`;

        detailsHtml += `</div>`;

        // Raw JSON container
        detailsHtml += `
            <div id="${rawJsonId}" style="display: none; margin-top: 1.5rem;">
                <div class="raw-data">
                    <pre style="margin: 0; white-space: pre-wrap; word-wrap: break-word;">${escapeHtml(JSON.stringify(analysis, null, 2))}</pre>
                </div>
            </div>
        `;

        return detailsHtml;
    }

    function renderDeepAnalyses(records) {
        var container = document.getElementById('deepAnalysesList');
        if (!container) return;

        if (!records || records.length === 0) {
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888; background: var(--bg-surface); border-radius: var(--radius-lg); border: 1px dashed var(--border);">当前没有深度分析记录</div>';
            return;
        }

        var html = '';
        var hasPending = false;

        records.forEach(function(record) {
            if (record.status === 'pending') hasPending = true;
            var isExpanded = expandedIds.has(record.id);
            var cardClass = isExpanded ? 'da-card da-card-expanded' : 'da-card';

            html += '<div id="da-record-' + record.id + '" class="' + cardClass + '">';
            html += buildSummaryHtml(record);

            html += '<div id="da-details-' + record.id + '" class="da-details" style="' + (isExpanded ? 'display: block;' : 'display: none;') + '">';
            html += buildDetailsHtml(record);
            html += '</div>';

            html += '</div>';
        });

        container.innerHTML = html;

        if (hasPending) {
            startAutoRefresh();
        } else {
            stopAutoRefresh();
        }
    }

    function hasMore() {
        return hasMoreRecords;
    }

    function renderPagination() {
        var container = document.getElementById('deepAnalysesPagination');
        if (!container) return;
        renderLoadMorePagination(container, {
            loaded: loadedRecords.length,
            total: totalRecords,
            batchSize: perPage,
            hasMore: hasMore(),
            isLoading: isLoadingMore,
            onLoadMore: loadMore
        });
    }

    function getFilters() {
        var statusFilter = document.getElementById('daStatusFilter');
        var engineFilter = document.getElementById('daEngineFilter');
        return {
            status: statusFilter ? statusFilter.value : '',
            engine: engineFilter ? engineFilter.value : ''
        };
    }

    function load() {
        currentPage = 1;
        loadedRecords = [];
        totalRecords = 0;
        totalPages = 1;
        nextCursor = null;
        hasMoreRecords = false;

        var container = document.getElementById('deepAnalysesList');
        if (container && !expandedIds.size) {
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888;">正在加载深度分析记录...</div>';
        }
        fetchPage(null, false);
    }

    function fetchPage(cursor, append) {
        var filters = getFilters();
        var container = document.getElementById('deepAnalysesList');
        API.getAllDeepAnalyses(1, perPage, filters.status, filters.engine, cursor)
            .then(function(res) {
                if (res.success) {
                    var data = res.data || {};
                    currentPage = append ? currentPage + 1 : 1;
                    totalRecords = data.total || 0;
                    totalPages = data.total_pages || Math.max(1, Math.ceil(totalRecords / perPage));
                    nextCursor = data.next_cursor || null;
                    hasMoreRecords = !!data.has_more;
                    loadedRecords = append ? loadedRecords.concat(data.items || []) : (data.items || []);
                    if (append) isLoadingMore = false;
                    renderDeepAnalyses(loadedRecords);
                    renderPagination();
                } else {
                    isLoadingMore = false;
                    if (append && typeof showToast === 'function') {
                        showToast('加载更多失败: ' + (res.error || '未知错误'), 'error');
                    } else if (container) {
                        container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">加载失败: ' + escapeHtml(res.error) + '</div>';
                    }
                    renderPagination();
                    stopAutoRefresh();
                }
            })
            .catch(function(error) {
                isLoadingMore = false;
                if (append && typeof showToast === 'function') {
                    showToast('加载更多失败: ' + error.message, 'error');
                } else if (container) {
                    container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">加载异常: ' + escapeHtml(error.message) + '</div>';
                }
                renderPagination();
                stopAutoRefresh();
            });
    }

    function loadMore() {
        if (!hasMore() || isLoadingMore) return;
        isLoadingMore = true;
        renderPagination();
        fetchPage(nextCursor, true);
    }

    function startAutoRefresh() {
        if (autoRefreshTimer) return;
        autoRefreshTimer = setInterval(function() { load(); }, 15000);
    }

    function stopAutoRefresh() {
        if (autoRefreshTimer) {
            clearInterval(autoRefreshTimer);
            autoRefreshTimer = null;
        }
    }

    async function forwardResult(analysisId) {
        var url = prompt('请输入转发目标 URL（留空将尝试使用系统默认规则）：\n\n飞书机器人: https://open.feishu.cn/open-apis/bot/v2/hook/xxx\n企业微信/钉钉/其他 Webhook: https://your-server.com/hook', '');

        if (url === null) return; // User clicked Cancel

        url = url.trim();
        if (!url) {
            alert('❌ 操作取消: 转发 URL 不能为空');
            return;
        }
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            alert('❌ 格式错误: 请输入有效的 HTTP/HTTPS URL');
            return;
        }

        try {
            var result = await API.forwardDeepAnalysis(analysisId, url);
            if (result.success) {
                alert('✅ ' + (result.message || '推送成功'));
            } else {
                alert('❌ 转发失败: ' + (result.message || '未知错误'));
            }
        } catch (e) {
            alert('❌ 请求失败: ' + e.message);
        }
    }

    async function reanalyzeFromDeepAnalysis(webhookEventId) {
        if (!confirm('确定要对此告警重新发起深层诊断吗？\n这将创建一个新的分析任务流。')) return;

        try {
            var result = await API.deepAnalyze(webhookEventId, '', 'auto');
            if (result.success) {
                alert('🚀 已发起全新分析任务');
                load();
            } else {
                alert('❌ 任务发起失败: ' + (result.message || result.error || '未知错误'));
            }
        } catch (e) {
            alert('❌ 请求失败: ' + e.message);
        }
    }

    async function retryAnalysis(analysisId) {
        if (!confirm('重新从远程引擎拉取结果？')) return;

        try {
            var result = await API.retryDeepAnalysis(analysisId);
            if (result.success) {
                alert('🔄 ' + (result.message || '已触发拉取'));
                load();
            } else {
                alert('❌ 重试失败: ' + (result.error || result.message || '未知错误'));
            }
        } catch (e) {
            alert('❌ 请求失败: ' + e.message);
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        var statusFilter = document.getElementById('daStatusFilter');
        var engineFilter = document.getElementById('daEngineFilter');
        if (statusFilter) statusFilter.addEventListener('change', function() { load(); });
        if (engineFilter) engineFilter.addEventListener('change', function() { load(); });
    });

    return {
        load: load,
        loadMore: loadMore,
        stopAutoRefresh: stopAutoRefresh,
        forwardResult: forwardResult,
        retryAnalysis: retryAnalysis,
        reanalyzeFromDeepAnalysis: reanalyzeFromDeepAnalysis,
        toggleExpand: toggleExpand
    };
})();
