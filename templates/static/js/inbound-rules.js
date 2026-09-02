/**
 * Inbound Rules — what an alert costs on the way IN.
 *
 * The counterpart to forward-rules.js. A forward rule decides where an alert
 * goes; an inbound rule decides what is spent on it before anyone looks: today,
 * whether it reaches the model and whether it funds an investigation.
 *
 * Deliberately smaller than the forwarding page. There is no target to
 * configure and no delivery to test — so the form is the match criteria plus
 * a verb, and for the verbs that carry a value (a severity ceiling, a digest
 * window in minutes) one field for it.
 */

let inboundRules = [];
let inboundActions = ['skip_ai', 'skip_deep_analysis', 'cap_importance', 'digest'];
// Which verbs take a value. The API says, so a new verb needs no UI release.
let inboundActionsWithValue = ['cap_importance', 'digest'];

async function loadInboundRules() {
    const container = document.getElementById('inboundRulesList');
    if (!container) return;
    container.innerHTML = `<div class="loading"><div class="spinner"></div><p>${t('common.loading')}</p></div>`;
    try {
        const result = await API.getInboundRules();
        if (!result.success) throw new Error(result.error || t('common.unknownError'));
        inboundRules = result.data || [];
        if (Array.isArray(result.actions) && result.actions.length) inboundActions = result.actions;
        if (Array.isArray(result.actions_with_value)) inboundActionsWithValue = result.actions_with_value;
        renderInboundRules(inboundRules);
    } catch (error) {
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                <p>${wwIcon('x')} ${t('common.loadFailed')}: ${escapeHtml(error.message || String(error))}</p>
                <button class="btn" data-act="loadInboundRules" data-args="" style="margin-top: 10px;">${t('common.retry')}</button>
            </div>`;
    }
}

function _inboundCriteria(rule) {
    // Only the criteria that are set: a rule listing seven empty fields reads
    // as complicated when it is usually one line.
    const parts = [];
    const add = (labelKey, value) => {
        if (value) parts.push(`${t(labelKey)}: <strong>${escapeHtml(String(value))}</strong>`);
    };
    add('inbound.field.ruleName', rule.match_rule_name);
    add('inbound.field.source', rule.match_source);
    add('inbound.field.eventType', rule.match_event_type);
    add('inbound.field.importance', rule.match_importance);
    add('inbound.field.project', rule.match_project);
    add('inbound.field.region', rule.match_region);
    add('inbound.field.environment', rule.match_environment);
    add('inbound.field.payload', rule.match_payload);
    return parts.length ? parts.join(' · ') : t('inbound.noCriteria');
}

// The verb and, when it carries one, its value: "汇总投递 · 60 分钟",
// "重要度上限 · medium". A digest saved without a number means the default.
function _inboundActionBadge(rule) {
    // t() returns the key itself when a label is missing, so the fallback must be passed in.
    const label = t('inbound.action.' + rule.action, null, rule.action);
    if (rule.action === 'digest') {
        return label + ' · ' + t('inbound.badge.digestWindow', { n: rule.action_value || '60' });
    }
    return rule.action_value ? label + ' · ' + rule.action_value : label;
}

// What the value field asks for, per verb. A verb the UI does not know yet
// still gets a field (the API said it takes a value), labelled generically.
function _inboundValueSpec(action) {
    if (action === 'digest') {
        return { labelKey: 'inbound.field.digestWindow', placeholder: '60', hintKey: 'inbound.hint.digestWindow' };
    }
    if (action === 'cap_importance') {
        return { labelKey: 'inbound.field.capCeiling', placeholder: 'high / medium / low', hintKey: '' };
    }
    return { labelKey: 'inbound.field.actionValue', placeholder: '', hintKey: '' };
}

function _syncInboundValueField() {
    const select = document.getElementById('ir-action');
    const group = document.getElementById('ir-action_value-group');
    if (!select || !group) return;
    const action = select.value;
    const spec = _inboundValueSpec(action);
    group.style.display = inboundActionsWithValue.indexOf(action) >= 0 ? 'block' : 'none';
    const label = document.getElementById('ir-action_value-label');
    const input = document.getElementById('ir-action_value');
    const hint = document.getElementById('ir-action_value-hint');
    if (label) label.textContent = t(spec.labelKey);
    if (input) input.placeholder = spec.placeholder;
    if (hint) hint.textContent = spec.hintKey ? t(spec.hintKey) : '';
}

let inboundQuery = '';
let inboundPage = 1;

function renderInboundRules(rules) {
    const container = document.getElementById('inboundRulesList');
    if (!container) return;
    if (!rules.length) {
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                <p>${t('inbound.empty')}</p>
            </div>`;
        return;
    }
    const paged = wwFilterPage(rules, inboundQuery, inboundPage, 20, function (rule) {
        return [rule.name, rule.action, rule.action_value, rule.match_rule_name, rule.match_source,
            rule.match_event_type, rule.match_importance, rule.match_project, rule.match_region,
            rule.match_environment, rule.match_payload].filter(Boolean).join(' ');
    });
    inboundPage = paged.page;
    if (!paged.rows.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('filter') + '</div>' +
            '<div class="empty-title">' + t('common.noMatches') + '</div></div>';
        return;
    }
    container.innerHTML = paged.rows.map(function (rule) {
        const actionLabel = _inboundActionBadge(rule);
        const state = rule.enabled
            ? `<span class="badge badge-success">${t('inbound.status.enabled')}</span>`
            : `<span class="badge badge-outline">${t('inbound.status.disabled')}</span>`;
        return `
            <div class="card" style="margin-bottom: 12px;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;">
                    <div style="min-width: 260px;">
                        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                            <strong>${escapeHtml(rule.name || '')}</strong>
                            ${state}
                            <span class="badge badge-outline">${escapeHtml(actionLabel)}</span>
                        </div>
                        <div style="margin-top:6px; color: var(--text-secondary); font-size: var(--fs-sm);">
                            ${_inboundCriteria(rule)}
                        </div>
                        ${rule.comment ? `<div style="margin-top:4px; color: var(--text-muted); font-size: var(--fs-sm);">${escapeHtml(rule.comment)}</div>` : ''}
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button class="btn" data-act="showInboundRuleForm" data-args="${rule.id}">${t('rules.action.edit')}</button>
                        <button class="btn" data-act="deleteInboundRule" data-args="${rule.id}"
                            style="color: var(--danger); border-color: transparent; background: transparent;">${t('rules.action.delete')}</button>
                    </div>
                </div>
            </div>`;
    }).join('') + wwPagerHtml(paged, 'InboundRulesModule.page');
}

function showInboundRuleForm(id) {
    const rule = id ? inboundRules.find(r => String(r.id) === String(id)) || {} : {};
    const container = document.getElementById('inboundRuleForm');
    if (!container) return;
    // Design-system form idiom (form-group/form-label/form-input), matching
    // the silence and integration forms. The previous markup used classes
    // that exist nowhere in the stylesheets (.input, .btn.primary, .mem-bar),
    // so every field rendered as a bare browser-default box.
    const field = (key, name, value, placeholder) => `
        <div class="form-group">
            <label class="form-label" for="ir-${name}">${t(key)}</label>
            <input type="text" class="form-input" id="ir-${name}" value="${escapeHtml(String(value || ''))}"
                   placeholder="${escapeHtml(placeholder || '')}" autocomplete="off">
        </div>`;
    container.innerHTML = `
        <div class="card" style="margin-bottom:16px;">
            <div class="section-title" style="font-size: var(--fs-md); margin-bottom: 1rem;">
                ${t(rule.id ? 'inbound.form.editTitle' : 'inbound.form.addTitle')}
            </div>
            <input type="hidden" id="ir-id" value="${escapeHtml(String(rule.id || ''))}">
            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 0 1.25rem;">
                ${field('inbound.field.name', 'name', rule.name, '')}
                <div class="form-group">
                    <label class="form-label" for="ir-action">${t('inbound.field.action')}</label>
                    <select class="form-input" id="ir-action">
                        ${inboundActions.map(a => `<option value="${escapeHtml(a)}" ${rule.action === a ? 'selected' : ''}>${escapeHtml(t('inbound.action.' + a, null, a))}</option>`).join('')}
                    </select>
                </div>
                <div class="form-group" id="ir-action_value-group" style="display:none;">
                    <label class="form-label" for="ir-action_value" id="ir-action_value-label">${t('inbound.field.actionValue')}</label>
                    <input type="text" class="form-input" id="ir-action_value" value="${escapeHtml(String(rule.action_value || ''))}"
                           placeholder="" autocomplete="off">
                    <div id="ir-action_value-hint" style="margin-top: 0.25rem; color: var(--text-muted); font-size: var(--fs-sm);"></div>
                </div>
            </div>
            ${field('inbound.field.ruleName', 'match_rule_name', rule.match_rule_name, t('inbound.hint.ruleName'))}
            <div style="margin:-0.4rem 0 1rem;">
                <button class="btn btn-sm" data-act="pickInboundRuleName" data-args="">${wwIcon('list')} ${t('inbound.pickFromTraffic')}</button>
                <div id="ir-picker" style="display:none;"></div>
            </div>
            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 0 1.25rem;">
                ${field('inbound.field.source', 'match_source', rule.match_source, 'grafana,prometheus')}
                ${field('inbound.field.environment', 'match_environment', rule.match_environment, 'prod,!test')}
                ${field('inbound.field.payload', 'match_payload', rule.match_payload, 'commonLabels.type=signal')}
                ${field('inbound.field.importance', 'match_importance', rule.match_importance, t('inbound.hint.importance'))}
            </div>
            ${field('inbound.field.comment', 'comment', rule.comment, '')}
            <div style="display:flex; gap:8px; align-items:center; margin-top: 0.5rem;">
                <button class="btn btn-primary" data-act="saveInboundRule" data-args="">${t('common.save')}</button>
                <button class="btn" data-act="hideInboundRuleForm" data-args="">${t('common.cancel')}</button>
                <span id="ir-status" style="color: var(--danger); font-size: var(--fs-sm);"></span>
            </div>
        </div>`;
    container.style.display = 'block';
    // The value field follows the verb: shown, labelled and hinted for the
    // verbs that take one, hidden for the rest.
    const actionSelect = document.getElementById('ir-action');
    if (actionSelect) actionSelect.addEventListener('change', _syncInboundValueField);
    _syncInboundValueField();
}

function hideInboundRuleForm() {
    const container = document.getElementById('inboundRuleForm');
    if (container) {
        container.innerHTML = '';
        container.style.display = 'none';
    }
}

async function saveInboundRule() {
    const value = (name) => (document.getElementById('ir-' + name) || {}).value || '';
    const id = value('id');
    const payload = {
        name: value('name').trim(),
        action: value('action'),
        action_value: value('action_value').trim(),
        match_rule_name: value('match_rule_name').trim(),
        match_source: value('match_source').trim(),
        match_environment: value('match_environment').trim(),
        match_payload: value('match_payload').trim(),
        match_importance: value('match_importance').trim(),
        comment: value('comment').trim(),
        enabled: true
    };
    const status = document.getElementById('ir-status');
    try {
        const result = id ? await API.updateInboundRule(id, payload) : await API.createInboundRule(payload);
        if (!result.success) {
            // The server refuses rules that would never match; showing that
            // verbatim is the whole point of validating on write.
            if (status) status.textContent = result.error || result.detail || t('common.unknownError');
            return;
        }
        hideInboundRuleForm();
        await loadInboundRules();
    } catch (error) {
        if (status) status.textContent = error.message || String(error);
    }
}

async function deleteInboundRule(id) {
    // Themed dialog, not the browser's confirm(): native dialogs are the one
    // surface the design system cannot reach, and the rest of the dashboard
    // already replaced them.
    if (!(await wwConfirm(t('inbound.confirmDelete'), { danger: true }))) return;
    try {
        await API.deleteInboundRule(id);
        await loadInboundRules();
    } catch (error) {
        console.error('Failed to delete inbound rule:', error);
    }
}

const InboundRulesModule = {
    load: loadInboundRules,
    search: function (value) {
        inboundQuery = String(value || '');
        inboundPage = 1;
        renderInboundRules(inboundRules);
    },
    page: function (page) {
        inboundPage = Number(page) || 1;
        renderInboundRules(inboundRules);
    }
};
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('InboundRulesModule', InboundRulesModule);


// Matching is exact, so a name that is off by one character excludes nothing
// and says nothing. The names are in the traffic; this puts them in front of
// the person writing the rule instead of asking them to remember.
async function pickInboundRuleName() {
    const box = document.getElementById('ir-picker');
    if (!box) return;
    if (box.style.display === 'block') { box.style.display = 'none'; return; }
    box.style.display = 'block';
    box.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
    try {
        const result = await API.getAlertRuleInventory(30);
        const rules = (result && result.rules) || [];
        if (!rules.length) {
            box.innerHTML = `<div class="da-preview-empty">${escapeHtml(t('inbound.pickEmpty'))}</div>`;
            return;
        }
        box.innerHTML = `<div class="ir-picker-list">` + rules.map(function (row) {
            // distinct_verdicts is the useful column: a rule the model has only
            // ever answered one way is the safe kind to stop paying for.
            const verdicts = Number(row.distinct_verdicts || 0);
            const hint = verdicts <= 1 ? t('inbound.pickSteady') : t('inbound.pickVaries', { n: verdicts });
            return `<button type="button" class="ir-pick" data-ir-pick="${escapeHtml(String(row.rule || ''))}">
                <span class="ir-pick-name">${escapeHtml(String(row.rule || ''))}</span>
                <span class="ir-pick-meta">${escapeHtml(String(row.alerts || 0))} · ${escapeHtml(hint)}</span>
            </button>`;
        }).join('') + `</div>`;
        box.querySelectorAll('[data-ir-pick]').forEach(function (button) {
            button.addEventListener('click', function () {
                const input = document.getElementById('ir-match_rule_name');
                const picked = button.getAttribute('data-ir-pick');
                if (!input) return;
                const existing = input.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
                if (existing.indexOf(picked) < 0) existing.push(picked);
                input.value = existing.join(',');
            });
        });
    } catch (error) {
        box.innerHTML = `<div class="da-preview-error">${escapeHtml(error.message || String(error))}</div>`;
    }
}
