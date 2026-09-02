/**
 * Forward Rule Management Module
 * Implements create, read, update, delete, and test functionality for forward rules
 */

// Stores the current list of rules
let forwardRules = [];
// Client-side search + page over the loaded list; mirrors silences.js.
let ruleQuery = '';
let rulePage = 1;
// Rows the operator has unfolded; a re-render (toggle, search, paging)
// rebuilds them open so comparing two rules' conditions survives a click.
const expandedRuleIds = new Set();

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

    let html = '<div class="rules-list">';

    paged.rows.forEach(rule => {
        html += renderRuleRow(rule);
    });

    html += '</div>';
    html += wwPagerHtml(paged, 'ForwardRulesModule.page');
    container.innerHTML = html;
}

// Event-type enum → display key. Shared by the row summary and the detail.
const RULE_EVENT_TYPE_KEYS = {
    webhook_forward: 'rules.evtType.webhook_forward',
    manual_forward: 'rules.evtType.manual_forward',
    ai_error: 'rules.evtType.ai_error',
    ai_degraded: 'rules.evtType.ai_degraded',
    deep_analysis: 'rules.evtType.deep_analysis',
    outbox_exhausted: 'rules.evtType.outbox_exhausted',
    rule_test: 'rules.evtType.rule_test'
};

function formatEventTypes(raw) {
    return String(raw || '').split(',').map(function (et) {
        const key = RULE_EVENT_TYPE_KEYS[et.trim()];
        return key ? t(key) : et.trim();
    }).filter(Boolean);
}

/**
 * One line naming ONLY the conditions a rule constrains.
 *
 * The card listed seven conditions per rule and most read "全部": twenty-three
 * rules were eleven screens of the word "all". A rule that constrains nothing
 * says so in two words; a constrained one reads like a sentence —
 * "重要性 高,严重 · 仅新告警".
 */
function ruleMatchSummary(rule) {
    const parts = [];
    if (rule.match_event_type) parts.push(t('rules.card.eventType') + ' ' + formatEventTypes(rule.match_event_type).join(','));
    if (rule.match_importance) parts.push(t('rules.card.importance') + ' ' + formatImportance(rule.match_importance));
    if (rule.match_duplicate && rule.match_duplicate !== 'all') parts.push(formatDuplicateStatus(rule.match_duplicate));
    [['match_source', 'rules.card.source'], ['match_project', 'rules.card.project'],
        ['match_region', 'rules.card.region'], ['match_environment', 'rules.card.environment']].forEach(function (pair) {
        if (rule[pair[0]]) parts.push(t(pair[1]) + ' ' + rule[pair[0]]);
    });
    if (rule.match_payload) parts.push(t('rules.card.payload') + ' ' + rule.match_payload);
    return parts.length ? parts.join(' · ') : t('rules.row.matchAll');
}

/** Scheme, host and first path segment; the rest — where tokens live — is elided. */
function maskRuleUrl(url) {
    const raw = String(url || '');
    if (!raw) return '';
    const match = raw.match(/^([a-z][a-z0-9+.-]*:\/\/[^/?#]+)(\/[^/?#]*)?/i);
    if (!match) return raw.length > 24 ? raw.slice(0, 24) + '…' : raw;
    const rest = raw.slice(match[0].length);
    return match[1] + (match[2] || '') + (rest ? '/…' : '');
}

// Name the gateway INSTANCE, not just the platform: with several configured,
// "hookprobe" no longer identifies where this rule sends.
function deepGatewayLabel(deepTarget) {
    if (!deepTarget) return '';
    return [deepTarget.name && deepTarget.name !== 'default' ? deepTarget.name : '', deepTarget.platform]
        .filter(Boolean).join(' · ');
}

/**
 * Where a rule sends: an icon per target type, the target's name, and the
 * address (masked) on hover. A deep-analysis rule carries no address of its
 * own — the gateway is server configuration — so it names the gateway.
 */
function ruleTargetSummary(rule) {
    const type = String(rule.target_type || '');
    const icon = type === 'feishu' ? 'message' : (type === 'deep_analysis' ? 'flask' : 'link');
    const typeText = formatTargetType(type);
    const deepTarget = rule.deep_analysis_target;
    let name = rule.target_name || typeText;
    let address = maskRuleUrl(rule.target_url);
    if (deepTarget) {
        const gateway = deepGatewayLabel(deepTarget);
        if (!rule.target_name || rule.target_name === typeText) name = gateway || typeText;
        address = deepTarget.error
            ? String(deepTarget.error)
            : (deepTarget.gateway_url ? String(deepTarget.gateway_url) : t('rules.deepTarget.unset'));
    }
    return { icon: icon, name: name, title: typeText + (address ? ' · ' + address : '') };
}

function ruleDeliveryHealth(rule) {
    const status = String(rule.delivery_status || 'unknown');
    const failures = Number(rule.delivery_failure_count_24h || 0);
    return {
        status: status,
        failures: failures,
        unhealthy: status === 'exhausted' || failures > 0,
        retrying: status === 'retrying' || status === 'processing'
    };
}

/**
 * Render a single rule as a compact row: switch, name, health, a one-line
 * match summary, target, priority, actions. The full conditions + action
 * detail unfolds beneath on demand (chevron or row body) and is generated
 * lazily — see toggleRuleDetail.
 * @param {Object} rule - rule object
 */
function renderRuleRow(rule) {
    const id = Number(rule.id);
    const isEnabled = rule.enabled !== false;
    const health = ruleDeliveryHealth(rule);
    const expanded = expandedRuleIds.has(id);
    const target = ruleTargetSummary(rule);
    const summary = ruleMatchSummary(rule);
    const priority = Number(rule.priority || 0);

    const deliveryBadge = health.unhealthy
        ? '<span class="badge badge-danger">' + wwIcon('alert-triangle') + ' ' + t('rules.health.failed', { count: health.failures || 1 }) + '</span>'
        : (health.retrying
            ? '<span class="badge badge-warning">' + wwIcon('clock') + ' ' + t('rules.health.retrying') + '</span>'
            : (health.status === 'sent'
                ? '<span class="badge badge-success">' + wwIcon('check') + ' ' + t('rules.health.healthy') + '</span>'
                : ''));

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

    const rowClass = 'rule-row' + (isEnabled ? '' : ' is-disabled') +
        (health.unhealthy ? ' is-unhealthy' : '') + (expanded ? ' is-open' : '');

    // data-stop on the switch, the badges and the actions: their clicks are
    // their own (toggle, drill, test/edit/delete) and must not also unfold
    // the row. Everything else in the row body expands it.
    return '<div class="' + rowClass + '" data-rule-id="' + id + '">' +
        '<div class="rule-row-main" data-act="ForwardRulesModule.toggleDetail" data-args="' + id + '">' +
            '<label class="switch rule-row-toggle" data-stop>' +
                '<input type="checkbox"' + (isEnabled ? ' checked' : '') + ' data-toggle-rule="' + id + '"' +
                    ' aria-label="' + escapeHtml(t('rules.row.toggleAria', { name: rule.name })) + '">' +
                '<span class="slider"></span>' +
            '</label>' +
            '<button type="button" class="rule-row-expander" data-act="ForwardRulesModule.toggleDetail" data-args="' + id + '"' +
                ' aria-expanded="' + (expanded ? 'true' : 'false') + '" aria-controls="rule-detail-' + id + '"' +
                ' title="' + escapeHtml(t('rules.row.toggleDetails')) + '">' +
                wwIcon('chevron-down', 'rule-row-chevron') +
                '<span class="rule-row-name">' + escapeHtml(rule.name) + '</span>' +
            '</button>' +
            (isEnabled ? '' : '<span class="badge badge-outline">' + t('rules.card.disabled') + '</span>') +
            '<span class="rule-row-badges" data-stop>' + hitBadge + deliveryBadge + '</span>' +
            '<span class="rule-row-match" title="' + escapeHtml(summary) + '">' + escapeHtml(summary) + '</span>' +
            '<span class="rule-row-target" title="' + escapeHtml(target.title) + '">' + wwIcon(target.icon) +
                '<span class="rule-row-target-name">' + escapeHtml(target.name) + '</span></span>' +
            '<span class="rule-row-priority ww-muted ww-mono" title="' + escapeHtml(t('rules.card.priority', { n: priority })) + '">' + priority + '</span>' +
            '<span class="rule-row-actions" data-stop>' +
                '<button class="btn btn-sm btn-quiet-primary" data-act="testRule" data-args="' + id + '">' + wwIcon('flask') + ' ' + t('rules.action.test') + '</button>' +
                '<button class="btn btn-sm" data-act="showRuleForm" data-args="' + id + '">' + wwIcon('pencil') + ' ' + t('rules.action.edit') + '</button>' +
                '<button class="btn btn-sm btn-ghost rule-row-delete" data-act="deleteRule" data-args="' + id + '">' + wwIcon('trash') + ' ' + t('rules.action.delete') + '</button>' +
            '</span>' +
        '</div>' +
        '<div class="rule-detail" id="rule-detail-' + id + '"' + (expanded ? '' : ' hidden') + '>' +
            (expanded ? renderRuleDetail(rule) : '') +
        '</div>' +
    '</div>';
}

/**
 * The full conditions + action detail beneath a row. Generated on first
 * expand (or up front for rows the operator already had open).
 * @param {Object} rule - rule object
 */
function renderRuleDetail(rule) {
    const health = ruleDeliveryHealth(rule);
    const deepTarget = rule.deep_analysis_target;
    const typeText = formatTargetType(rule.target_type);
    const suppressTargetName = !!deepTarget && rule.target_name === typeText;
    const gateway = deepGatewayLabel(deepTarget);
    const deepTargetSuffix = gateway ? ' <span class="rule-detail-gateway">' + escapeHtml(gateway) + '</span>' : '';

    let targetAddress = escapeHtml(rule.target_url || '-');
    if (deepTarget) {
        if (deepTarget.error) {
            // A rule naming a removed gateway delivers nothing. Say it here, in
            // the one place someone reads to answer "where does this go".
            targetAddress = '<span class="rule-detail-error">' + escapeHtml(String(deepTarget.error)) + '</span>';
        } else {
            const where = deepTarget.gateway_url ? escapeHtml(String(deepTarget.gateway_url)) : t('rules.deepTarget.unset');
            const off = deepTarget.enabled ? '' : ' — ' + t('rules.deepTarget.disabled');
            targetAddress = where + '<span class="ww-muted"> (' + t('rules.deepTarget.fromConfig') + off + ')</span>';
        }
    }

    const field = function (labelKey, value) {
        return '<div class="rule-detail-field"><strong>' + t(labelKey) + ':</strong> ' + value + '</div>';
    };
    const eventTypes = rule.match_event_type
        ? field('rules.card.eventType', formatEventTypes(rule.match_event_type).map(function (label) {
            return '<span class="badge badge-outline">' + escapeHtml(label) + '</span>';
        }).join(' '))
        : '';
    const conditions = eventTypes +
        field('rules.card.importance', escapeHtml(formatImportance(rule.match_importance))) +
        field('rules.card.alertStatus', escapeHtml(formatDuplicateStatus(rule.match_duplicate))) +
        field('rules.card.source', escapeHtml(rule.match_source || t('common.all'))) +
        field('rules.card.project', escapeHtml(rule.match_project || t('common.all'))) +
        field('rules.card.region', escapeHtml(rule.match_region || t('common.all'))) +
        field('rules.card.environment', escapeHtml(rule.match_environment || t('common.all'))) +
        (rule.match_payload ? field('rules.card.payload', '<code class="ww-mono">' + escapeHtml(rule.match_payload) + '</code>') : '');

    const formatWhen = function (value) {
        return typeof formatTime === 'function' ? formatTime(value) : value;
    };
    const deliveryDetail = rule.last_delivery_at
        ? '<div class="rule-detail-note' + (health.unhealthy ? ' is-danger' : '') + '">' +
            escapeHtml(t('rules.health.lastDelivery', { time: formatWhen(rule.last_delivery_at) })) +
            (rule.last_delivery_error ? '<div>' + escapeHtml(rule.last_delivery_error) + '</div>' : '') + '</div>'
        : '';
    const lastMatched = rule.last_matched_at
        ? '<div class="rule-detail-note">' + t('rules.roi.lastMatched', { time: formatWhen(rule.last_matched_at) }) + '</div>'
        : '';

    return '<div class="rule-detail-grid">' +
        '<div class="rule-detail-panel">' +
            '<div class="ww-eyebrow">' + wwIcon('target') + ' ' + t('rules.card.matchConditions') + '</div>' +
            conditions +
        '</div>' +
        '<div class="rule-detail-panel">' +
            '<div class="ww-eyebrow">' + wwIcon('send') + ' ' + t('rules.card.action') + '</div>' +
            '<div class="rule-detail-field"><strong>' + t('rules.card.pushTo') + ':</strong> ' + escapeHtml(typeText) + deepTargetSuffix +
                (suppressTargetName || !rule.target_name ? '' : ' (' + escapeHtml(rule.target_name) + ')') + '</div>' +
            '<div class="rule-detail-address ww-mono">' + targetAddress + '</div>' +
            (rule.stop_on_match ? '<div class="rule-detail-stop">' + wwIcon('zap') + ' ' + t('rules.card.stopOnMatch') + '</div>' : '') +
            deliveryDetail +
        '</div>' +
    '</div>' + lastMatched;
}

/**
 * Unfold / fold one rule's detail. The detail markup is built on first open;
 * open rows are remembered so a re-render (toggle, search, paging) keeps them.
 * @param {number} id - rule ID
 */
function toggleRuleDetail(id) {
    const ruleId = Number(id);
    const detail = document.getElementById('rule-detail-' + ruleId);
    const row = detail ? detail.closest('.rule-row') : null;
    if (!detail || !row) return;
    const opening = detail.hasAttribute('hidden');
    if (opening) {
        if (!detail.innerHTML.trim()) {
            const rule = forwardRules.find(r => Number(r.id) === ruleId);
            if (rule) detail.innerHTML = renderRuleDetail(rule);
        }
        detail.removeAttribute('hidden');
        expandedRuleIds.add(ruleId);
    } else {
        detail.setAttribute('hidden', '');
        expandedRuleIds.delete(ruleId);
    }
    row.classList.toggle('is-open', opening);
    const expander = row.querySelector('.rule-row-expander');
    if (expander) expander.setAttribute('aria-expanded', opening ? 'true' : 'false');
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
    },
    // Chevron / row-body click: unfold the full conditions + action detail.
    toggleDetail: toggleRuleDetail
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
