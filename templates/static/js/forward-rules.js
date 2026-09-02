/**
 * Forward Rule Management Module
 * Implements create, read, update, delete, and test functionality for forward rules
 */

// Stores the current list of rules
let forwardRules = [];
// Client-side search + page over the loaded list; mirrors silences.js.
let ruleQuery = '';
let rulePage = 1;

/**
 * Load the list of forward rules
 */
async function loadForwardRules() {
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
        } else {
            container.innerHTML = `
                <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <p>${wwIcon('x')} ${t('common.loadFailed')}: ${escapeHtml(result.error || t('common.unknownError'))}</p>
                    <button class="btn" data-act="loadForwardRules" data-args="" style="margin-top: 10px;">${t('common.retry')}</button>
                </div>
            `;
        }
    } catch (error) {
        console.error('Failed to load forward rules:', error);
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                <p>${wwIcon('x')} ${t('common.loadFailed')}: ${escapeHtml(error.message || String(error))}</p>
                <button class="btn" data-act="loadForwardRules" data-args="" style="margin-top: 10px;">${t('common.retry')}</button>
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
                <div style="font-size: 48px; margin-bottom: 20px;">${wwIcon('inbox')}</div>
                <p style="font-size: 16px; margin-bottom: 10px;">${t('rules.empty.title')}</p>
                <p style="font-size: 14px;">${t('rules.empty.text')}</p>
            </div>
        `;
        return;
    }

    // Sort by priority (higher priority first)
    const sortedRules = [...rules].sort((a, b) => (b.priority || 0) - (a.priority || 0));

    const paged = wwFilterPage(sortedRules, ruleQuery, rulePage, 20, (r) =>
        [r.name, r.match_source, r.match_project, r.match_region, r.match_environment,
            r.match_importance, r.target_type, r.target_name, r.target_url].filter(Boolean).join(' '));
    rulePage = paged.page;
    if (!paged.rows.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('filter') + '</div>' +
            '<div class="empty-title">' + t('common.noMatches') + '</div></div>';
        return;
    }

    let html = '<div class="rules-list" style="display: flex; flex-direction: column; gap: 15px;">';

    paged.rows.forEach(rule => {
        html += renderRuleCard(rule);
    });

    html += '</div>';
    html += wwPagerHtml(paged, 'ForwardRulesModule.page');
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

    // A deep-analysis rule carries no address of its own: the gateway is server
    // configuration, so one setting repoints every such rule. The card used to
    // read "deep analysis (deep analysis)" over an empty address — true, and
    // useless. Name the gateway that actually answers, and say where it is.
    const deepTarget = rule.deep_analysis_target;
    const suppressTargetName = !!deepTarget && rule.target_name === formatTargetType(rule.target_type);
    // Name the gateway INSTANCE, not just the platform: with several configured,
    // "hookprobe" no longer identifies where this rule sends.
    const deepGatewayLabel = deepTarget
        ? [deepTarget.name && deepTarget.name !== 'default' ? deepTarget.name : '', deepTarget.platform]
            .filter(Boolean).join(' · ')
        : '';
    const deepTargetSuffix = deepGatewayLabel
        ? ` <span style="color: var(--primary); font-weight: 600;">${escapeHtml(deepGatewayLabel)}</span>`
        : '';
    let targetAddressText = escapeHtml(rule.target_url || '-');
    if (deepTarget) {
        if (deepTarget.error) {
            // A rule naming a removed gateway delivers nothing. Say it here, in
            // the one place someone reads to answer "where does this go".
            targetAddressText = `<span style="color: var(--danger);">${escapeHtml(String(deepTarget.error))}</span>`;
        } else {
            const where = deepTarget.gateway_url
                ? escapeHtml(String(deepTarget.gateway_url))
                : t('rules.deepTarget.unset');
            const off = deepTarget.enabled ? '' : ` — ${t('rules.deepTarget.disabled')}`;
            targetAddressText = `${where}<span style="color: var(--text-muted);"> (${t('rules.deepTarget.fromConfig')}${off})</span>`;
        }
    }

    const isEnabled = rule.enabled;
    const deliveryStatus = String(rule.delivery_status || 'unknown');
    const deliveryFailures = Number(rule.delivery_failure_count_24h || 0);
    const deliveryUnhealthy = deliveryStatus === 'exhausted' || deliveryFailures > 0;
    const deliveryRetrying = deliveryStatus === 'retrying' || deliveryStatus === 'processing';
    const cardBorder = deliveryUnhealthy
        ? 'border-left: 3px solid var(--danger);'
        : (isEnabled ? '' : '');
    const cardOpacity = isEnabled ? 'opacity: 1;' : 'opacity: 0.65; background: var(--bg-subtle);';
    const titleColor = isEnabled ? 'color: var(--text-main);' : 'color: var(--text-muted); text-decoration: line-through;';
    const deliveryBadge = deliveryUnhealthy
        ? '<span class="badge badge-danger">' + wwIcon('alert-triangle') + ' ' + t('rules.health.failed', { count: deliveryFailures || 1 }) + '</span>'
        : (deliveryRetrying
            ? '<span class="badge badge-warning">' + wwIcon('clock') + ' ' + t('rules.health.retrying') + '</span>'
            : (deliveryStatus === 'sent'
                ? '<span class="badge badge-success">' + wwIcon('check') + ' ' + t('rules.health.healthy') + '</span>'
                : ''));
    const deliveryDetail = rule.last_delivery_at
        ? '<div style="margin-top:0.75rem; font-size:0.82rem; color:' +
            (deliveryUnhealthy ? 'var(--danger)' : 'var(--text-muted)') + ';">' +
            escapeHtml(t('rules.health.lastDelivery', {
                time: (typeof formatTime === 'function' ? formatTime(rule.last_delivery_at) : rule.last_delivery_at)
            })) +
            (rule.last_delivery_error ? '<div style="margin-top:0.35rem; overflow-wrap:anywhere;">' +
                escapeHtml(rule.last_delivery_error) + '</div>' : '') + '</div>'
        : '';

    // ROI: how many alerts this rule has matched. A high count = it's carrying
    // load; an enabled rule with zero matches is a "zombie" rule worth reviewing.
    const hits = Number(rule.hit_count || 0);
    const hitBadge = hits > 0
        ? '<button type="button" class="badge badge-success badge-drill" title="' + escapeHtml(t('rules.roi.tooltip')) + '"' +
            ' data-drill-rule="' + escapeHtml(rule.name) + '">' +
            t('rules.roi.hits', { count: hits }) + '</button>'
        : (isEnabled
            ? '<span class="badge badge-danger" title="' + escapeHtml(t('rules.roi.zombieTooltip')) + '">' +
                t('rules.roi.zombie') + '</span>'
            : '<span class="badge badge-outline">' + t('rules.roi.hits', { count: 0 }) + '</span>');
    const lastMatched = rule.last_matched_at
        ? '<div style="margin-top: 0.75rem; color: var(--text-muted); font-size: 0.85rem;">' +
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
            <div class="rule-header" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1.5rem;">
                <div style="display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; min-width: 0;">
                    <!-- Modern Toggle Switch -->
                    <label class="switch" style="position: relative; display: inline-block; width: 44px; height: 24px; margin: 0;">
                        <input type="checkbox" ${isEnabled ? 'checked' : ''} data-toggle-rule="${rule.id}" style="opacity: 0; width: 0; height: 0;">
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
                    ${!isEnabled ? '<span class="badge badge-outline" style="color: var(--text-muted); font-size: 0.75rem; border: 1px solid var(--border);">' + t('rules.card.disabled') + '</span>' : ''}
                    ${hitBadge}
                    ${deliveryBadge}
                </div>
                <span style="
                    background: var(--bg-subtle);
                    padding: 4px 12px;
                    border-radius: 9999px;
                    font-size: 0.85rem;
                    font-weight: 600;
                    color: var(--text-secondary);
                    border: 1px solid var(--border);
                ">${t('rules.card.priority', { n: rule.priority || 0 })}</span>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem;">
                <!-- Match conditions area -->
                <div class="rule-conditions" style="font-size: 0.95rem; color: var(--text-secondary); background: var(--bg-subtle); padding: 1.25rem; border-radius: 8px; border: 1px dashed var(--border);">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">${wwIcon('target')} ${t('rules.card.matchConditions')}</div>
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
                <div class="rule-target" style="font-size: 0.95rem; color: var(--text-secondary); background: var(--bg-subtle); padding: 1.25rem; border-radius: 8px; border: 1px dashed var(--border);">
                    <div style="font-size: 0.8rem; text-transform: uppercase; color: var(--text-secondary); margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;"><span style="color: var(--success);">${wwIcon('send')}</span> ${t('rules.card.action')}</div>
                    <div style="margin-bottom: 0.75rem;">
                        <strong>${t('rules.card.pushTo')}:</strong> ${targetTypeText}${deepTargetSuffix}
                        ${suppressTargetName ? '' : (rule.target_name ? `(${escapeHtml(rule.target_name)})` : '')}
                    </div>
                    <div style="word-break: break-all; color: var(--text-main); font-family: var(--font-mono); font-size: 0.85rem; background: var(--bg-surface); padding: 0.75rem; border-radius: 6px; border: 1px solid var(--border); box-shadow: inset 0 1px 2px rgba(0,0,0,0.02);">
                        ${targetAddressText}
                    </div>
                    ${rule.stop_on_match ? '<div style="margin-top: 0.75rem; color: var(--warning); font-weight: 600; font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem;">' + wwIcon('zap') + ' ' + t('rules.card.stopOnMatch') + '</div>' : ''}
                    ${deliveryDetail}
                </div>
            </div>

            ${lastMatched}

            <div class="rule-actions" style="display: flex; gap: 0.75rem; justify-content: flex-end; padding-top: 1.25rem; border-top: 1px solid var(--border);">
                <button class="btn" data-act="testRule" data-args="${rule.id}" style="color: var(--primary); border-color: var(--primary-light); background: transparent; font-weight: 600;">
                    ${wwIcon('flask')} ${t('rules.action.test')}
                </button>
                <button class="btn" data-act="showRuleForm" data-args="${rule.id}" style="font-weight: 600;">
                    ${wwIcon('pencil')} ${t('rules.action.edit')}
                </button>
                <button class="btn" data-act="deleteRule" data-args="${rule.id}" style="color: var(--danger); border-color: transparent; background: transparent;">
                    ${wwIcon('trash')} ${t('rules.action.delete')}
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
    const map = { 'critical': t('common.critical'), 'high': t('common.high'), 'medium': t('common.medium'), 'low': t('common.low') };
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
        'deep_analysis': t('rules.targetType.deep_analysis'),
        'webhook': t('rules.targetType.webhook')
    };
    return map[type] || type || t('rules.targetType.unknown');
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
    const gatewayGroupReset = document.getElementById('ruleFormGatewayGroup');
    if (gatewayGroupReset) gatewayGroupReset.style.display = 'none';
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
                showToast(t('rules.alert.editNeedsWriteKey'), 'info');
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
            populateGatewaySelect(rule.target_gateway || '');
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

// Configured gateways, fetched once per page load. A rule picks one by name;
// the list is server-side because the addresses and tokens are.
let deepAnalysisGateways = null;

async function loadDeepAnalysisGateways() {
    if (deepAnalysisGateways !== null) return deepAnalysisGateways;
    try {
        const result = await API.getDeepAnalysisGateways();
        deepAnalysisGateways = result.data || [];
    } catch (e) {
        // Never block the form: fall back to the default gateway only.
        deepAnalysisGateways = [{ name: 'default', platform: '', gateway_url: '', configured: true }];
    }
    return deepAnalysisGateways;
}

async function populateGatewaySelect(selected) {
    const select = document.getElementById('ruleFormGateway');
    const hint = document.getElementById('ruleFormGatewayHint');
    if (!select) return;
    const gateways = await loadDeepAnalysisGateways();
    const want = String(selected || '').trim().toLowerCase() || 'default';
    select.innerHTML = gateways.map(function(g) {
        const label = g.name === 'default'
            ? t('rule.gateway.default', { platform: g.platform || '-' })
            : `${g.name}${g.platform ? ' · ' + g.platform : ''}`;
        return '<option value="' + escapeHtml(g.name === 'default' ? '' : g.name) + '"'
            + ((g.name === want || (want === 'default' && g.name === 'default')) ? ' selected' : '')
            + '>' + escapeHtml(label) + '</option>';
    }).join('');
    // A rule may still name a gateway that has since been removed from
    // configuration. Keep it selectable and say so, rather than silently
    // rewriting the rule to the default on the next save.
    const known = gateways.some(g => (g.name === 'default' ? '' : g.name) === String(selected || ''));
    if (selected && !known) {
        select.insertAdjacentHTML('afterbegin',
            '<option value="' + escapeHtml(selected) + '" selected>' + escapeHtml(selected) + '</option>');
        if (hint) hint.innerHTML = '<span class="ww-dot ww-dot-danger"></span> ' + t('rule.gateway.missing');
        return;
    }
    const chosen = gateways.find(g => (g.name === 'default' ? '' : g.name) === String(selected || '')) || gateways[0];
    if (hint) hint.textContent = chosen && chosen.gateway_url ? chosen.gateway_url : t('rule.gateway.unset');
}

/**
 * Probe the selected gateway: reachable, and does the token work.
 *
 * Answers at configuration time what would otherwise only surface when alerts
 * started failing. Costs nothing — it asks for a session that cannot exist
 * rather than starting an investigation.
 */
async function testRuleGateway() {
    const select = document.getElementById('ruleFormGateway');
    const hint = document.getElementById('ruleFormGatewayHint');
    if (!select || !hint) return;
    const name = select.value.trim() || 'default';
    hint.innerHTML = '<span class="ww-dot ww-dot-muted"></span> ' + t('rule.gateway.testing');
    try {
        const probe = (await API.testDeepAnalysisGateway(name)).data || {};
        const dot = probe.ok ? 'ww-dot-success' : 'ww-dot-danger';
        hint.innerHTML = '<span class="ww-dot ' + dot + '"></span> ' + escapeHtml(String(probe.detail || probe.state || ''));
    } catch (e) {
        hint.innerHTML = '<span class="ww-dot ww-dot-danger"></span> '
            + escapeHtml(t('rule.gateway.testFailed') + ': ' + (e.message || String(e)));
    }
}

/**
 * Handle target-type changes
 */
function onTargetTypeChange() {
    const targetType = document.getElementById('ruleFormTargetType').value;
    const urlGroup = document.getElementById('ruleFormTargetUrlGroup');
    const gatewayGroup = document.getElementById('ruleFormGatewayGroup');

    // The deep-analysis target has no address: the gateway is server config, so
    // the rule names one instead of carrying a URL.
    const isDeep = targetType === 'deep_analysis';
    urlGroup.style.display = isDeep ? 'none' : 'block';
    if (gatewayGroup) {
        gatewayGroup.style.display = isDeep ? 'block' : 'none';
        if (isDeep) populateGatewaySelect(document.getElementById('ruleFormGateway').value);
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
    const targetGateway = targetType === 'deep_analysis'
        ? document.getElementById('ruleFormGateway').value.trim()
        : '';

    // Validate required fields
    if (!name) {
        showToast(t('rules.alert.nameRequired'), 'info');
        return;
    }

    if (targetType !== 'deep_analysis' && !targetUrl) {
        showToast(t('rules.alert.targetUrlRequired'), 'info');
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
        target_url: targetType === 'deep_analysis' ? '' : targetUrl,
        target_gateway: targetGateway,
        target_name: targetName,
        stop_on_match: document.getElementById('ruleFormStopOnMatch').checked
    };

    try {
        let result;
        if (ruleId) {
            // Update rule
            result = await API.updateForwardRule(ruleId, ruleData);
        } else {
            // Create rule
            result = await API.createForwardRule(ruleData);
        }

        if (result.success) {
            showToast(ruleId ? '' + t('rules.alert.updateSuccess') : '' + t('rules.alert.createSuccess'), 'info');
            closeRuleForm();
            loadForwardRules();
        } else {
            showToast(t('rules.alert.saveFailed') + ': ' + (result.error || t('common.unknownError')), 'error');
        }
    } catch (error) {
        console.error('Failed to save rule:', error);
        showToast(t('rules.alert.saveFailed') + ': ' + error.message, 'error');
    }
}

/**
 * Enable/disable a rule
 * @param {number} id - rule ID
 * @param {boolean} enabled - whether to enable
 */
async function toggleRule(id, enabled) {
    try {
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
            showToast(t('rules.alert.operationFailed') + ': ' + (result.error || t('common.unknownError')), 'error');
            loadForwardRules(); // Reload to restore state
        }
    } catch (error) {
        console.error('Failed to toggle rule state:', error);
        showToast(t('rules.alert.operationFailed') + ': ' + error.message, 'error');
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

    if (!(await wwConfirm(t('rules.confirm.delete', { name: ruleName })))) {
        return;
    }

    try {
        const result = await API.deleteForwardRule(id);

        if (result.success) {
            showToast(t('rules.alert.deleteSuccess'), 'info');
            loadForwardRules();
        } else {
            showToast(t('rules.alert.deleteFailed') + ': ' + (result.error || t('common.unknownError')), 'error');
        }
    } catch (error) {
        console.error('Failed to delete rule:', error);
        showToast(t('rules.alert.deleteFailed') + ': ' + error.message, 'error');
    }
}

/**
 * Test a rule
 * @param {number} id - rule ID
 */
async function testRule(id) {
    const rule = forwardRules.find(r => r.id === id);
    const ruleName = rule ? rule.name : t('rules.thisRule');

    if (!(await wwConfirm(t('rules.confirm.test', { name: ruleName })))) {
        return;
    }

    try {
        const result = await API.testForwardRule(id);

        if (result.success) {
            showToast(t('rules.alert.testSuccess') + '\n\n' + (result.message || t('rules.alert.testMessageSent')), 'info');
        } else {
            showToast(t('rules.alert.testFailed') + ': ' + (result.error || t('common.unknownError')), 'error');
        }
    } catch (error) {
        console.error('Failed to test rule:', error);
        showToast(t('rules.alert.testFailed') + ': ' + error.message, 'error');
    }
}

// Export the module (used by dashboard.js for initialization detection)
const ForwardRulesModule = {
    init: function() {
    },
    loadRules: loadForwardRules,
    page: function (n) {
        rulePage = Number(n) || 1;
        renderForwardRules(forwardRules);
    },
    search: function (value) {
        ruleQuery = String(value || '');
        rulePage = 1;
        renderForwardRules(forwardRules);
    }
};


// Rule-name drill: delegated because rule names carry arbitrary characters
// (Chinese, quotes) that inline onclick escaping would mangle. getAttribute
// returns the decoded original, which is exactly what the filter needs.
document.addEventListener('click', function (event) {
    const drill = event.target.closest('[data-drill-rule]');
    if (!drill) return;
    const name = drill.getAttribute('data-drill-rule');
    if (typeof navigateTo === 'function') navigateTo('trace');
    if (typeof DecisionTraceModule !== 'undefined' && typeof DecisionTraceModule.filterByRule === 'function') {
        DecisionTraceModule.filterByRule(name);
    }
});

// The rule enable/disable switch was the last inline handler in the codebase —
// the CSP burn-down took 97 of them to zero and missed this one, so
// `script-src-attr 'none'` has been silently blocking it: clicking the switch
// did nothing at all, and only the console said why. Delegated like the rest.
document.addEventListener('change', function (event) {
    const box = event.target.closest('[data-toggle-rule]');
    if (!box) return;
    const id = box.getAttribute('data-toggle-rule');
    if (typeof toggleRule === 'function') toggleRule(id, box.checked);
});

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="ForwardRulesModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('ForwardRulesModule', ForwardRulesModule);
