/**
 * API 调用封装模块
 * 统一封装所有后端 API 调用，提供统一的错误处理和响应解析
 */

const API = {
    /**
     * 获取认证 Token
     */
    getToken() {
        return localStorage.getItem('webhook_api_key') || '';
    },

    // 全局鉴权锁，防止并发请求时弹出多个输入框
    _authPromise: null,

    /**
     * 包装 fetch，自动添加 Auth 头和处理 401
     */
    async authenticatedFetch(url, options = {}) {
        // 如果有正在进行的鉴权弹窗，则等待它完成
        if (this._authPromise) {
            await this._authPromise;
        }

        const token = this.getToken();
        const headers = {
            ...options.headers,
            'Content-Type': 'application/json'
        };
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        const response = await fetch(url, { ...options, headers });
        
        if (response.status === 401) {
            // 在弹窗前再次检查是否已经被其他并发请求处理过了
            const currentToken = this.getToken();
            if (currentToken && currentToken !== token) {
                // Token 已被更新，直接使用新 Token 重试
                return this.authenticatedFetch(url, options);
            }

            if (!this._authPromise) {
                // 创建一个 Promise 锁，并阻塞其他并发请求
                this._authPromise = new Promise((resolve) => {
                    // 使用 setTimeout 确保 UI 线程不被死锁，并给浏览器渲染机会
                    setTimeout(() => {
                        const key = prompt('请输入 WebhookWise 的管理接口 API Key:');
                        if (key) {
                            localStorage.setItem('webhook_api_key', key);
                        }
                        resolve(key);
                        this._authPromise = null;
                    }, 50);
                });
            }

            const newKey = await this._authPromise;
            if (newKey) {
                // 有了新 Token，递归重试该请求
                return this.authenticatedFetch(url, options);
            }
        }
        
        return response;
    },

    // ========== 告警相关 API ==========

    /**
     * 获取告警列表
     * @param {object} params - 查询参数
     * @param {number} params.page - 页码
     * @param {number} params.page_size - 每页数量
     * @param {string} params.fields - 返回字段（summary 或 all）
     * @returns {Promise<object>} 告警列表数据
     */
    async getWebhooks(params = {}) {
        if (params.use_cursor || params.cursor_id !== undefined || params.limit) {
            const queryParams = new URLSearchParams();
            const limit = params.limit || params.page_size || 200;
            queryParams.append('limit', limit);
            if (params.cursor_id !== null && params.cursor_id !== undefined) queryParams.append('cursor_id', params.cursor_id);
            if (params.fields) queryParams.append('fields', params.fields);
            if (params.importance) queryParams.append('importance', params.importance);
            if (params.source) queryParams.append('source', params.source);

            const response = await this.authenticatedFetch('/api/webhooks/cursor?' + queryParams.toString());
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return await response.json();
        }

        const queryParams = new URLSearchParams();
        if (params.page) queryParams.append('page', params.page);
        if (params.page_size) queryParams.append('page_size', params.page_size);
        if (params.fields) queryParams.append('fields', params.fields);
        if (params.include_total !== undefined) queryParams.append('include_total', String(params.include_total));

        const response = await this.authenticatedFetch('/api/webhooks?' + queryParams.toString());
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取单个告警详情
     * @param {number} id - 告警 ID
     * @returns {Promise<object>} 告警详情数据
     */
    async getWebhook(id) {
        const response = await this.authenticatedFetch('/api/webhooks/' + id);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 重新分析告警
     * @param {number} id - 告警 ID
     * @returns {Promise<object>} 分析结果
     */
    async reanalyze(id) {
        const response = await this.authenticatedFetch('/api/reanalyze/' + id, { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 转发告警
     * @param {number} id - 告警 ID
     * @param {string} url - 转发目标 URL
     * @returns {Promise<object>} 转发结果
     */
    async forward(id, url) {
        const response = await this.authenticatedFetch('/api/forward/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ forward_url: url })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== AI 相关 API ==========

    /**
     * 获取 AI 使用统计
     * @param {string} period - 统计周期（day/week/month）
     * @returns {Promise<object>} AI 使用统计数据
     */
    async getAIUsage(period = 'day') {
        const response = await this.authenticatedFetch('/api/ai-usage?period=' + period);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取当前 Prompt 配置
     * @returns {Promise<object>} Prompt 配置
     */
    async getPrompt() {
        const response = await this.authenticatedFetch('/api/prompt');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 重新加载 Prompt 配置
     * @returns {Promise<object>} 重载结果
     */
    async reloadPrompt() {
        const response = await this.authenticatedFetch('/api/prompt/reload', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 配置相关 API ==========

    /**
     * 获取系统配置
     * @returns {Promise<object>} 配置数据
     */
    async getConfig() {
        const response = await this.authenticatedFetch('/api/config');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 保存系统配置
     * @param {object} data - 配置数据
     * @returns {Promise<object>} 保存结果
     */
    async saveConfig(data) {
        const response = await this.authenticatedFetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 深度分析 API ==========

    /**
     * 获取所有深度分析记录（分页+筛选）
     */
    async getAllDeepAnalyses(page = 1, perPage = 20, status = '', engine = '') {
        const params = new URLSearchParams({ page: page, per_page: perPage });
        if (status) params.set('status', status);
        if (engine) params.set('engine', engine);
        const response = await this.authenticatedFetch('/api/deep-analyses?' + params.toString());
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取深度分析历史记录
     * @param {number} webhookId - 告警 ID
     * @returns {Promise<object>} 深度分析历史记录列表
     */
    async getDeepAnalyses(webhookId) {
        const response = await this.authenticatedFetch('/api/deep-analyses/' + webhookId);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 执行深度分析
     * @param {number} id - 告警 ID
     * @param {string} question - 分析问题
     * @param {string} engine - 分析引擎（'local'/'openclaw'/'auto'）
     * @returns {Promise<object>} 分析结果
     */
    async deepAnalyze(id, question, engine = 'auto') {
        const response = await this.authenticatedFetch('/api/deep-analyze/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                user_question: question,
                engine: engine 
            })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 转发深度分析结果
     * @param {number} analysisId - 深度分析记录 ID
     * @param {string} targetUrl - 转发目标 URL
     * @returns {Promise<object>} 转发结果
     */
    async forwardDeepAnalysis(analysisId, targetUrl) {
        const response = await this.authenticatedFetch('/api/deep-analyses/' + analysisId + '/forward', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_url: targetUrl })
        });
        return await response.json();
    },

    /**
     * 重新拉取失败的深度分析结果
     * @param {number} analysisId - 深度分析记录 ID
     * @returns {Promise<object>} 重试结果
     */
    async retryDeepAnalysis(analysisId) {
        const response = await this.authenticatedFetch('/api/deep-analyses/' + analysisId + '/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        return await response.json();
    },

    // ========== 转发规则 API ==========

    /**
     * 获取转发规则列表
     * @returns {Promise<object>} 规则列表
     */
    async getForwardRules() {
        const response = await this.authenticatedFetch('/api/forward-rules');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 创建转发规则
     * @param {object} ruleData - 规则数据
     * @returns {Promise<object>} 创建结果
     */
    async createForwardRule(ruleData) {
        const response = await this.authenticatedFetch('/api/forward-rules', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ruleData)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 更新转发规则
     * @param {number} id - 规则 ID
     * @param {object} ruleData - 规则数据
     * @returns {Promise<object>} 更新结果
     */
    async updateForwardRule(id, ruleData) {
        const response = await this.authenticatedFetch('/api/forward-rules/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ruleData)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 删除转发规则
     * @param {number} id - 规则 ID
     * @returns {Promise<object>} 删除结果
     */
    async deleteForwardRule(id) {
        const response = await this.authenticatedFetch('/api/forward-rules/' + id, {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 测试转发规则
     * @param {number} id - 规则 ID
     * @returns {Promise<object>} 测试结果
     */
    async testForwardRule(id) {
        const response = await this.authenticatedFetch('/api/forward-rules/' + id + '/test', {
            method: 'POST'
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    }
};
