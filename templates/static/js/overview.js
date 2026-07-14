/**
 * Overview home page — one-screen operational summary.
 *
 * Composes the overview endpoint (volume / forward rate / skip distribution /
 * delivery success / top sources) with AI usage (cost + calls) for the same
 * window. Read-only; reuses existing endpoints. Default landing tab.
 */
const OverviewModule = {
    currentPeriod: 'day',
    _chartLibPromise: null,

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

    async load(period) {
        this.currentPeriod = period || this.currentPeriod || 'day';
        this.updatePeriodButtons(this.currentPeriod);
        const container = document.getElementById('overviewContent');
        if (!container) return;
        const mark = document.getElementById('ovLastRefreshed');
        // Start loading Chart.js in parallel with the data fetch so its download
        // overlaps the API round-trips instead of blocking first paint.
        const chartLibReady = this._ensureChartLib();
        try {
            // Overview + AI usage + recent incidents + sparkline, in parallel.
            const [ovRes, aiRes, incRes, sparkRes] = await Promise.all([
                API.getOverview(this.currentPeriod),
                API.getAIUsage(this.currentPeriod).catch(() => null),
                API.getIncidents({ status: 'active', page_size: 5 }).catch(() => null),
                this._fetchSparkline(7).catch(() => null),
            ]);
            if (!ovRes || !ovRes.success || !ovRes.data) {
                container.innerHTML = this.emptyHtml();
            } else {
                const incidents = (incRes && incRes.success && incRes.data) ? incRes.data : [];
                var sparkData = (sparkRes && sparkRes.success && sparkRes.data) ? sparkRes.data : [];
                container.innerHTML = this.renderHtml(ovRes.data, aiRes && aiRes.success ? aiRes.data : null, incidents, sparkData);
                this.initOverviewChart(sparkData);
                // If Chart.js was not ready at render time, upgrade the fallback
                // bars to the line chart in place once the library loads.
                if (typeof Chart === 'undefined' && sparkData && sparkData.length > 1) {
                    chartLibReady.then(() => {
                        const box = document.getElementById('overviewTrendBox');
                        if (box) {
                            box.innerHTML = this._trendInner(sparkData);
                            this.initOverviewChart(sparkData);
                        }
                    }).catch(() => { /* fallback bars remain */ });
                }
            }
            if (mark) mark.textContent = t('common.lastRefreshed', { time: new Date().toLocaleTimeString() });
        } catch (err) {
            console.error('Failed to load overview:', err);
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">' + t('common.loadFailed') + '</div><div class="empty-text">' + escapeHtml(String(err && err.message || err)) + '</div></div>';
        }
    },

    emptyHtml() {
        return '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-title">' + t('overview.empty.title') + '</div><div class="empty-text">' + t('overview.empty.text') + '</div></div>';
    },

    renderHtml(d, ai, incidents, sparkData) {
        const fmt = (typeof formatNumber === 'function') ? formatNumber : (n) => String(n);
        const delivery = d.delivery || {};
        const cost = ai ? (ai.cost && ai.cost.total) || 0 : null;
        const aiCalls = ai ? this._routeCount(ai, 'ai') : null;

        // Top stat cards.
        let html = '<div class="stats-grid" style="margin-bottom: 1.5rem;">';
        var prev = d.previous || {};
        var totalDelta = (prev.total_delta_pct != null) ? (prev.total_delta_pct > 0 ? '↑' : '↓') + Math.abs(prev.total_delta_pct).toFixed(1) + '%' : '';
        html += this._card('📥', t('overview.card.processed'), fmt(d.total) + (totalDelta ? ' <span style="font-size:0.7em;color:' + (prev.total_delta_pct > 0 ? 'var(--danger)' : 'var(--success)') + ';">' + totalDelta + '</span>' : ''), t('overview.card.processedTrend'), 'var(--primary)');
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
                    ' title="' + escapeHtml(t('overview.skipChip.drill')) + '"' +
                    ' onclick="OverviewModule.drillToSkip(\'' + escapeHtml(k) + '\')">' +
                    escapeHtml(k) + ' <strong>' + fmt(skip[k]) + '</strong></span>';
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
                    '<span>📡 ' + escapeHtml(s.source) + '</span><span style="color:var(--text-muted);">' + fmt(s.count) + '</span></div>' +
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
                html += '<div class="incident-row" style="display:flex; align-items:center; gap:0.75rem; padding:0.6rem 0.75rem; background:var(--bg-surface); border:1px solid var(--border); border-radius:8px; cursor:pointer;" onclick="openInboxIncidents()">';
                html += '<span style="font-size:1.2rem;">🔥</span>';
                html += '<div style="flex:1; min-width:0;">';
                html += '<div style="font-weight:500; font-size:0.9rem;">' + escapeHtml(inc.title) + '</div>';
                html += '<div style="font-size:0.76rem; color:var(--text-muted);">' + escapeHtml(inc.source || '') + ' · ' + inc.alert_count + ' alerts · ' + (impEmoji[inc.top_importance] || '') + (inc.top_importance || '') + '</div>';
                html += '</div>';
                html += '<span style="color:var(--text-muted); font-size:0.7rem;">' + (inc.started_at ? inc.started_at.slice(0, 16).replace('T', ' ') : '') + '</span>';
                html += '</div>';
            }
            html += '</div>';
        }
        // 7-day sparkline trend. Chart.js is loaded lazily (see load()); until it
        // arrives the box shows a lightweight CSS-bar fallback that is upgraded to
        // the line chart in place once the library is ready.
        if (sparkData && sparkData.length > 1) {
            html += '<div style="font-size:1rem; font-weight:600; margin:1.5rem 0 0.5rem;">📈 ' + t('overview.section.trend') + '</div>';
            html += '<div id="overviewTrendBox" style="background:var(--bg-surface); border:1px solid var(--border); border-radius:8px; padding:1.25rem;">' + this._trendInner(sparkData) + '</div>';
        }
        return html;
    },

    // Trend markup: the Chart.js canvas when the library is available, otherwise
    // a CSS-bar fallback. Kept as one helper so load() can re-render it in place
    // after Chart.js finishes loading.
    _trendInner(sparkData) {
        if (typeof Chart !== 'undefined') {
            return '<div style="height: 160px; position: relative;"><canvas id="overviewTrendChart"></canvas></div>';
        }
        var maxVal = Math.max.apply(null, sparkData.map(function (d) { return d.count; })) || 1;
        var bars = sparkData.map(function (d) {
            var h = Math.max(2, Math.round((d.count / maxVal) * 40));
            return '<div title="' + d.day + ': ' + d.count + '" style="flex:1; display:flex; flex-direction:column; align-items:center; gap:2px;">' +
                '<div style="width:100%; max-width:24px; height:' + h + 'px; background:var(--primary); border-radius:2px 2px 0 0; min-height:2px;"></div>' +
                '<span style="font-size:0.55rem; color:var(--text-muted);">' + (d.day || '').slice(5) + '</span></div>';
        }).join('');
        return '<div style="display:flex; align-items:flex-end; gap:2px; height:50px;">' + bars + '</div>';
    },

    _card(icon, label, value, trend, color) {
        return '<div class="stat-card" style="border-left: 4px solid ' + color + ';">' +
            '<div class="stat-label">' + icon + ' ' + label + '</div>' +
            '<div class="stat-value" style="font-size: 2rem; color: ' + color + ';">' + value + '</div>' +
            '<div class="stat-trend">' + trend + '</div></div>';
    },

    // Lazy-load the Chart.js UMD build the first time the trend chart is drawn.
    // Loading it on demand (rather than a render-blocking <script> in <head>)
    // keeps this heavy third-party dependency off the critical path. CSP
    // script-src-elem allows cdn.jsdelivr.net.
    _ensureChartLib() {
        if (typeof Chart !== 'undefined') return Promise.resolve();
        if (this._chartLibPromise) return this._chartLibPromise;
        this._chartLibPromise = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js';
            script.onload = () => resolve();
            script.onerror = () => {
                // Don't memoize the failure — clear the cached promise (and drop
                // the dead <script>) so a later render can re-attempt the load.
                this._chartLibPromise = null;
                script.remove();
                reject(new Error('Chart.js failed to load'));
            };
            document.head.appendChild(script);
        });
        return this._chartLibPromise;
    },

    initOverviewChart(sparkData) {
        const ctx = document.getElementById('overviewTrendChart');
        if (!ctx || typeof Chart === 'undefined') return;

        if (window.ovTrendChartInstance) {
            window.ovTrendChartInstance.destroy();
        }

        const labels = sparkData.map(d => (d.day || '').slice(5));
        const data = sparkData.map(d => d.count);

        const isDark = document.documentElement.classList.contains('theme-dark');
        const primaryColor = isDark ? '#818cf8' : '#6366f1';
        const gridColor = isDark ? '#1e293b' : '#eef1f6';
        const textColor = isDark ? '#94a3b8' : '#475569';

        window.ovTrendChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: t('overview.section.trend') || 'Throughput',
                    data: data,
                    borderColor: primaryColor,
                    backgroundColor: isDark ? 'rgba(129, 140, 248, 0.15)' : 'rgba(99, 102, 241, 0.08)',
                    borderWidth: 2.5,
                    fill: true,
                    tension: 0.35,
                    pointBackgroundColor: primaryColor,
                    pointRadius: 4,
                    pointHoverRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: textColor, font: { size: 10 } }
                    },
                    y: {
                        grid: { color: gridColor },
                        ticks: { color: textColor, font: { size: 10 }, stepSize: Math.max(1, Math.round(Math.max(...data) / 4)) }
                    }
                }
            }
        });
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
