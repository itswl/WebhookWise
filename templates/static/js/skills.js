/**
 * 平台连接管理模块 (Skills Module)
 * 管理 Skill 配置、代码编辑和测试
 */

const SkillsModule = {
    skills: [],
    externalSkills: [],

    /**
     * 初始化模块
     */
    init() {
        this.loadSkills();
        this.loadExternalSkills();
    },

    /**
     * 加载 Skill 列表
     */
    async loadSkills() {
        try {
            const result = await API.getSkills();
            if (!result.success || !result.data) {
                throw new Error('数据格式错误');
            }

            this.skills = result.data;
            this.renderSkills(result.data);
        } catch (error) {
            console.error('加载 Skill 列表失败:', error);
            document.getElementById('skillsGrid').innerHTML =
                '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">加载失败</div><div class="empty-text">' + error.message + '</div></div>';
        }
    },

    /**
     * 渲染 Skill 卡片
     */
    renderSkills(skills) {
        const container = document.getElementById('skillsGrid');

        if (skills.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">🔌</div><div class="empty-title">暂无连接</div><div class="empty-text">点击"添加连接"按钮创建新的平台连接</div></div>';
            return;
        }

        // 添加 Skill 卡片样式
        let html = '<style>' +
            '.skill-card { background: #fff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 1rem; transition: all 0.2s; }' +
            '.skill-card:hover { box-shadow: 0 3px 10px rgba(0,0,0,0.1); }' +
            '.skill-card.disabled { opacity: 0.6; background: #f6f8fa; }' +
            '.skill-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }' +
            '.skill-name { font-weight: 600; color: #24292e; }' +
            '.skill-type { font-size: 0.75rem; color: #586069; background: #f6f8fa; padding: 0.125rem 0.5rem; border-radius: 12px; }' +
            '.skill-desc { font-size: 0.875rem; color: #586069; margin-bottom: 0.75rem; min-height: 1.5rem; }' +
            '.skill-status { display: flex; align-items: center; gap: 0.5rem; font-size: 0.75rem; margin-bottom: 0.75rem; }' +
            '.status-indicator { width: 8px; height: 8px; border-radius: 50%; }' +
            '.status-indicator.healthy { background: #28a745; }' +
            '.status-indicator.unhealthy { background: #d73a49; }' +
            '.status-indicator.unknown { background: #959da5; }' +
            '.skill-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }' +
            '.skills-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }' +
            '</style>';

        skills.forEach((skill) => {
            const isEnabled = skill.enabled;
            const isCustom = skill.skill_type === 'custom';
            const health = skill.health;
            let healthStatus = 'unknown';
            let healthText = '未检查';

            if (health) {
                healthStatus = health.healthy ? 'healthy' : 'unhealthy';
                healthText = health.healthy ? '健康' : '异常';
            }

            html += '<div class="skill-card ' + (isEnabled ? '' : 'disabled') + '">';
            html += '<div class="skill-header">';
            html += '<span class="skill-name">' + (skill.display_name || skill.name) + '</span>';
            html += '<span class="skill-type">' + skill.skill_type + '</span>';
            html += '</div>';
            html += '<div class="skill-desc">' + (skill.description || '无描述') + '</div>';
            html += '<div class="skill-status">';
            html += '<span class="status-indicator ' + healthStatus + '"></span>';
            html += '<span>' + healthText + '</span>';
            html += '<span style="margin-left: auto;">' + (isEnabled ? '已启用' : '已禁用') + '</span>';
            html += '</div>';
            html += '<div class="skill-actions">';
            html += '<button class="btn btn-sm" onclick="SkillsModule.openConfigModal(' + skill.id + ')">编辑</button>';
            if (isCustom) {
                html += '<button class="btn btn-sm" onclick="SkillsModule.openCodeEditor(' + skill.id + ', \'' + skill.name + '\')">编辑代码</button>';
            }
            html += '<button class="btn btn-sm" onclick="SkillsModule.toggleSkillEnabled(' + skill.id + ', \'' + skill.name + '\', ' + isEnabled + ')">' + (isEnabled ? '禁用' : '启用') + '</button>';
            if (!skill.is_builtin) {
                html += '<button class="btn btn-sm" onclick="SkillsModule.deleteSkillConfig(' + skill.id + ', \'' + skill.name + '\')" style="color: #d73a49;">删除</button>';
            }
            html += '</div>';
            html += '</div>';
        });

        container.innerHTML = html;
    },

    /**
     * 打开 Skill 配置模态框
     * @param {number} configId - 配置 ID（为空表示新建）
     */
    openConfigModal(configId) {
        document.getElementById('skillConfigModal').classList.add('active');
        if (configId) {
            document.getElementById('skillConfigModalTitle').textContent = '编辑连接';
            // 编辑模式 - 加载配置
            API.getSkillConfig(configId)
                .then(data => {
                    if (data.success) {
                        const cfg = data.data;
                        document.getElementById('skillConfigId').value = cfg.id;
                        document.getElementById('skillConfigName').value = cfg.name;
                        document.getElementById('skillConfigDisplayName').value = cfg.display_name;
                        document.getElementById('skillConfigType').value = cfg.skill_type;
                        document.getElementById('skillConfigDescription').value = cfg.description || '';
                        document.getElementById('skillConfigEnabled').checked = cfg.enabled;
                        this.onSkillTypeChange();
                        // 填充配置值
                        if (cfg.config) {
                            Object.keys(cfg.config).forEach(key => {
                                const input = document.querySelector('[data-config-key="' + key + '"]');
                                if (input) input.value = cfg.config[key];
                            });
                        }
                    }
                });
        } else {
            document.getElementById('skillConfigModalTitle').textContent = '添加连接';
            // 添加模式
            document.getElementById('skillConfigId').value = '';
            document.getElementById('skillConfigName').value = '';
            document.getElementById('skillConfigDisplayName').value = '';
            document.getElementById('skillConfigType').value = '';
            document.getElementById('skillConfigDescription').value = '';
            document.getElementById('skillConfigEnabled').checked = true;
            this.onSkillTypeChange();
        }
    },

    /**
     * 关闭 Skill 配置模态框
     */
    closeConfigModal() {
        document.getElementById('skillConfigModal').classList.remove('active');
    },

    /**
     * Skill 类型改变时更新配置区域显示
     */
    onSkillTypeChange() {
        const type = document.getElementById('skillConfigType').value;
        document.querySelectorAll('.skill-config-section').forEach(section => {
            section.style.display = section.dataset.type === type ? 'block' : 'none';
        });
    },

    /**
     * 保存 Skill 配置
     */
    saveSkillConfig() {
        const id = document.getElementById('skillConfigId').value;
        const type = document.getElementById('skillConfigType').value;

        // 收集配置
        const config = {};
        document.querySelectorAll('.skill-config-section[data-type="' + type + '"] [data-config-key]').forEach(input => {
            config[input.dataset.configKey] = input.value;
        });

        const data = {
            name: document.getElementById('skillConfigName').value,
            display_name: document.getElementById('skillConfigDisplayName').value,
            skill_type: type,
            description: document.getElementById('skillConfigDescription').value,
            enabled: document.getElementById('skillConfigEnabled').checked,
            config: config
        };

        const savePromise = id
            ? API.updateSkillConfig(id, data)
            : API.createSkillConfig(data);

        savePromise
            .then(data => {
                if (data.success) {
                    this.closeConfigModal();
                    this.loadSkills();
                } else {
                    alert('保存失败: ' + (data.error || '未知错误'));
                }
            });
    },

    /**
     * 删除 Skill 配置
     * @param {number} id - 配置 ID
     * @param {string} name - Skill 名称
     */
    deleteSkillConfig(id, name) {
        if (!confirm('确定要删除 Skill "' + name + '" 吗？')) return;

        API.deleteSkillConfig(id)
            .then(data => {
                if (data.success) {
                    this.loadSkills();
                } else {
                    alert('删除失败: ' + (data.error || '未知错误'));
                }
            });
    },

    /**
     * 切换 Skill 启用状态
     * @param {number} id - 配置 ID
     * @param {string} name - Skill 名称
     * @param {boolean} currentEnabled - 当前启用状态
     */
    toggleSkillEnabled(id, name, currentEnabled) {
        API.toggleSkillConfig(id, !currentEnabled)
            .then(data => {
                if (data.success) {
                    this.loadSkills();
                }
            });
    },

    /**
     * 配置内置 Skill
     * @param {string} skillName - Skill 名称
     */
    configureBuiltinSkill(skillName) {
        // 查找是否已存在该 Skill 的配置
        const existingConfig = this.skills.find(s => s.name === skillName);
        if (existingConfig) {
            this.openConfigModal(existingConfig.id);
        } else {
            // 创建新配置
            this.openConfigModal(null);
            document.getElementById('skillConfigName').value = skillName;
            document.getElementById('skillConfigDisplayName').value = skillName;
        }
    },

    // ========== 代码编辑器 ==========

    /**
     * 打开代码编辑器
     * @param {number} configId - 配置 ID
     * @param {string} skillName - Skill 名称
     */
    openCodeEditor(configId, skillName) {
        document.getElementById('skillCodeEditorModal').classList.add('active');
        document.getElementById('skillCodeEditorId').value = configId;
        document.getElementById('skillCodeEditorTitle').textContent = '编辑代码 - ' + skillName;
        document.getElementById('skillCodeValidationResult').style.display = 'none';

        API.getSkillCode(configId)
            .then(data => {
                if (data.success) {
                    document.getElementById('skillCodeEditorContent').value = data.data.code;
                }
            });
    },

    /**
     * 关闭代码编辑器
     */
    closeCodeEditor() {
        document.getElementById('skillCodeEditorModal').classList.remove('active');
        document.getElementById('skillCodeValidationResult').style.display = 'none';
    },

    /**
     * 加载 Skill 代码模板
     */
    loadSkillTemplate() {
        const titleText = document.getElementById('skillCodeEditorTitle').textContent;
        const skillName = titleText.split(' - ')[1] || 'my_skill';
        API.getSkillTemplate(skillName)
            .then(data => {
                if (data.success) {
                    document.getElementById('skillCodeEditorContent').value = data.data.template;
                }
            });
    },

    /**
     * 格式化 Skill 代码
     */
    formatSkillCode() {
        // 简单的格式化：规范化缩进
        const textarea = document.getElementById('skillCodeEditorContent');
        const lines = textarea.value.split('\n');
        let indent = 0;
        const formatted = lines.map(line => {
            const trimmed = line.trim();
            if (trimmed.endsWith(':')) indent++;
            if (trimmed.startsWith('return') || trimmed.startsWith('pass')) indent = Math.max(0, indent - 1);
            return '    '.repeat(Math.max(0, indent - (trimmed.endsWith(':') ? 0 : 1))) + trimmed;
        }).join('\n');
        textarea.value = formatted;
    },

    /**
     * 验证 Skill 代码
     */
    validateSkillCode() {
        const code = document.getElementById('skillCodeEditorContent').value;
        const resultDiv = document.getElementById('skillCodeValidationResult');
        const id = document.getElementById('skillCodeEditorId').value;

        API.testSkillCode(id, code)
            .then(data => {
                resultDiv.style.display = 'block';
                if (data.success && data.data.valid) {
                    resultDiv.style.background = '#d4edda';
                    resultDiv.style.color = '#155724';
                    resultDiv.innerHTML = '✓ 代码验证通过';
                } else {
                    resultDiv.style.background = '#f8d7da';
                    resultDiv.style.color = '#721c24';
                    resultDiv.innerHTML = '✗ 验证失败: ' + (data.data && data.data.error ? data.data.error : data.error || '未知错误');
                }
            });
    },

    /**
     * 测试 Skill 代码
     */
    testSkillCode() {
        const code = document.getElementById('skillCodeEditorContent').value;
        const action = prompt('输入要测试的 action 名称:', 'query_data');
        if (!action) return;

        const id = document.getElementById('skillCodeEditorId').value;

        API.testSkillCode(id, code, action, {})
            .then(data => {
                const resultDiv = document.getElementById('skillCodeValidationResult');
                resultDiv.style.display = 'block';
                if (data.success && data.data.valid) {
                    resultDiv.style.background = '#d4edda';
                    resultDiv.style.color = '#155724';
                    resultDiv.innerHTML = '<strong>测试成功</strong><br><pre style="white-space: pre-wrap; word-break: break-all;">' + JSON.stringify(data.data.execution, null, 2) + '</pre>';
                } else {
                    resultDiv.style.background = '#f8d7da';
                    resultDiv.style.color = '#721c24';
                    resultDiv.innerHTML = '<strong>测试失败</strong><br>' + (data.data && data.data.error ? data.data.error : data.error || '未知错误');
                }
            });
    },

    /**
     * 保存 Skill 代码
     */
    saveSkillCode() {
        const id = document.getElementById('skillCodeEditorId').value;
        const code = document.getElementById('skillCodeEditorContent').value;

        API.updateSkillCode(id, code)
            .then(data => {
                if (data.success) {
                    this.closeCodeEditor();
                    this.loadSkills();
                } else {
                    alert('保存失败: ' + (data.error || '未知错误'));
                }
            });
    },

    // ========== 外部 Skill 管理 ==========

    /**
     * 加载外部 Skill 列表
     */
    async loadExternalSkills() {
        try {
            const result = await API.getExternalSkills();
            if (!result.success) {
                throw new Error(result.error || '数据格式错误');
            }
            this.externalSkills = result.data || [];
            this.renderExternalSkills();
        } catch (error) {
            console.error('加载外部 Skill 列表失败:', error);
            const container = document.getElementById('external-skills-list');
            if (container) {
                container.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">加载失败</div><div class="empty-text">' + error.message + '</div></div>';
            }
        }
    },

    /**
     * 渲染外部 Skill 卡片列表
     */
    renderExternalSkills() {
        const container = document.getElementById('external-skills-list');
        if (!container) return;

        if (this.externalSkills.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📦</div><div class="empty-title">暂无外部 Skill</div><div class="empty-text">将 Skill 放入 skills/ 目录，然后点击"刷新发现"按钮</div></div>';
            return;
        }

        let html = '';
        this.externalSkills.forEach((skill) => {
            const isHealthy = skill.healthy ?? (skill.health && skill.health.healthy) ?? false;
            const healthStatus = isHealthy ? 'healthy' : 'unhealthy';
            const healthText = isHealthy ? '健康' : '异常';
            const hasSecrets = skill.has_secrets;
            const scriptsCount = skill.scripts_count || (skill.scripts ? skill.scripts.length : 0);

            html += '<div class="skill-card external-skill-card">';
            html += '<div class="skill-header">';
            html += '<span class="skill-name">' + this.escapeHtml(skill.name) + '</span>';
            html += '<span class="skill-type external-tag">外部</span>';
            html += '</div>';
            html += '<div class="skill-version">v' + this.escapeHtml(skill.version || '1.0.0') + '</div>';
            html += '<div class="skill-desc">' + this.escapeHtml(skill.description || '无描述') + '</div>';
            html += '<div class="skill-meta">';
            html += '<span class="skill-path" title="' + this.escapeHtml(skill.skill_dir || '') + '">📁 ' + this.escapeHtml(skill.skill_dir || skill.source_dir || skill.path || '-') + '</span>';
            html += '</div>';
            html += '<div class="skill-status">';
            html += '<span class="status-indicator ' + healthStatus + '"></span>';
            html += '<span>' + healthText + '</span>';
            if (scriptsCount > 0) {
                html += '<span style="margin-left: auto;">📜 ' + scriptsCount + ' 个脚本</span>';
            }
            if (hasSecrets) {
                html += '<span style="margin-left: 8px;">🔑 密钥配置</span>';
            }
            html += '</div>';
            html += '<div class="skill-actions">';
            html += '<button class="btn btn-sm" onclick="showSkillDetail(\'' + this.escapeHtml(skill.name) + '\')">📖 查看文档</button>';
            if (hasSecrets) {
                html += '<button class="btn btn-sm" onclick="showSkillSecrets(\'' + this.escapeHtml(skill.name) + '\')">🔐 配置密钥</button>';
            }
            html += '</div>';
            html += '</div>';
        });

        container.innerHTML = html;
    },

    /**
     * 重新扫描外部 Skill
     */
    async reloadExternalSkills() {
        try {
            const container = document.getElementById('external-skills-list');
            if (container) {
                container.innerHTML = '<div class="loading"><div class="spinner"></div><p>扫描中...</p></div>';
            }
            
            const result = await API.reloadExternalSkills();
            if (result.success) {
                await this.loadExternalSkills();
            } else {
                throw new Error(result.error || '扫描失败');
            }
        } catch (error) {
            console.error('重新扫描外部 Skill 失败:', error);
            alert('扫描失败: ' + error.message);
            await this.loadExternalSkills();
        }
    },

    /**
     * 显示 Skill 文档模态框
     * @param {string} name - Skill 名称
     */
    async showSkillDetail(name) {
        try {
            // 查找 skill 信息
            const skill = this.externalSkills.find(s => s.name === name);
            const version = skill ? skill.version : '';

            // 获取文档内容
            const result = await API.getExternalSkillDetail(name);
            
            // 创建模态框
            const modal = document.createElement('div');
            modal.className = 'modal active';
            modal.id = 'skillDetailModal';
            modal.onclick = (e) => {
                if (e.target === modal) {
                    modal.remove();
                }
            };

            let contentHtml = '';
            if (result.success && result.data && result.data.content) {
                // 尝试使用 marked.js 渲染 Markdown
                if (typeof marked !== 'undefined') {
                    contentHtml = marked.parse(result.data.content);
                } else {
                    contentHtml = '<pre style="white-space: pre-wrap; word-break: break-word;">' + this.escapeHtml(result.data.content) + '</pre>';
                }
            } else {
                contentHtml = '<div class="empty-state"><div class="empty-icon">📄</div><div class="empty-title">暂无文档</div><div class="empty-text">该 Skill 没有提供 SKILL.md 文档</div></div>';
            }

            modal.innerHTML = '<div class="modal-content" style="max-width: 800px;">' +
                '<div class="modal-header">' +
                '<h2 class="modal-title">' + this.escapeHtml(name) + (version ? ' <small style="font-weight: normal; color: #586069;">v' + this.escapeHtml(version) + '</small>' : '') + '</h2>' +
                '</div>' +
                '<div class="modal-body" style="max-height: 60vh; overflow-y: auto;">' +
                '<div class="skill-doc-content">' + contentHtml + '</div>' +
                '</div>' +
                '<div class="modal-footer">' +
                '<button class="btn" onclick="document.getElementById(\'skillDetailModal\').remove()">关闭</button>' +
                '</div>' +
                '</div>';

            document.body.appendChild(modal);
        } catch (error) {
            console.error('获取 Skill 文档失败:', error);
            alert('获取文档失败: ' + error.message);
        }
    },

    /**
     * 显示 Skill Secrets 编辑模态框
     * @param {string} name - Skill 名称
     */
    async showSkillSecrets(name) {
        try {
            // 获取 secrets 列表
            const result = await API.getExternalSkillSecrets(name);
            
            // 创建模态框
            const modal = document.createElement('div');
            modal.className = 'modal active';
            modal.id = 'skillSecretsModal';
            modal.onclick = (e) => {
                if (e.target === modal) {
                    modal.remove();
                }
            };

            let secretsHtml = '';
            const secrets = (result.success && result.data) ? (result.data.secrets || result.data.keys || []) : [];
            const values = (result.success && result.data) ? (result.data.values || {}) : {};
            
            if (secrets.length > 0) {
                secretsHtml = '<div id="secretsFormContainer">';
                secrets.forEach((key, index) => {
                    const currentValue = values[key] || '';
                    secretsHtml += '<div class="form-group secret-row" data-index="' + index + '">' +
                        '<label class="form-label">' + this.escapeHtml(key) + '</label>' +
                        '<input type="password" class="form-input secret-input" data-key="' + this.escapeHtml(key) + '" value="' + this.escapeHtml(currentValue) + '" placeholder="输入密钥值...">' +
                        '</div>';
                });
                secretsHtml += '</div>';
            } else {
                secretsHtml = '<div id="secretsFormContainer">' +
                    '<p class="text-muted" style="margin-bottom: 16px;">暂无已配置的密钥。点击下方按钮添加新密钥。</p>' +
                    '</div>';
            }

            modal.innerHTML = '<div class="modal-content" style="max-width: 600px;">' +
                '<div class="modal-header">' +
                '<h2 class="modal-title">🔐 配置 ' + this.escapeHtml(name) + ' 密钥</h2>' +
                '</div>' +
                '<div class="modal-body">' +
                secretsHtml +
                '<button class="btn btn-sm" onclick="SkillsModule.addSecretRow()" style="margin-top: 8px;">+ 添加密钥</button>' +
                '</div>' +
                '<div class="modal-footer">' +
                '<button class="btn" onclick="document.getElementById(\'skillSecretsModal\').remove()">取消</button>' +
                '<button class="btn btn-primary" onclick="SkillsModule.saveSkillSecrets(\'' + this.escapeHtml(name) + '\')">保存</button>' +
                '</div>' +
                '</div>';

            document.body.appendChild(modal);
        } catch (error) {
            console.error('获取 Skill Secrets 失败:', error);
            alert('获取密钥配置失败: ' + error.message);
        }
    },

    /**
     * 添加新的 Secret 输入行
     */
    addSecretRow() {
        const container = document.getElementById('secretsFormContainer');
        if (!container) return;

        const index = container.querySelectorAll('.secret-row').length;
        const row = document.createElement('div');
        row.className = 'form-group secret-row new-secret-row';
        row.dataset.index = index;
        row.innerHTML = '<div style="display: flex; gap: 8px;">' +
            '<input type="text" class="form-input secret-key-input" placeholder="密钥名称 (KEY)" style="flex: 1;">' +
            '<input type="password" class="form-input secret-value-input" placeholder="密钥值" style="flex: 2;">' +
            '<button class="btn btn-sm" onclick="this.parentElement.parentElement.remove()" style="color: #d73a49;">删除</button>' +
            '</div>';
        container.appendChild(row);
    },

    /**
     * 保存 Skill Secrets
     * @param {string} name - Skill 名称
     */
    async saveSkillSecrets(name) {
        try {
            const secrets = {};
            
            // 收集已有的 secret 值
            document.querySelectorAll('#secretsFormContainer .secret-input').forEach(input => {
                const key = input.dataset.key;
                const value = input.value;
                if (key && value) {
                    secrets[key] = value;
                }
            });

            // 收集新添加的 secret
            document.querySelectorAll('#secretsFormContainer .new-secret-row').forEach(row => {
                const keyInput = row.querySelector('.secret-key-input');
                const valueInput = row.querySelector('.secret-value-input');
                if (keyInput && valueInput && keyInput.value && valueInput.value) {
                    secrets[keyInput.value] = valueInput.value;
                }
            });

            const result = await API.updateExternalSkillSecrets(name, secrets);
            if (result.success) {
                alert('密钥配置已保存');
                document.getElementById('skillSecretsModal').remove();
                await this.loadExternalSkills();
            } else {
                throw new Error(result.error || '保存失败');
            }
        } catch (error) {
            console.error('保存 Skill Secrets 失败:', error);
            alert('保存密钥失败: ' + error.message);
        }
    },

    /**
     * HTML 转义工具方法
     * @param {string} text - 待转义的文本
     * @returns {string} 转义后的文本
     */
    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};
