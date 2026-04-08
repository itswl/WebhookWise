/**
 * 深度分析页面模块
 */
var DeepAnalysesModule = (function() {
    var currentPage = 1;
    var perPage = 20;
    var autoRefreshTimer = null;
    var expandedIds = new Set();

    function buildSummaryHtml(record) {
        var analysis = record.analysis_result || {};
        var engineLabel = record.engine === 'openclaw' ? '🦞 OpenClaw' : '🤖 本地 AI';
        var time = record.created_at ? new Date(record.created_at).toLocaleString('zh-CN') : '-';
        var duration = record.duration_seconds ? record.duration_seconds.toFixed(1) + 's' : '-';
        var source = record.source || 'unknown';

        // 告警类型标签
        var alertTypeTag = '';
        if (record.is_duplicate) {
            alertTypeTag = record.beyond_window ? '<span class="da-alert-type da-alert-type-beyond">🔁 窗口外重复</span>' : '<span class="da-alert-type da-alert-type-dup">🔁 窗口内重复</span>';
        } else {
            alertTypeTag = '<span class="da-alert-type da-alert-type-new">🆕 新告警</span>';
        }

        // 构建更丰富的摘要信息
        var summaryLines = [];
        
        if (record.status === 'completed') {
            // 根因分析
            var rootCause = analysis.root_cause || '';
            if (rootCause) {
                if (rootCause.length > 100) rootCause = rootCause.substring(0, 100) + '...';
                summaryLines.push('<div class="da-summary-root"><strong>🔍 根因：</strong>' + escapeHtml(rootCause) + '</div>');
            }
            
            // 影响范围
            var impact = analysis.impact || '';
            if (impact) {
                if (impact.length > 80) impact = impact.substring(0, 80) + '...';
                summaryLines.push('<div class="da-summary-impact"><strong>💥 影响：</strong>' + escapeHtml(impact) + '</div>');
            }
            
            // 修复建议数量
            if (analysis.recommendations && Array.isArray(analysis.recommendations) && analysis.recommendations.length > 0) {
                summaryLines.push('<div class="da-summary-recs">✅ ' + analysis.recommendations.length + ' 条修复建议</div>');
            }
            
            // 置信度
            if (analysis.confidence !== undefined && analysis.confidence !== null) {
                var confPercent = (analysis.confidence * 100).toFixed(0);
                var confColor = analysis.confidence >= 0.8 ? '#52c41a' : (analysis.confidence >= 0.6 ? '#faad14' : '#ff4d4f');
                summaryLines.push('<div class="da-summary-conf" style="color:' + confColor + '">📊 置信度: ' + confPercent + '%</div>');
            }
            
            // OpenClaw 原始文本（如果没有结构化数据）
            if (!rootCause && analysis._openclaw_text) {
                var textPreview = analysis._openclaw_text;
                if (textPreview.length > 120) textPreview = textPreview.substring(0, 120) + '...';
                summaryLines.push('<div class="da-summary-text">' + escapeHtml(textPreview) + '</div>');
            }
            
        } else if (record.status === 'pending') {
            summaryLines.push('<div class="da-summary-pending">⏳ OpenClaw 正在分析中' + (record.openclaw_run_id ? ' (Run: ' + escapeHtml(record.openclaw_run_id.substring(0, 8)) + '...)' : '') + '</div>');
        } else if (record.status === 'failed') {
            var errorMsg = analysis.root_cause || analysis.error || '分析失败';
            if (errorMsg.length > 100) errorMsg = errorMsg.substring(0, 100) + '...';
            summaryLines.push('<div class="da-summary-failed">❌ ' + escapeHtml(errorMsg) + '</div>');
        }

        var html = '<div class="da-summary" onclick="DeepAnalysesModule.toggleExpand(' + record.id + ')">';
        html += '<div class="da-summary-header">';
        html += '<div class="da-summary-left">';
        html += getStatusLabel(record.status);
        html += alertTypeTag;
        html += '<span class="da-engine">' + engineLabel + '</span>';
        html += '<span class="da-source">📡 ' + escapeHtml(source) + '</span>';
        html += '<span class="da-webhook-id">🔔 告警 #' + record.webhook_event_id + '</span>';
        html += '</div>';
        html += '<div class="da-summary-right">';
        html += '<span class="da-time">🕒 ' + time + '</span>';
        html += '<span class="da-duration">⏱️ ' + duration + '</span>';
        html += '<span class="da-expand-icon" id="expand-icon-' + record.id + '">▶</span>';
        html += '</div>';
        html += '</div>';
        
        // 添加详细信息行
        if (summaryLines.length > 0) {
            html += '<div class="da-summary-details">';
            html += summaryLines.join('');
            html += '</div>';
        }
        
        html += '</div>';
        return html;
    }

    function buildDetailHtml(record) {
        var analysis = record.analysis_result || {};
        var engineLabel = record.engine === 'openclaw' ? '🦞 OpenClaw' : '🤖 本地 AI';
        var time = record.created_at ? new Date(record.created_at).toLocaleString('zh-CN') : '-';
        var duration = record.duration_seconds ? record.duration_seconds.toFixed(1) + 's' : '-';
        var source = record.source || 'unknown';

        var html = '';

        // 头部信息行
        html += '<div class="da-detail-header">';
        html += '<div class="da-detail-meta">';
        html += '<span class="da-engine-tag">' + engineLabel + '</span>';
        html += '<span class="da-source-tag">来源: ' + escapeHtml(source) + '</span>';
        html += '<span class="da-webhook-tag">告警 #' + record.webhook_event_id + '</span>';
        if (record.openclaw_run_id) {
            html += '<span class="da-run-id">Run: ' + escapeHtml(record.openclaw_run_id) + '</span>';
        }
        if (record.openclaw_session_key) {
            html += '<span class="da-session-key">Session: ' + escapeHtml(record.openclaw_session_key) + '</span>';
        }
        html += '</div>';
        html += '<div class="da-detail-time">';
        html += '<span>' + time + ' | 耗时 ' + duration + '</span>';
        html += '<button class="da-collapse-btn" onclick="DeepAnalysesModule.toggleExpand(' + record.id + ')">收起 ▲</button>';
        html += '</div>';
        html += '</div>';

        // 用户问题
        if (record.user_question) {
            html += '<div class="da-user-question">';
            html += '<strong>用户问题：</strong>' + escapeHtml(record.user_question);
            html += '</div>';
        }

        // 内容区
        if (record.status === 'pending') {
            html += '<div class="da-pending-box">';
            html += '<div class="da-pending-icon">⏳</div>';
            html += '<div class="da-pending-text">OpenClaw 正在分析中...</div>';
            if (record.openclaw_run_id) {
                html += '<div class="da-pending-run">Run ID: ' + escapeHtml(record.openclaw_run_id) + '</div>';
            }
            html += '</div>';
            html += '<div class="da-btn-row">';
            html += '<button class="da-btn da-btn-retry" onclick="DeepAnalysesModule.retryAnalysis(' + record.id + ')">🔄 重新拉取</button>';
            html += '<button class="da-btn da-btn-reanalyze" onclick="DeepAnalysesModule.reanalyzeFromDeepAnalysis(' + record.webhook_event_id + ')">🔬 重新分析</button>';
            html += '</div>';
        } else if (record.status === 'failed') {
            html += '<div class="da-failed-box">';
            html += '<strong>分析失败</strong>';
            if (analysis.root_cause) html += '<p>' + escapeHtml(analysis.root_cause) + '</p>';
            html += '</div>';
            html += '<div class="da-btn-row">';
            if (record.openclaw_session_key) {
                html += '<button class="da-btn da-btn-retry" onclick="DeepAnalysesModule.retryAnalysis(' + record.id + ')">🔄 重新拉取</button>';
            }
            html += '<button class="da-btn da-btn-reanalyze" onclick="DeepAnalysesModule.reanalyzeFromDeepAnalysis(' + record.webhook_event_id + ')">🔬 重新分析</button>';
            html += '</div>';
        } else {
            // completed
            if (analysis._openclaw_text) {
                if (typeof marked !== 'undefined') {
                    html += '<div class="da-analysis-content openclaw-analysis-content">' + marked.parse(analysis._openclaw_text) + '</div>';
                } else {
                    html += '<pre class="da-analysis-content">' + escapeHtml(analysis._openclaw_text) + '</pre>';
                }
                if (analysis.confidence !== undefined && analysis.confidence !== null) {
                    html += '<div class="da-confidence">置信度: ' + (analysis.confidence * 100).toFixed(0) + '%</div>';
                }
            } else {
                if (analysis.root_cause) {
                    html += '<div class="da-section"><strong>🔍 根因分析：</strong><p>' + escapeHtml(analysis.root_cause) + '</p></div>';
                }
                if (analysis.impact) {
                    html += '<div class="da-section"><strong>💥 影响范围：</strong><p>' + escapeHtml(analysis.impact) + '</p></div>';
                }
                if (analysis.recommendations && Array.isArray(analysis.recommendations) && analysis.recommendations.length > 0) {
                    html += '<div class="da-section"><strong>✅ 修复建议：</strong><ul>';
                    analysis.recommendations.forEach(function(rec) {
                        if (typeof rec === 'object' && rec !== null) {
                            var label = (rec.priority ? '<strong>' + escapeHtml(rec.priority) + '</strong>: ' : '') + escapeHtml(rec.action || JSON.stringify(rec));
                            html += '<li>' + label + '</li>';
                        } else {
                            html += '<li>' + escapeHtml(String(rec)) + '</li>';
                        }
                    });
                    html += '</ul></div>';
                }
                if (analysis.confidence !== undefined && analysis.confidence !== null) {
                    html += '<div class="da-confidence">置信度: ' + (analysis.confidence * 100).toFixed(0) + '%</div>';
                }
                if (!analysis.root_cause && !analysis.impact && !analysis.recommendations) {
                    html += '<pre class="da-analysis-content">' + escapeHtml(JSON.stringify(analysis, null, 2)) + '</pre>';
                }
            }

            html += '<div class="da-btn-row">';
            if (record.openclaw_session_key) {
                html += '<button class="da-btn da-btn-retry" onclick="DeepAnalysesModule.retryAnalysis(' + record.id + ')">🔄 重新拉取</button>';
            }
            html += '<button class="da-btn da-btn-forward" onclick="DeepAnalysesModule.forwardAnalysis(' + record.id + ')">📤 转发</button>';
            html += '<button class="da-btn da-btn-reanalyze" onclick="DeepAnalysesModule.reanalyzeFromDeepAnalysis(' + record.webhook_event_id + ')">🔬 重新分析</button>';
            html += '</div>';
        }

        return html;
    }

    async function load(page) {
        if (page) currentPage = page;

        var statusFilter = document.getElementById('daStatusFilter');
        var engineFilter = document.getElementById('daEngineFilter');
        var perPageSelect = document.getElementById('daPerPage');
        var status = statusFilter ? statusFilter.value : '';
        var engine = engineFilter ? engineFilter.value : '';
        if (perPageSelect) perPage = parseInt(perPageSelect.value, 10) || 20;

        var container = document.getElementById('deepAnalysesList');
        if (!container) return;

        container.innerHTML = '<div style="text-align:center; padding:40px;"><div class="spinner"></div><p>加载中...</p></div>';

        try {
            var result = await API.getAllDeepAnalyses(currentPage, perPage, status, engine);
            if (!result.success || !result.data) {
                container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">暂无深度分析记录</div>';
                document.getElementById('deepAnalysesPagination').innerHTML = '';
                return;
            }

            var items = result.data.items || [];
            var total = result.data.total || 0;
            var totalPages = result.data.total_pages || 1;

            if (items.length === 0) {
                container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">暂无深度分析记录</div>';
                renderPagination(0, totalPages, total);
                return;
            }

            var hasPending = false;
            var html = '';

            items.forEach(function(record) {
                if (record.status === 'pending') hasPending = true;
                var isExpanded = expandedIds.has(record.id);
                html += '<div class="da-card' + (isExpanded ? ' da-card-expanded' : '') + '" id="card-' + record.id + '" data-id="' + record.id + '" data-record="' + encodeURIComponent(JSON.stringify(record)) + '">';
                html += buildSummaryHtml(record);
                if (isExpanded) {
                    html += '<div class="da-detail" id="detail-' + record.id + '">';
                    html += buildDetailHtml(record);
                    html += '</div>';
                }
                html += '</div>';
            });

            container.innerHTML = html;
            renderPagination(totalPages, totalPages, total);

            if (hasPending) {
                startAutoRefresh();
            } else {
                stopAutoRefresh();
            }

        } catch (e) {
            container.innerHTML = '<div style="color:red; padding:20px; text-align:center;">加载失败: ' + e.message + '</div>';
        }
    }

    function toggleExpand(id) {
        var card = document.getElementById('card-' + id);
        var detail = document.getElementById('detail-' + id);
        var icon = document.getElementById('expand-icon-' + id);

        if (expandedIds.has(id)) {
            expandedIds.delete(id);
            if (card) card.classList.remove('da-card-expanded');
            if (detail) detail.style.display = 'none';
            if (icon) icon.textContent = '▶';
        } else {
            expandedIds.add(id);
            if (card) card.classList.add('da-card-expanded');
            if (detail) {
                detail.style.display = 'block';
            } else if (card && card.dataset.record) {
                var record;
                try {
                    record = JSON.parse(decodeURIComponent(card.dataset.record));
                } catch (e) {
                    record = { id: id, status: 'unknown', analysis_result: {} };
                }
                var detailDiv = document.createElement('div');
                detailDiv.className = 'da-detail';
                detailDiv.id = 'detail-' + id;
                detailDiv.innerHTML = buildDetailHtml(record);
                card.appendChild(detailDiv);
            }
            if (icon) icon.textContent = '▼';
        }
    }

    function getStatusLabel(status) {
        switch(status) {
            case 'pending':
                return '<span class="da-status da-status-pending">⏳ 分析中</span>';
            case 'completed':
                return '<span class="da-status da-status-completed">✅ 已完成</span>';
            case 'failed':
                return '<span class="da-status da-status-failed">❌ 失败</span>';
            default:
                return '<span class="da-status da-status-unknown">' + (status || 'unknown') + '</span>';
        }
    }

    function escapeHtml(text) {
        if (!text) return '';
        var div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function renderPagination(totalPages, _unused, total) {
        var container = document.getElementById('deepAnalysesPagination');
        if (!container) return;

        if (totalPages <= 1 && total <= perPage) {
            container.innerHTML = '<span class="da-total">共 ' + total + ' 条</span>';
            return;
        }

        var html = '<div class="da-pagination">';

        // 上一页
        if (currentPage > 1) {
            html += '<button class="da-page-btn" onclick="DeepAnalysesModule.load(' + (currentPage - 1) + ')">‹ 上一页</button>';
        } else {
            html += '<button class="da-page-btn da-page-btn-disabled" disabled>‹ 上一页</button>';
        }

        // 页码按钮
        var maxVisible = 7;
        var startPage = Math.max(1, currentPage - 3);
        var endPage = Math.min(totalPages, startPage + maxVisible - 1);
        if (endPage - startPage < maxVisible - 1) {
            startPage = Math.max(1, endPage - maxVisible + 1);
        }

        if (startPage > 1) {
            html += '<button class="da-page-btn' + (currentPage === 1 ? ' da-page-btn-active' : '') + '" onclick="DeepAnalysesModule.load(1)">1</button>';
            if (startPage > 2) html += '<span class="da-page-ellipsis">...</span>';
        }

        for (var p = startPage; p <= endPage; p++) {
            html += '<button class="da-page-btn' + (p === currentPage ? ' da-page-btn-active' : '') + '" onclick="DeepAnalysesModule.load(' + p + ')">' + p + '</button>';
        }

        if (endPage < totalPages) {
            if (endPage < totalPages - 1) html += '<span class="da-page-ellipsis">...</span>';
            html += '<button class="da-page-btn' + (currentPage === totalPages ? ' da-page-btn-active' : '') + '" onclick="DeepAnalysesModule.load(' + totalPages + ')">' + totalPages + '</button>';
        }

        // 下一页
        if (currentPage < totalPages) {
            html += '<button class="da-page-btn" onclick="DeepAnalysesModule.load(' + (currentPage + 1) + ')">下一页 ›</button>';
        } else {
            html += '<button class="da-page-btn da-page-btn-disabled" disabled>下一页 ›</button>';
        }

        html += '</div>';
        html += '<span class="da-total">共 ' + total + ' 条</span>';
        container.innerHTML = html;
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

    async function forwardAnalysis(analysisId) {
        var url = prompt('请输入转发目标 URL：\n\n飞书: https://open.feishu.cn/open-apis/bot/v2/hook/xxx\n通用 Webhook: https://your-server.com/hook', '');
        if (url === null) return;
        url = url.trim();
        if (!url) return alert('URL 不能为空');
        if (!url.startsWith('http://') && !url.startsWith('https://')) return alert('请输入有效的 HTTP/HTTPS URL');

        try {
            var result = await API.forwardDeepAnalysis(analysisId, url);
            if (result.success) {
                alert('✅ ' + (result.message || '转发成功'));
            } else {
                alert('❌ ' + (result.message || '转发失败'));
            }
        } catch (e) {
            alert('❌ 请求失败: ' + e.message);
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        var statusFilter = document.getElementById('daStatusFilter');
        var engineFilter = document.getElementById('daEngineFilter');
        var perPageSelect = document.getElementById('daPerPage');
        if (statusFilter) statusFilter.addEventListener('change', function() { load(1); });
        if (engineFilter) engineFilter.addEventListener('change', function() { load(1); });
        if (perPageSelect) perPageSelect.addEventListener('change', function() { load(1); });
    });

    async function reanalyzeFromDeepAnalysis(webhookEventId) {
        if (!confirm('确定要对此告警重新触发 OpenClaw 深度分析吗？\n\n这将创建一个新的分析记录。')) return;

        try {
            var result = await API.deepAnalyze(webhookEventId, '', 'auto');
            if (result.success) {
                alert('已重新开始深度分析，请等待结果');
                load();
                startAutoRefresh();
            } else {
                alert('重新分析失败: ' + (result.message || result.error || '未知错误'));
            }
        } catch (e) {
            alert('请求失败: ' + e.message);
        }
    }

    async function retryAnalysis(analysisId) {
        if (!confirm('确定要重新拉取此分析结果吗？')) return;

        try {
            var result = await API.retryDeepAnalysis(analysisId);
            if (result.success) {
                alert('已重新开始拉取，请等待结果');
                load();
                startAutoRefresh();
            } else {
                alert('重试失败: ' + (result.error || '未知错误'));
            }
        } catch (e) {
            alert('请求失败: ' + e.message);
        }
    }

    return {
        load: load,
        stopAutoRefresh: stopAutoRefresh,
        forwardAnalysis: forwardAnalysis,
        retryAnalysis: retryAnalysis,
        reanalyzeFromDeepAnalysis: reanalyzeFromDeepAnalysis,
        toggleExpand: toggleExpand
    };
})();


