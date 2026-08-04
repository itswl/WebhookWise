/**
 * Runtime Settings admin panel (Operations → Runtime settings sub-view).
 *
 * Live-tunable policy values from GET /v1/runtime-settings, grouped by domain.
 * Each key has an environment default plus an optional admin override; the
 * effective value is the override when set, otherwise the env default. Saving
 * (PUT) or clearing (DELETE) an override is admin-write and propagates to all
 * processes within ~1 minute (pub/sub + periodic refresh) — no restart.
 * Mirrors the kb-drafts Operations sub-view pattern.
 */
const RuntimeSettingsModule = (function () {
    // Fixed display order for domain groups; unknown domains render after them
    // so a newly added backend domain is never hidden.
    const DOMAIN_ORDER = ['flapping', 'escalation', 'backpressure', 'kb', 'noise', 'cadence', 'retention'];

    const MONO = 'font-family: ui-monospace, SFMono-Regular, Menlo, monospace;';

    let settings = [];
    let editingKey = null;    // key currently in inline edit mode (one at a time)
    let editingValue = null;  // preserves the typed value across a failed-save re-render
    let rowError = null;      // { key, message } — backend message shown verbatim in the row

    function domainLabel(domain) {
        const key = 'rs.domain.' + domain;
        const label = t(key);
        return label === key ? domain : label;
    }

    /**
     * Pick the edit widget from the env default: booleans get a true/false
     * select, numerics a number input, everything else free text.
     */
    function valueKind(setting) {
        const env = String(setting.env_value != null ? setting.env_value : '').trim();
        const lower = env.toLowerCase();
        if (lower === 'true' || lower === 'false') return 'boolean';
        if (env !== '' && isFinite(Number(env))) return 'number';
        return 'text';
    }

    function hasOverride(setting) {
        return setting.override !== null && setting.override !== undefined;
    }

    function displayValue(value) {
        if (value === null || value === undefined || String(value) === '') return '—';
        return String(value);
    }

    /**
     * Format an ISO timestamp; falls back to the project-wide formatTime if present.
     */
    function formatSettingTime(iso) {
        if (!iso) return '';
        if (typeof formatTime === 'function') return formatTime(iso);
        try {
            return formatTimeFull(iso);
        } catch (e) {
            return iso;
        }
    }

    /**
     * Inline editor for one row: input prefilled with the effective value (or
     * the value the operator already typed, after a failed save), then
     * Save / Clear override (only when an override exists) / Cancel.
     */
    function renderEditor(setting) {
        const kind = valueKind(setting);
        const raw = editingValue !== null
            ? editingValue
            : String(setting.effective != null ? setting.effective : '');
        let input;
        if (kind === 'boolean') {
            const truthy = raw.trim().toLowerCase() === 'true';
            input = '<select id="rsEditInput" class="filter-input" style="width: 100%;">' +
                '<option value="true"' + (truthy ? ' selected' : '') + '>true</option>' +
                '<option value="false"' + (truthy ? '' : ' selected') + '>false</option>' +
                '</select>';
        } else if (kind === 'number') {
            input = '<input type="number" step="any" id="rsEditInput" class="filter-input" style="width: 100%; ' + MONO + '" value="' + escapeHtml(raw) + '">';
        } else {
            input = '<input type="text" id="rsEditInput" class="filter-input" style="width: 100%; ' + MONO + '" value="' + escapeHtml(raw) + '" autocomplete="off" spellcheck="false">';
        }
        return '<div style="display: flex; flex-direction: column; gap: 6px; min-width: 200px;">' + input +
            '<div style="display: flex; gap: 6px; flex-wrap: wrap;">' +
            '<button type="button" class="btn btn-sm btn-primary" data-rs-save="' + escapeHtml(setting.key) + '">' + t('common.save') + '</button>' +
            (hasOverride(setting)
                ? '<button type="button" class="btn btn-sm" data-rs-clear="' + escapeHtml(setting.key) + '" style="color: var(--danger); border-color: rgba(225,29,72,0.25); background: var(--danger-bg);">' + t('rs.action.clearOverride') + '</button>'
                : '') +
            '<button type="button" class="btn btn-sm" data-rs-cancel>' + t('common.cancel') + '</button>' +
            '</div></div>';
    }

    function renderRow(setting) {
        const td = 'padding: 0.55rem 0.75rem; border-top: 1px solid var(--border); vertical-align: top;';
        const overridden = hasOverride(setting);
        const errorHtml = rowError && rowError.key === setting.key
            ? '<div style="color: var(--danger); font-size: 0.78rem; margin-top: 4px; overflow-wrap: anywhere;">' + escapeHtml(rowError.message) + '</div>'
            : '';

        let overrideCell;
        if (editingKey === setting.key) {
            overrideCell = renderEditor(setting) + errorHtml;
        } else {
            overrideCell = '<span style="' + MONO + '">' + (overridden ? escapeHtml(String(setting.override)) : '—') + '</span>' +
                '<button type="button" class="btn btn-sm" data-rs-edit="' + escapeHtml(setting.key) + '" style="margin-left: 6px;">' + wwIcon('pencil') + ' ' + t('rs.action.edit') + '</button>' +
                errorHtml;
        }

        const updatedBy = setting.updated_by ? escapeHtml(String(setting.updated_by)) : '';
        const updatedAt = setting.updated_at ? escapeHtml(formatSettingTime(setting.updated_at)) : '';
        const updatedCell = (updatedBy || updatedAt)
            ? updatedBy + (updatedBy && updatedAt ? '<br>' : '') +
                '<span style="color: var(--text-muted); white-space: nowrap;">' + updatedAt + '</span>'
            : '—';

        return '<tr' + (overridden ? ' style="background: rgba(245, 158, 11, 0.06);"' : '') + '>' +
            '<td style="' + td + ' ' + MONO + ' overflow-wrap: anywhere; font-weight: 600;">' + escapeHtml(setting.key) + '</td>' +
            '<td style="' + td + ' color: var(--text-secondary);">' + escapeHtml(setting.description || '') + '</td>' +
            '<td style="' + td + ' ' + MONO + '">' + escapeHtml(displayValue(setting.env_value)) + '</td>' +
            '<td style="' + td + '">' + overrideCell + '</td>' +
            '<td style="' + td + '"><strong style="' + MONO + '">' + escapeHtml(displayValue(setting.effective)) + '</strong>' +
                (overridden ? ' <span class="badge badge-medium" style="font-size: 0.65rem;">' + t('rs.badge.override') + '</span>' : '') + '</td>' +
            '<td style="' + td + ' font-size: 0.8rem;">' + updatedCell + '</td>' +
            '</tr>';
    }

    function render() {
        const container = document.getElementById('runtimeSettingsList');
        if (!container) return;

        if (!settings.length) {
            container.innerHTML = '<div class="empty-state" style="text-align: center; padding: 60px; color: var(--text-secondary);">' +
                '<div style="font-size: 48px; margin-bottom: 16px;">' + wwIcon('sliders') + '</div>' +
                '<div class="empty-title">' + escapeHtml(t('rs.empty.title')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('rs.empty.text')) + '</div></div>';
            return;
        }

        const groups = {};
        settings.forEach(function (setting) {
            const domain = String(setting.domain || 'other');
            (groups[domain] = groups[domain] || []).push(setting);
        });
        const domains = DOMAIN_ORDER.filter(function (domain) { return groups[domain]; });
        Object.keys(groups).sort().forEach(function (domain) {
            if (domains.indexOf(domain) < 0) domains.push(domain);
        });

        const th = 'padding: 0.55rem 0.75rem; font-weight: 600;';
        let html = '<div style="overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface);">';
        html += '<table style="width: 100%; border-collapse: collapse; font-size: 0.85rem;">';
        html += '<thead><tr style="color: var(--text-muted); text-align: left;">' +
            '<th style="' + th + '">' + t('rs.col.key') + '</th>' +
            '<th style="' + th + '">' + t('rs.col.description') + '</th>' +
            '<th style="' + th + '">' + t('rs.col.envDefault') + '</th>' +
            '<th style="' + th + '">' + t('rs.col.override') + '</th>' +
            '<th style="' + th + '">' + t('rs.col.effective') + '</th>' +
            '<th style="' + th + '">' + t('rs.col.updated') + '</th>' +
            '</tr></thead><tbody>';
        domains.forEach(function (domain) {
            html += '<tr><td colspan="6" style="padding: 0.6rem 0.75rem; border-top: 1px solid var(--border); background: var(--bg-subtle); font-weight: 700; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-secondary);">' +
                escapeHtml(domainLabel(domain)) + '</td></tr>';
            groups[domain].slice().sort(function (a, b) {
                return String(a.key).localeCompare(String(b.key));
            }).forEach(function (setting) {
                html += renderRow(setting);
            });
        });
        html += '</tbody></table></div>';
        container.innerHTML = html;
        bindRowActions(container);
    }

    function bindRowActions(container) {
        container.querySelectorAll('[data-rs-edit]').forEach(function (button) {
            button.addEventListener('click', function () { startEdit(button.getAttribute('data-rs-edit')); });
        });
        container.querySelectorAll('[data-rs-save]').forEach(function (button) {
            button.addEventListener('click', function () { save(button.getAttribute('data-rs-save')); });
        });
        container.querySelectorAll('[data-rs-clear]').forEach(function (button) {
            button.addEventListener('click', function () { clearOverride(button.getAttribute('data-rs-clear')); });
        });
        container.querySelectorAll('[data-rs-cancel]').forEach(function (button) {
            button.addEventListener('click', cancelEdit);
        });
        const input = document.getElementById('rsEditInput');
        if (input) {
            input.addEventListener('keydown', function (event) {
                if (event.key === 'Enter' && editingKey) {
                    event.preventDefault();
                    save(editingKey);
                } else if (event.key === 'Escape') {
                    cancelEdit();
                }
            });
            window.requestAnimationFrame(function () { input.focus(); });
        }
    }

    function startEdit(key) {
        editingKey = key;
        editingValue = null;
        rowError = null;
        render();
    }

    function cancelEdit() {
        editingKey = null;
        editingValue = null;
        rowError = null;
        render();
    }

    // Disable the editor controls while a request is in flight; the re-render
    // on either outcome recreates them enabled.
    function setRowBusy() {
        const container = document.getElementById('runtimeSettingsList');
        if (!container) return;
        container.querySelectorAll('[data-rs-save], [data-rs-clear], [data-rs-cancel], #rsEditInput').forEach(function (element) {
            element.disabled = true;
        });
    }

    /**
     * Merge the PUT/DELETE response (the updated setting object) back into the
     * local list; fall back to a full reload when the shape is unexpected.
     */
    function applyUpdated(result) {
        const updated = result && result.data;
        if (updated && updated.key) {
            const index = settings.findIndex(function (s) { return s.key === updated.key; });
            if (index >= 0) settings[index] = updated; else settings.push(updated);
            render();
            return;
        }
        load();
    }

    async function save(key) {
        const input = document.getElementById('rsEditInput');
        if (!input || editingKey !== key) return;
        const value = String(input.value);
        setRowBusy();
        try {
            const result = await API.updateRuntimeSetting(key, value);
            editingKey = null;
            editingValue = null;
            rowError = null;
            applyUpdated(result);
            alert('' + t('rs.alert.saved'));
        } catch (error) {
            // Keep the row in edit mode with the typed value; the backend's
            // validation message is shown verbatim under the input.
            editingValue = value;
            rowError = { key: key, message: error.message || String(error) };
            render();
        }
    }

    async function clearOverride(key) {
        if (!window.confirm(t('rs.confirm.clear'))) return;
        setRowBusy();
        try {
            const result = await API.clearRuntimeSetting(key);
            editingKey = null;
            editingValue = null;
            rowError = null;
            applyUpdated(result);
            alert('' + t('rs.alert.cleared'));
        } catch (error) {
            rowError = { key: key, message: error.message || String(error) };
            render();
        }
    }

    async function load() {
        const container = document.getElementById('runtimeSettingsList');
        if (!container) return;
        editingKey = null;
        editingValue = null;
        rowError = null;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        try {
            const result = await API.getRuntimeSettings();
            settings = (result && result.data && result.data.settings) || [];
            render();
        } catch (error) {
            container.innerHTML = '<div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">' +
                '<p>' + escapeHtml(t('common.loadFailed')) + ': ' + escapeHtml(error.message || String(error)) + '</p>' +
                '<button class="btn" data-act="RuntimeSettingsModule.load" data-args="" style="margin-top: 10px;">' + t('common.retry') + '</button></div>';
        }
    }

    return { load: load };
})();
