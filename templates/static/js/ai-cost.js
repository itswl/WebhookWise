/**
 * AI Cost monitoring module
 * Displays usage statistics and cost information for AI analysis
 */

const AICostModule = {
    currentPeriod: 'day',

    /**
     * Initialize the module
     */
    init() {
        this.loadStats('day');
        this.bindEvents();
    },

    /**
     * Bind events
     */
    bindEvents() {
        // Period toggle buttons
        const periodButtons = document.querySelectorAll('[data-ai-period]');
        periodButtons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                const button = e.target.closest('[data-ai-period]');
                const period = button ? button.getAttribute('data-ai-period') : null;
                if (period) {
                    this.loadStats(period);
                }
            });
        });
    },

    updatePeriodButtons(period) {
        document.querySelectorAll('[data-ai-period]').forEach(btn => {
            btn.classList.toggle('active', btn.getAttribute('data-ai-period') === period);
        });
    },

    /**
     * Load statistics data
     * @param {string} period - Statistics period (day/week/month)
     */
    async loadStats(period = 'day') {
        this.currentPeriod = period;
        this.updatePeriodButtons(period);

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

        const routeAi = this.routeCount(data, ['ai']);
        const routeRule = this.routeCount(data, ['rule']);
        const routeCache = this.routeCount(data, ['cache']);
        const routeReuse = this.routeCount(data, ['reuse', 'redis_reuse', 'db_reuse', 'rechain']);

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
                <div class="stat-card" style="border-left: 4px solid var(--primary);">
                    <div class="stat-label">${t('aicost.card.totalSpent')}</div>
                    <div class="stat-value" style="color: var(--primary); font-size: 2.5rem;">${this.formatCurrency(costTotal)}</div>
                    <div class="stat-trend" style="display: flex; justify-content: space-between;">
                        <span>${t('aicost.card.tokensLabel', { n: formatNumber(tokensTotal) })}</span>
                        <span>${t('aicost.card.apiCallLabel', { n: formatNumber(totalCalls) })}</span>
                    </div>
                </div>
                <div class="stat-card" style="border-left: 4px solid var(--success); background: #f0fdf4;">
                    <div class="stat-label" style="color: #059669;">${t('aicost.card.totalSaved')}</div>
                    <div class="stat-value" style="color: var(--success); font-size: 2.5rem;">${this.formatCurrency(costSaved)}</div>
                    <div class="stat-trend" style="color: #059669;">${t('aicost.card.totalSavedTrend')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">${t('aicost.card.inputThroughput')}</div>
                    <div class="stat-value" style="font-size: 2rem;">${formatNumber(tokensInput)}</div>
                    <div class="stat-trend">${t('aicost.card.tokensSent')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">${t('aicost.card.outputGenerated')}</div>
                    <div class="stat-value" style="font-size: 2rem;">${formatNumber(tokensOutput)}</div>
                    <div class="stat-trend">${t('aicost.card.tokensReceived')}</div>
                </div>
            </div>

            <!-- Analysis route distribution -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">${t('aicost.section.routeFunnel')}</div>
            <div style="background: var(--bg-surface); padding: 1.5rem; border-radius: var(--radius-lg); border: 1px solid var(--border); box-shadow: var(--shadow-sm); margin-bottom: 2.5rem;">

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--primary);">🤖 ${t('aicost.route.ai')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeAi), pct: this.formatPercent(percentAi) })}</span>
                    </div>
                    <div style="height: 8px; background: #e0e7ff; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--primary); width: ${percentAi}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--success);">💾 ${t('aicost.route.cache')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeCache), pct: this.formatPercent(percentCache) })}</span>
                    </div>
                    <div style="height: 8px; background: #d1fae5; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--success); width: ${percentCache}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--warning);">🔄 ${t('aicost.route.reuse')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeReuse), pct: this.formatPercent(percentReuse) })}</span>
                    </div>
                    <div style="height: 8px; background: #fef3c7; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--warning); width: ${percentReuse}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div>
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--text-muted);">📋 ${t('aicost.route.rule')}</span>
                        <span style="color: var(--text-muted);">${t('aicost.route.calls', { n: formatNumber(routeRule), pct: this.formatPercent(percentRule) })}</span>
                    </div>
                    <div style="height: 8px; background: #f1f5f9; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: #94a3b8; width: ${percentRule}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

            </div>

            <!-- Cache efficiency area -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">${t('aicost.section.efficiencyRadar')}</div>
            <div class="stats-grid">
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">${t('aicost.card.activeFingerprints')}</div>
                    <div class="stat-value" style="font-size: 1.75rem;">${formatNumber(cacheEntries)}</div>
                    <div class="stat-trend">${t('aicost.card.activeRedisKeys')}</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">${t('aicost.card.antiPenetration')}</div>
                    <div class="stat-value" style="font-size: 1.75rem;">${formatNumber(cacheSavedCalls)}</div>
                    <div class="stat-trend">${t('aicost.card.antiPenetrationTrend')}</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">${t('aicost.card.avgUtilization')}</div>
                    <div class="stat-value" style="font-size: 1.75rem;">${cacheAvgHits.toFixed(1)} <span style="font-size:1rem; color:var(--text-muted); font-weight:500;">x</span></div>
                    <div class="stat-trend">${t('aicost.card.avgUtilizationTrend')}</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem; border-left: 3px solid var(--success);">
                    <div class="stat-label">${t('aicost.card.hitRate')}</div>
                    <div class="stat-value" style="font-size: 1.75rem; color: var(--success);">${this.formatPercent(cacheHitRate)}</div>
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
            const pct = Math.max(2, (cost / maxCost) * 100);
            const title = `${p.time} · ${this.formatCurrency(cost)} · ${t('aicost.trend.callsTip', { n: formatNumber(p.total_calls || 0), ai: formatNumber(p.ai_calls || 0) })}`;
            const label = String(p.time).slice(5); // MM-DD
            return `<div class="aicost-trend-col" title="${title}">
                        <div class="aicost-trend-bar-wrap"><div class="aicost-trend-bar" style="height: ${pct}%;"></div></div>
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

        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-title">' + t('aicost.empty.title') + '</div><div class="empty-text">' + t('aicost.empty.text') + '</div></div>';
    }
};
