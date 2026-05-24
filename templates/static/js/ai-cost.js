/**
 * AI 成本监控模块
 * 显示 AI 分析的使用统计和成本信息
 */

const AICostModule = {
    currentPeriod: 'day',

    /**
     * 初始化模块
     */
    init() {
        this.loadStats('day');
        this.bindEvents();
    },

    /**
     * 绑定事件
     */
    bindEvents() {
        // 周期切换按钮
        const periodButtons = document.querySelectorAll('[data-ai-period]');
        periodButtons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                const period = e.target.getAttribute('data-ai-period');
                this.loadStats(period);
            });
        });
    },

    /**
     * 加载统计数据
     * @param {string} period - 统计周期（day/week/month）
     */
    async loadStats(period = 'day') {
        this.currentPeriod = period;

        try {
            const result = await API.getAIUsage(period);

            if (result.success && result.data) {
                this.renderStats(result.data);
            } else {
                console.warn('AI 统计数据为空或加载失败');
                this.renderEmptyStats();
            }
        } catch (error) {
            console.error('加载 AI 统计失败:', error);
            this.renderEmptyStats();
        }
    },

    /**
     * 安全获取嵌套属性值
     * @param {object} obj - 对象
     * @param {string} path - 属性路径（如 'tokens.total'）
     * @param {*} defaultValue - 默认值
     * @returns {*} 属性值或默认值
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
     * 格式化美元金额
     * @param {number} amount - 金额
     * @returns {string} 格式化后的金额
     */
    formatCurrency(amount) {
        const value = parseFloat(amount) || 0;
        return '$' + value.toFixed(4);
    },

    /**
     * 格式化百分比
     * @param {number} value - 百分比值
     * @returns {string} 格式化后的百分比
     */
    formatPercent(value) {
        const num = parseFloat(value) || 0;
        return num.toFixed(1) + '%';
    },

    /**
     * 渲染统计数据
     * @param {object} data - 统计数据（API 返回的 result.data）
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

        const percentAi = this.safeGet(data, 'percentages.ai', 0);
        const percentRule = this.safeGet(data, 'percentages.rule', 0);
        const percentCache = this.safeGet(data, 'percentages.cache', 0);
        const percentReuse = this.safeGet(data, 'percentages.reuse', 0);

        const routeAi = this.safeGet(data, 'route_breakdown.ai', 0);
        const routeRule = this.safeGet(data, 'route_breakdown.rule', 0);
        const routeCache = this.safeGet(data, 'route_breakdown.cache', 0);
        const routeReuse = this.safeGet(data, 'route_breakdown.reuse', 0);

        const cacheStats = data.cache_statistics || {};
        const cacheEntries = this.safeGet(cacheStats, 'total_cache_entries', 0);
        const cacheTotalHits = this.safeGet(cacheStats, 'total_hits', 0);
        const cacheAvgHits = this.safeGet(cacheStats, 'avg_hits_per_entry', 0);
        const cacheHitRate = this.safeGet(cacheStats, 'cache_hit_rate', 0);
        const cacheSavedCalls = this.safeGet(cacheStats, 'saved_calls', 0);

        let html = `
            <!-- 核心数据看板 -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">核心账单 (USD)</div>
            <div class="stats-grid" style="margin-bottom: 2.5rem;">
                <div class="stat-card" style="border-left: 4px solid var(--primary);">
                    <div class="stat-label">总消耗预算 (Estimated)</div>
                    <div class="stat-value" style="color: var(--primary); font-size: 2.5rem;">${this.formatCurrency(costTotal)}</div>
                    <div class="stat-trend" style="display: flex; justify-content: space-between;">
                        <span>Tokens: ${formatNumber(tokensTotal)}</span>
                        <span>API Call: ${formatNumber(totalCalls)}</span>
                    </div>
                </div>
                <div class="stat-card" style="border-left: 4px solid var(--success); background: #f0fdf4;">
                    <div class="stat-label" style="color: #059669;">累计节省 (Saved)</div>
                    <div class="stat-value" style="color: var(--success); font-size: 2.5rem;">${this.formatCurrency(costSaved)}</div>
                    <div class="stat-trend" style="color: #059669;">通过降噪策略与缓存重用</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">输入吞吐量 (Prompt)</div>
                    <div class="stat-value" style="font-size: 2rem;">${formatNumber(tokensInput)}</div>
                    <div class="stat-trend">Tokens 发送</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">输出生成量 (Completion)</div>
                    <div class="stat-value" style="font-size: 2rem;">${formatNumber(tokensOutput)}</div>
                    <div class="stat-trend">Tokens 接收</div>
                </div>
            </div>

            <!-- 分析路由分布 -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">处理路由漏斗 (Traffic Routing)</div>
            <div style="background: var(--bg-surface); padding: 1.5rem; border-radius: var(--radius-lg); border: 1px solid var(--border); box-shadow: var(--shadow-sm); margin-bottom: 2.5rem;">

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--primary);">🤖 原生大模型分析 (AI Engine)</span>
                        <span style="color: var(--text-muted);">${formatNumber(routeAi)} 次 (${this.formatPercent(percentAi)})</span>
                    </div>
                    <div style="height: 8px; background: #e0e7ff; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--primary); width: ${percentAi}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--success);">💾 语义缓存拦截 (Cache Hit)</span>
                        <span style="color: var(--text-muted);">${formatNumber(routeCache)} 次 (${this.formatPercent(percentCache)})</span>
                    </div>
                    <div style="height: 8px; background: #d1fae5; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--success); width: ${percentCache}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div style="margin-bottom: 1.5rem;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--warning);">🔄 衍生降噪合并 (Deduplication)</span>
                        <span style="color: var(--text-muted);">${formatNumber(routeReuse)} 次 (${this.formatPercent(percentReuse)})</span>
                    </div>
                    <div style="height: 8px; background: #fef3c7; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: var(--warning); width: ${percentReuse}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

                <div>
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem; font-size: 0.9rem;">
                        <span style="font-weight: 500; color: var(--text-muted);">📋 规则降级 (Rule Fallback)</span>
                        <span style="color: var(--text-muted);">${formatNumber(routeRule)} 次 (${this.formatPercent(percentRule)})</span>
                    </div>
                    <div style="height: 8px; background: #f1f5f9; border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: #94a3b8; width: ${percentRule}%; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);"></div>
                    </div>
                </div>

            </div>

            <!-- 缓存效能区域 -->
            <div style="font-size: 1.1rem; font-weight: 600; color: var(--text-main); margin-bottom: 1.25rem;">降本增效雷达 (Efficiency Radar)</div>
            <div class="stats-grid">
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">活跃语义指纹</div>
                    <div class="stat-value" style="font-size: 1.75rem;">${formatNumber(cacheEntries)}</div>
                    <div class="stat-trend">Redis 活跃键值</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">缓存防穿透次数</div>
                    <div class="stat-value" style="font-size: 1.75rem;">${formatNumber(cacheSavedCalls)}</div>
                    <div class="stat-trend">成功拦截 AI 调用</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem;">
                    <div class="stat-label">单条指纹平均利用率</div>
                    <div class="stat-value" style="font-size: 1.75rem;">${cacheAvgHits.toFixed(1)} <span style="font-size:1rem; color:var(--text-muted); font-weight:500;">x</span></div>
                    <div class="stat-trend">每条缓存使用频次</div>
                </div>
                <div class="stat-card" style="padding: 1.25rem; border-left: 3px solid var(--success);">
                    <div class="stat-label">全局缓存命中率</div>
                    <div class="stat-value" style="font-size: 1.75rem; color: var(--success);">${this.formatPercent(cacheHitRate)}</div>
                    <div class="stat-trend">缓存 / (缓存+穿透)</div>
                </div>
            </div>
        `;

        container.innerHTML = html;
    },

    /**
     * 渲染空状态
     */
    renderEmptyStats() {
        const container = document.getElementById('aiCostStats');
        if (!container) return;

        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-title">暂无数据</div><div class="empty-text">暂无 AI 使用统计数据</div></div>';
    }
};
