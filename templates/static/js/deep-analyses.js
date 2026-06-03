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
    const DEEP_ANALYSES_AUTO_REFRESH_INTERVAL_MS = 60000;

    const TEXT_FIELD_CANDIDATES = [
        'summary',
        'root_cause',
        '_openclaw_text',
        'analysis',
        'conclusion',
        'content',
        'text',
        'message',
        'finding',
        'observation',
        'result',
        'cause',
        'error',
        'failure_reason',
        'description',
        'annotations',
        'details',
        'detail',
        'reason',
        'action',
        'recommendation',
        'solution',
        'value',
        'name',
        'title'
    ];

    function displayValue(value, options) {
        const opts = Object.assign({ pretty: false, separator: ' · ', maxDepth: 3 }, options || {});
        return displayValueInternal(value, opts, 0);
    }

    function displayValueInternal(value, opts, depth) {
        if (value === null || value === undefined || value === '') return '';

        if (typeof value === 'string') return value.trim();
        if (typeof value === 'number' || typeof value === 'boolean') return String(value);

        if (Array.isArray(value)) {
            const items = value
                .map(item => displayValueInternal(item, opts, depth + 1))
                .filter(Boolean);
            return items.join(opts.separator);
        }

        if (typeof value === 'object') {
            if (depth < opts.maxDepth) {
                for (const key of TEXT_FIELD_CANDIDATES) {
                    if (Object.prototype.hasOwnProperty.call(value, key)) {
                        const text = displayValueInternal(value[key], opts, depth + 1);
                        if (text) return text;
                    }
                }
            }

            try {
                return JSON.stringify(value, null, opts.pretty ? 2 : 0);
            } catch (e) {
                return '无法展示的对象数据';
            }
        }

        return String(value);
    }

    function extractJsonBlock(text) {
        if (typeof text !== 'string') return '';
        const start = text.search(/[\{\[]/);
        if (start < 0) return '';

        const opener = text[start];
        const closer = opener === '{' ? '}' : ']';
        let depth = 0;
        let inString = false;
        let escaped = false;

        for (let i = start; i < text.length; i += 1) {
            const ch = text[i];
            if (inString) {
                if (escaped) {
                    escaped = false;
                } else if (ch === '\\') {
                    escaped = true;
                } else if (ch === '"') {
                    inString = false;
                }
                continue;
            }

            if (ch === '"') {
                inString = true;
            } else if (ch === opener) {
                depth += 1;
            } else if (ch === closer) {
                depth -= 1;
                if (depth === 0) return text.slice(start, i + 1);
            }
        }

        return '';
    }

    function stripMarkdownJsonFence(text) {
        if (typeof text !== 'string') return '';
        return text
            .trim()
            .replace(/^```(?:json)?\s*/i, '')
            .replace(/\s*```$/i, '')
            .trim();
    }

    function parseJsonLikeText(value) {
        if (typeof value !== 'string') return null;
        const stripped = stripMarkdownJsonFence(value);
        if (!stripped) return null;

        const jsonBlock = extractJsonBlock(stripped);
        const candidates = [stripped, sanitizeLooseJson(stripped), jsonBlock, sanitizeLooseJson(jsonBlock)].filter(Boolean);
        for (const candidate of candidates) {
            try {
                const parsed = JSON.parse(candidate);
                if (parsed && typeof parsed === 'object') return parsed;
            } catch (e) {
                // Ignore non-JSON prose and fall back to the next candidate.
            }
        }
        return null;
    }

    function sanitizeLooseJson(text) {
        if (typeof text !== 'string') return '';
        return text.replace(/\\(?!["\\/bfnrtu])/g, '\\\\');
    }

    function isRawJsonLikeText(value) {
        if (typeof value !== 'string') return false;
        const text = value.trim();
        return text.startsWith('```') || text.startsWith('{') || text.startsWith('[');
    }

    function buildAnalysisView(analysis) {
        const data = normalizeAnalysisResult(analysis);
        const parsed =
            parseJsonLikeText(data.summary) ||
            parseJsonLikeText(data.root_cause) ||
            parseJsonLikeText(data._openclaw_text) ||
            parseJsonLikeText(data.analysis) ||
            parseJsonLikeText(data.details);

        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            return Object.assign({}, data, parsed);
        }
        return data;
    }

    function displayPreviewValue(value) {
        const parsed = parseJsonLikeText(value);
        if (parsed) return buildPreviewText(parsed);
        if (isRawJsonLikeText(value)) return '';
        return displayValue(value, { separator: '；' });
    }

    function buildPreviewText(analysis) {
        const view = buildAnalysisView(analysis);
        const candidates = [
            view.summary,
            view.conclusion,
            view.root_cause,
            view.impact,
            view.impact_scope,
            view.error,
            view.failure_reason,
            view.message
        ];

        for (const candidate of candidates) {
            const text = displayPreviewValue(candidate);
            if (text) return text;
        }

        return '';
    }

    function normalizeAnalysisResult(value) {
        if (!value) return {};
        if (typeof value === 'object' && !Array.isArray(value)) return value;
        const text = displayValue(value);
        return text ? { root_cause: text, _openclaw_text: text } : {};
    }

    function firstDisplayValue(values, options) {
        for (const value of values) {
            const text = displayValue(value, options);
            if (text) return text;
        }
        return '';
    }

    function truncateText(text, maxLength) {
        if (!text) return '';
        const normalized = String(text).replace(/\s+/g, ' ').trim();
        return normalized.length > maxLength ? normalized.substring(0, maxLength) + '...' : normalized;
    }

    function renderListItems(value) {
        const items = Array.isArray(value) ? value : (value ? [value] : []);
        return items
            .map(item => displayValue(item))
            .filter(Boolean)
            .map(item => `<li style="margin-bottom: 0.5rem;">${escapeHtml(item)}</li>`)
            .join('');
    }

    function hasDisplayValue(value) {
        return !!displayValue(value);
    }

    function renderParagraph(value) {
        const text = displayValue(value, { pretty: true, separator: '\n' });
        if (!text) return '';
        return `<p>${escapeHtml(text)}</p>`;
    }

    function renderValueBody(value) {
        if (value === null || value === undefined || value === '') return '';

        if (Array.isArray(value)) {
            const items = renderListItems(value);
            return items ? `<ul>${items}</ul>` : '';
        }

        if (typeof value === 'object') {
            const text = displayValue(value, { pretty: true, separator: '\n' });
            return text ? `<p>${escapeHtml(text)}</p>` : renderKeyValueGrid(value);
        }

        return renderParagraph(value);
    }

    function renderSection(title, value, extraClass) {
        const body = renderValueBody(value);
        if (!body) return '';
        return `
            <section class="da-analysis-section ${extraClass || ''}">
                <h4>${escapeHtml(title)}</h4>
                ${body}
            </section>
        `;
    }

    function formatFieldName(key) {
        const labels = {
            source: '来源',
            project: '项目',
            region: '区域',
            namespace: '命名空间',
            service: '服务',
            resource_name: '资源名称',
            resource_id: '资源 ID',
            rule_name: '规则',
            rule_id: '规则 ID',
            metric_name: '指标',
            severity: '级别',
            status: '状态'
        };
        return labels[key] || String(key).replace(/_/g, ' ');
    }

    function renderKeyValueGrid(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
        const rows = Object.keys(value)
            .filter(key => hasDisplayValue(value[key]))
            .map(key => `
                <div class="da-kv-row">
                    <dt>${escapeHtml(formatFieldName(key))}</dt>
                    <dd>${escapeHtml(displayValue(value[key], { pretty: true, separator: '；' }))}</dd>
                </div>
            `)
            .join('');
        return rows ? `<dl class="da-kv-grid">${rows}</dl>` : '';
    }

    function renderStructuredAnalysis(analysis) {
        const view = buildAnalysisView(analysis);
        const summary = displayPreviewValue(view.summary);
        const rootCause = view.root_cause || view.reason || view.analysis;
        const impact = view.impact || view.impact_scope;
        const recommendationSource =
            Array.isArray(view.recommendations) && view.recommendations.length > 0
                ? view.recommendations
                : (view.recommendations || view.actions || view.next_steps || view.solution);
        const evidence = view.evidence || view.supports || view.observations;

        let html = '<div class="da-analysis-report">';
        if (summary) {
            html += `
                <section class="da-analysis-section da-analysis-summary">
                    <h4>分析摘要</h4>
                    <p>${escapeHtml(summary)}</p>
                </section>
            `;
        }

        html += '<div class="da-analysis-grid">';
        html += renderSection('根因定位', rootCause);
        html += renderSection('影响评估', impact);
        html += renderSection('修复建议', recommendationSource, 'da-analysis-section-wide');
        html += renderSection('关键证据', evidence, 'da-analysis-section-wide');
        html += '</div>';

        if (view.alert_identity) {
            html += `
                <section class="da-analysis-section da-analysis-section-wide">
                    <h4>告警身份</h4>
                    ${renderKeyValueGrid(view.alert_identity)}
                </section>
            `;
        }

        if (!summary && !hasDisplayValue(rootCause) && !hasDisplayValue(impact) && !hasDisplayValue(recommendationSource) && !hasDisplayValue(evidence)) {
            html += renderSection('分析内容', view, 'da-analysis-section-wide');
        }

        html += '</div>';
        return html;
    }

    function escapeHtml(unsafe) {
        const text = displayValue(unsafe);
        if (!text) return '';
        return text
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
        var analysis = normalizeAnalysisResult(record.analysis_result);
        var time = record.created_at ? new Date(record.created_at).toLocaleString('zh-CN') : '-';
        var duration = typeof record.duration_seconds === 'number' ? record.duration_seconds.toFixed(2) + 's' : '-';
        var source = displayValue(record.source || analysis.source) || '未知来源';

        const engineMap = {
            'openclaw': { label: 'OpenClaw', class: 'badge-high', icon: '🦞', bg: '#dbeafe', color: '#4338ca' },
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
                <div class="da-summary-main">
                    <div class="da-summary-meta-row">
                        <span class="badge ${status.class}" style="display: flex; align-items: center; font-size: 0.7rem;">${status.icon} ${status.label}</span>
                        ${engineLabel}
                        <span class="da-alert-title">🔔 告警 #${escapeHtml(record.webhook_event_id)}</span>
                        <span class="da-source">📡 ${escapeHtml(source)}</span>
                        ${alertTypeTag}
                    </div>
        `;

        if (record.status === 'completed') {
            let textPreview = buildPreviewText(analysis);
            if (!textPreview && !isRawJsonLikeText(record.analysis_result)) {
                textPreview = firstDisplayValue([record.analysis_result], { separator: '；' });
            }
            textPreview = truncateText(textPreview, 180);
            if (textPreview) {
                html += `<div class="da-preview">${escapeHtml(textPreview)}</div>`;
            }
        } else if (record.status === 'pending') {
            const runIdText = record.openclaw_run_id ? `(Run ID: <span style="font-family: monospace;">${escapeHtml(record.openclaw_run_id.substring(0,8))}</span>)` : '';
            var pollInfo = [];
            if (record.poll_attempts != null) pollInfo.push('轮询 ' + record.poll_attempts + ' 次');
            if (record.last_polled_at) pollInfo.push('上次 ' + new Date(record.last_polled_at).toLocaleTimeString('zh-CN'));
            const pollText = pollInfo.length > 0 ? `<div style="color: var(--text-muted); font-size: 0.75rem; margin-top: 0.2rem;">${pollInfo.join(' · ')}</div>` : '';
            html += `<div class="da-preview da-preview-pending">正在等待 ${engine.label} 返回诊断报告... ${runIdText}</div>${pollText}`;
        } else if (record.status === 'failed') {
            let errorMsg = firstDisplayValue([analysis.root_cause, analysis.error, analysis.failure_reason, analysis.message], { separator: '；' }) || '未知错误';
            errorMsg = truncateText(errorMsg, 160);
            html += `<div class="da-preview da-preview-error">❌ ${escapeHtml(errorMsg)}</div>`;
        }

        html += `
                </div>
                <div class="da-summary-runtime">
                    <div>${time}</div>
                    <div class="da-duration-value">⏱️ ${duration}</div>
                </div>
            </div>
        `;
        return html;
    }

    function buildDetailsHtml(record) {
        var analysis = normalizeAnalysisResult(record.analysis_result);
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
            const openclawText = displayValue(analysis._openclaw_text, { pretty: true, separator: '\n' });
            const parsedOpenclawText = parseJsonLikeText(openclawText);
            if (parsedOpenclawText) {
                detailsHtml += renderStructuredAnalysis(Object.assign({}, analysis, parsedOpenclawText));
            } else if (openclawText && !isRawJsonLikeText(openclawText)) {
                const mdHtml = renderPlainMarkdown(openclawText);
                detailsHtml += `
                    <div style="background: #ffffff; padding: 1.5rem; border-radius: var(--radius-md); border: 1px solid var(--border); font-size: 0.95rem; line-height: 1.6; color: var(--text-main);" class="markdown-body">
                        ${mdHtml}
                    </div>
                `;
            } else {
                // Structured output (Local Engine)
                detailsHtml += renderStructuredAnalysis(analysis);
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
        autoRefreshTimer = setInterval(function() { load(); }, DEEP_ANALYSES_AUTO_REFRESH_INTERVAL_MS);
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
