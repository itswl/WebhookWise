/**
 * AI Cost monitoring module
 * Displays usage statistics and cost information for AI analysis
 */

const AICostModule = {
    currentPeriod: 'day',

    /**
     * Initialize the module
     */

    /**
     * Bind events
     */


    /**
     * Load statistics data
     * @param {string} period - Statistics period (day/week/month)
     */
    async loadStats(period = 'day') {
        this.currentPeriod = period;
        // Period-button highlighting belongs to DecisionTraceModule, which owns
        // the shared Day/Week/Month toggle for every sub-view of this tab.

        try {
            const result = await API.getAIUsage(period);

            if (result.success && result.data) {
                this.renderStats(result.data);
            } else {
                console.warn('AI statistics data is empty or failed to load');
                this.renderEmptyStats();
            }
        } catch (error) {
            console.error('Failed to load AI statistics:', error);
            this.renderEmptyStats();
        }
    },

    /**
     * Safely get a nested property value
     * @param {object} obj - The object
     * @param {string} path - Property path (e.g. 'tokens.total')
     * @param {*} defaultValue - Default value
     * @returns {*} The property value or the default value
     */
    safeGet(obj, path, defaultValue = 0) {
        if (!obj) return defaultValue;
        const keys = path.split('.');
        let result = obj;
        for (const key of keys) {
            if (result === null || result === undefined) return defaultValue;
            result = result[key];
        }
        return result !== null && result !== undefined ? result : defaultValue;
    },

    /**
     * Format a USD amount
     * @param {number} amount - The amount
     * @returns {string} The formatted amount
     */
    /**
     * The basis line under the spend figure: which model, at what rate.
     *
     * Unreconciled rates get a warning dot rather than a hidden asterisk. The
     * point is that an operator reading "$16.50" can see in the same glance
     * whether that number was ever checked against a price list — and fix it in
     * two settings if not.
     */
    renderCostBasis(basis) {
        const rateIn = Number(basis.input_per_1k_usd || 0).toFixed(4);
        const rateOut = Number(basis.output_per_1k_usd || 0).toFixed(4);
        const reconciled = !!basis.reconciled_with_provider;
        const dot = reconciled ? 'ww-dot-success' : 'ww-dot-warning';
        const label = reconciled ? t('aicost.basis.reconciled') : t('aicost.basis.defaults');
        return `<div class="ww-meta" style="margin-top: var(--sp-2); font-size: var(--fs-xs);">
            <span class="ww-dot ${dot}"></span>
            <span>${escapeHtml(String(basis.model || '-'))} · ${t('aicost.basis.rates', { rateIn: rateIn, rateOut: rateOut })}</span>
            <span style="color: var(--text-muted);">${label}</span>
        </div>`;
    },

    formatCurrency(amount) {
        const value = parseFloat(amount) || 0;
        return '$' + value.toFixed(4);
    },

    /**
     * Format a percentage
     * @param {number} value - The percentage value
     * @returns {string} The formatted percentage
     */
    formatPercent(value) {
        const num = parseFloat(value) || 0;
        return num.toFixed(1) + '%';
    },

    routeCount(data, keys) {
        return keys.reduce((total, key) => total + Number(this.safeGet(data, `route_breakdown.${key}`, 0) || 0), 0);
    },

    routePercent(count, totalCalls) {
        return totalCalls > 0 ? (count / totalCalls) * 100 : 0;
    },

    /**
     * Render statistics data
     * @param {object} data - Statistics data (result.data returned by the API)
     */
    renderStats(data) {
        const container = document.getElementById('aiCostStats');
        if (!container) return;

        const totalCalls = this.safeGet(data, 'total_calls', 0);
        const tokensTotal = this.safeGet(data, 'tokens.total', 0);
        const tokensInput = this.safeGet(data, 'tokens.input', 0);
        const tokensOutput = this.safeGet(data, 'tokens.output', 0);
        const costTotal = this.safeGet(data, 'cost.total', 0);
        const costSaved = this.safeGet(data, 'cost.saved_estimate', 0);
        // Every currency figure here is tokens x a configured rate, never an
        // invoice. Say so next to the number: the shipped rates are Claude-era
        // and the model is configurable, so the total can be confidently wrong
        // with nothing on screen admitting it.
        const basis = this.safeGet(data, 'cost.basis', null);

        const routeAi = this.routeCount(data, ['ai']);
        const routeRule = this.routeCount(data, ['rule']);
        const routeCache = this.routeCount(data, ['cache']);
        // The no-LLM route list lives in ONE place (services/webhooks/types.py,
        // NO_LLM_REUSE_ROUTE_TYPES) and the backend already sums it into
        // cache_statistics. Re-listing the routes here would mean a future
        // reuse route silently missing from the funnel while the headline hit
        // rate counted it.
        const routeReuse = this.safeGet(data, 'cache_statistics.saved_calls', 0) - this.routeCount(data, ['cache']);

        const percentAi = this.routePercent(routeAi, totalCalls);
        const percentRule = this.routePercent(routeRule, totalCalls);
        const percentCache = this.routePercent(routeCache, totalCalls);
        const percentReuse = this.routePercent(routeReuse, totalCalls);

        const cacheStats = data.cache_statistics || {};
        const cacheEntries = this.safeGet(cacheStats, 'total_cache_entries', 0);
        const cacheTotalHits = this.safeGet(cacheStats, 'total_hits', 0);
        const cacheAvgHits = this.safeGet(cacheStats, 'avg_hits_per_entry', 0);
        const cacheHitRate = this.safeGet(cacheStats, 'cache_hit_rate', 0);
        const cacheSavedCalls = this.safeGet(cacheStats, 'saved_calls', 0);

        let html = `
            <!-- Core data dashboard -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">${t('aicost.section.coreBilling')}</div>
            <div class="stats-grid" style="margin-bottom: 2.5rem;">
                <div class="stat-card">
                    <div class="stat-label">${t('aicost.card.totalSpent')}</div>
                    <div class="stat-value" style="font-size: 2.5rem;">${this.formatCurrency(costTotal)}</div>
                    <div class="stat-trend" style="display: flex; justify-content: space-between;">
                        <span>${t('aicost.card.tokensLabel', { n: formatNumber(tokensTotal) })}</span>
                        <span>${t('aicost.card.apiCallLabel', { n: formatNumber(totalCalls) })}</span>
                    </div>
                    ${basis ? this.renderCostBasis(basis) : ''}
                </div>
                <div class="stat-card">
                    <div class="stat-label" style="color: var(--success);">${t('aicost.card.totalSaved')}</div>
                    <div class="stat-value" style="font-size: 2.5rem;">${this.formatCurrency(costSaved)}</div>
                    <div class="stat-trend" style="color: var(--success);">${t('aicost.card.totalSavedTrend')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">${t('aicost.card.inputThroughput')}</div>
                    <div class="stat-value">${formatNumber(tokensInput)}</div>
                    <div class="stat-trend">${t('aicost.card.tokensSent')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">${t('aicost.card.outputGenerated')}</div>
                    <div class="stat-value">${formatNumber(tokensOutput)}</div>
                    <div class="stat-trend">${t('aicost.card.tokensReceived')}</div>
                </div>
            </div>

            <!-- Analysis route distribution -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">${t('aicost.section.routeFunnel')}</div>
            <div style="background: var(--bg-surface); padding: 1.5rem; border-radius: var(--radius-lg); border: 1px solid var(--border); box-shadow: var(--shadow-sm); margin-bottom: 2.5rem;">

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--primary);">${wwIcon('sparkles')} ${t('aicost.route.ai')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeAi), pct: this.formatPercent(percentAi) })}</span>
                    </div>
                    <div style="height: 4px; background: var(--bg-subtle); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--primary); width: ${percentAi}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--success);">${wwIcon('database')} ${t('aicost.route.cache')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeCache), pct: this.formatPercent(percentCache) })}</span>
                    </div>
                    <div style="height: 4px; background: var(--bg-subtle); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--primary); width: ${percentCache}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--warning);">${wwIcon('refresh')} ${t('aicost.route.reuse')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeReuse), pct: this.formatPercent(percentReuse) })}</span>
                    </div>
                    <div style="height: 4px; background: var(--bg-subtle); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--primary); width: ${percentReuse}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div>
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--text-muted);">${wwIcon('list')} ${t('aicost.route.rule')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeRule), pct: this.formatPercent(percentRule) })}</span>
                    </div>
                    <div style="height: 4px; background: var(--bg-subtle); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--text-muted); width: ${percentRule}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

            </div>

            <!-- Cache efficiency area -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">${t('aicost.section.efficiencyRadar')}</div>
            <div class="stats-grid">
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">${t('aicost.card.activeFingerprints')}</div>
                    <div class="stat-value">${formatNumber(cacheEntries)}</div>
                    <div class="stat-trend">${t('aicost.card.activeRedisKeys')}</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">${t('aicost.card.antiPenetration')}</div>
                    <div class="stat-value">${formatNumber(cacheSavedCalls)}</div>
                    <div class="stat-trend">${t('aicost.card.antiPenetrationTrend')}</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">${t('aicost.card.avgUtilization')}</div>
                    <div class="stat-value">${cacheAvgHits.toFixed(1)} <span style="font-size:1rem; color:var(--text-muted); font-weight:500;">x</span></div>
                    <div class="stat-trend">${t('aicost.card.avgUtilizationTrend')}</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem; border-left: 3px solid var(--success);">
                    <div class="stat-label">${t('aicost.card.hitRate')}</div>
                    <div class="stat-value" style="color: var(--success);">${this.formatPercent(cacheHitRate)}</div>
                    <div class="stat-trend">${t('aicost.card.hitRateTrend')}</div>
                </div>
            </div>
            ${this.renderTrend(data.trend)}
        `;

        container.innerHTML = html;
    },

    /**
     * Render a per-day cost/calls trend as a lightweight CSS bar chart.
     * @param {Array} trend - [{time, total_calls, ai_calls, cost, tokens}]
     */
    renderTrend(trend) {
        if (!Array.isArray(trend) || trend.length === 0) return '';
        const maxCost = Math.max(...trend.map(p => Number(p.cost) || 0), 0.000001);
        const bars = trend.map(p => {
            const cost = Number(p.cost) || 0;
            // Top out at 82% of the band: the value label sits above the bar.
            const pct = Math.max(2, (cost / maxCost) * 82);
            const title = `${p.time} · ${this.formatCurrency(cost)} · ${t('aicost.trend.callsTip', { n: formatNumber(p.total_calls || 0), ai: formatNumber(p.ai_calls || 0) })}`;
            const label = String(p.time).slice(5); // MM-DD
            return `<div class="aicost-trend-col" title="${title}">
                        <div class="aicost-trend-bar-wrap" style="flex-direction: column; justify-content: flex-end; align-items: center;">
                            <div class="aicost-trend-val">${this.formatCurrency(cost)}</div>
                            <div class="aicost-trend-bar" style="height: ${pct}%;"></div>
                        </div>
                        <div class="aicost-trend-x">${label}</div>
                    </div>`;
        }).join('');
        return `
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin: 2.5rem 0 1.25rem;">${t('aicost.section.trend')}</div>
            <div style="background: var(--bg-surface); padding: 1.5rem; border-radius: var(--radius-lg); border: 1px solid var(--border); box-shadow: var(--shadow-sm);">
                <div class="aicost-trend-chart">${bars}</div>
                <div style="margin-top: 0.75rem; color: var(--text-muted); font-size: 0.8rem;">${t('aicost.trend.note')}</div>
            </div>`;
    },

    /**
     * Render the empty state
     */
    renderEmptyStats() {
        const container = document.getElementById('aiCostStats');
        if (!container) return;

        container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('bar-chart') + '</div><div class="empty-title">' + t('aicost.empty.title') + '</div><div class="empty-text">' + t('aicost.empty.text') + '</div></div>';
    }
};
