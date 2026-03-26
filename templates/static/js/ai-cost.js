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

        // 从 API 返回格式中提取数据
        const totalCalls = this.safeGet(data, 'total_calls', 0);
        const tokensTotal = this.safeGet(data, 'tokens.total', 0);
        const tokensInput = this.safeGet(data, 'tokens.input', 0);
        const tokensOutput = this.safeGet(data, 'tokens.output', 0);
        const costTotal = this.safeGet(data, 'cost.total', 0);
        const costSaved = this.safeGet(data, 'cost.saved_estimate', 0);

        const percentAi = this.safeGet(data, 'percentages.ai', 0);
        const percentRule = this.safeGet(data, 'percentages.rule', 0);
        const percentCache = this.safeGet(data, 'percentages.cache', 0);

        const routeAi = this.safeGet(data, 'route_breakdown.ai', 0);
        const routeRule = this.safeGet(data, 'route_breakdown.rule', 0);
        const routeCache = this.safeGet(data, 'route_breakdown.cache', 0);

        const cacheStats = data.cache_statistics || {};
        const cacheEntries = this.safeGet(cacheStats, 'total_cache_entries', 0);
        const cacheTotalHits = this.safeGet(cacheStats, 'total_hits', 0);
        const cacheAvgHits = this.safeGet(cacheStats, 'avg_hits_per_entry', 0);
        const cacheHitRate = this.safeGet(cacheStats, 'cache_hit_rate', 0);
        const cacheSavedCalls = this.safeGet(cacheStats, 'saved_calls', 0);

        let html = '';

        // ========== 统计卡片区域 ==========
        html += '<div class="section-title">使用统计</div>';
        html += '<div class="stats-grid">';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">总调用次数</div>';
        html += '<div class="stat-value">' + formatNumber(totalCalls) + '</div>';
        html += '<div class="stat-trend">本周期内所有请求</div>';
        html += '</div>';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">总 Token 数</div>';
        html += '<div class="stat-value">' + formatNumber(tokensTotal) + '</div>';
        html += '<div class="stat-trend">输入 + 输出</div>';
        html += '</div>';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">输入 Token</div>';
        html += '<div class="stat-value">' + formatNumber(tokensInput) + '</div>';
        html += '<div class="stat-trend">Prompt 消耗</div>';
        html += '</div>';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">输出 Token</div>';
        html += '<div class="stat-value">' + formatNumber(tokensOutput) + '</div>';
        html += '<div class="stat-trend">Completion 消耗</div>';
        html += '</div>';

        html += '<div class="stat-card highlight">';
        html += '<div class="stat-label">预估成本</div>';
        html += '<div class="stat-value">' + this.formatCurrency(costTotal) + '</div>';
        html += '<div class="stat-trend">基于实际 API 费率</div>';
        html += '</div>';

        html += '<div class="stat-card highlight-green">';
        html += '<div class="stat-label">节省成本</div>';
        html += '<div class="stat-value">' + this.formatCurrency(costSaved) + '</div>';
        html += '<div class="stat-trend">通过缓存和规则路由</div>';
        html += '</div>';

        html += '</div>';

        // ========== 路由分布区域 ==========
        html += '<div class="section-title" style="margin-top: 30px;">路由分布</div>';
        html += '<div class="route-distribution">';

        // AI 调用
        html += '<div class="route-item">';
        html += '<div class="route-header">';
        html += '<span class="route-label">🤖 AI 调用</span>';
        html += '<span class="route-value">' + formatNumber(routeAi) + ' 次 (' + this.formatPercent(percentAi) + ')</span>';
        html += '</div>';
        html += '<div class="progress-bar"><div class="progress-fill progress-ai" style="width: ' + percentAi + '%"></div></div>';
        html += '</div>';

        // 规则路由
        html += '<div class="route-item">';
        html += '<div class="route-header">';
        html += '<span class="route-label">📋 规则路由</span>';
        html += '<span class="route-value">' + formatNumber(routeRule) + ' 次 (' + this.formatPercent(percentRule) + ')</span>';
        html += '</div>';
        html += '<div class="progress-bar"><div class="progress-fill progress-rule" style="width: ' + percentRule + '%"></div></div>';
        html += '</div>';

        // 缓存命中
        html += '<div class="route-item">';
        html += '<div class="route-header">';
        html += '<span class="route-label">💾 缓存命中</span>';
        html += '<span class="route-value">' + formatNumber(routeCache) + ' 次 (' + this.formatPercent(percentCache) + ')</span>';
        html += '</div>';
        html += '<div class="progress-bar"><div class="progress-fill progress-cache" style="width: ' + percentCache + '%"></div></div>';
        html += '</div>';

        html += '</div>';

        // ========== 缓存效能区域 ==========
        html += '<div class="section-title" style="margin-top: 30px;">缓存效能</div>';
        html += '<div class="stats-grid">';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">活跃缓存条目</div>';
        html += '<div class="stat-value">' + formatNumber(cacheEntries) + '</div>';
        html += '<div class="stat-trend">当前缓存的分析结果</div>';
        html += '</div>';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">缓存累计命中</div>';
        html += '<div class="stat-value">' + formatNumber(cacheTotalHits) + '</div>';
        html += '<div class="stat-trend">复用已有分析结果</div>';
        html += '</div>';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">平均命中次数</div>';
        html += '<div class="stat-value">' + cacheAvgHits.toFixed(1) + '</div>';
        html += '<div class="stat-trend">每条缓存平均使用</div>';
        html += '</div>';

        html += '<div class="stat-card">';
        html += '<div class="stat-label">缓存命中率</div>';
        html += '<div class="stat-value">' + this.formatPercent(cacheHitRate) + '</div>';
        html += '<div class="stat-trend">缓存利用效率</div>';
        html += '</div>';

        html += '<div class="stat-card highlight-green">';
        html += '<div class="stat-label">缓存节省调用</div>';
        html += '<div class="stat-value">' + formatNumber(cacheSavedCalls) + '</div>';
        html += '<div class="stat-trend">避免的 AI API 调用</div>';
        html += '</div>';

        html += '</div>';

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
