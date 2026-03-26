/**
 * API 调用封装模块
 * 统一封装所有后端 API 调用，提供统一的错误处理和响应解析
 */

const API = {
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
        const queryParams = new URLSearchParams();
        if (params.page) queryParams.append('page', params.page);
        if (params.page_size) queryParams.append('page_size', params.page_size);
        if (params.fields) queryParams.append('fields', params.fields);

        const response = await fetch('/api/webhooks?' + queryParams.toString());
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取单个告警详情
     * @param {number} id - 告警 ID
     * @returns {Promise<object>} 告警详情数据
     */
    async getWebhook(id) {
        const response = await fetch('/api/webhooks/' + id);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 重新分析告警
     * @param {number} id - 告警 ID
     * @returns {Promise<object>} 分析结果
     */
    async reanalyze(id) {
        const response = await fetch('/api/reanalyze/' + id, { method: 'POST' });
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
        const response = await fetch('/api/forward/' + id, {
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
        const response = await fetch('/api/ai-usage?period=' + period);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取当前 Prompt 配置
     * @returns {Promise<object>} Prompt 配置
     */
    async getPrompt() {
        const response = await fetch('/api/prompt');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 重新加载 Prompt 配置
     * @returns {Promise<object>} 重载结果
     */
    async reloadPrompt() {
        const response = await fetch('/api/prompt/reload', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 预测相关 API ==========

    /**
     * 获取预测列表
     * @returns {Promise<object>} 预测数据列表
     */
    async getPredictions() {
        const response = await fetch('/api/predictions');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 运行预测分析
     * @returns {Promise<object>} 预测结果
     */
    async runPrediction() {
        const response = await fetch('/api/predictions/run', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取模式分析结果
     * @returns {Promise<object>} 模式分析数据
     */
    async getPatterns() {
        const response = await fetch('/api/patterns');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 分析告警模式
     * @returns {Promise<object>} 分析结果
     */
    async analyzePatterns() {
        const response = await fetch('/api/patterns/analyze', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 修复相关 API ==========

    /**
     * 获取 Runbook 列表
     * @returns {Promise<object>} Runbook 列表
     */
    async getRunbooks() {
        const response = await fetch('/api/remediation/runbooks');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 执行 Runbook
     * @param {string} name - Runbook 名称
     * @param {object} params - 执行参数
     * @returns {Promise<object>} 执行结果
     */
    async executeRunbook(name, params) {
        const response = await fetch('/api/remediation/execute/' + name, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取修复历史记录
     * @returns {Promise<object>} 修复历史
     */
    async getRemediationHistory() {
        const response = await fetch('/api/remediation/history');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 拓扑相关 API ==========

    /**
     * 获取服务拓扑数据
     * @returns {Promise<object>} 拓扑数据
     */
    async getTopology() {
        const response = await fetch('/api/topology');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 添加拓扑依赖关系
     * @param {object} data - 依赖关系数据 {source, target, type}
     * @returns {Promise<object>} 添加结果
     */
    async addTopologyDependency(data) {
        const response = await fetch('/api/topology/dependencies', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 删除拓扑依赖关系
     * @param {string} source - 源服务
     * @param {string} target - 目标服务
     * @returns {Promise<object>} 删除结果
     */
    async deleteTopologyDependency(source, target) {
        const response = await fetch('/api/topology/dependencies?source=' + encodeURIComponent(source) + '&target=' + encodeURIComponent(target), {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 自动发现拓扑
     * @returns {Promise<object>} 发现的拓扑数据
     */
    async discoverTopology() {
        const response = await fetch('/api/topology/discover', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== Skill 相关 API ==========

    /**
     * 获取 Skill 列表
     * @returns {Promise<object>} Skill 列表
     */
    async getSkills() {
        const response = await fetch('/api/skill-configs');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 测试 Skill
     * @param {string} name - Skill 名称
     * @returns {Promise<object>} 测试结果
     */
    async testSkill(name) {
        const response = await fetch('/api/skills/' + name + '/test', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取 Skill 配置详情
     * @param {number} id - 配置 ID
     * @returns {Promise<object>} 配置详情
     */
    async getSkillConfig(id) {
        const response = await fetch('/api/skill-configs/' + id);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 创建 Skill 配置
     * @param {object} data - 配置数据
     * @returns {Promise<object>} 创建结果
     */
    async createSkillConfig(data) {
        const response = await fetch('/api/skill-configs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 更新 Skill 配置
     * @param {number} id - 配置 ID
     * @param {object} data - 配置数据
     * @returns {Promise<object>} 更新结果
     */
    async updateSkillConfig(id, data) {
        const response = await fetch('/api/skill-configs/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 删除 Skill 配置
     * @param {number} id - 配置 ID
     * @returns {Promise<object>} 删除结果
     */
    async deleteSkillConfig(id) {
        const response = await fetch('/api/skill-configs/' + id, { method: 'DELETE' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 切换 Skill 启用状态
     * @param {number} id - 配置 ID
     * @param {boolean} enabled - 是否启用
     * @returns {Promise<object>} 切换结果
     */
    async toggleSkillConfig(id, enabled) {
        const response = await fetch('/api/skill-configs/' + id + '/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabled })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取 Skill 代码
     * @param {number} id - 配置 ID
     * @returns {Promise<object>} 代码数据
     */
    async getSkillCode(id) {
        const response = await fetch('/api/skill-configs/' + id + '/code');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 更新 Skill 代码
     * @param {number} id - 配置 ID
     * @param {string} code - 代码内容
     * @returns {Promise<object>} 更新结果
     */
    async updateSkillCode(id, code) {
        const response = await fetch('/api/skill-configs/' + id + '/code', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 测试 Skill 代码
     * @param {number} id - 配置 ID
     * @param {string} code - 代码内容
     * @param {string} action - 测试的 action
     * @param {object} params - 测试参数
     * @returns {Promise<object>} 测试结果
     */
    async testSkillCode(id, code, action, params) {
        const body = { code: code };
        if (action) body.action = action;
        if (params) body.params = params;

        const response = await fetch('/api/skill-configs/' + id + '/test-code', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取 Skill 代码模板
     * @param {string} name - Skill 名称
     * @returns {Promise<object>} 模板数据
     */
    async getSkillTemplate(name) {
        const response = await fetch('/api/skill-template?name=' + encodeURIComponent(name));
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 外部 Skill API ==========

    /**
     * 获取所有外部 Skill 列表
     * @returns {Promise<object>} 外部 Skill 列表
     */
    async getExternalSkills() {
        const response = await fetch('/api/external-skills');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 重新扫描 skills/ 目录
     * @returns {Promise<object>} 扫描结果
     */
    async reloadExternalSkills() {
        const response = await fetch('/api/external-skills/reload', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取外部 Skill 详细文档 (SKILL.md)
     * @param {string} name - Skill 名称
     * @returns {Promise<object>} 文档内容
     */
    async getExternalSkillDetail(name) {
        const response = await fetch('/api/external-skills/' + encodeURIComponent(name) + '/detail');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 获取外部 Skill 的 secrets 键列表（不含值）
     * @param {string} name - Skill 名称
     * @returns {Promise<object>} secrets 键列表
     */
    async getExternalSkillSecrets(name) {
        const response = await fetch('/api/external-skills/' + encodeURIComponent(name) + '/secrets');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 更新外部 Skill 的 secrets
     * @param {string} name - Skill 名称
     * @param {object} secrets - secrets 键值对
     * @returns {Promise<object>} 更新结果
     */
    async updateExternalSkillSecrets(name, secrets) {
        const response = await fetch('/api/external-skills/' + encodeURIComponent(name) + '/secrets', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ secrets: secrets })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 配置相关 API ==========

    /**
     * 获取系统配置
     * @returns {Promise<object>} 配置数据
     */
    async getConfig() {
        const response = await fetch('/api/config');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * 保存系统配置
     * @param {object} data - 配置数据
     * @returns {Promise<object>} 保存结果
     */
    async saveConfig(data) {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== 深度分析 API ==========

    /**
     * 执行深度分析
     * @param {number} id - 告警 ID
     * @param {string} question - 分析问题
     * @returns {Promise<object>} 分析结果
     */
    async deepAnalyze(id, question) {
        const response = await fetch('/api/deep-analyze/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question: question })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    }
};
