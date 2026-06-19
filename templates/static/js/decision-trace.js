/**
 * Decision Trace module
 *
 * Read-only view of why each alert was forwarded or skipped:
 *  - a time-windowed aggregate strip (forwarded vs skipped + the skip-reason
 *    distribution), clickable to filter the list below;
 *  - a paginated trace list, each row expandable to its full ordered decision
 *    chain (dedup -> analysis -> noise -> silence -> rule_match -> forward),
 *    served inline so expansion needs no extra request.
 */
var DecisionTraceModule = (function () {
    var currentPeriod = 'day';
    var currentSkipCode = '';
    var currentOutcome = '';
    var loadedTraces = [];
    var nextCursor = null;
    var hasMoreTraces = false;
    var isLoadingMore = false;
    var perPage = 50;
    var expandedIds = new Set();

    // Display metadata per skip_code: icon + i18n label key. "none" is the
    // forwarded case; the rest are the suppressor / no-rule outcomes.
    var SKIP_CODE_META = {
        'none': { icon: '✅', key: 'dt.code.none' },
        'silenced': { icon: '🔕', key: 'dt.code.silenced' },
        'cooldown': { icon: '⏳', key: 'dt.code.cooldown' },
        'duplicate_no_rule': { icon: '🔁', key: 'dt.code.duplicateNoRule' },
        'noise_suppressed': { icon: '🌊', key: 'dt.code.noiseSuppressed' },
        'no_match': { icon: '🚫', key: 'dt.code.noMatch' },
        'periodic_no_rule': { icon: '🔔', key: 'dt.code.periodicNoRule' }
    };

    var STEP_META = {
        'dedup': { icon: '🧬', key: 'dt.step.dedup' },
        'analysis': { icon: '🧠', key: 'dt.step.analysis' },
        'noise': { icon: '🌊', key: 'dt.step.noise' },
        'silence': { icon: '🔕', key: 'dt.step.silence' },
        'rule_match': { icon: '🎯', key: 'dt.step.ruleMatch' },
        'forward': { icon: '📤', key: 'dt.step.forward' }
    };

    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function skipCodeLabel(code) {
        var meta = SKIP_CODE_META[code];
        return meta ? (meta.icon + ' ' + t(meta.key)) : escapeHtml(code);
    }

    // ── Aggregate strip ──────────────────────────────────────────────

    function updatePeriodButtons(period) {
        document.querySelectorAll('[data-dt-period]').forEach(function (btn) {
            btn.classList.toggle('active', btn.getAttribute('data-dt-period') === period);
        });
    }

    function renderStats(data) {
        var container = document.getElementById('decisionTraceStats');
        if (!container) return;

        var total = data.total || 0;
        var forwarded = data.forwarded || 0;
        var skipped = data.skipped || 0;
        var fwdPct = total > 0 ? (forwarded / total * 100) : 0;
        var skipBreakdown = data.skip_code_breakdown || {};

        var html = '' +
            '<div class="stats-grid" style="margin-bottom: 1.5rem;">' +
                '<div class="stat-card" style="border-left: 4px solid var(--primary);">' +
                    '<div class="stat-label">' + t('dt.stat.total') + '</div>' +
                    '<div class="stat-value" style="font-size: 2rem;">' + formatNumber(total) + '</div>' +
                    '<div class="stat-trend">' + t('dt.stat.totalTrend') + '</div>' +
                '</div>' +
                '<div class="stat-card" style="border-left: 4px solid var(--success); background: #f0fdf4;">' +
                    '<div class="stat-label" style="color: #059669;">✅ ' + t('dt.stat.forwarded') + '</div>' +
                    '<div class="stat-value" style="color: var(--success); font-size: 2rem;">' + formatNumber(forwarded) + '</div>' +
                    '<div class="stat-trend" style="color: #059669;">' + fwdPct.toFixed(1) + '%</div>' +
                '</div>' +
                '<div class="stat-card" style="border-left: 4px solid var(--text-muted);">' +
                    '<div class="stat-label">⏭️ ' + t('dt.stat.skipped') + '</div>' +
                    '<div class="stat-value" style="font-size: 2rem;">' + formatNumber(skipped) + '</div>' +
                    '<div class="stat-trend">' + (100 - fwdPct).toFixed(1) + '%</div>' +
                '</div>' +
            '</div>';

        // Skip-reason distribution: clickable chips that filter the list below.
        html += '<div style="font-size: 1rem; font-weight: 600; color: var(--text-main); margin-bottom: 0.75rem;">' + t('dt.section.skipReasons') + '</div>';
        var codes = Object.keys(skipBreakdown);
        if (codes.length === 0) {
            html += '<div style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1.5rem;">' + t('dt.section.noSkips') + '</div>';
        } else {
            html += '<div style="display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 1.5rem;">';
            // "All" chip resets the filter.
            var allActive = currentSkipCode === '' && currentOutcome === '';
            html += '<button class="btn dt-chip' + (allActive ? ' active' : '') + '" onclick="DecisionTraceModule.filterByOutcome(\'\')">' + t('common.all') + '</button>';
            codes.sort(function (a, b) { return (skipBreakdown[b] || 0) - (skipBreakdown[a] || 0); });
            codes.forEach(function (code) {
                var active = currentSkipCode === code;
                html += '<button class="btn dt-chip' + (active ? ' active' : '') + '" onclick="DecisionTraceModule.filterBySkipCode(\'' + escapeHtml(code) + '\')">' +
                    skipCodeLabel(code) + ' <strong>' + formatNumber(skipBreakdown[code]) + '</strong></button>';
            });
            html += '</div>';
        }

        container.innerHTML = html;
    }

    function renderEmptyStats() {
        var container = document.getElementById('decisionTraceStats');
        if (!container) return;
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-title">' + t('dt.empty.statsTitle') + '</div><div class="empty-text">' + t('dt.empty.statsText') + '</div></div>';
    }

    function loadStats(period) {
        currentPeriod = period || currentPeriod || 'day';
        updatePeriodButtons(currentPeriod);
        return API.getDecisionTraceStats(currentPeriod)
            .then(function (res) {
                if (res && res.success && res.data) {
                    renderStats(res.data);
                } else {
                    renderEmptyStats();
                }
            })
            .catch(function (err) {
                console.error('Failed to load decision-trace stats:', err);
                renderEmptyStats();
            });
    }

    // ── Trace list ───────────────────────────────────────────────────

    function outcomeBadge(trace) {
        if (trace.outcome === 'forwarded') {
            return '<span class="badge badge-success" style="font-size: 0.7rem;">✅ ' + t('dt.outcome.forwarded') + '</span>';
        }
        return '<span class="badge" style="background: #e2e8f0; color: #334155; font-size: 0.7rem;">⏭️ ' + t('dt.outcome.skipped') + '</span>';
    }

    function renderStepValue(step) {
        // Render a compact, human-readable summary per step type. Unknown keys
        // fall back to a JSON dump so a future step kind still shows something.
        switch (step.step) {
            case 'dedup':
                return t('dt.step.dedup.detail', {
                    action: escapeHtml(step.action),
                    dup: step.is_duplicate ? t('common.yes') : t('common.no'),
                    orig: step.original_event_id != null ? ('#' + escapeHtml(step.original_event_id)) : '—'
                });
            case 'analysis':
                return t('dt.step.analysis.detail', {
                    route: escapeHtml(step.route),
                    importance: escapeHtml(step.importance || '—'),
                    degraded: step.degraded ? t('common.yes') : t('common.no')
                });
            case 'noise':
                return t('dt.step.noise.detail', {
                    relation: escapeHtml(step.relation || '—'),
                    suppress: step.suppress_forward ? t('common.yes') : t('common.no'),
                    reason: escapeHtml(step.reason || '—')
                });
            case 'silence':
                return t('dt.step.silence.detail', { id: escapeHtml(step.silence_id != null ? step.silence_id : '—') });
            case 'rule_match':
                var matched = Array.isArray(step.matched) ? step.matched : [];
                return matched.length
                    ? escapeHtml(matched.join(', '))
                    : t('dt.step.ruleMatch.none');
            case 'forward':
                return t('dt.step.forward.detail', {
                    outcome: escapeHtml(step.outcome),
                    code: skipCodeLabel(step.skip_code),
                    reason: escapeHtml(step.skip_reason || '—')
                });
            default:
                try { return escapeHtml(JSON.stringify(step)); } catch (e) { return ''; }
        }
    }

    function renderChain(steps) {
        if (!Array.isArray(steps) || steps.length === 0) {
            return '<div style="color: var(--text-muted); padding: 1rem;">' + t('dt.chain.empty') + '</div>';
        }
        var rows = steps.map(function (step) {
            var meta = STEP_META[step.step] || { icon: '•', key: '' };
            var label = meta.key ? t(meta.key) : escapeHtml(step.step);
            return '<div class="dt-step-row">' +
                '<div class="dt-step-name">' + meta.icon + ' ' + label + '</div>' +
                '<div class="dt-step-value">' + renderStepValue(step) + '</div>' +
            '</div>';
        }).join('');
        return '<div class="dt-chain">' + rows + '</div>';
    }

    function buildSummaryHtml(trace) {
        var time = trace.created_at ? new Date(trace.created_at).toLocaleString() : '-';
        var parts = [];
        parts.push(outcomeBadge(trace));
        if (trace.outcome === 'skipped') {
            parts.push('<span class="badge badge-outline" style="font-size: 0.7rem;">' + skipCodeLabel(trace.skip_code) + '</span>');
        }
        if (trace.is_periodic_reminder) {
            parts.push('<span class="badge" style="background: #fef3c7; color: #b45309; font-size: 0.7rem;">🔔 ' + t('dt.periodicReminder') + '</span>');
        }
        var metaLine = [escapeHtml(trace.source || '—'), escapeHtml(trace.importance || '—')]
            .filter(Boolean).join(' · ');

        return '' +
            '<div class="da-summary" onclick="DecisionTraceModule.toggleExpand(' + trace.id + ')">' +
                '<div class="da-summary-main">' +
                    '<div class="da-summary-meta-row">' +
                        parts.join(' ') +
                        '<span class="da-alert-title">🔔 ' + t('dt.alertNumber', { n: escapeHtml(trace.webhook_event_id) }) + '</span>' +
                        '<span class="da-source">📡 ' + metaLine + '</span>' +
                    '</div>' +
                '</div>' +
                '<div class="da-summary-runtime"><div>' + escapeHtml(time) + '</div></div>' +
            '</div>';
    }

    function findLoadedTrace(id) {
        var numericId = Number(id);
        return loadedTraces.find(function (trace) { return Number(trace.id) === numericId; }) || null;
    }

    function toggleExpand(id) {
        var card = document.getElementById('dt-record-' + id);
        var details = document.getElementById('dt-details-' + id);
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
        // The full chain ships inline with the list row, so render immediately.
        var trace = findLoadedTrace(id);
        details.innerHTML = trace ? renderChain(trace.steps) : '';
    }

    function renderTraces(traces) {
        var container = document.getElementById('decisionTraceList');
        if (!container) return;

        if (!traces || traces.length === 0) {
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888; background: var(--bg-surface); border-radius: var(--radius-lg); border: 1px dashed var(--border);">' + t('dt.empty.noTraces') + '</div>';
            return;
        }

        var html = '';
        traces.forEach(function (trace) {
            var isExpanded = expandedIds.has(trace.id);
            html += '<div id="dt-record-' + trace.id + '" class="' + (isExpanded ? 'da-card da-card-expanded' : 'da-card') + '">';
            html += buildSummaryHtml(trace);
            html += '<div id="dt-details-' + trace.id + '" class="da-details" style="' + (isExpanded ? 'display: block;' : 'display: none;') + '">';
            html += isExpanded ? renderChain(trace.steps) : '';
            html += '</div></div>';
        });
        container.innerHTML = html;
    }

    function renderPagination() {
        var container = document.getElementById('decisionTracePagination');
        if (!container) return;
        renderLoadMorePagination(container, {
            loaded: loadedTraces.length,
            total: loadedTraces.length + (hasMoreTraces ? perPage : 0),
            batchSize: perPage,
            hasMore: hasMoreTraces,
            isLoading: isLoadingMore,
            onLoadMore: loadMore
        });
    }

    function fetchPage(cursor, append) {
        var container = document.getElementById('decisionTraceList');
        return API.getDecisionTraces({
            cursor: cursor,
            page_size: perPage,
            outcome: currentOutcome,
            skip_code: currentSkipCode,
            source: ''
        })
            .then(function (res) {
                if (res && res.success) {
                    nextCursor = (res.pagination && res.pagination.next_cursor) || null;
                    hasMoreTraces = !!(res.pagination && res.pagination.has_more);
                    var incoming = res.data || [];
                    loadedTraces = append ? loadedTraces.concat(incoming) : incoming;
                    if (append) isLoadingMore = false;
                    renderTraces(loadedTraces);
                    renderPagination();
                } else {
                    isLoadingMore = false;
                    if (container && !append) {
                        container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">' + t('common.loadFailed') + ': ' + escapeHtml(res && res.error) + '</div>';
                    }
                    renderPagination();
                }
            })
            .catch(function (err) {
                isLoadingMore = false;
                if (container && !append) {
                    container.innerHTML = '<div style="text-align: center; padding: 40px; color: red;">' + t('common.loadFailed') + ': ' + escapeHtml(err && err.message) + '</div>';
                }
                renderPagination();
            });
    }

    function loadList() {
        loadedTraces = [];
        nextCursor = null;
        hasMoreTraces = false;
        var container = document.getElementById('decisionTraceList');
        if (container && !expandedIds.size) {
            container.innerHTML = '<div style="text-align: center; padding: 40px; color: #888;">' + t('common.loading') + '</div>';
        }
        return fetchPage(null, false);
    }

    function loadMore() {
        if (!hasMoreTraces || isLoadingMore) return;
        isLoadingMore = true;
        renderPagination();
        fetchPage(nextCursor, true);
    }

    // Full reload of both the aggregate strip and the list (tab open / refresh).
    function load() {
        loadStats(currentPeriod);
        loadList();
    }

    function setPeriod(period) {
        currentPeriod = period;
        loadStats(period);
    }

    function filterBySkipCode(code) {
        // Toggle off if the active chip is clicked again.
        currentSkipCode = (currentSkipCode === code) ? '' : code;
        currentOutcome = currentSkipCode ? 'skipped' : '';
        expandedIds.clear();
        loadStats(currentPeriod);
        loadList();
    }

    function filterByOutcome(outcome) {
        currentOutcome = outcome;
        currentSkipCode = '';
        expandedIds.clear();
        loadStats(currentPeriod);
        loadList();
    }

    function bindEvents() {
        document.querySelectorAll('[data-dt-period]').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                var button = e.target.closest('[data-dt-period]');
                var period = button ? button.getAttribute('data-dt-period') : null;
                if (period) setPeriod(period);
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        bindEvents();
    });

    return {
        load: load,
        loadMore: loadMore,
        setPeriod: setPeriod,
        toggleExpand: toggleExpand,
        filterBySkipCode: filterBySkipCode,
        filterByOutcome: filterByOutcome
    };
})();
