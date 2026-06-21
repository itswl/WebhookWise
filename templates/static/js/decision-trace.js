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
    var currentView = 'trace';  // 'trace' | 'cost' (AI cost merged in from its old tab)
    var currentSkipCode = '';
    var currentOutcome = '';
    var currentSource = '';
    var currentDelivery = '';  // '' | 'failed'
    var knownSources = [];     // populated from quality stats to fill the source dropdown
    var loadedTraces = [];
    var nextCursor = null;
    var hasMoreTraces = false;
    var isLoadingMore = false;
    var perPage = 50;
    var expandedIds = new Set();
    // Show a manual re-enqueue button on retryable delivery targets in the
    // expanded view (so the trace fully covers what the Forward Queue tab did).
    var DT_ENABLE_RETRY = true;

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

    // ── AI judgment quality (proxy signals) ──────────────────────────

    function distributionBar(breakdown, total, colorMap) {
        // A single horizontal stacked bar of label→count, with a legend below.
        var keys = Object.keys(breakdown || {});
        if (!keys.length || !total) return '<div style="color: var(--text-muted); font-size: 0.85rem;">—</div>';
        keys.sort(function (a, b) { return (breakdown[b] || 0) - (breakdown[a] || 0); });
        var segs = keys.map(function (k) {
            var pct = (breakdown[k] / total * 100);
            var color = (colorMap && colorMap[k]) || '#94a3b8';
            return '<div title="' + escapeHtml(k) + ': ' + breakdown[k] + '" style="width:' + pct + '%; background:' + color + ';"></div>';
        }).join('');
        var legend = keys.map(function (k) {
            var color = (colorMap && colorMap[k]) || '#94a3b8';
            var label = (colorMap && colorMap['_label_' + k]) || k;
            return '<span style="display:inline-flex; align-items:center; gap:4px; margin-right:12px; font-size:0.8rem; color:var(--text-secondary);">' +
                '<span style="width:10px; height:10px; border-radius:2px; background:' + color + '; display:inline-block;"></span>' +
                escapeHtml(label) + ' <strong>' + breakdown[k] + '</strong></span>';
        }).join('');
        return '<div style="display:flex; height:10px; border-radius:5px; overflow:hidden; margin-bottom:0.5rem;">' + segs + '</div>' +
            '<div style="display:flex; flex-wrap:wrap;">' + legend + '</div>';
    }

    var IMPORTANCE_COLORS = {
        'high': '#e11d48', '_label_high': 'high',
        'medium': '#d97706', '_label_medium': 'medium',
        'low': '#059669', '_label_low': 'low',
        'unknown': '#94a3b8', '_label_unknown': 'unknown'
    };

    function renderQuality(data) {
        var container = document.getElementById('decisionTraceQuality');
        if (!container) return;

        var aiTotal = data.ai_total || 0;
        var overrideRate = data.override_rate || 0;
        var degradedRate = data.degraded_rate || 0;

        var html = '' +
            '<div style="font-size: 1rem; font-weight: 600; color: var(--text-main); margin: 1rem 0 0.75rem;">' + t('dt.quality.title') + '</div>' +
            '<div style="background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 1.25rem; margin-bottom: 1.5rem;">' +
                '<p style="margin: 0 0 1rem; color: var(--text-muted); font-size: 0.8rem;">' + t('dt.quality.note') + '</p>' +
                '<div class="stats-grid" style="margin-bottom: 1.25rem;">' +
                    '<div class="stat-card"><div class="stat-label">🧠 ' + t('dt.quality.aiJudgments') + '</div>' +
                        '<div class="stat-value" style="font-size: 1.75rem;">' + formatNumber(aiTotal) + '</div>' +
                        '<div class="stat-trend">' + t('dt.quality.aiJudgmentsTrend') + '</div></div>' +
                    '<div class="stat-card" style="border-left: 3px solid var(--warning);"><div class="stat-label">⚖️ ' + t('dt.quality.overrideRate') + '</div>' +
                        '<div class="stat-value" style="font-size: 1.75rem; color: var(--warning);">' + overrideRate.toFixed(1) + '%</div>' +
                        '<div class="stat-trend">' + t('dt.quality.overrideTrend', { n: formatNumber(data.override_count || 0) }) + '</div></div>' +
                    '<div class="stat-card" style="border-left: 3px solid var(--text-muted);"><div class="stat-label">📉 ' + t('dt.quality.degradedRate') + '</div>' +
                        '<div class="stat-value" style="font-size: 1.75rem;">' + degradedRate.toFixed(1) + '%</div>' +
                        '<div class="stat-trend">' + t('dt.quality.degradedTrend', { n: formatNumber(data.degraded_total || 0) }) + '</div></div>' +
                '</div>';

        // AI importance distribution (fresh ai-route judgments only).
        html += '<div style="font-size: 0.9rem; font-weight: 600; margin: 0.5rem 0;">' + t('dt.quality.importanceDist') + '</div>';
        html += distributionBar(data.ai_importance_breakdown, aiTotal, IMPORTANCE_COLORS);

        // Degradation reasons, if any.
        var reasons = data.degraded_reasons || {};
        if (Object.keys(reasons).length) {
            html += '<div style="font-size: 0.9rem; font-weight: 600; margin: 1.25rem 0 0.5rem;">' + t('dt.quality.degradedReasons') + '</div>';
            html += '<div style="display:flex; flex-wrap:wrap; gap:0.5rem;">';
            Object.keys(reasons).sort(function (a, b) { return reasons[b] - reasons[a]; }).forEach(function (r) {
                html += '<span class="badge badge-outline" style="font-size:0.75rem;">' + escapeHtml(r) + ' <strong>' + reasons[r] + '</strong></span>';
            });
            html += '</div>';
        }

        // Per-source importance distribution (only sources with ai judgments).
        var bySource = data.ai_importance_by_source || {};
        var sources = Object.keys(bySource);
        if (sources.length) {
            html += '<div style="font-size: 0.9rem; font-weight: 600; margin: 1.25rem 0 0.5rem;">' + t('dt.quality.bySource') + '</div>';
            sources.sort();
            sources.forEach(function (src) {
                var dist = bySource[src];
                var srcTotal = Object.keys(dist).reduce(function (s, k) { return s + dist[k]; }, 0);
                html += '<div style="margin-bottom: 0.75rem;">' +
                    '<div style="font-size:0.8rem; color:var(--text-secondary); margin-bottom:0.3rem;">📡 ' + escapeHtml(src) + ' <span style="color:var(--text-muted);">(' + srcTotal + ')</span></div>' +
                    distributionBar(dist, srcTotal, IMPORTANCE_COLORS) + '</div>';
            });
        }

        html += '</div>';
        container.innerHTML = html;
    }

    function loadQuality(period) {
        return API.getDecisionTraceQualityStats(period || currentPeriod || 'day')
            .then(function (res) {
                if (res && res.success && res.data) {
                    renderQuality(res.data);
                } else {
                    var c = document.getElementById('decisionTraceQuality');
                    if (c) c.innerHTML = '';
                }
            })
            .catch(function (err) {
                console.error('Failed to load decision-trace quality stats:', err);
                var c = document.getElementById('decisionTraceQuality');
                if (c) c.innerHTML = '';
            });
    }

    // ── Trace list ───────────────────────────────────────────────────

    function outcomeBadge(trace) {
        if (trace.outcome === 'forwarded') {
            return '<span class="badge badge-success" style="font-size: 0.7rem;">✅ ' + t('dt.outcome.forwarded') + '</span>';
        }
        return '<span class="badge" style="background: #e2e8f0; color: #334155; font-size: 0.7rem;">⏭️ ' + t('dt.outcome.skipped') + '</span>';
    }

    // Delivery (outbox) badge for a forwarded row — shows whether the
    // notification actually reached its target. Absent when there's no outbox
    // record (e.g. skipped rows, or pre-feature data).
    function deliveryBadge(trace) {
        var d = trace.delivery;
        if (!d || !d.state) return '';
        if (d.state === 'sent') {
            return '<span class="badge badge-success" style="font-size: 0.7rem;">📨 ' + t('dt.delivery.sent') + '</span>';
        }
        if (d.state === 'pending') {
            return '<span class="badge" style="background: #fef3c7; color: #b45309; font-size: 0.7rem;">🔁 ' + t('dt.delivery.pending') + '</span>';
        }
        if (d.state === 'failed') {
            return '<span class="badge badge-danger" style="font-size: 0.7rem;">❌ ' + t('dt.delivery.failed') + '</span>';
        }
        return '';
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

    function deliveryStatusBadge(status) {
        var map = {
            'sent': { cls: 'badge-success', icon: '📨' },
            'pending': { cls: 'badge-outline', icon: '⏳' },
            'processing': { cls: 'badge-outline', icon: '📤' },
            'retrying': { cls: 'badge-outline', icon: '🔁' },
            'exhausted': { cls: 'badge-danger', icon: '❌' },
            'expired': { cls: 'badge-danger', icon: '⌛' }
        };
        var m = map[status] || { cls: 'badge-outline', icon: '•' };
        var label = t('outbox.status.' + status);
        return '<span class="badge ' + m.cls + '" style="font-size: 0.7rem;">' + m.icon + ' ' + escapeHtml(label) + '</span>';
    }

    function kvRow(label, value) {
        if (value === null || value === undefined || value === '') return '';
        return '<div class="dt-step-row"><div class="dt-step-name">' + label + '</div>' +
            '<div class="dt-step-value">' + value + '</div></div>';
    }

    // Full per-target delivery detail in the expanded view — mirrors the
    // Forward Queue tab's depth (status, attempts, urls, errors, timestamps,
    // and a manual re-enqueue button for retryable targets).
    function renderDeliveryTarget(tgt) {
        var rows = '';
        rows += kvRow('📊 ' + t('dt.delivery.state'), deliveryStatusBadge(tgt.status));
        rows += kvRow('🎯 ' + t('dt.delivery.target'),
            escapeHtml(tgt.target_name || tgt.target_type || '—') +
            (tgt.target_type ? ' <span style="color:var(--text-muted);">(' + escapeHtml(tgt.target_type) + ')</span>' : ''));
        if (tgt.target_url) rows += kvRow('🔗 ' + t('dt.delivery.targetUrl'), '<code style="word-break:break-all;">' + escapeHtml(tgt.target_url) + '</code>');
        if (tgt.rule_name) rows += kvRow('⚙️ ' + t('dt.delivery.rule'), escapeHtml(tgt.rule_name));
        if (tgt.event_type) rows += kvRow('🏷️ ' + t('dt.delivery.eventType'),
            escapeHtml(tgt.event_type) + (tgt.is_periodic_reminder ? ' <span class="badge badge-outline" style="font-size:0.6rem;">' + t('dt.periodicReminder') + '</span>' : ''));
        rows += kvRow('🔢 ' + t('dt.delivery.attempts'), escapeHtml((tgt.attempts != null ? tgt.attempts : '—') + ' / ' + (tgt.max_attempts != null ? tgt.max_attempts : '—')));
        if (tgt.sent_at) rows += kvRow('🕓 ' + t('dt.delivery.sentAt'), escapeHtml(new Date(tgt.sent_at).toLocaleString()));
        if (tgt.next_attempt_at && tgt.status !== 'sent') rows += kvRow('⏭️ ' + t('dt.delivery.nextRetry'), escapeHtml(new Date(tgt.next_attempt_at).toLocaleString()));
        if (tgt.last_error) rows += kvRow('⚠️ ' + t('dt.delivery.error'), '<span style="color: var(--danger);">' + escapeHtml(tgt.last_error) + '</span>');
        if (tgt.retryable && DT_ENABLE_RETRY) {
            rows += '<div class="dt-step-row"><div class="dt-step-name"></div><div class="dt-step-value">' +
                '<button class="btn btn-sm" onclick="DecisionTraceModule.retryDelivery(' + tgt.outbox_id + ')">🔄 ' + t('dt.delivery.retry') + '</button></div></div>';
        }
        return '<div class="dt-chain" style="padding-top: 0;">' + rows + '</div>';
    }

    function renderDelivery(trace) {
        var d = trace.delivery;
        if (!d) return '';
        var targets = Array.isArray(d.targets) ? d.targets : [];
        var header = '<div style="font-size: 0.85rem; font-weight: 600; margin: 0.75rem 1.25rem 0.25rem;">' +
            t('dt.delivery.title') + (targets.length > 1 ? ' (' + targets.length + ')' : '') + '</div>';
        if (!targets.length) return header + '<div style="color: var(--text-muted); padding: 0 1.25rem 0.75rem;">—</div>';
        return header + targets.map(renderDeliveryTarget).join('<div style="height:1px;background:var(--border);margin:0 1.25rem;"></div>');
    }

    function retryDelivery(outboxId) {
        if (!confirm(t('dt.delivery.retryConfirm', { n: outboxId }))) return;
        API.retryOutbox(outboxId).then(function (r) {
            if (r && r.success) {
                if (typeof showToast === 'function') showToast(t('dt.delivery.retryOk'), 'success');
                load();
            } else if (typeof showToast === 'function') {
                showToast((r && r.error) || t('dt.delivery.retryFail'), 'error');
            }
        }).catch(function (e) {
            if (typeof showToast === 'function') showToast(t('dt.delivery.retryFail') + ': ' + (e && e.message), 'error');
        });
    }

    function renderDetails(trace) {
        return renderChain(trace.steps) + renderDelivery(trace);
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
        var delivery = deliveryBadge(trace);
        if (delivery) parts.push(delivery);
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
        details.innerHTML = trace ? renderDetails(trace) : '';
    }

    function updateSourceDropdown(traces) {
        // Keep the source filter options in sync with sources actually seen
        // (union of what we've loaded), without clobbering the current selection.
        var sel = document.getElementById('dtSourceFilter');
        if (!sel) return;
        (traces || []).forEach(function (tr) {
            if (tr.source && knownSources.indexOf(tr.source) === -1) knownSources.push(tr.source);
        });
        knownSources.sort();
        var current = sel.value;
        var opts = '<option value="">' + t('dt.filter.allSources') + '</option>';
        knownSources.forEach(function (s) {
            opts += '<option value="' + escapeHtml(s) + '"' + (s === current ? ' selected' : '') + '>' + escapeHtml(s) + '</option>';
        });
        sel.innerHTML = opts;
    }

    function renderTraces(traces) {
        var container = document.getElementById('decisionTraceList');
        updateSourceDropdown(traces);
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
            html += isExpanded ? renderDetails(trace) : '';
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
            source: currentSource,
            delivery: currentDelivery
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

    function loadCost() {
        // The AI cost view reuses the existing AICostModule renderer.
        if (typeof AICostModule !== 'undefined') {
            AICostModule.loadStats(currentPeriod);
        }
    }

    // Load whichever view is active (tab open / refresh / period change).
    function loadActiveView() {
        if (currentView === 'cost') {
            loadCost();
        } else {
            loadStats(currentPeriod);
            loadQuality(currentPeriod);
            loadList();
        }
    }

    function load() {
        loadActiveView();
    }

    function setPeriod(period) {
        currentPeriod = period;
        loadActiveView();
    }

    function setView(view) {
        currentView = view === 'cost' ? 'cost' : 'trace';
        var traceEl = document.getElementById('dtViewTrace');
        var costEl = document.getElementById('dtViewCost');
        if (traceEl) traceEl.style.display = currentView === 'trace' ? 'block' : 'none';
        if (costEl) costEl.style.display = currentView === 'cost' ? 'block' : 'none';
        document.querySelectorAll('[data-dt-view]').forEach(function (btn) {
            btn.classList.toggle('active', btn.getAttribute('data-dt-view') === currentView);
        });
        loadActiveView();
    }

    // A skip-code chip narrows to skipped+that code; it overrides the dropdown
    // filters, so reset those controls to keep the UI honest.
    function _resetListFilterControls(outcomeVal) {
        var outcomeSel = document.getElementById('dtOutcomeFilter');
        var sourceSel = document.getElementById('dtSourceFilter');
        var failedChk = document.getElementById('dtDeliveryFailedFilter');
        if (outcomeSel) outcomeSel.value = outcomeVal || '';
        if (sourceSel) sourceSel.value = '';
        if (failedChk) failedChk.checked = false;
        currentSource = '';
        currentDelivery = '';
    }

    function filterBySkipCode(code) {
        // Toggle off if the active chip is clicked again.
        currentSkipCode = (currentSkipCode === code) ? '' : code;
        currentOutcome = currentSkipCode ? 'skipped' : '';
        _resetListFilterControls(currentOutcome);
        expandedIds.clear();
        loadStats(currentPeriod);
        loadList();
    }

    function filterByOutcome(outcome) {
        currentOutcome = outcome;
        currentSkipCode = '';
        _resetListFilterControls(outcome);
        expandedIds.clear();
        loadStats(currentPeriod);
        loadList();
    }

    // Read the list filter controls and reload the list. Selecting an outcome
    // here clears any skip-code chip (they both drive `outcome`), and vice versa
    // the chips reset these via loadList re-reading state.
    function applyListFilters() {
        var outcomeSel = document.getElementById('dtOutcomeFilter');
        var sourceSel = document.getElementById('dtSourceFilter');
        var failedChk = document.getElementById('dtDeliveryFailedFilter');
        currentOutcome = outcomeSel ? outcomeSel.value : '';
        currentSource = sourceSel ? sourceSel.value : '';
        currentDelivery = (failedChk && failedChk.checked) ? 'failed' : '';
        // A picked outcome supersedes a skip-code chip; "delivery failed" implies forwarded.
        if (currentDelivery === 'failed') { currentOutcome = 'forwarded'; currentSkipCode = ''; }
        else { currentSkipCode = ''; }
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
        document.querySelectorAll('[data-dt-view]').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                var button = e.target.closest('[data-dt-view]');
                var view = button ? button.getAttribute('data-dt-view') : null;
                if (view) setView(view);
            });
        });
        ['dtOutcomeFilter', 'dtSourceFilter', 'dtDeliveryFailedFilter'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.addEventListener('change', applyListFilters);
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        bindEvents();
    });

    return {
        load: load,
        loadMore: loadMore,
        setPeriod: setPeriod,
        setView: setView,
        toggleExpand: toggleExpand,
        retryDelivery: retryDelivery,
        filterBySkipCode: filterBySkipCode,
        filterByOutcome: filterByOutcome
    };
})();
