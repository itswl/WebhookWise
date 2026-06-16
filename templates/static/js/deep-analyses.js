/**
 * Deep Analysis page module (Modernized)
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
    const DEEP_ANALYSIS_REPORT_SCHEMA = 'deep_analysis_report.v1';

    function displayValue(value, options) {
        const opts = Object.assign({ pretty: false, separator: ' · ' }, options || {});
        if (value === null || value === undefined || value === '') return '';

        if (typeof value === 'string') return value.trim();
        if (typeof value === 'number' || typeof value === 'boolean') return String(value);

        if (Array.isArray(value)) {
            const items = value
                .map(item => displayValue(item, opts))
                .filter(Boolean);
            return items.join(opts.separator);
        }

        if (typeof value === 'object') {
            try {
                return JSON.stringify(value, null, opts.pretty ? 2 : 0);
            } catch (e) {
                return 'Unable to display object data';
            }
        }

        return String(value);
    }

    function emptyReport() {
        return {
            schema: DEEP_ANALYSIS_REPORT_SCHEMA,
            summary: '',
            root_cause: '',
            impact: '',
            recommendations: [],
            evidence: [],
            next_checks: [],
            alert_identity: {},
            confidence: null,
            analysis_failed: false,
            failure_reason: '',
            primary_text: '',
            source_format: 'missing',
            raw_text: '',
            sections: []
        };
    }

    function normalizedReport(record) {
        const report = record && record.normalized_report;
        if (!report || typeof report !== 'object' || Array.isArray(report) || report.schema !== DEEP_ANALYSIS_REPORT_SCHEMA) {
            return emptyReport();
        }
        return Object.assign(emptyReport(), report, {
            recommendations: Array.isArray(report.recommendations) ? report.recommendations : [],
            evidence: Array.isArray(report.evidence) ? report.evidence : [],
            next_checks: Array.isArray(report.next_checks) ? report.next_checks : [],
            alert_identity: report.alert_identity && typeof report.alert_identity === 'object' && !Array.isArray(report.alert_identity)
                ? report.alert_identity
                : {},
            sections: Array.isArray(report.sections) ? report.sections : []
        });
    }

    function reportPreviewText(report) {
        return displayValue(report.primary_text || report.summary || report.root_cause || report.impact || report.failure_reason, { separator: '; ' });
    }

    function confidenceLabel(confidence) {
        if (typeof confidence !== 'number' || !Number.isFinite(confidence)) return '—';
        return Math.round(Math.max(0, Math.min(1, confidence)) * 100) + '%';
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
            .map(item => `<li>${escapeHtml(item)}</li>`)
            .join('');
    }

    function renderTextSection(title, value, extraClass) {
        const text = displayValue(value, { separator: '\n' });
        if (!text) return '';
        return `
            <section class="da-analysis-section ${extraClass || ''}">
                <h4>${escapeHtml(title)}</h4>
                <p>${escapeHtml(text)}</p>
            </section>
        `;
    }

    function renderListSection(title, value, extraClass) {
        const items = renderListItems(value);
        if (!items) return '';
        return `
            <section class="da-analysis-section ${extraClass || ''}">
                <h4>${escapeHtml(title)}</h4>
                <ul class="da-report-list">${items}</ul>
            </section>
        `;
    }

    function formatFieldName(key) {
        const labels = {
            source: 'Source',
            project: 'Project',
            region: 'Region',
            namespace: 'Namespace',
            service: 'Service',
            resource_name: 'Resource Name',
            resource_id: 'Resource ID',
            rule_name: 'Rule',
            rule_id: 'Rule ID',
            metric_name: 'Metric',
            severity: 'Severity',
            status: 'Status'
        };
        return labels[key] || String(key).replace(/_/g, ' ');
    }

    function renderKeyValueGrid(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
        const rows = Object.keys(value)
            .filter(key => displayValue(value[key]))
            .map(key => `
                <div class="da-kv-row">
                    <dt>${escapeHtml(formatFieldName(key))}</dt>
                    <dd>${escapeHtml(value[key])}</dd>
                </div>
            `)
            .join('');
        return rows ? `<dl class="da-kv-grid">${rows}</dl>` : '';
    }

    function reportHasContent(report) {
        return !!(
            report.summary ||
            report.root_cause ||
            report.impact ||
            report.recommendations.length ||
            report.evidence.length ||
            report.next_checks.length ||
            Object.keys(report.alert_identity).length ||
            report.primary_text ||
            report.failure_reason
        );
    }

    function renderNormalizedReport(report) {
        let html = '<div class="da-analysis-report">';
        if (!reportHasContent(report)) {
            html += `
                <section class="da-empty-report">
                    <strong>Structured report unavailable</strong>
                    <span>The backend did not return normalized_report, so it cannot be displayed reliably.</span>
                </section>
            `;
            html += '</div>';
            return html;
        }

        if (report.summary) {
            html += `
                <section class="da-analysis-section da-analysis-summary">
                    <h4>Analysis Summary</h4>
                    <p>${escapeHtml(report.summary)}</p>
                </section>
            `;
        }

        const confidence = confidenceLabel(report.confidence);
        html += `
            <div class="da-report-strip">
                <span>Structure: ${escapeHtml(report.source_format || 'unknown')}</span>
                <span>Confidence: ${escapeHtml(confidence)}</span>
                ${report.analysis_failed ? '<span class="da-report-failed">Failed report</span>' : '<span>Completed report</span>'}
            </div>
        `;

        html += '<div class="da-analysis-grid">';
        html += renderTextSection('Root Cause', report.root_cause || report.failure_reason);
        html += renderTextSection('Impact Assessment', report.impact);
        html += renderListSection('Recommendations', report.recommendations, 'da-analysis-section-wide');
        html += renderListSection('Key Evidence', report.evidence, 'da-analysis-section-wide');
        html += renderListSection('Follow-up Checks', report.next_checks, 'da-analysis-section-wide');
        html += '</div>';

        if (Object.keys(report.alert_identity).length) {
            html += `
                <section class="da-analysis-section da-analysis-section-wide">
                    <h4>Alert Identity</h4>
                    ${renderKeyValueGrid(report.alert_identity)}
                </section>
            `;
        }

        if (!report.summary && !report.root_cause && !report.impact && report.primary_text) {
            html += renderTextSection('Analysis Content', report.primary_text, 'da-analysis-section-wide');
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

    function toggleExpand(id) {
        var card = document.getElementById('da-record-' + id);
        var details = document.getElementById('da-details-' + id);
        if (!card || !details) return;

        if (expandedIds.has(id)) {
            expandedIds.delete(id);
            card.className = 'da-card';
            details.style.display = 'none';
            return;
        }

        expandedIds.add(id);
        card.className = 'da-card da-card-expanded';
        details.style.display = 'block';

        var record = findLoadedRecord(id);
        if (!record) return;

        // Full report (normalized_report + analysis_result) is not in the list
        // payload; fetch it once on first expand, then cache on the record.
        if (record._detailLoaded) {
            details.innerHTML = buildDetailsHtml(record);
            return;
        }
        details.innerHTML = '<div style="padding: 1.5rem; text-align: center; color: var(--text-muted);"><div class="spinner" style="width: 20px; height: 20px; display: inline-block; vertical-align: middle; margin-right: 8px;"></div>Loading detailed report...</div>';
        API.getDeepAnalysisDetail(id)
            .then(function(res) {
                if (res && res.success && res.data) {
                    Object.assign(record, res.data);
                    record._detailLoaded = true;
                }
                // Only render if the row is still expanded.
                if (expandedIds.has(id)) {
                    var liveDetails = document.getElementById('da-details-' + id);
                    if (liveDetails) liveDetails.innerHTML = buildDetailsHtml(record);
                }
            })
            .catch(function(err) {
                var liveDetails = document.getElementById('da-details-' + id);
                if (liveDetails) {
                    liveDetails.innerHTML = '<div style="padding: 1.5rem; color: var(--danger);">Failed to load detailed report: ' + escapeHtml(String(err && err.message || err)) + '</div>';
                }
            });
    }

    function findLoadedRecord(id) {
        const numericId = Number(id);
        return loadedRecords.find(record => Number(record.id) === numericId) || null;
    }

    function toggleDebugPayload(id) {
        const rawJsonId = `raw-da-${id}`;
        const container = document.getElementById(rawJsonId);
        if (!container) return;

        if (container.hidden) {
            if (container.dataset.rendered !== 'true') {
                const record = findLoadedRecord(id);
                const report = normalizedReport(record);
                container.innerHTML = `
                    <div class="raw-data">
                        <pre style="margin: 0; white-space: pre-wrap; word-wrap: break-word;">${escapeHtml(JSON.stringify({
                            normalized_report: report,
                            analysis_result: record ? record.analysis_result || null : null
                        }, null, 2))}</pre>
                    </div>
                `;
                container.dataset.rendered = 'true';
            }
            container.hidden = false;
        } else {
            container.hidden = true;
        }
    }

    function buildSummaryHtml(record) {
        var report = normalizedReport(record);
        var time = record.created_at ? new Date(record.created_at).toLocaleString('zh-CN') : '-';
        var duration = typeof record.duration_seconds === 'number' ? record.duration_seconds.toFixed(2) + 's' : '-';
        var source = displayValue(record.source || report.alert_identity.source) || 'Unknown source';

        const engineMap = {
            'openclaw': { label: 'OpenClaw', class: 'badge-high', icon: '🦞', bg: '#dbeafe', color: '#4338ca' },
            'hermes': { label: 'Hermes', class: 'badge-medium', icon: '⚡', bg: '#fae8ff', color: '#4c1d95' },
            'local': { label: 'Local AI', class: 'badge-low', icon: '💻', bg: '#f3f4f6', color: '#4b5563' },
            'auto': { label: 'Auto', class: 'badge-outline', icon: '🤖', bg: '#fef3c7', color: '#b45309' }
        };
        const engine = engineMap[record.engine] || engineMap['local'];
        const engineLabel = `<span class="badge" style="background: ${engine.bg}; color: ${engine.color}; border: none; font-size: 0.7rem;">${engine.icon} ${engine.label}</span>`;

        const statusMap = {
            'pending': { label: 'Analyzing', class: 'badge-warning', icon: '<div class="spinner" style="width: 10px; height: 10px; border-width: 2px; margin-right: 4px; display: inline-block;"></div>' },
            'completed': { label: 'Completed', class: 'badge-success', icon: '✅' },
            'failed': { label: 'Failed', class: 'badge-danger', icon: '❌' }
        };
        const status = statusMap[record.status] || { label: 'Unknown', class: 'badge-outline', icon: '❓' };

        var alertTypeTag = '';
        if (record.is_duplicate) {
            alertTypeTag = '<span class="badge" style="background: #e2e8f0; color: #334155; font-size: 0.7rem;">🔁 Duplicate Alert</span>';
        } else {
            alertTypeTag = '<span class="badge" style="background: #dcfce7; color: #059669; font-size: 0.7rem;">🆕 New Alert</span>';
        }

        let html = `
            <div class="da-summary" onclick="DeepAnalysesModule.toggleExpand(${record.id})">
                <div class="da-summary-main">
                    <div class="da-summary-meta-row">
                        <span class="badge ${status.class}" style="display: flex; align-items: center; font-size: 0.7rem;">${status.icon} ${status.label}</span>
                        ${engineLabel}
                        <span class="da-alert-title">🔔 Alert #${escapeHtml(record.webhook_event_id)}</span>
                        <span class="da-source">📡 ${escapeHtml(source)}</span>
                        ${alertTypeTag}
                    </div>
        `;

        if (record.status === 'completed') {
            // Prefer the lightweight server-provided preview; fall back to the
            // normalized report when a full record has already been fetched.
            let textPreview = record.summary_preview || reportPreviewText(report);
            textPreview = truncateText(textPreview, 180);
            if (textPreview) {
                html += `<div class="da-preview">${escapeHtml(textPreview)}</div>`;
            } else {
                html += '<div class="da-preview da-preview-empty">Structured report unavailable</div>';
            }
        } else if (record.status === 'pending') {
            const runIdText = record.openclaw_run_id ? `(Run ID: <span style="font-family: monospace;">${escapeHtml(record.openclaw_run_id.substring(0,8))}</span>)` : '';
            var pollInfo = [];
            if (record.poll_attempts != null) pollInfo.push('Polled ' + record.poll_attempts + ' times');
            if (record.last_polled_at) pollInfo.push('Last ' + new Date(record.last_polled_at).toLocaleTimeString('zh-CN'));
            const pollText = pollInfo.length > 0 ? `<div style="color: var(--text-muted); font-size: 0.75rem; margin-top: 0.2rem;">${pollInfo.join(' · ')}</div>` : '';
            html += `<div class="da-preview da-preview-pending">Waiting for ${engine.label} to return the diagnostic report... ${runIdText}</div>${pollText}`;
        } else if (record.status === 'failed') {
            let errorMsg = record.summary_preview || report.failure_reason || report.root_cause || report.primary_text || 'Unknown error';
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
        var report = normalizedReport(record);
        const engineLabel = record.engine === 'openclaw' ? 'OpenClaw' : (record.engine === 'hermes' ? 'Hermes' : 'Local AI');

        let detailsHtml = '';

        if (record.user_question) {
            detailsHtml += `
                <div style="background: #f8fafc; padding: 1rem 1.25rem; border-radius: var(--radius-md); border-left: 4px solid var(--primary); margin-bottom: 1.5rem;">
                    <strong style="color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; display: block; margin-bottom: 0.5rem;">👤 User Follow-up Question</strong>
                    <div style="color: var(--text-main); font-size: 0.95rem;">${escapeHtml(record.user_question)}</div>
                </div>
            `;
        }

        if (record.status === 'pending') {
            detailsHtml += `
                <div style="text-align: center; padding: 3rem 1rem;">
                    <div class="spinner" style="width: 32px; height: 32px; margin: 0 auto 1rem auto; border-left-color: var(--primary);"></div>
                    <div style="color: var(--text-main); font-weight: 500; font-size: 1.1rem; margin-bottom: 0.5rem;">${engineLabel} is running deep analysis, please wait</div>
                    ${record.openclaw_run_id ? `<div style="color: var(--text-muted); font-family: monospace; font-size: 0.85rem; background: #f1f5f9; display: inline-block; padding: 0.25rem 0.5rem; border-radius: 4px; margin-top: 0.5rem;">Run ID: ${escapeHtml(record.openclaw_run_id)}</div>` : ''}
                </div>
            `;
        } else if (record.status === 'failed') {
            detailsHtml += `
                <div style="background: var(--danger-bg); border: 1px solid rgba(239,68,68,0.2); padding: 1.5rem; border-radius: var(--radius-md); color: var(--danger);">
                    <h4 style="margin-bottom: 0.5rem; font-weight: 600;">⚠️ Analysis task crashed</h4>
                    <p style="margin: 0; font-size: 0.95rem;">${escapeHtml(report.failure_reason || report.root_cause || report.primary_text || 'Unknown exception')}</p>
                </div>
            `;
            detailsHtml += renderNormalizedReport(report);
        } else {
            detailsHtml += renderNormalizedReport(report);
        }

        // Action Buttons Row
        detailsHtml += `<div style="margin-top: 1.5rem; padding-top: 1.5rem; border-top: 1px solid var(--border); display: flex; gap: 1rem; flex-wrap: wrap;">`;

        if (record.status === 'completed') {
            detailsHtml += `<button class="btn" style="background: #f1f5f9; border-color: #cbd5e1;" onclick="DeepAnalysesModule.forwardResult(${record.id})">📨 Push Result</button>`;
        }
        if (record.status === 'failed' || record.status === 'pending') {
            detailsHtml += `<button class="btn btn-primary" onclick="DeepAnalysesModule.retryAnalysis(${record.id})">🔄 Re-fetch Result</button>`;
        }
        detailsHtml += `<button class="btn" style="border-color: var(--success); color: var(--success); background: var(--success-bg);" onclick="DeepAnalysesModule.reanalyzeFromDeepAnalysis(${record.webhook_event_id})">🔬 Start Fresh Deep Analysis</button>`;

        // Raw Data Toggle
        const rawJsonId = `raw-da-${record.id}`;
        detailsHtml += `<button class="btn" onclick="DeepAnalysesModule.toggleDebugPayload(${record.id})">💻 Debug Payload</button>`;

        detailsHtml += `</div>`;

        detailsHtml += `<div id="${rawJsonId}" style="margin-top: 1.5rem;" hidden></div>`;

        return detailsHtml;
    }

    function renderDeepAnalyses(records) {
        var container = document.getElementById('deepAnalysesList');
        if (!container) return;

        if (!records || records.length === 0) {
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888; background: var(--bg-surface); border-radius: var(--radius-lg); border: 1px dashed var(--border);">No deep analysis records yet</div>';
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

            // Details are rendered lazily on expand (full report fetched on
            // demand), so a collapsed row carries no heavy markup. An already
            // expanded row (e.g. preserved across auto-refresh) is re-rendered
            // only if its full record is present.
            var detailsInner = (isExpanded && record._detailLoaded) ? buildDetailsHtml(record) : '';
            html += '<div id="da-details-' + record.id + '" class="da-details" style="' + (isExpanded ? 'display: block;' : 'display: none;') + '">';
            html += detailsInner;
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
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888;">Loading deep analysis records...</div>';
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
                    var incoming = data.items || [];
                    // Preserve already-fetched full report across a refresh so an
                    // expanded row keeps showing its details (and pending rows
                    // that just completed still re-fetch correctly).
                    if (!append) {
                        var prevById = {};
                        loadedRecords.forEach(function(r) { prevById[r.id] = r; });
                        incoming.forEach(function(item) {
                            var prev = prevById[item.id];
                            if (prev && prev._detailLoaded && prev.status === item.status) {
                                item.normalized_report = prev.normalized_report;
                                item.analysis_result = prev.analysis_result;
                                item.user_question = prev.user_question;
                                item._detailLoaded = true;
                            }
                        });
                    }
                    loadedRecords = append ? loadedRecords.concat(incoming) : incoming;
                    if (append) isLoadingMore = false;
                    renderDeepAnalyses(loadedRecords);
                    renderPagination();
                } else {
                    isLoadingMore = false;
                    if (append && typeof showToast === 'function') {
                        showToast('Failed to load more: ' + (res.error || 'Unknown error'), 'error');
                    } else if (container) {
                        container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">Load failed: ' + escapeHtml(res.error) + '</div>';
                    }
                    renderPagination();
                    stopAutoRefresh();
                }
            })
            .catch(function(error) {
                isLoadingMore = false;
                if (append && typeof showToast === 'function') {
                    showToast('Failed to load more: ' + error.message, 'error');
                } else if (container) {
                    container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">Load error: ' + escapeHtml(error.message) + '</div>';
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
        var url = prompt('Enter the forwarding target URL (leave empty to try the system default rule):\n\nFeishu bot: https://open.feishu.cn/open-apis/bot/v2/hook/xxx\nWeCom/DingTalk/other Webhook: https://your-server.com/hook', '');

        if (url === null) return; // User clicked Cancel

        url = url.trim();
        if (!url) {
            alert('❌ Cancelled: forwarding URL cannot be empty');
            return;
        }
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            alert('❌ Invalid format: please enter a valid HTTP/HTTPS URL');
            return;
        }

        try {
            var result = await API.forwardDeepAnalysis(analysisId, url);
            if (result.success) {
                alert('✅ ' + (result.message || 'Pushed successfully'));
            } else {
                alert('❌ Forwarding failed: ' + (result.message || 'Unknown error'));
            }
        } catch (e) {
            alert('❌ Request failed: ' + e.message);
        }
    }

    async function reanalyzeFromDeepAnalysis(webhookEventId) {
        if (!confirm('Are you sure you want to re-run deep diagnosis on this alert?\nThis will create a new analysis task flow.')) return;

        try {
            var result = await API.deepAnalyze(webhookEventId, '', 'auto');
            if (result.success) {
                alert('🚀 New analysis task started');
                load();
            } else {
                alert('❌ Failed to start task: ' + (result.message || result.error || 'Unknown error'));
            }
        } catch (e) {
            alert('❌ Request failed: ' + e.message);
        }
    }

    async function retryAnalysis(analysisId) {
        if (!confirm('Re-fetch the result from the remote engine?')) return;

        try {
            var result = await API.retryDeepAnalysis(analysisId);
            if (result.success) {
                alert('🔄 ' + (result.message || 'Fetch triggered'));
                load();
            } else {
                alert('❌ Retry failed: ' + (result.error || result.message || 'Unknown error'));
            }
        } catch (e) {
            alert('❌ Request failed: ' + e.message);
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
        toggleExpand: toggleExpand,
        toggleDebugPayload: toggleDebugPayload
    };
})();
