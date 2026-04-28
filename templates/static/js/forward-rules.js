/**
 * 转发规则管理模块
 * 实现转发规则的增删改查和测试功能
 */

// 存储当前规则列表
let forwardRules = [];

/**
 * 加载转发规则列表
 */
async function loadForwardRules() {
    console.log('📋 加载转发规则...');
    const container = document.getElementById('forwardRulesList');

    try {
        container.innerHTML = `
            <div class="loading">
                <div class="spinner"></div>
                <p>加载中...</p>
            </div>
        `;

        const result = await API.getForwardRules();

        if (result.success) {
            forwardRules = result.data || [];
            renderForwardRules(forwardRules);
            console.log('✅ 加载了', forwardRules.length, '条规则');
        } else {
            container.innerHTML = `
                <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <p>❌ 加载失败: ${escapeHtml(result.error || '未知错误')}</p>
                    <button class="btn" onclick="loadForwardRules()" style="margin-top: 10px;">重试</button>
                </div>
            `;
        }
    } catch (error) {
        console.error('❌ 加载转发规则失败:', error);
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                <p>❌ 加载失败: ${escapeHtml(error.message || String(error))}</p>
                <button class="btn" onclick="loadForwardRules()" style="margin-top: 10px;">重试</button>
            </div>
        `;
    }
}

/**
 * 渲染规则列表
 * @param {Array} rules - 规则数组
 */
function renderForwardRules(rules) {
    const container = document.getElementById('forwardRulesList');

    if (!rules || rules.length === 0) {
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 60px; color: var(--text-secondary);">
                <div style="font-size: 48px; margin-bottom: 20px;">📭</div>
                <p style="font-size: 16px; margin-bottom: 10px;">暂无转发规则</p>
                <p style="font-size: 14px;">点击"新增规则"按钮创建第一条转发规则</p>
            </div>
        `;
        return;
    }

    // 按优先级排序（高优先级在前）
    const sortedRules = [...rules].sort((a, b) => (b.priority || 0) - (a.priority || 0));

    let html = '<div class="rules-list" style="display: flex; flex-direction: column; gap: 15px;">';

    sortedRules.forEach(rule => {
        html += renderRuleCard(rule);
    });

    html += '</div>';
    container.innerHTML = html;
}

/**
 * 渲染单条规则卡片
 * @param {Object} rule - 规则对象
 */
function renderRuleCard(rule) {
    const importanceText = escapeHtml(formatImportance(rule.match_importance));
    const duplicateText = escapeHtml(formatDuplicateStatus(rule.match_duplicate));
    const sourceText = escapeHtml(rule.match_source || '全部');
    const targetTypeText = escapeHtml(formatTargetType(rule.target_type));

    const isEnabled = rule.enabled;
    const cardBorder = isEnabled ? 'border-left: 4px solid var(--primary);' : 'border-left: 4px solid #cbd5e1;';
    const cardOpacity = isEnabled ? 'opacity: 1;' : 'opacity: 0.65; background: #f8fafc;';
    const titleColor = isEnabled ? 'color: var(--text-main);' : 'color: var(--text-muted); text-decoration: line-through;';

    return `
        <div class="rule-card" style="
            background: #ffffff;
            border: 1px solid #cbd5e1;
            ${cardBorder}
            border-radius: var(--radius-lg);
            padding: 1.25rem 1.5rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -1px rgba(0,0,0,0.03);
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            ${cardOpacity}
        ">
            <div class="rule-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <!-- Modern Toggle Switch -->
                    <label class="switch" style="position: relative; display: inline-block; width: 44px; height: 24px; margin: 0;">
                        <input type="checkbox" ${isEnabled ? 'checked' : ''} onchange="toggleRule(${rule.id}, this.checked)" style="opacity: 0; width: 0; height: 0;">
                        <span class="slider" style="
                            position: absolute;
                            cursor: pointer;
                            top: 0; left: 0; right: 0; bottom: 0;
                            background-color: ${isEnabled ? 'var(--primary)' : '#cbd5e1'};
                            transition: 0.3s;
                            border-radius: 24px;
                            box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);
                        ">
                            <span style="
                                position: absolute;
                                content: '';
                                height: 18px; width: 18px;
                                left: ${isEnabled ? '23px' : '3px'};
                                bottom: 3px;
                                background-color: white;
                                transition: 0.3s;
                                border-radius: 50%;
                                box-shadow: 0 1px 2px rgba(0,0,0,0.2);
                            "></span>
                        </span>
                    </label>
                    <span style="font-weight: 600; font-size: 1.15rem; ${titleColor}">${escapeHtml(rule.name)}</span>
                    ${!isEnabled ? '<span class="badge" style="background: #f1f5f9; color: #64748b; font-size: 0.75rem; border: 1px solid #e2e8f0;">已停用</span>' : ''}
                </div>
                <span style="
                    background: #f1f5f9;
                    padding: 4px 12px;
                    border-radius: 9999px;
                    font-size: 0.85rem;
                    font-weight: 600;
                    color: #475569;
                    border: 1px solid #cbd5e1;
                ">⬆️ 优先级: ${rule.priority || 0}</span>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem;">
                <!-- 匹配条件区 -->
                <div class="rule-conditions" style="font-size: 0.95rem; color: #334155; background: #f8fafc; padding: 1.25rem; border-radius: 8px; border: 1px dashed #cbd5e1;">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🎯 命中条件</div>
                    <div style="margin-bottom: 0.5rem;"><strong>重要性:</strong> ${importanceText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>告警状态:</strong> ${duplicateText}</div>
                    <div><strong>事件来源:</strong> ${sourceText}</div>
                </div>

                <!-- 转发目标区 -->
                <div class="rule-target" style="font-size: 0.95rem; color: #334155; background: #f0fdf4; padding: 1.25rem; border-radius: 8px; border: 1px dashed #86efac;">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: #059669; margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🚀 动作执行</div>
                    <div style="margin-bottom: 0.75rem;">
                        <strong>推送到:</strong> ${targetTypeText}
                        ${rule.target_name ? `(${escapeHtml(rule.target_name)})` : ''}
                    </div>
                    <div style="word-break: break-all; color: #0f172a; font-family: 'Fira Code', monospace; font-size: 0.85rem; background: #ffffff; padding: 0.75rem; border-radius: 6px; border: 1px solid #d1fae5; box-shadow: inset 0 1px 2px rgba(0,0,0,0.02);">
                        ${escapeHtml(rule.target_url || '-')}
                    </div>
                    ${rule.stop_on_match ? '<div style="margin-top: 0.75rem; color: #d97706; font-weight: 600; font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem;"><span>🛑</span> 命中此规则后，停止匹配后续规则</div>' : ''}
                </div>
            </div>

            <div class="rule-actions" style="display: flex; gap: 0.75rem; justify-content: flex-end; padding-top: 1.25rem; border-top: 1px solid #e2e8f0;">
                <button class="btn" onclick="testRule(${rule.id})" style="color: #4338ca; border-color: #c7d2fe; background: #e0e7ff; font-weight: 600;">
                    🧪 测试通道
                </button>
                <button class="btn" onclick="showRuleForm(${rule.id})" style="font-weight: 600;">
                    ✏️ 编辑
                </button>
                <button class="btn" onclick="deleteRule(${rule.id})" style="color: #dc2626; border-color: #fecaca; background: #fef2f2; font-weight: 600;">
                    🗑️ 删除
                </button>
            </div>
        </div>
    `;
}
/**
 * 格式化重要性显示
 */
function formatImportance(importance) {
    if (!importance) return '全部';
    const map = { 'high': '高', 'medium': '中', 'low': '低' };
    return importance.split(',').map(i => map[i.trim()] || i.trim()).join(',') || '全部';
}

/**
 * 格式化重复状态显示
 */
function formatDuplicateStatus(status) {
    const map = {
        'all': '全部',
        'new': '新告警',
        'duplicate': '窗口内重复',
        'beyond_window': '窗口外重复'
    };
    return map[status] || status || '全部';
}

/**
 * 格式化目标类型显示
 */
function formatTargetType(type) {
    const map = {
        'feishu': '飞书',
        'openclaw': 'OpenClaw',
        'webhook': 'Webhook'
    };
    return map[type] || type || '未知';
}

/**
 * HTML 转义
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * 显示规则表单（新增或编辑）
 * @param {number} ruleId - 规则 ID，不传表示新增
 */
function showRuleForm(ruleId) {
    const modal = document.getElementById('ruleFormModal');
    const title = document.getElementById('ruleFormTitle');

    // 重置表单
    document.getElementById('ruleFormId').value = '';
    document.getElementById('ruleFormName').value = '';
    document.getElementById('ruleFormPriority').value = '10';
    document.getElementById('ruleFormImportanceHigh').checked = false;
    document.getElementById('ruleFormImportanceMedium').checked = false;
    document.getElementById('ruleFormImportanceLow').checked = false;
    document.getElementById('ruleFormDuplicate').value = 'all';
    document.getElementById('ruleFormSource').value = '';
    document.getElementById('ruleFormTargetType').value = 'feishu';
    document.getElementById('ruleFormTargetUrl').value = '';
    document.getElementById('ruleFormTargetName').value = '';
    document.getElementById('ruleFormStopOnMatch').checked = false;
    document.getElementById('ruleFormEnabled').checked = true;

    // 显示目标地址输入框
    document.getElementById('ruleFormTargetUrlGroup').style.display = 'block';

    if (ruleId) {
        // 编辑模式
        title.textContent = '编辑转发规则';
        const rule = forwardRules.find(r => r.id === ruleId);
        if (rule) {
            document.getElementById('ruleFormId').value = rule.id;
            document.getElementById('ruleFormName').value = rule.name || '';
            document.getElementById('ruleFormPriority').value = rule.priority || 10;

            // 设置重要性复选框
            if (rule.match_importance) {
                const importances = rule.match_importance.split(',').map(s => s.trim());
                document.getElementById('ruleFormImportanceHigh').checked = importances.includes('high');
                document.getElementById('ruleFormImportanceMedium').checked = importances.includes('medium');
                document.getElementById('ruleFormImportanceLow').checked = importances.includes('low');
            }

            document.getElementById('ruleFormDuplicate').value = rule.match_duplicate || 'all';
            document.getElementById('ruleFormSource').value = rule.match_source || '';
            document.getElementById('ruleFormTargetType').value = rule.target_type || 'feishu';
            document.getElementById('ruleFormTargetUrl').value = rule.target_url || '';
            document.getElementById('ruleFormTargetName').value = rule.target_name || '';
            document.getElementById('ruleFormStopOnMatch').checked = rule.stop_on_match || false;
            document.getElementById('ruleFormEnabled').checked = rule.enabled !== false;

            // 根据目标类型显示/隐藏地址输入框
            onTargetTypeChange();
        }
    } else {
        // 新增模式
        title.textContent = '新增转发规则';
    }

    modal.classList.add('active');
}

/**
 * 关闭规则表单
 */
function closeRuleForm() {
    document.getElementById('ruleFormModal').classList.remove('active');
}

/**
 * 目标类型改变时的处理
 */
function onTargetTypeChange() {
    const targetType = document.getElementById('ruleFormTargetType').value;
    const urlGroup = document.getElementById('ruleFormTargetUrlGroup');

    // OpenClaw 类型不需要填写地址
    if (targetType === 'openclaw') {
        urlGroup.style.display = 'none';
    } else {
        urlGroup.style.display = 'block';
    }
}

/**
 * 保存规则
 */
async function saveRule() {
    // 获取表单数据
    const ruleId = document.getElementById('ruleFormId').value;
    const name = document.getElementById('ruleFormName').value.trim();
    const priority = parseInt(document.getElementById('ruleFormPriority').value) || 10;
    const targetType = document.getElementById('ruleFormTargetType').value;
    const targetUrl = document.getElementById('ruleFormTargetUrl').value.trim();
    const targetName = document.getElementById('ruleFormTargetName').value.trim();

    // 验证必填字段
    if (!name) {
        alert('请输入规则名称');
        return;
    }

    if (targetType !== 'openclaw' && !targetUrl) {
        alert('请输入目标地址');
        return;
    }

    // 收集重要性选项
    const importances = [];
    if (document.getElementById('ruleFormImportanceHigh').checked) importances.push('high');
    if (document.getElementById('ruleFormImportanceMedium').checked) importances.push('medium');
    if (document.getElementById('ruleFormImportanceLow').checked) importances.push('low');

    // 构建规则数据
    const ruleData = {
        name: name,
        enabled: document.getElementById('ruleFormEnabled').checked,
        priority: priority,
        match_importance: importances.join(','),
        match_duplicate: document.getElementById('ruleFormDuplicate').value,
        match_source: document.getElementById('ruleFormSource').value.trim(),
        target_type: targetType,
        target_url: targetType === 'openclaw' ? '' : targetUrl,
        target_name: targetName,
        stop_on_match: document.getElementById('ruleFormStopOnMatch').checked
    };

    try {
        let result;
        if (ruleId) {
            // 更新规则
            console.log('📝 更新规则:', ruleId, ruleData);
            result = await API.updateForwardRule(ruleId, ruleData);
        } else {
            // 创建规则
            console.log('➕ 创建规则:', ruleData);
            result = await API.createForwardRule(ruleData);
        }

        if (result.success) {
            alert(ruleId ? '✅ 规则更新成功' : '✅ 规则创建成功');
            closeRuleForm();
            loadForwardRules();
        } else {
            alert('❌ 保存失败: ' + (result.error || '未知错误'));
        }
    } catch (error) {
        console.error('❌ 保存规则失败:', error);
        alert('❌ 保存失败: ' + error.message);
    }
}

/**
 * 启用/禁用规则
 * @param {number} id - 规则 ID
 * @param {boolean} enabled - 是否启用
 */
async function toggleRule(id, enabled) {
    try {
        console.log(enabled ? '✅ 启用规则:' : '⏸️ 禁用规则:', id);
        const result = await API.updateForwardRule(id, { enabled: enabled });

        if (result.success) {
            // 更新本地数据
            const rule = forwardRules.find(r => r.id === id);
            if (rule) {
                rule.enabled = enabled;
            }
            // 重新渲染
            renderForwardRules(forwardRules);
        } else {
            alert('❌ 操作失败: ' + (result.error || '未知错误'));
            loadForwardRules(); // 重新加载以恢复状态
        }
    } catch (error) {
        console.error('❌ 切换规则状态失败:', error);
        alert('❌ 操作失败: ' + error.message);
        loadForwardRules();
    }
}

/**
 * 删除规则
 * @param {number} id - 规则 ID
 */
async function deleteRule(id) {
    const rule = forwardRules.find(r => r.id === id);
    const ruleName = rule ? rule.name : '该规则';

    if (!confirm(`确定要删除规则"${ruleName}"吗？\n\n此操作不可撤销。`)) {
        return;
    }

    try {
        console.log('🗑️ 删除规则:', id);
        const result = await API.deleteForwardRule(id);

        if (result.success) {
            alert('✅ 规则已删除');
            loadForwardRules();
        } else {
            alert('❌ 删除失败: ' + (result.error || '未知错误'));
        }
    } catch (error) {
        console.error('❌ 删除规则失败:', error);
        alert('❌ 删除失败: ' + error.message);
    }
}

/**
 * 测试规则
 * @param {number} id - 规则 ID
 */
async function testRule(id) {
    const rule = forwardRules.find(r => r.id === id);
    const ruleName = rule ? rule.name : '该规则';

    if (!confirm(`确定要测试规则"${ruleName}"吗？\n\n将发送测试消息到目标地址。`)) {
        return;
    }

    try {
        console.log('🧪 测试规则:', id);
        const result = await API.testForwardRule(id);

        if (result.success) {
            alert('✅ 测试成功！\n\n' + (result.message || '测试消息已发送'));
        } else {
            alert('❌ 测试失败: ' + (result.error || '未知错误'));
        }
    } catch (error) {
        console.error('❌ 测试规则失败:', error);
        alert('❌ 测试失败: ' + error.message);
    }
}

// 导出模块（用于 dashboard.js 初始化检测）
const ForwardRulesModule = {
    init: function() {
        console.log('📋 转发规则模块初始化');
    },
    loadRules: loadForwardRules
};
