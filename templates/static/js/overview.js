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

    escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    },

    async load(period) {
        this.currentPeriod = period || this.currentPeriod || 'day';
        this.updatePeriodButtons(this.currentPeriod);
        const container = document.getElementById('overviewContent');
        if (!container) return;
        const mark = document.getElementById('ovLastRefreshed');
        try {
            // Overview + AI usage + recent incidents, in parallel.
            const [ovRes, aiRes, incRes] = await Promise.all([
                API.getOverview(this.currentPeriod),
                API.getAIUsage(this.currentPeriod).catch(() => null),
                API.getIncidents({ status: 'active', page_size: 5 }).catch(() => null),
            ]);
            if (!ovRes || !ovRes.success || !ovRes.data) {
                container.innerHTML = this.emptyHtml();
            } else {
                const incidents = (incRes && incRes.success && incRes.data) ? incRes.data : [];
                container.innerHTML = this.renderHtml(ovRes.data, aiRes && aiRes.success ? aiRes.data : null, incidents);
            }
            if (mark) mark.textContent = t('common.lastRefreshed', { time: new Date().toLocaleTimeString() });
        } catch (err) {
            console.error('Failed to load overview:', err);
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">' + t('common.loadFailed') + '</div><div class="empty-text">' + this.escapeHtml(String(err && err.message || err)) + '</div></div>';
        }
    },

    emptyHtml() {
        return '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-title">' + t('overview.empty.title') + '</div><div class="empty-text">' + t('overview.empty.text') + '</div></div>';
    },

    renderHtml(d, ai, incidents) {
        const fmt = (typeof formatNumber === 'function') ? formatNumber : (n) => String(n);
        const delivery = d.delivery || {};
        const cost = ai ? (ai.cost && ai.cost.total) || 0 : null;
        const aiCalls = ai ? this._routeCount(ai, 'ai') : null;

        // Top stat cards.
        let html = '<div class="stats-grid" style="margin-bottom: 1.5rem;">';
        html += this._card('📥', t('overview.card.processed'), fmt(d.total), t('overview.card.processedTrend'), 'var(--primary)');
        html += this._card('✅', t('overview.card.forwardRate'), (d.forward_rate || 0).toFixed(1) + '%',
            t('overview.card.forwardRateTrend', { fwd: fmt(d.forwarded), skip: fmt(d.skipped) }), 'var(--success)');
        html += this._card('📨', t('overview.card.deliveryRate'),
            (delivery.success_rate != null ? delivery.success_rate.toFixed(1) + '%' : '—'),
            t('overview.card.deliveryRateTrend', { ok: fmt(delivery.delivered || 0), fail: fmt(delivery.failed || 0) }),
            (delivery.failed > 0 ? 'var(--danger)' : 'var(--success)'));
        if (ai) {
            html += this._card('💰', t('overview.card.aiCost'), '$' + (Number(cost) || 0).toFixed(4),
                t('overview.card.aiCostTrend', { n: fmt(aiCalls || 0) }), 'var(--warning)');
        }
        html += '</div>';

        // Skip-reason distribution.
        const skip = d.skip_code_breakdown || {};
        const skipKeys = Object.keys(skip);
        if (skipKeys.length) {
            html += '<div style="font-size: 1rem; font-weight: 600; margin: 1.5rem 0 0.75rem;">' + t('overview.section.skipReasons') + '</div>';
            html += '<div style="display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 1rem;">';
            skipKeys.sort((a, b) => skip[b] - skip[a]).forEach((k) => {
                // Clickable: drill from the Overview summary into the Decision Trace
                // sub-view, pre-filtered to this skip reason.
                html += '<span class="badge badge-outline" role="button" tabindex="0" style="font-size: 0.8rem; cursor: pointer;"' +
                    ' title="' + this.escapeHtml(t('overview.skipChip.drill')) + '"' +
                    ' onclick="OverviewModule.drillToSkip(\'' + this.escapeHtml(k) + '\')">' +
                    this.escapeHtml(k) + ' <strong>' + fmt(skip[k]) + '</strong></span>';
            });
            html += '</div>';
        }

        // Top sources.
        const sources = d.top_sources || [];
        if (sources.length) {
            const max = Math.max(...sources.map((s) => s.count || 0), 1);
            html += '<div style="font-size: 1rem; font-weight: 600; margin: 1.5rem 0 0.75rem;">' + t('overview.section.topSources') + '</div>';
            html += '<div style="background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 1.25rem;">';
            sources.forEach((s) => {
                const pct = ((s.count || 0) / max) * 100;
                html += '<div style="margin-bottom: 0.75rem;">' +
                    '<div style="display:flex; justify-content:space-between; font-size:0.85rem; margin-bottom:0.25rem;">' +
                    '<span>📡 ' + this.escapeHtml(s.source) + '</span><span style="color:var(--text-muted);">' + fmt(s.count) + '</span></div>' +
                    '<div style="height:8px; background:var(--bg-subtle, #f1f5f9); border-radius:4px; overflow:hidden;">' +
                    '<div style="height:100%; width:' + pct + '%; background:var(--primary);"></div></div></div>';
            });
            html += '</div>';
        }

        // Recent active incidents — quick glance at what's happening right now.
        if (incidents && incidents.length) {
            html += '<div style="font-size: 1rem; font-weight: 600; margin: 1.5rem 0 0.75rem;">🚨 ' + t('overview.section.incidents') + '</div>';
            html += '<div style="display:flex; flex-direction:column; gap:0.5rem;">';
            var impEmoji = { high: '🔴', medium: '🟠', low: '🟢' };
            for (var i = 0; i < Math.min(incidents.length, 5); i++) {
                var inc = incidents[i];
                html += '<div class="incident-row" style="display:flex; align-items:center; gap:0.75rem; padding:0.6rem 0.75rem; background:var(--bg-surface); border:1px solid var(--border); border-radius:8px; cursor:pointer;" onclick="document.querySelector(\'[data-tab=incidents]\').click()">';
                html += '<span style="font-size:1.2rem;">🔥</span>';
                html += '<div style="flex:1; min-width:0;">';
                html += '<div style="font-weight:500; font-size:0.9rem;">' + this.escapeHtml(inc.title) + '</div>';
                html += '<div style="font-size:0.76rem; color:var(--text-muted);">' + this.escapeHtml(inc.source || '') + ' · ' + inc.alert_count + ' alerts · ' + (impEmoji[inc.top_importance] || '') + (inc.top_importance || '') + '</div>';
                html += '</div>';
                html += '<span style="color:var(--text-muted); font-size:0.7rem;">' + (inc.started_at ? inc.started_at.slice(0, 16).replace('T', ' ') : '') + '</span>';
                html += '</div>';
            }
            html += '</div>';
        }
        return html;
    },

    _card(icon, label, value, trend, color) {
        return '<div class="stat-card" style="border-left: 4px solid ' + color + ';">' +
            '<div class="stat-label">' + icon + ' ' + label + '</div>' +
            '<div class="stat-value" style="font-size: 2rem; color: ' + color + ';">' + value + '</div>' +
            '<div class="stat-trend">' + trend + '</div></div>';
    },

    _routeCount(ai, key) {
        return (ai.route_breakdown && ai.route_breakdown[key]) || 0;
    },
};
