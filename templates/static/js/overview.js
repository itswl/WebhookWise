/**
 * Overview home page — one-screen operational summary.
 *
 * Composes the overview endpoint (volume / forward rate / skip distribution /
 * delivery success / top sources) with AI usage (cost + calls) for the same
 * window. Read-only; reuses existing endpoints. Default landing tab.
 */
const OverviewModule = {
    currentPeriod: 'day',

    init() {
        this.bindEvents();
    },

    bindEvents() {
        document.querySelectorAll('[data-ov-period]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                const b = e.target.closest('[data-ov-period]');
                const period = b ? b.getAttribute('data-ov-period') : null;
                if (period) this.load(period);
            });
        });
    },

    updatePeriodButtons(period) {
        document.querySelectorAll('[data-ov-period]').forEach((btn) => {
            btn.classList.toggle('active', btn.getAttribute('data-ov-period') === period);
        });
    },

    // Drill from a skip-reason chip into the Decision Trace sub-view, filtered to
    // that skip_code. Overview and Decision Trace are sub-views of the same tab,
    // so this is an in-tab switch (no navigation).
    drillToSkip(skipCode) {
        if (typeof DecisionTraceModule === 'undefined') return;
        DecisionTraceModule.setView('trace');
        if (typeof DecisionTraceModule.filterBySkipCode === 'function') {
            DecisionTraceModule.filterBySkipCode(skipCode);
        }
    },

    // Same drill, keyed on outcome rather than skip reason — what the forward
    // and delivery headline cards are actually summarising.
    drillToOutcome(outcome) {
        if (typeof DecisionTraceModule === 'undefined') return;
        DecisionTraceModule.setView('trace');
        if (typeof DecisionTraceModule.filterByOutcome === 'function') {
            DecisionTraceModule.filterByOutcome(outcome);
        }
    },

    async load(period) {
        this.currentPeriod = period || this.currentPeriod || 'day';
        this.updatePeriodButtons(this.currentPeriod);
        const container = document.getElementById('overviewContent');
        if (!container) return;
        const mark = document.getElementById('ovLastRefreshed');
        try {
            // Overview + AI usage + recent incidents + sparkline + queue health,
            // in parallel. Everything but the core overview is best-effort
            // (.catch → null) so one failing probe never blanks the page.
            const [ovRes, aiRes, incRes, sparkRes, queueRes] = await Promise.all([
                API.getOverview(this.currentPeriod),
                API.getAIUsage(this.currentPeriod).catch(() => null),
                API.getIncidents({ status: 'active', page_size: 5 }).catch(() => null),
                this._fetchSparkline(7).catch(() => null),
                API.getQueueHealth().catch(() => null),
            ]);
            if (!ovRes || !ovRes.success || !ovRes.data) {
                container.innerHTML = this.emptyHtml();
            } else {
                const incidents = (incRes && incRes.success && incRes.data) ? incRes.data : [];
                var sparkData = (sparkRes && sparkRes.success && sparkRes.data) ? sparkRes.data : [];
                const queue = (queueRes && queueRes.success && queueRes.data) ? queueRes.data : null;
                container.innerHTML = this.renderHtml(ovRes.data, aiRes && aiRes.success ? aiRes.data : null, incidents, sparkData, queue);
            }
            if (mark) mark.textContent = t('common.lastRefreshed', { time: new Date().toLocaleTimeString() });
        } catch (err) {
            console.error('Failed to load overview:', err);
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('alert-triangle') + '</div><div class="empty-title">' + t('common.loadFailed') + '</div><div class="empty-text">' + escapeHtml(String(err && err.message || err)) + '</div></div>';
        }
    },

    emptyHtml() {
        return '<div class="empty-state"><div class="empty-icon">' + wwIcon('bar-chart') + '</div><div class="empty-title">' + t('overview.empty.title') + '</div><div class="empty-text">' + t('overview.empty.text') + '</div></div>';
    },

    renderHtml(d, ai, incidents, sparkData, queue) {
        const fmt = (typeof formatNumber === 'function') ? formatNumber : (n) => String(n);
        const delivery = d.delivery || {};
        const cost = ai ? (ai.cost && ai.cost.total) || 0 : null;
        const aiCalls = ai ? this._routeCount(ai, 'ai') : null;

        // Top stat cards.
        let html = '<div class="stats-grid" style="margin-bottom: 1.5rem;">';
        var prev = d.previous || {};
        var totalDelta = (prev.total_delta_pct != null) ? (prev.total_delta_pct > 0 ? '↑' : '↓') + Math.abs(prev.total_delta_pct).toFixed(1) + '%' : '';
        html += this._card(wwIcon('inbox'), t('overview.card.processed'), fmt(d.total) + (totalDelta ? ' <span style="font-size:0.7em;color:' + (prev.total_delta_pct > 0 ? 'var(--danger)' : 'var(--success)') + ';">' + totalDelta + '</span>' : ''), t('overview.card.processedTrend'), 'var(--primary)');
        html += this._card(wwIcon('check'), t('overview.card.forwardRate'), (d.forward_rate || 0).toFixed(1) + '%',
            t('overview.card.forwardRateTrend', { fwd: fmt(d.forwarded), skip: fmt(d.skipped) }), 'var(--success)',
            "OverviewModule.drillToOutcome('forwarded')");
        // The one stateful headline: a degraded delivery rate must not sit
        // there looking as calm as a healthy one.
        var rateStr = delivery.success_rate != null ? delivery.success_rate.toFixed(1) + '%' : '—';
        if (delivery.success_rate != null && delivery.failed > 0) {
            var rateColor = delivery.success_rate < 80 ? 'var(--danger)' : 'var(--warning)';
            rateStr = '<span style="color:' + rateColor + ';">' + rateStr + '</span>';
        }
        html += this._card(wwIcon('send'), t('overview.card.deliveryRate'),
            rateStr,
            t('overview.card.deliveryRateTrend', { ok: fmt(delivery.delivered || 0), fail: fmt(delivery.failed || 0) }),
            null,
            "OverviewModule.drillToOutcome('forwarded')");
        if (ai) {
            html += this._card(wwIcon('dollar'), t('overview.card.aiCost'), '$' + (Number(cost) || 0).toFixed(4),
                t('overview.card.aiCostTrend', { n: fmt(aiCalls || 0) }), 'var(--warning)',
                "navigateTo('cost')");
        }
        html += '</div>';

        // Queue health and skip reasons share one band: both are small, and
        // stacking them full-width left the bottom half of the screen empty.
        const queueCard = this._renderQueueHealth(queue);
        const skipCard = this._renderSkipCard(d.skip_code_breakdown || {}, fmt);
        if (queueCard && skipCard) {
            html += '<div class="ov-row">' + queueCard + skipCard + '</div>';
        } else if (queueCard || skipCard) {
            html += '<div style="margin-bottom: 1.5rem;">' + (queueCard || skipCard) + '</div>';
        }

        // Top alert rules (rule grain; unidentified senders fall back to source).
        const sources = d.top_rules || [];
        if (sources.length) {
            const max = Math.max(...sources.map((s) => s.count || 0), 1);
            html += '<div style="font-size: 1rem; font-weight: 600; margin: 1.5rem 0 0.75rem;">' + t('overview.section.topRules') + '</div>';
            html += '<div style="background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 1.25rem;">';
            sources.forEach((s) => {
                const pct = ((s.count || 0) / max) * 100;
                html += '<div style="margin-bottom: 0.75rem;">' +
                    '<div style="display:flex; justify-content:space-between; font-size:0.85rem; margin-bottom:0.25rem;">' +
                    '<span>' + wwIcon('tag') + ' ' + escapeHtml(s.name) + wwSourceSuffix(s.name, s.sources) + '</span><span style="color:var(--text-muted);">' + fmt(s.count) + '</span></div>' +
                    '<div style="height:4px; background:var(--bg-subtle, var(--bg-subtle)); border-radius:4px; overflow:hidden;">' +
                    '<div style="height:100%; width:' + pct + '%; background:var(--primary);"></div></div></div>';
            });
            html += '</div>';
        }

        // Recent active incidents — quick glance at what's happening right now.
        if (incidents && incidents.length) {
            html += '<div style="font-size: 1rem; font-weight: 600; margin: 1.5rem 0 0.75rem;">' + t('overview.section.incidents') + '</div>';
            html += '<div style="display:flex; flex-direction:column; gap:0.5rem;">';
            var impEmoji = { high: '<span class="ww-dot ww-dot-danger"></span>', medium: '<span class="ww-dot ww-dot-warning"></span>', low: '<span class="ww-dot ww-dot-success"></span>' };
            for (var i = 0; i < Math.min(incidents.length, 5); i++) {
                var inc = incidents[i];
                html += '<div class="incident-row" style="display:flex; align-items:center; gap:0.75rem; padding:0.6rem 0.75rem; background:var(--bg-surface); border:1px solid var(--border); border-radius:8px; cursor:pointer;" onclick="openIncident(' + Number(inc.id) + ')">';
                html += '<div style="flex:1; min-width:0;">';
                html += '<div style="font-weight:500; font-size:0.9rem;">' + escapeHtml(inc.title) + '</div>';
                html += '<div style="font-size:0.76rem; color:var(--text-muted);">' + escapeHtml(inc.source || '') + ' · ' + inc.alert_count + ' alerts · ' + (impEmoji[inc.top_importance] || '') + (inc.top_importance || '') + '</div>';
                html += '</div>';
                html += '<span style="color:var(--text-muted); font-size:0.7rem;">' + (inc.started_at ? inc.started_at.slice(0, 16).replace('T', ' ') : '') + '</span>';
                html += '</div>';
            }
            html += '</div>';
        }
        // Dependency-free 7-day sparkline trend.
        if (sparkData && sparkData.length > 1) {
            html += '<div style="font-size:1rem; font-weight:600; margin:1.5rem 0 0.5rem;">' + t('overview.section.trend') + '</div>';
            html += '<div id="overviewTrendBox" style="background:var(--bg-surface); border:1px solid var(--border); border-radius:8px; padding:1.25rem;">' + this._trendInner(sparkData) + '</div>';
        }
        return html;
    },

    // Native CSS bars keep the dashboard self-contained and CSP-friendly.
    // Bars carry their values (a chart the reader has to hover to read is a
    // riddle, not a chart); the in-progress last day gets the full accent,
    // completed days step back so "today" is the figure and history the ground.
    _trendInner(sparkData) {
        var maxVal = Math.max.apply(null, sparkData.map(function (d) { return d.count; })) || 1;
        var last = sparkData.length - 1;
        var bars = sparkData.map(function (d, i) {
            var h = Math.max(3, Math.round((d.count / maxVal) * 88));
            var isToday = i === last;
            var barBg = isToday ? 'var(--primary)' : 'color-mix(in srgb, var(--primary) 45%, var(--bg-subtle))';
            return '<div title="' + d.day + ': ' + d.count + '" style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; gap:4px; height:100%;">' +
                '<span style="font-size:0.68rem; color:' + (isToday ? 'var(--text-main)' : 'var(--text-muted)') + '; font-variant-numeric:tabular-nums;">' + (d.count || 0) + '</span>' +
                '<div style="width:100%; max-width:36px; height:' + h + 'px; background:' + barBg + '; border-radius:3px 3px 0 0;"></div>' +
                '<span style="font-size:0.68rem; color:var(--text-muted);">' + (d.day || '').slice(5) + '</span></div>';
        }).join('');
        return '<div style="display:flex; align-items:stretch; gap:8px; height:132px;">' + bars + '</div>';
    },

    // Skip reasons as a card so it can share a band with queue health.
    // Codes render through the trace view's presenter — the overview endpoint
    // reports them uppercase, and a raw NOISE_SUPPRESSED must never hit the UI.
    _renderSkipCard(skip, fmt) {
        const skipKeys = Object.keys(skip);
        if (!skipKeys.length) return '';
        const label = (k) => {
            const norm = String(k).toLowerCase();
            if (typeof DecisionTraceModule !== 'undefined' && DecisionTraceModule.skipCodeLabel) {
                return DecisionTraceModule.skipCodeLabel(norm);
            }
            return escapeHtml(norm);
        };
        let html = '<div style="background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 1.25rem;">';
        html += '<div style="font-weight: 600; margin-bottom: 0.75rem;">' + t('overview.section.skipReasons') + '</div>';
        html += '<div style="display: flex; flex-wrap: wrap; gap: 0.5rem;">';
        skipKeys.sort((a, b) => skip[b] - skip[a]).forEach((k) => {
            // Clickable: drill from the Overview summary into the Decision Trace
            // sub-view, pre-filtered to this skip reason.
            html += '<span class="badge badge-outline" role="button" tabindex="0" style="font-size: 0.8rem; cursor: pointer;"' +
                ' title="' + escapeHtml(t('overview.skipChip.drill')) + '"' +
                ' onclick="OverviewModule.drillToSkip(\'' + escapeHtml(k) + '\')">' +
                label(k) + ' <strong>' + fmt(skip[k]) + '</strong></span>';
        });
        html += '</div></div>';
        return html;
    },

    // Ingest-queue health. The alarm signal is backlog_fraction (undelivered lag
    // + un-acked pending vs maxlen), NOT the raw fill level (depth vs maxlen):
    // a healthy busy stream sits at depth==maxlen permanently (Redis trims
    // lazily, not on ack), so depth/maxlen is shown only as informational
    // "retention". Any field may be null when the probe failed → render "—".
    // When `backlogged` the tile tints critical and notes the trim-boundary risk.
    _renderQueueHealth(q) {
        if (!q) return '';
        const fmt = (typeof formatNumber === 'function') ? formatNumber : (n) => String(n);
        const dash = '—';
        const bf = q.backlog_fraction;
        const hasBf = bf !== null && bf !== undefined;
        const pct = hasBf ? Math.round(bf * 100) : null;
        const warn = q.warn_fraction;
        const high = q.high_water_fraction;
        // Zero backlog is quiet, not a celebration — and never an alarm: with a
        // threshold of 0 the old `bf >= high` fired on a perfectly empty queue,
        // painting a healthy 0% red.
        let color = 'var(--text-muted)';
        if (hasBf && bf > 0) {
            color = 'var(--success)';
            if (q.backlogged || (high != null && bf >= high)) color = 'var(--danger)';
            else if (warn != null && bf >= warn) color = 'var(--warning)';
        } else if (q.backlogged) {
            color = 'var(--danger)';
        }
        const barWidth = hasBf ? Math.min(100, Math.max(0, pct)) : 0;
        const backlog = q.backlog != null ? fmt(q.backlog) : dash;
        const depth = q.depth != null ? fmt(q.depth) : dash;
        const maxlen = q.maxlen != null ? fmt(q.maxlen) : dash;
        const pending = q.pending != null ? fmt(q.pending) : dash;
        const lag = q.lag != null ? fmt(q.lag) : dash;

        let html = '<div style="background: var(--bg-surface); border: 1px solid var(--border);' +
            (q.backlogged ? ' border-left: 3px solid var(--danger);' : '') +
            ' border-radius: var(--radius-lg); padding: 1.25rem;">';
        html += '<div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.5rem;">' +
            '<span style="font-weight: 600;">' + t('overview.queue.title') + '</span>' +
            '<span style="font-size: 1.25rem; font-weight: 700; color: ' + color + ';">' + (pct != null ? pct + '%' : dash) + '</span></div>';
        // Gauge = backlog_fraction (the at-risk-of-trim share).
        html += '<div style="height: 8px; background: var(--bg-subtle, var(--bg-subtle)); border-radius: 4px; overflow: hidden; margin-bottom: 0.6rem;">' +
            '<div style="height: 100%; width: ' + barWidth + '%; background: ' + color + ';"></div></div>';
        html += '<div style="display: flex; flex-wrap: wrap; gap: 1.25rem; font-size: 0.8rem; color: var(--text-muted);">';
        html += '<span>' + t('overview.queue.backlog') + ': <strong style="color: var(--text-main);">' + backlog + '</strong></span>';
        // depth / maxlen is informational retention, not the alarm signal.
        html += '<span>' + t('overview.queue.retention') + ': <strong style="color: var(--text-main);">' + depth + ' / ' + maxlen + '</strong></span>';
        html += '<span>' + t('overview.queue.pending') + ': <strong style="color: var(--text-main);">' + pending + '</strong></span>';
        html += '<span>' + t('overview.queue.lag') + ': <strong style="color: var(--text-main);">' + lag + '</strong></span>';
        if (q.stream) {
            html += '<span>' + t('overview.queue.stream') + ': <strong style="color: var(--text-main);">' + escapeHtml(q.stream) + '</strong></span>';
        }
        html += '</div>';
        if (q.backlogged) {
            html += '<div style="margin-top: 0.6rem; font-size: 0.8rem; color: var(--danger);">' + t('overview.queue.backlogged') + '</div>';
        }
        html += '</div>';
        return html;
    },

    // Same promise the hover lift was already making. The skip-reason chips
    // below these cards have drilled in for a while; the headline numbers,
    // which are what people look at first, did not.
    _card(icon, label, value, trend, color, onClick) {
        const clickable = onClick ? ' onclick="' + onClick + '" ' : '';
        return '<div class="stat-card"' + clickable + ' style="'
            + (onClick ? 'cursor:pointer;' : '') + '">' +
            '<div class="stat-label">' + icon + ' ' + label + '</div>' +
            '<div class="stat-value">' + value + '</div>' +
            '<div class="stat-trend">' + trend + '</div></div>';
    },

    async _fetchSparkline(days) {
        try {
            const resp = await API.authenticatedFetch('/v1/sparkline?days=' + days);
            if (!resp.ok) return null;
            return await resp.json();
        } catch (e) { return null; }
    },

    _routeCount(ai, key) {
        return (ai.route_breakdown && ai.route_breakdown[key]) || 0;
    },
};
