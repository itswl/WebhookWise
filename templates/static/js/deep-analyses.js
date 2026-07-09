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
                return t('deep.unableToDisplayObject');
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
            source: t('deep.field.source'),
            project: t('deep.field.project'),
            region: t('deep.field.region'),
            namespace: t('deep.field.namespace'),
            service: t('deep.field.service'),
            resource_name: t('deep.field.resourceName'),
            resource_id: t('deep.field.resourceId'),
            rule_name: t('deep.field.ruleName'),
            rule_id: t('deep.field.ruleId'),
            metric_name: t('deep.field.metricName'),
            severity: t('deep.field.severity'),
            status: t('deep.field.status')
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
                    <strong>${escapeHtml(t('deep.report.unavailable'))}</strong>
                    <span>${escapeHtml(t('deep.report.unavailableHint'))}</span>
                </section>
            `;
            html += '</div>';
            return html;
        }

        if (report.summary) {
            html += `
                <section class="da-analysis-section da-analysis-summary">
                    <h4>${escapeHtml(t('deep.section.summary'))}</h4>
                    <p>${escapeHtml(report.summary)}</p>
                </section>
            `;
        }

        const confidence = confidenceLabel(report.confidence);
        html += `
            <div class="da-report-strip">
                <span>${escapeHtml(t('deep.report.structure'))}: ${escapeHtml(report.source_format || t('deep.report.structureUnknown'))}</span>
                <span>${escapeHtml(t('deep.report.confidence'))}: ${escapeHtml(confidence)}</span>
                ${report.analysis_failed ? `<span class="da-report-failed">${escapeHtml(t('deep.report.failed'))}</span>` : `<span>${escapeHtml(t('deep.report.completed'))}</span>`}
            </div>
        `;

        html += '<div class="da-analysis-grid">';
        html += renderTextSection(t('deep.section.rootCause'), report.root_cause || report.failure_reason);
        html += renderTextSection(t('deep.section.impact'), report.impact);
        html += renderListSection(t('deep.section.recommendations'), report.recommendations, 'da-analysis-section-wide');
        html += renderListSection(t('deep.section.evidence'), report.evidence, 'da-analysis-section-wide');
        html += renderListSection(t('deep.section.nextChecks'), report.next_checks, 'da-analysis-section-wide');
        html += '</div>';

        if (Object.keys(report.alert_identity).length) {
            html += `
                <section class="da-analysis-section da-analysis-section-wide">
                    <h4>${escapeHtml(t('deep.section.alertIdentity'))}</h4>
                    ${renderKeyValueGrid(report.alert_identity)}
                </section>
            `;
        }

        if (!report.summary && !report.root_cause && !report.impact && report.primary_text) {
            html += renderTextSection(t('deep.section.analysisContent'), report.primary_text, 'da-analysis-section-wide');
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
        details.innerHTML = '<div style="padding: 1.5rem; text-align: center; color: var(--text-muted);"><div class="spinner" style="width: 20px; height: 20px; display: inline-block; vertical-align: middle; margin-right: 8px;"></div>' + escapeHtml(t('deep.loadingDetail')) + '</div>';
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
                    liveDetails.innerHTML = '<div style="padding: 1.5rem; color: var(--danger);">' + escapeHtml(t('deep.loadDetailFailed')) + ': ' + escapeHtml(String(err && err.message || err)) + '</div>';
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
        var source = displayValue(record.source || report.alert_identity.source) || t('deep.unknownSource');

        const engineMap = {
            'openclaw': { label: t('deep.engine.openclaw'), class: 'badge-high', icon: '🦞', bg: '#dbeafe', color: '#4338ca' },
            'hermes': { label: t('deep.engine.hermes'), class: 'badge-medium', icon: '⚡', bg: '#fae8ff', color: '#4c1d95' },
            'local': { label: t('deep.engine.local'), class: 'badge-low', icon: '💻', bg: '#f3f4f6', color: '#4b5563' },
            'auto': { label: t('deep.engine.auto'), class: 'badge-outline', icon: '🤖', bg: '#fef3c7', color: '#b45309' }
        };
        const engine = engineMap[record.engine] || engineMap['local'];
        const engineLabel = `<span class="badge" style="background: ${engine.bg}; color: ${engine.color}; border: none; font-size: 0.7rem;">${engine.icon} ${engine.label}</span>`;

        const statusMap = {
            'pending': { label: t('deep.status.pending'), class: 'badge-warning', icon: '<div class="spinner" style="width: 10px; height: 10px; border-width: 2px; margin-right: 4px; display: inline-block;"></div>' },
            'completed': { label: t('deep.status.completed'), class: 'badge-success', icon: '✅' },
            'failed': { label: t('deep.status.failed'), class: 'badge-danger', icon: '❌' }
        };
        const status = statusMap[record.status] || { label: t('common.unknown'), class: 'badge-outline', icon: '❓' };

        var alertTypeTag = '';
        if (record.is_duplicate) {
            alertTypeTag = '<span class="badge" style="background: #e2e8f0; color: #334155; font-size: 0.7rem;">🔁 ' + escapeHtml(t('deep.duplicateAlert')) + '</span>';
        } else {
            alertTypeTag = '<span class="badge" style="background: #dcfce7; color: #059669; font-size: 0.7rem;">🆕 ' + escapeHtml(t('deep.newAlert')) + '</span>';
        }

        let html = `
            <div class="da-summary" onclick="DeepAnalysesModule.toggleExpand(${record.id})">
                <div class="da-summary-main">
                    <div class="da-summary-meta-row">
                        <span class="badge ${status.class}" style="display: flex; align-items: center; font-size: 0.7rem;">${status.icon} ${status.label}</span>
                        ${engineLabel}
                        <span class="da-alert-title">🔔 ${escapeHtml(t('deep.alertNumber', { n: record.webhook_event_id }))}</span>
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
                html += '<div class="da-preview da-preview-empty">' + escapeHtml(t('deep.report.unavailable')) + '</div>';
            }
        } else if (record.status === 'pending') {
            const runIdText = record.openclaw_run_id ? `(${escapeHtml(t('deep.runId'))}: <span style="font-family: monospace;">${escapeHtml(record.openclaw_run_id.substring(0,8))}</span>)` : '';
            var pollInfo = [];
            if (record.poll_attempts != null) pollInfo.push(t('deep.polledTimes', { n: record.poll_attempts }));
            if (record.last_polled_at) pollInfo.push(t('deep.lastPolled', { time: new Date(record.last_polled_at).toLocaleTimeString('zh-CN') }));
            const pollText = pollInfo.length > 0 ? `<div style="color: var(--text-muted); font-size: 0.75rem; margin-top: 0.2rem;">${escapeHtml(pollInfo.join(' · '))}</div>` : '';
            html += `<div class="da-preview da-preview-pending">${escapeHtml(t('deep.waitingForReport', { engine: engine.label }))} ${runIdText}</div>${pollText}`;
        } else if (record.status === 'failed') {
            let errorMsg = record.summary_preview || report.failure_reason || report.root_cause || report.primary_text || t('common.unknownError');
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
        const engineLabel = record.engine === 'openclaw' ? t('deep.engine.openclaw') : (record.engine === 'hermes' ? t('deep.engine.hermes') : t('deep.engine.local'));

        let detailsHtml = '';

        if (record.user_question) {
            detailsHtml += `
                <div style="background: var(--bg-base); padding: 1rem 1.25rem; border-radius: var(--radius-md); border-left: 4px solid var(--primary); margin-bottom: 1.5rem;">
                    <strong style="color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; display: block; margin-bottom: 0.5rem;">👤 ${escapeHtml(t('deep.userQuestion'))}</strong>
                    <div style="color: var(--text-main); font-size: 0.95rem;">${escapeHtml(record.user_question)}</div>
                </div>
            `;
        }

        if (record.status === 'pending') {
            detailsHtml += `
                <div style="text-align: center; padding: 3rem 1rem;">
                    <div class="spinner" style="width: 32px; height: 32px; margin: 0 auto 1rem auto; border-left-color: var(--primary);"></div>
                    <div style="color: var(--text-main); font-weight: 500; font-size: 1.1rem; margin-bottom: 0.5rem;">${escapeHtml(t('deep.runningAnalysis', { engine: engineLabel }))}</div>
                    ${record.openclaw_run_id ? `<div style="color: var(--text-muted); font-family: monospace; font-size: 0.85rem; background: var(--bg-subtle); display: inline-block; padding: 0.25rem 0.5rem; border-radius: 4px; margin-top: 0.5rem;">${escapeHtml(t('deep.runId'))}: ${escapeHtml(record.openclaw_run_id)}</div>` : ''}
                </div>
            `;
        } else if (record.status === 'failed') {
            detailsHtml += `
                <div style="background: var(--danger-bg); border: 1px solid rgba(239,68,68,0.2); padding: 1.5rem; border-radius: var(--radius-md); color: var(--danger);">
                    <h4 style="margin-bottom: 0.5rem; font-weight: 600;">⚠️ ${escapeHtml(t('deep.taskCrashed'))}</h4>
                    <p style="margin: 0; font-size: 0.95rem;">${escapeHtml(report.failure_reason || report.root_cause || report.primary_text || t('deep.unknownException'))}</p>
                </div>
            `;
            detailsHtml += renderNormalizedReport(report);
        } else {
            detailsHtml += renderNormalizedReport(report);
        }

        // Action Buttons Row
        detailsHtml += `<div style="margin-top: 1.5rem; padding-top: 1.5rem; border-top: 1px solid var(--border); display: flex; gap: 1rem; flex-wrap: wrap;">`;

        if (record.status === 'completed') {
            detailsHtml += `<button class="btn" style="background: var(--bg-subtle); border-color: var(--border); color: var(--text-main);" onclick="DeepAnalysesModule.forwardResult(${record.id})">📨 ${escapeHtml(t('deep.btn.pushResult'))}</button>`;
        }
        if (record.status === 'failed' || record.status === 'pending') {
            detailsHtml += `<button class="btn btn-primary" onclick="DeepAnalysesModule.retryAnalysis(${record.id})">🔄 ${escapeHtml(t('deep.btn.refetchResult'))}</button>`;
        }
        detailsHtml += `<button class="btn" style="border-color: var(--success); color: var(--success); background: var(--success-bg);" onclick="DeepAnalysesModule.reanalyzeFromDeepAnalysis(${record.webhook_event_id})">🔬 ${escapeHtml(t('deep.btn.freshAnalysis'))}</button>`;

        // Raw Data Toggle
        const rawJsonId = `raw-da-${record.id}`;
        detailsHtml += `<button class="btn" onclick="DeepAnalysesModule.toggleDebugPayload(${record.id})">💻 ${escapeHtml(t('deep.btn.debugPayload'))}</button>`;

        detailsHtml += `</div>`;

        detailsHtml += `<div id="${rawJsonId}" style="margin-top: 1.5rem;" hidden></div>`;

        return detailsHtml;
    }

    function renderDeepAnalyses(records) {
        var container = document.getElementById('deepAnalysesList');
        if (!container) return;

        if (!records || records.length === 0) {
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888; background: var(--bg-surface); border-radius: var(--radius-lg); border: 1px dashed var(--border);">' + escapeHtml(t('deep.empty.noRecords')) + '</div>';
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
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888;">' + escapeHtml(t('deep.loadingRecords')) + '</div>';
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
                        showToast(t('deep.loadMoreFailed') + ': ' + (res.error || t('common.unknownError')), 'error');
                    } else if (container) {
                        container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">' + escapeHtml(t('common.loadFailed')) + ': ' + escapeHtml(res.error) + '</div>';
                    }
                    renderPagination();
                    stopAutoRefresh();
                }
            })
            .catch(function(error) {
                isLoadingMore = false;
                if (append && typeof showToast === 'function') {
                    showToast(t('deep.loadMoreFailed') + ': ' + error.message, 'error');
                } else if (container) {
                    container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">' + escapeHtml(t('deep.loadError')) + ': ' + escapeHtml(error.message) + '</div>';
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
        var url = prompt(t('deep.forward.prompt'), '');

        if (url === null) return; // User clicked Cancel

        url = url.trim();
        if (!url) {
            alert('❌ ' + t('deep.forward.urlEmpty'));
            return;
        }
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            alert('❌ ' + t('deep.forward.urlInvalid'));
            return;
        }

        try {
            var result = await API.forwardDeepAnalysis(analysisId, url);
            if (result.success) {
                alert('✅ ' + (result.message || t('deep.forward.pushed')));
            } else {
                alert('❌ ' + t('deep.forward.failed') + ': ' + (result.message || t('common.unknownError')));
            }
        } catch (e) {
            alert('❌ ' + t('common.requestFailed') + ': ' + e.message);
        }
    }

    async function reanalyzeFromDeepAnalysis(webhookEventId) {
        if (!confirm(t('deep.reanalyze.confirm'))) return;

        try {
            var result = await API.deepAnalyze(webhookEventId, '', 'auto');
            if (result.success) {
                alert('🚀 ' + t('deep.reanalyze.started'));
                load();
            } else {
                alert('❌ ' + t('deep.reanalyze.failed') + ': ' + (result.message || result.error || t('common.unknownError')));
            }
        } catch (e) {
            alert('❌ ' + t('common.requestFailed') + ': ' + e.message);
        }
    }

    async function retryAnalysis(analysisId) {
        if (!confirm(t('deep.retry.confirm'))) return;

        try {
            var result = await API.retryDeepAnalysis(analysisId);
            if (result.success) {
                alert('🔄 ' + (result.message || t('deep.retry.triggered')));
                load();
            } else {
                alert('❌ ' + t('deep.retry.failed') + ': ' + (result.error || result.message || t('common.unknownError')));
            }
        } catch (e) {
            alert('❌ ' + t('common.requestFailed') + ': ' + e.message);
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
