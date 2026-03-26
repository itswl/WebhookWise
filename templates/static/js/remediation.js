/**
 * 自动修复模块
 * 处理 Runbook 管理和修复执行
 */

const RemediationModule = {
    runbooks: [],
    history: [],

    /**
     * 初始化模块
     */
    init() {
        this.loadRunbooks();
        this.loadHistory();
        this.bindEvents();
    },

    /**
     * 绑定事件
     */
    bindEvents() {
        // Runbook 执行按钮事件委托
        const runbooksContainer = document.getElementById('runbooksList');
        if (runbooksContainer) {
            runbooksContainer.addEventListener('click', (e) => {
                const btn = e.target.closest('[data-runbook-action]');
                if (btn) {
                    const action = btn.getAttribute('data-runbook-action');
                    const name = btn.getAttribute('data-runbook-name');

                    if (action === 'execute') {
                        this.executeRunbook(name, false);
                    } else if (action === 'dry-run') {
                        this.executeRunbook(name, true);
                    }
                }
            });
        }
    },

    /**
     * 加载 Runbook 列表
     */
    async loadRunbooks() {
        try {
            const result = await API.getRunbooks();

            if (result.success && result.data) {
                this.runbooks = Array.isArray(result.data) ? result.data : (result.data.runbooks || []);
                this.renderRunbooks(this.runbooks);
            } else {
                this.renderEmptyRunbooks();
            }
        } catch (error) {
            console.error('加载 Runbook 失败:', error);
            this.renderEmptyRunbooks();
        }
    },

    /**
     * 渲染 Runbook 列表
     * @param {array} runbooks - Runbook 数据数组
     */
    renderRunbooks(runbooks) {
        const container = document.getElementById('runbooksList');
        if (!container) return;

        if (runbooks.length === 0) {
            this.renderEmptyRunbooks();
            return;
        }

        let html = '<div class="runbooks-grid">';

        runbooks.forEach((runbook) => {
            const isEnabled = runbook.enabled !== false;

            html += '<div class="runbook-card ' + (isEnabled ? '' : 'disabled') + '">';

            html += '<div class="runbook-header">';
            html += '<span class="runbook-name">' + (runbook.name || '未命名') + '</span>';
            html += '<span class="runbook-status ' + (isEnabled ? 'enabled' : 'disabled') + '">' + (isEnabled ? '已启用' : '已禁用') + '</span>';
            html += '</div>';

            html += '<div class="runbook-body">';
            html += '<p class="runbook-desc">' + (runbook.description || '无描述') + '</p>';

            if (runbook.triggers && runbook.triggers.length > 0) {
                html += '<div class="runbook-triggers">';
                html += '<span class="label">触发条件:</span> ';
                runbook.triggers.forEach((trigger, idx) => {
                    html += '<span class="trigger-tag">' + trigger + '</span>';
                    if (idx < runbook.triggers.length - 1) {
                        html += ' ';
                    }
                });
                html += '</div>';
            }

            if (runbook.actions && runbook.actions.length > 0) {
                html += '<div class="runbook-actions-count">';
                html += '<span class="label">执行动作:</span> ' + runbook.actions.length + ' 个';
                html += '</div>';
            }
            html += '</div>';

            // 如果 Runbook 有参数定义，渲染输入表单
            if (runbook.parameters && runbook.parameters.length > 0) {
                html += '<div class="runbook-parameters">';
                html += '<div class="params-header">执行参数</div>';
                html += '<div class="params-hint">手动执行时需填入以下参数，关联告警时自动填充</div>';
                html += '<div class="params-form" id="params-form-' + runbook.name + '">';
                
                runbook.parameters.forEach(param => {
                    const requiredMark = param.required ? '<span style="color:#dc3545">*</span>' : '';
                    const defaultHint = param.default ? '（默认: ' + param.default + '）' : '';
                    
                    html += '<div class="param-row">';
                    html += '<label>' + requiredMark + param.name + defaultHint + '</label>';
                    html += '<input type="text" class="param-input" ' +
                            'name="' + param.name + '" ' +
                            'data-required="' + param.required + '" ' +
                            'placeholder="' + (param.description || param.name) + '" ' +
                            (param.default ? 'value="' + param.default + '"' : '') + '/>';
                    html += '</div>';
                });
                
                html += '</div></div>';
            }

            html += '<div class="runbook-footer">';
            html += '<button class="btn btn-sm" data-runbook-action="dry-run" data-runbook-name="' + runbook.name + '">试运行</button>';
            html += '<button class="btn btn-sm btn-primary" data-runbook-action="execute" data-runbook-name="' + runbook.name + '">执行修复</button>';
            html += '</div>';

            html += '</div>';
        });

        html += '</div>';
        container.innerHTML = html;
    },

    /**
     * 渲染空 Runbook 状态
     */
    renderEmptyRunbooks() {
        const container = document.getElementById('runbooksList');
        if (!container) return;

        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📋</div><div class="empty-title">暂无 Runbook</div><div class="empty-text">在 runbooks/ 目录下添加 YAML 文件</div></div>';
    },

    /**
     * 加载修复历史
     */
    async loadHistory() {
        try {
            const result = await API.getRemediationHistory();

            if (result.success && result.data) {
                this.history = Array.isArray(result.data) ? result.data : (result.data.history || []);
                this.renderHistory(this.history);
            }
        } catch (error) {
            console.error('加载修复历史失败:', error);
        }
    },

    /**
     * 渲染修复历史
     * @param {array} history - 历史记录数组
     */
    renderHistory(history) {
        const container = document.getElementById('remediationHistory');
        if (!container) return;

        if (history.length === 0) {
            container.innerHTML = '<div class="empty-text">暂无执行记录</div>';
            return;
        }

        let html = '<div class="history-list">';

        // 只显示最近 10 条
        history.slice(0, 10).forEach((item) => {
            const statusClass = item.status || 'unknown';

            html += '<div class="history-item ' + statusClass + '">';
            html += '<div class="history-header">';
            html += '<span class="history-runbook">' + (item.runbook_name || '-') + '</span>';
            html += '<span class="history-status ' + statusClass + '">' + this.getStatusText(item.status) + '</span>';
            html += '</div>';

            html += '<div class="history-meta">';
            html += '<span>告警: #' + (item.alert_id || '-') + '</span>';
            html += '<span>' + timeAgo(item.executed_at) + '</span>';
            html += '</div>';

            if (item.result) {
                html += '<div class="history-result">';
                html += '<pre>' + JSON.stringify(item.result, null, 2) + '</pre>';
                html += '</div>';
            }

            if (item.status === 'pending_approval') {
                html += '<div class="history-actions">';
                html += '<button class="btn btn-sm btn-primary" onclick="RemediationModule.approveExecution(' + item.id + ')">批准执行</button>';
                html += '</div>';
            }

            html += '</div>';
        });

        html += '</div>';
        container.innerHTML = html;
    },

    /**
     * 获取状态文本
     * @param {string} status - 状态码
     * @returns {string} 状态文本
     */
    getStatusText(status) {
        const statusMap = {
            'success': '成功',
            'failed': '失败',
            'pending_approval': '待审批',
            'running': '执行中',
            'cancelled': '已取消',
            'unknown': '未知'
        };
        return statusMap[status] || status;
    },

    /**
     * 执行 Runbook
     * @param {string} name - Runbook 名称
     * @param {boolean} dryRun - 是否为试运行
     * @param {number} alertId - 关联的告警 ID（可选）
     */
    async executeRunbook(name, dryRun = false, alertId = null) {
        // 收集参数表单数据
        const paramsForm = document.getElementById('params-form-' + name);
        const manualParameters = {};

        if (paramsForm) {
            const inputs = paramsForm.querySelectorAll('.param-input');
            inputs.forEach(input => {
                const val = input.value.trim();
                if (val) {
                    manualParameters[input.name] = val;
                }
            });

            // 非干运行且无 alertId 时，验证必填参数
            if (!dryRun && !alertId) {
                const missing = [];
                inputs.forEach(input => {
                    if (input.dataset.required === 'true' && !input.value.trim()) {
                        missing.push(input.name);
                        input.style.borderColor = '#dc3545';
                    } else {
                        input.style.borderColor = '';
                    }
                });

                if (missing.length > 0) {
                    alert('❗ 请填入必需参数: ' + missing.join(', '));
                    return;
                }
            }
        }

        if (!dryRun) {
            if (!confirm('确认要执行修复操作 "' + name + '" 吗？')) {
                return;
            }
        }

        try {
            const params = {
                dry_run: dryRun
            };
            if (alertId) {
                params.alert_id = alertId;
            }
            // 添加手动输入的参数
            if (Object.keys(manualParameters).length > 0) {
                params.manual_parameters = manualParameters;
            }

            const result = await API.executeRunbook(name, params);

            if (result.success) {
                if (dryRun) {
                    alert('✅ 试运行完成！\n\n结果:\n' + JSON.stringify(result.data, null, 2));
                } else {
                    alert('✅ 修复任务已启动！');
                    this.loadHistory();
                }
            } else {
                alert('❌ 执行失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            console.error('执行 Runbook 失败:', error);
            alert('❌ 请求失败: ' + error.message);
        }
    },

    /**
     * 批准执行
     * @param {number} executionId - 执行记录 ID
     */
    async approveExecution(executionId) {
        try {
            // 调用 API 批准执行
            const response = await fetch('/api/remediation/' + executionId + '/approve', {
                method: 'POST'
            });
            const result = await response.json();

            if (result.success) {
                alert('✅ 已批准执行！');
                this.loadHistory();
            } else {
                alert('❌ 批准失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            console.error('批准执行失败:', error);
            alert('❌ 请求失败: ' + error.message);
        }
    }
};
