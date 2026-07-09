/**
 * Forward Rule Management Module
 * Implements create, read, update, delete, and test functionality for forward rules
 */

// Stores the current list of rules
let forwardRules = [];

/**
 * Load the list of forward rules
 */
async function loadForwardRules() {
    console.log('📋 Loading forward rules...');
    const container = document.getElementById('forwardRulesList');

    try {
        container.innerHTML = `
            <div class="loading">
                <div class="spinner"></div>
                <p>${t('common.loading')}</p>
            </div>
        `;

        const tokenStatus = typeof API.getTokenStatus === 'function' ? API.getTokenStatus() : { write: false };
        const result = await API.getForwardRules({ includeSensitive: !!tokenStatus.write });

        if (result.success) {
            forwardRules = result.data || [];
            renderForwardRules(forwardRules);
            console.log('✅ Loaded', forwardRules.length, 'rules');
        } else {
            container.innerHTML = `
                <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <p>❌ ${t('common.loadFailed')}: ${escapeHtml(result.error || t('common.unknownError'))}</p>
                    <button class="btn" onclick="loadForwardRules()" style="margin-top: 10px;">${t('common.retry')}</button>
                </div>
            `;
        }
    } catch (error) {
        console.error('❌ Failed to load forward rules:', error);
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                <p>❌ ${t('common.loadFailed')}: ${escapeHtml(error.message || String(error))}</p>
                <button class="btn" onclick="loadForwardRules()" style="margin-top: 10px;">${t('common.retry')}</button>
            </div>
        `;
    }
}

/**
 * Render the rule list
 * @param {Array} rules - array of rules
 */
function renderForwardRules(rules) {
    const container = document.getElementById('forwardRulesList');

    if (!rules || rules.length === 0) {
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 60px; color: var(--text-secondary);">
                <div style="font-size: 48px; margin-bottom: 20px;">📭</div>
                <p style="font-size: 16px; margin-bottom: 10px;">${t('rules.empty.title')}</p>
                <p style="font-size: 14px;">${t('rules.empty.text')}</p>
            </div>
        `;
        return;
    }

    // Sort by priority (higher priority first)
    const sortedRules = [...rules].sort((a, b) => (b.priority || 0) - (a.priority || 0));

    let html = '<div class="rules-list" style="display: flex; flex-direction: column; gap: 15px;">';

    sortedRules.forEach(rule => {
        html += renderRuleCard(rule);
    });

    html += '</div>';
    container.innerHTML = html;
}

/**
 * Render a single rule card
 * @param {Object} rule - rule object
 */
function renderRuleCard(rule) {
    const importanceText = escapeHtml(formatImportance(rule.match_importance));
    const duplicateText = escapeHtml(formatDuplicateStatus(rule.match_duplicate));
    const sourceText = escapeHtml(rule.match_source || t('common.all'));
    const projectText = escapeHtml(rule.match_project || t('common.all'));
    const regionText = escapeHtml(rule.match_region || t('common.all'));
    const environmentText = escapeHtml(rule.match_environment || t('common.all'));
    const targetTypeText = escapeHtml(formatTargetType(rule.target_type));

    const isEnabled = rule.enabled;
    const cardBorder = isEnabled ? 'border-left: 4px solid var(--primary);' : 'border-left: 4px solid #cbd5e1;';
    const cardOpacity = isEnabled ? 'opacity: 1;' : 'opacity: 0.65; background: #f8fafc;';
    const titleColor = isEnabled ? 'color: var(--text-main);' : 'color: var(--text-muted); text-decoration: line-through;';

    // ROI: how many alerts this rule has matched. A high count = it's carrying
    // load; an enabled rule with zero matches is a "zombie" rule worth reviewing.
    const hits = Number(rule.hit_count || 0);
    const hitBadge = hits > 0
        ? '<span class="badge badge-success" title="' + escapeHtml(t('rules.roi.tooltip')) + '">🎯 ' +
            t('rules.roi.hits', { count: hits }) + '</span>'
        : (isEnabled
            ? '<span class="badge badge-danger" title="' + escapeHtml(t('rules.roi.zombieTooltip')) + '">⚠️ ' +
                t('rules.roi.zombie') + '</span>'
            : '<span class="badge badge-outline">' + t('rules.roi.hits', { count: 0 }) + '</span>');
    const lastMatched = rule.last_matched_at
        ? '<div style="margin-top: 0.75rem; color: #64748b; font-size: 0.85rem;">' +
            t('rules.roi.lastMatched', { time: (typeof formatTime === 'function' ? formatTime(rule.last_matched_at) : rule.last_matched_at) }) + '</div>'
        : '';

    return `
        <div class="rule-card" style="
            background: var(--bg-surface);
            border: 1px solid var(--border);
            ${cardBorder}
            border-radius: var(--radius-lg);
            padding: 1.25rem 1.5rem;
            margin-bottom: 1.5rem;
            box-shadow: var(--shadow-sm);
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
                            background-color: ${isEnabled ? 'var(--primary)' : 'var(--border)'};
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
                                background-color: var(--bg-surface);
                                transition: 0.3s;
                                border-radius: 50%;
                                box-shadow: 0 1px 2px rgba(0,0,0,0.2);
                            "></span>
                        </span>
                    </label>
                    <span style="font-weight: 600; font-size: 1.15rem; ${titleColor}">${escapeHtml(rule.name)}</span>
                    ${!isEnabled ? '<span class="badge" style="background: var(--bg-subtle); color: var(--text-muted); font-size: 0.75rem; border: 1px solid var(--border);">' + t('rules.card.disabled') + '</span>' : ''}
                    ${hitBadge}
                </div>
                <span style="
                    background: var(--bg-subtle);
                    padding: 4px 12px;
                    border-radius: 9999px;
                    font-size: 0.85rem;
                    font-weight: 600;
                    color: var(--text-secondary);
                    border: 1px solid var(--border);
                ">⬆️ ${t('rules.card.priority', { n: rule.priority || 0 })}</span>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem;">
                <!-- Match conditions area -->
                <div class="rule-conditions" style="font-size: 0.95rem; color: var(--text-secondary); background: var(--bg-subtle); padding: 1.25rem; border-radius: 8px; border: 1px dashed var(--border);">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🎯 ${t('rules.card.matchConditions')}</div>
                    ${rule.match_event_type ? '<div style="margin-bottom:0.5rem;"><strong>' + t('rules.card.eventType') + ':</strong> ' + (function() { var types = rule.match_event_type.split(',').map(function(et) { var m = { webhook_forward: t('rules.evtType.webhook_forward'), manual_forward: t('rules.evtType.manual_forward'), ai_error: t('rules.evtType.ai_error'), ai_degraded: t('rules.evtType.ai_degraded'), deep_analysis: t('rules.evtType.deep_analysis'), outbox_exhausted: t('rules.evtType.outbox_exhausted'), rule_test: t('rules.evtType.rule_test') }; return '<span style="display:inline-block;background:var(--primary-bg);color:var(--primary);padding:1px 6px;border-radius:4px;font-size:0.7rem;font-weight:600;margin-right:4px;">' + (m[et.trim()] || et.trim()) + '</span>'; }); return types.join(''); })() + '</div>' : ''}
                    <div style="margin-bottom: 0.5rem;"><strong>${t('rules.card.importance')}:</strong> ${importanceText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>${t('rules.card.alertStatus')}:</strong> ${duplicateText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>${t('rules.card.source')}:</strong> ${sourceText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>${t('rules.card.project')}:</strong> ${projectText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>${t('rules.card.region')}:</strong> ${regionText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>${t('rules.card.environment')}:</strong> ${environmentText}</div>
                    ${rule.match_payload ? '<div><strong>' + t('rules.card.payload') + ':</strong> <code style="font-size:0.8rem;">' + escapeHtml(rule.match_payload) + '</code></div>' : ''}
                </div>

                <!-- Forward target area -->
                <div class="rule-target" style="font-size: 0.95rem; color: var(--text-secondary); background: var(--success-bg); padding: 1.25rem; border-radius: 8px; border: 1px dashed rgba(5,150,105,0.3);">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: var(--success); margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🚀 ${t('rules.card.action')}</div>
                    <div style="margin-bottom: 0.75rem;">
                        <strong>${t('rules.card.pushTo')}:</strong> ${targetTypeText}
                        ${rule.target_name ? `(${escapeHtml(rule.target_name)})` : ''}
                    </div>
                    <div style="word-break: break-all; color: var(--text-main); font-family: var(--font-mono); font-size: 0.85rem; background: var(--bg-surface); padding: 0.75rem; border-radius: 6px; border: 1px solid var(--border); box-shadow: inset 0 1px 2px rgba(0,0,0,0.02);">
                        ${escapeHtml(rule.target_url || '-')}
                    </div>
                    ${rule.stop_on_match ? '<div style="margin-top: 0.75rem; color: var(--warning); font-weight: 600; font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem;"><span>🛑</span> ' + t('rules.card.stopOnMatch') + '</div>' : ''}
                </div>
            </div>

            ${lastMatched}

            <div class="rule-actions" style="display: flex; gap: 0.75rem; justify-content: flex-end; padding-top: 1.25rem; border-top: 1px solid var(--border);">
                <button class="btn" onclick="testRule(${rule.id})" style="color: var(--primary); border-color: var(--primary-light); background: var(--primary-bg); font-weight: 600;">
                    🧪 ${t('rules.action.test')}
                </button>
                <button class="btn" onclick="showRuleForm(${rule.id})" style="font-weight: 600;">
                    ✏️ ${t('rules.action.edit')}
                </button>
                <button class="btn" onclick="deleteRule(${rule.id})" style="color: var(--danger); border-color: rgba(225,29,72,0.2); background: var(--danger-bg); font-weight: 600;">
                    🗑️ ${t('rules.action.delete')}
                </button>
            </div>
        </div>
    `;
}
/**
 * Format importance display
 */
function formatImportance(importance) {
    if (!importance) return t('common.all');
    const map = { 'high': t('common.high'), 'medium': t('common.medium'), 'low': t('common.low') };
    return importance.split(',').map(i => map[i.trim()] || i.trim()).join(',') || t('common.all');
}

/**
 * Format duplicate-status display
 */
function formatDuplicateStatus(status) {
    const map = {
        'all': t('common.all'),
        'new': t('rules.dup.new'),
        'duplicate': t('rules.dup.duplicate')
    };
    return map[status] || status || t('common.all');
}

/**
 * Format target-type display
 */
function formatTargetType(type) {
    const map = {
        'feishu': t('rules.targetType.feishu'),
        'openclaw': t('rules.targetType.openclaw'),
        'webhook': t('rules.targetType.webhook')
    };
    return map[type] || type || t('rules.targetType.unknown');
}

/**
 * HTML escape
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Show the rule form (create or edit)
 * @param {number} ruleId - rule ID; omit for create
 */
function showRuleForm(ruleId) {
    const modal = document.getElementById('ruleFormModal');
    const title = document.getElementById('ruleFormTitle');

    // Reset the form
    document.getElementById('ruleFormId').value = '';
    document.getElementById('ruleFormName').value = '';
    document.getElementById('ruleFormPriority').value = '10';
    // Reset event-type checkboxes
    ['ruleFormEvtForward', 'ruleFormEvtManual', 'ruleFormEvtAIError', 'ruleFormEvtAIDegraded', 'ruleFormEvtDeep', 'ruleFormEvtExhausted'].forEach(function(id) {
        document.getElementById(id).checked = false;
    });
    document.getElementById('ruleFormImportanceHigh').checked = false;
    document.getElementById('ruleFormImportanceMedium').checked = false;
    document.getElementById('ruleFormImportanceLow').checked = false;
    document.getElementById('ruleFormDuplicate').value = 'all';
    document.getElementById('ruleFormSource').value = '';
    document.getElementById('ruleFormProject').value = '';
    document.getElementById('ruleFormRegion').value = '';
    document.getElementById('ruleFormEnvironment').value = '';
    document.getElementById('ruleFormPayload').value = '';
    document.getElementById('ruleFormTargetType').value = 'feishu';
    document.getElementById('ruleFormTargetUrl').value = '';
    document.getElementById('ruleFormTargetName').value = '';
    document.getElementById('ruleFormStopOnMatch').checked = false;
    document.getElementById('ruleFormEnabled').checked = true;

    // Show the target address input field
    document.getElementById('ruleFormTargetUrlGroup').style.display = 'block';

    if (ruleId) {
        // Edit mode
        title.textContent = t('rule.editTitle');
        const rule = forwardRules.find(r => r.id === ruleId);
        if (rule) {
            if (rule.target_url_sensitive === false) {
                alert(t('rules.alert.editNeedsWriteKey'));
                if (typeof openAuthModal === 'function') {
                    openAuthModal();
                }
                return;
            }
            document.getElementById('ruleFormId').value = rule.id;
            document.getElementById('ruleFormName').value = rule.name || '';
            document.getElementById('ruleFormPriority').value = rule.priority || 10;

            // Set event-type checkboxes
            var eventTypes = (rule.match_event_type || '').split(',').map(function(s) { return s.trim(); });
            var evtCheckIds = {
                'webhook_forward': 'ruleFormEvtForward',
                'manual_forward': 'ruleFormEvtManual',
                'ai_error': 'ruleFormEvtAIError',
                'ai_degraded': 'ruleFormEvtAIDegraded',
                'deep_analysis': 'ruleFormEvtDeep',
                'outbox_exhausted': 'ruleFormEvtExhausted'
            };
            eventTypes.forEach(function(et) {
                var id = evtCheckIds[et];
                if (id) document.getElementById(id).checked = true;
            });

            // Set importance checkboxes
            if (rule.match_importance) {
                const importances = rule.match_importance.split(',').map(s => s.trim());
                document.getElementById('ruleFormImportanceHigh').checked = importances.includes('high');
                document.getElementById('ruleFormImportanceMedium').checked = importances.includes('medium');
                document.getElementById('ruleFormImportanceLow').checked = importances.includes('low');
            }

            document.getElementById('ruleFormDuplicate').value = rule.match_duplicate || 'all';
            document.getElementById('ruleFormSource').value = rule.match_source || '';
            document.getElementById('ruleFormProject').value = rule.match_project || '';
            document.getElementById('ruleFormRegion').value = rule.match_region || '';
            document.getElementById('ruleFormEnvironment').value = rule.match_environment || '';
            document.getElementById('ruleFormPayload').value = rule.match_payload || '';
            document.getElementById('ruleFormTargetType').value = rule.target_type || 'feishu';
            document.getElementById('ruleFormTargetUrl').value = rule.target_url || '';
            document.getElementById('ruleFormTargetName').value = rule.target_name || '';
            document.getElementById('ruleFormStopOnMatch').checked = rule.stop_on_match || false;
            document.getElementById('ruleFormEnabled').checked = rule.enabled !== false;

            // Show/hide the address input field based on the target type
            onTargetTypeChange();
        }
    } else {
        // Create mode
        title.textContent = t('rule.addTitle');
    }

    modal.classList.add('active');
}

/**
 * Close the rule form
 */
function closeRuleForm() {
    document.getElementById('ruleFormModal').classList.remove('active');
}

/**
 * Handle target-type changes
 */
function onTargetTypeChange() {
    const targetType = document.getElementById('ruleFormTargetType').value;
    const urlGroup = document.getElementById('ruleFormTargetUrlGroup');

    // The OpenClaw type does not require an address
    if (targetType === 'openclaw') {
        urlGroup.style.display = 'none';
    } else {
        urlGroup.style.display = 'block';
    }
}

/**
 * Save the rule
 */
async function saveRule() {
    // Get form data
    const ruleId = document.getElementById('ruleFormId').value;
    const name = document.getElementById('ruleFormName').value.trim();
    const priority = parseInt(document.getElementById('ruleFormPriority').value) || 10;
    const targetType = document.getElementById('ruleFormTargetType').value;
    const targetUrl = document.getElementById('ruleFormTargetUrl').value.trim();
    const targetName = document.getElementById('ruleFormTargetName').value.trim();

    // Validate required fields
    if (!name) {
        alert(t('rules.alert.nameRequired'));
        return;
    }

    if (targetType !== 'openclaw' && !targetUrl) {
        alert(t('rules.alert.targetUrlRequired'));
        return;
    }

    // Collect importance options
    const importances = [];
    if (document.getElementById('ruleFormImportanceHigh').checked) importances.push('high');
    if (document.getElementById('ruleFormImportanceMedium').checked) importances.push('medium');
    if (document.getElementById('ruleFormImportanceLow').checked) importances.push('low');

    // Build rule data
    const ruleData = {
        name: name,
        enabled: document.getElementById('ruleFormEnabled').checked,
        priority: priority,
        match_event_type: [
            'ruleFormEvtForward', 'ruleFormEvtManual', 'ruleFormEvtAIError',
            'ruleFormEvtAIDegraded', 'ruleFormEvtDeep', 'ruleFormEvtExhausted'
        ].filter(function(id) { return document.getElementById(id).checked; })
         .map(function(id) { return document.getElementById(id).value; }).join(','),
        match_importance: importances.join(','),
        match_duplicate: document.getElementById('ruleFormDuplicate').value,
        match_source: document.getElementById('ruleFormSource').value.trim(),
        match_project: document.getElementById('ruleFormProject').value.trim(),
        match_region: document.getElementById('ruleFormRegion').value.trim(),
        match_environment: document.getElementById('ruleFormEnvironment').value.trim(),
        match_payload: document.getElementById('ruleFormPayload').value.trim(),
        target_type: targetType,
        target_url: targetType === 'openclaw' ? '' : targetUrl,
        target_name: targetName,
        stop_on_match: document.getElementById('ruleFormStopOnMatch').checked
    };

    try {
        let result;
        if (ruleId) {
            // Update rule
            console.log('📝 Updating rule:', ruleId, ruleData);
            result = await API.updateForwardRule(ruleId, ruleData);
        } else {
            // Create rule
            console.log('➕ Creating rule:', ruleData);
            result = await API.createForwardRule(ruleData);
        }

        if (result.success) {
            alert(ruleId ? '✅ ' + t('rules.alert.updateSuccess') : '✅ ' + t('rules.alert.createSuccess'));
            closeRuleForm();
            loadForwardRules();
        } else {
            alert('❌ ' + t('rules.alert.saveFailed') + ': ' + (result.error || t('common.unknownError')));
        }
    } catch (error) {
        console.error('❌ Failed to save rule:', error);
        alert('❌ ' + t('rules.alert.saveFailed') + ': ' + error.message);
    }
}

/**
 * Enable/disable a rule
 * @param {number} id - rule ID
 * @param {boolean} enabled - whether to enable
 */
async function toggleRule(id, enabled) {
    try {
        console.log(enabled ? '✅ Enabling rule:' : '⏸️ Disabling rule:', id);
        const result = await API.updateForwardRule(id, { enabled: enabled });

        if (result.success) {
            // Update local data
            const rule = forwardRules.find(r => r.id === id);
            if (rule) {
                rule.enabled = enabled;
            }
            // Re-render
            renderForwardRules(forwardRules);
        } else {
            alert('❌ ' + t('rules.alert.operationFailed') + ': ' + (result.error || t('common.unknownError')));
            loadForwardRules(); // Reload to restore state
        }
    } catch (error) {
        console.error('❌ Failed to toggle rule state:', error);
        alert('❌ ' + t('rules.alert.operationFailed') + ': ' + error.message);
        loadForwardRules();
    }
}

/**
 * Delete a rule
 * @param {number} id - rule ID
 */
async function deleteRule(id) {
    const rule = forwardRules.find(r => r.id === id);
    const ruleName = rule ? rule.name : t('rules.thisRule');

    if (!confirm(t('rules.confirm.delete', { name: ruleName }))) {
        return;
    }

    try {
        console.log('🗑️ Deleting rule:', id);
        const result = await API.deleteForwardRule(id);

        if (result.success) {
            alert('✅ ' + t('rules.alert.deleteSuccess'));
            loadForwardRules();
        } else {
            alert('❌ ' + t('rules.alert.deleteFailed') + ': ' + (result.error || t('common.unknownError')));
        }
    } catch (error) {
        console.error('❌ Failed to delete rule:', error);
        alert('❌ ' + t('rules.alert.deleteFailed') + ': ' + error.message);
    }
}

/**
 * Test a rule
 * @param {number} id - rule ID
 */
async function testRule(id) {
    const rule = forwardRules.find(r => r.id === id);
    const ruleName = rule ? rule.name : t('rules.thisRule');

    if (!confirm(t('rules.confirm.test', { name: ruleName }))) {
        return;
    }

    try {
        console.log('🧪 Testing rule:', id);
        const result = await API.testForwardRule(id);

        if (result.success) {
            alert('✅ ' + t('rules.alert.testSuccess') + '\n\n' + (result.message || t('rules.alert.testMessageSent')));
        } else {
            alert('❌ ' + t('rules.alert.testFailed') + ': ' + (result.error || t('common.unknownError')));
        }
    } catch (error) {
        console.error('❌ Failed to test rule:', error);
        alert('❌ ' + t('rules.alert.testFailed') + ': ' + error.message);
    }
}

// Export the module (used by dashboard.js for initialization detection)
const ForwardRulesModule = {
    init: function() {
        console.log('📋 Forward rule module initialized');
    },
    loadRules: loadForwardRules
};
