/**
 * API 调用封装模块
 * 统一封装所有后端 API 调用，提供统一的错误处理和响应解析
 */

const READ_TOKEN_KEY = 'webhook_api_key';
const WRITE_TOKEN_KEY = 'webhook_admin_write_key';

const API = {
    /**
     * 获取只读 API Token
     */
    getToken() {
        return this.getReadToken();
    },

    getReadToken() {
        return localStorage.getItem(READ_TOKEN_KEY) || '';
    },

    getWriteToken() {
        return localStorage.getItem(WRITE_TOKEN_KEY) || '';
    },

    setReadToken(token) {
        if (token) {
            localStorage.setItem(READ_TOKEN_KEY, token);
        }
    },

    setWriteToken(token) {
        if (token) {
            localStorage.setItem(WRITE_TOKEN_KEY, token);
        }
    },

    clearTokens() {
        localStorage.removeItem(READ_TOKEN_KEY);
        localStorage.removeItem(WRITE_TOKEN_KEY);
    },

    getTokenStatus() {
        return {
            read: Boolean(this.getReadToken()),
            write: Boolean(this.getWriteToken())
        };
    },

    getAuthMode(options = {}) {
        if (options.authMode) return options.authMode;
        const method = String(options.method || 'GET').toUpperCase();
        return method === 'GET' || method === 'HEAD' ? 'read' : 'write';
    },

    getTokenForMode(mode) {
        return mode === 'write' ? this.getWriteToken() : this.getReadToken();
    },

    // 全局鉴权锁，防止并发请求时弹出多个输入框
    _authPromises: {
        read: null,
        write: null
    },

    /**
     * 包装 fetch，自动添加 Auth 头和处理 401
     */
    async authenticatedFetch(url, options = {}, retryState = {}) {
        const authMode = this.getAuthMode(options);

        // 如果有正在进行的鉴权弹窗，则等待它完成
        if (this._authPromises[authMode]) {
            await this._authPromises[authMode];
        }

        const token = this.getTokenForMode(authMode);
        const headers = {
            ...options.headers,
            'Content-Type': 'application/json'
        };
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        const { authMode: _ignoredAuthMode, ...fetchOptions } = options;
        const response = await fetch(url, { ...fetchOptions, headers });

        if (await this.shouldPromptForAuth(response, authMode)) {
            // 在弹窗前再次检查是否已经被其他并发请求处理过了
            const currentToken = this.getTokenForMode(authMode);
            if (currentToken && currentToken !== token) {
                // Token 已被更新，直接使用新 Token 重试
                return this.authenticatedFetch(url, options, retryState);
            }

            if (retryState[authMode]) {
                return response;
            }

            if (!this._authPromises[authMode]) {
                // 创建一个 Promise 锁，并阻塞其他并发请求
                this._authPromises[authMode] = new Promise((resolve) => {
                    // 使用 setTimeout 确保 UI 线程不被死锁，并给浏览器渲染机会
                    setTimeout(() => {
                        const key = prompt(this.authPromptText(authMode));
                        if (key) {
                            if (authMode === 'write') {
                                this.setWriteToken(key);
                            } else {
                                this.setReadToken(key);
                            }
                            if (typeof updateAuthButtonState === 'function') {
                                updateAuthButtonState();
                            }
                        }
                        resolve(key);
                        this._authPromises[authMode] = null;
                    }, 50);
                });
            }

            const newKey = await this._authPromises[authMode];
            if (newKey) {
                // 有了新 Token，递归重试该请求
                return this.authenticatedFetch(url, options, { ...retryState, [authMode]: true });
            }
        }

        return response;
    },

    authPromptText(mode) {
        if (mode === 'write') {
            return '请输入 WebhookWise 的 ADMIN_WRITE_KEY（用于保存、转发、重试等写操作）:';
        }
        return '请输入 WebhookWise 的 API_KEY（用于 Dashboard 只读查询）:';
    },

    async shouldPromptForAuth(response, mode) {
        if (mode === 'read') {
            return response.status === 401;
        }
        if (response.status === 401) {
            return true;
        }
        if (response.status !== 403) {
            return false;
        }
        const body = await response.clone().json().catch(() => null);
        return body?.detail === 'Admin write permission required';
    },

    // ========== 告警相关 API ==========

    /**
     * 获取告警列表
     * @param {object} params - 查询参数
     * @param {number} params.page - 页码
     * @param {number} params.page_size - 每页数量
     * @param {number} params.cursor - 下一页游标
     * @returns {Promise<object>} 告警列表数据
     */
    async getWebhooks(params = {}) {
        const queryParams = new URLSearchParams();
        if (params.cursor !== null && params.cursor !== undefined) queryParams.append('cursor', params.cursor);
        if (params.page_size) queryParams.append('page_size', params.page_size);
        if (params.page) queryParams.append('page', params.page);
        if (params.importance) queryParams.append('importance', params.importance);
        if (params.source) queryParams.append('source', params.source);

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
            body: JSON.stringify({ target_url: url })
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
     * @param {string} engine - 分析引擎（'openclaw'/'auto'）
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
