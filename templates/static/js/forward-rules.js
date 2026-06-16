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
                <p>Loading...</p>
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
                    <p>❌ Load failed: ${escapeHtml(result.error || 'Unknown error')}</p>
                    <button class="btn" onclick="loadForwardRules()" style="margin-top: 10px;">Retry</button>
                </div>
            `;
        }
    } catch (error) {
        console.error('❌ Failed to load forward rules:', error);
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                <p>❌ Load failed: ${escapeHtml(error.message || String(error))}</p>
                <button class="btn" onclick="loadForwardRules()" style="margin-top: 10px;">Retry</button>
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
                <p style="font-size: 16px; margin-bottom: 10px;">No forward rules</p>
                <p style="font-size: 14px;">Click the "New Rule" button to create your first forward rule</p>
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
    const sourceText = escapeHtml(rule.match_source || 'All');
    const projectText = escapeHtml(rule.match_project || 'All');
    const regionText = escapeHtml(rule.match_region || 'All');
    const environmentText = escapeHtml(rule.match_environment || 'All');
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
                    ${!isEnabled ? '<span class="badge" style="background: #f1f5f9; color: #64748b; font-size: 0.75rem; border: 1px solid #e2e8f0;">Disabled</span>' : ''}
                </div>
                <span style="
                    background: #f1f5f9;
                    padding: 4px 12px;
                    border-radius: 9999px;
                    font-size: 0.85rem;
                    font-weight: 600;
                    color: #475569;
                    border: 1px solid #cbd5e1;
                ">⬆️ Priority: ${rule.priority || 0}</span>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem;">
                <!-- Match conditions area -->
                <div class="rule-conditions" style="font-size: 0.95rem; color: #334155; background: #f8fafc; padding: 1.25rem; border-radius: 8px; border: 1px dashed #cbd5e1;">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🎯 Match Conditions</div>
                    ${rule.match_event_type ? '<div style="margin-bottom:0.5rem;"><strong>Event Type:</strong> ' + (function() { var types = rule.match_event_type.split(',').map(function(t) { var m = { webhook_forward: 'Alert Forward', manual_forward: 'Manual Forward', ai_error: 'AI Error', ai_degraded: 'AI Degraded', deep_analysis: 'Deep Analysis', outbox_exhausted: 'Forward Exhausted', rule_test: 'Test' }; return '<span style="display:inline-block;background:var(--primary-bg);color:var(--primary);padding:1px 6px;border-radius:4px;font-size:0.7rem;font-weight:600;margin-right:4px;">' + (m[t.trim()] || t.trim()) + '</span>'; }); return types.join(''); })() + '</div>' : ''}
                    <div style="margin-bottom: 0.5rem;"><strong>Importance:</strong> ${importanceText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>Alert Status:</strong> ${duplicateText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>Source:</strong> ${sourceText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>Project:</strong> ${projectText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>Region:</strong> ${regionText}</div>
                    <div style="margin-bottom: 0.5rem;"><strong>Environment:</strong> ${environmentText}</div>
                    ${rule.match_payload ? '<div><strong>Payload:</strong> <code style="font-size:0.8rem;">' + escapeHtml(rule.match_payload) + '</code></div>' : ''}
                </div>

                <!-- Forward target area -->
                <div class="rule-target" style="font-size: 0.95rem; color: #334155; background: #f0fdf4; padding: 1.25rem; border-radius: 8px; border: 1px dashed #86efac;">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: #059669; margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🚀 Action</div>
                    <div style="margin-bottom: 0.75rem;">
                        <strong>Push to:</strong> ${targetTypeText}
                        ${rule.target_name ? `(${escapeHtml(rule.target_name)})` : ''}
                    </div>
                    <div style="word-break: break-all; color: #0f172a; font-family: 'Fira Code', monospace; font-size: 0.85rem; background: #ffffff; padding: 0.75rem; border-radius: 6px; border: 1px solid #d1fae5; box-shadow: inset 0 1px 2px rgba(0,0,0,0.02);">
                        ${escapeHtml(rule.target_url || '-')}
                    </div>
                    ${rule.stop_on_match ? '<div style="margin-top: 0.75rem; color: #d97706; font-weight: 600; font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem;"><span>🛑</span> After matching this rule, stop matching subsequent rules</div>' : ''}
                </div>
            </div>

            <div class="rule-actions" style="display: flex; gap: 0.75rem; justify-content: flex-end; padding-top: 1.25rem; border-top: 1px solid #e2e8f0;">
                <button class="btn" onclick="testRule(${rule.id})" style="color: #4338ca; border-color: #c7d2fe; background: #e0e7ff; font-weight: 600;">
                    🧪 Test Channel
                </button>
                <button class="btn" onclick="showRuleForm(${rule.id})" style="font-weight: 600;">
                    ✏️ Edit
                </button>
                <button class="btn" onclick="deleteRule(${rule.id})" style="color: #dc2626; border-color: #fecaca; background: #fef2f2; font-weight: 600;">
                    🗑️ Delete
                </button>
            </div>
        </div>
    `;
}
/**
 * Format importance display
 */
function formatImportance(importance) {
    if (!importance) return 'All';
    const map = { 'high': 'High', 'medium': 'Medium', 'low': 'Low' };
    return importance.split(',').map(i => map[i.trim()] || i.trim()).join(',') || 'All';
}

/**
 * Format duplicate-status display
 */
function formatDuplicateStatus(status) {
    const map = {
        'all': 'All',
        'new': 'New alerts only',
        'duplicate': 'Duplicate alerts only'
    };
    return map[status] || status || 'All';
}

/**
 * Format target-type display
 */
function formatTargetType(type) {
    const map = {
        'feishu': 'Feishu',
        'openclaw': 'OpenClaw',
        'webhook': 'Webhook'
    };
    return map[type] || type || 'Unknown';
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
        title.textContent = 'Edit Forward Rule';
        const rule = forwardRules.find(r => r.id === ruleId);
        if (rule) {
            if (rule.target_url_sensitive === false) {
                alert('Editing a forward rule requires saving the ADMIN_WRITE_KEY first, so the full target URL can be loaded.');
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
        title.textContent = 'New Forward Rule';
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
        alert('Please enter a rule name');
        return;
    }

    if (targetType !== 'openclaw' && !targetUrl) {
        alert('Please enter a target address');
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
            alert(ruleId ? '✅ Rule updated successfully' : '✅ Rule created successfully');
            closeRuleForm();
            loadForwardRules();
        } else {
            alert('❌ Save failed: ' + (result.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('❌ Failed to save rule:', error);
        alert('❌ Save failed: ' + error.message);
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
            alert('❌ Operation failed: ' + (result.error || 'Unknown error'));
            loadForwardRules(); // Reload to restore state
        }
    } catch (error) {
        console.error('❌ Failed to toggle rule state:', error);
        alert('❌ Operation failed: ' + error.message);
        loadForwardRules();
    }
}

/**
 * Delete a rule
 * @param {number} id - rule ID
 */
async function deleteRule(id) {
    const rule = forwardRules.find(r => r.id === id);
    const ruleName = rule ? rule.name : 'this rule';

    if (!confirm(`Are you sure you want to delete the rule "${ruleName}"?\n\nThis action cannot be undone.`)) {
        return;
    }

    try {
        console.log('🗑️ Deleting rule:', id);
        const result = await API.deleteForwardRule(id);

        if (result.success) {
            alert('✅ Rule deleted');
            loadForwardRules();
        } else {
            alert('❌ Delete failed: ' + (result.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('❌ Failed to delete rule:', error);
        alert('❌ Delete failed: ' + error.message);
    }
}

/**
 * Test a rule
 * @param {number} id - rule ID
 */
async function testRule(id) {
    const rule = forwardRules.find(r => r.id === id);
    const ruleName = rule ? rule.name : 'this rule';

    if (!confirm(`Are you sure you want to test the rule "${ruleName}"?\n\nA test message will be sent to the target address.`)) {
        return;
    }

    try {
        console.log('🧪 Testing rule:', id);
        const result = await API.testForwardRule(id);

        if (result.success) {
            alert('✅ Test successful!\n\n' + (result.message || 'Test message sent'));
        } else {
            alert('❌ Test failed: ' + (result.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('❌ Failed to test rule:', error);
        alert('❌ Test failed: ' + error.message);
    }
}

// Export the module (used by dashboard.js for initialization detection)
const ForwardRulesModule = {
    init: function() {
        console.log('📋 Forward rule module initialized');
    },
    loadRules: loadForwardRules
};
