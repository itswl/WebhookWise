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
    let query = '';           // live filter over key + description

    function domainLabel(domain) {
        const key = 'rs.domain.' + domain;
        const label = t(key);
        return label === key ? domain : label;
    }

    /**
     * Localized description with the backend's English text as fallback —
     * the reflection payload is English by contract, the zh dictionary
     * overlays it per key ('rs.desc.<KEY>').
     */
    function descText(setting) {
        const key = 'rs.desc.' + setting.key;
        const label = t(key);
        return label === key ? String(setting.description || '') : label;
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

    /**
     * One setting = one row, three columns. The old six-column layout let a
     * long env default (the keyword CSV lists) blow the table past the
     * viewport, shoving the override/effective/actions columns — including
     * the ONLY edit affordance — behind a horizontal scroll nobody notices.
     * Now: identity | value (wraps, with provenance underneath) | actions.
     */
    function renderRow(setting) {
        const overridden = hasOverride(setting);
        const errorHtml = rowError && rowError.key === setting.key
            ? '<div class="rs-error">' + escapeHtml(rowError.message) + '</div>'
            : '';

        let valueCell;
        if (editingKey === setting.key) {
            valueCell = renderEditor(setting) + errorHtml;
        } else {
            const meta = [];
            if (overridden) {
                meta.push('<span class="badge badge-medium rs-badge">' + t('rs.badge.override') + '</span>');
                meta.push('<span>' + escapeHtml(t('rs.meta.envDefault', { value: displayValue(setting.env_value) })) + '</span>');
                const by = setting.updated_by ? String(setting.updated_by) : '';
                const at = setting.updated_at ? formatSettingTime(setting.updated_at) : '';
                if (by || at) meta.push('<span>' + escapeHtml((by + ' ' + at).trim()) + '</span>');
            }
            valueCell = '<div class="rs-value">' + escapeHtml(displayValue(setting.effective)) + '</div>' +
                (meta.length ? '<div class="rs-meta">' + meta.join(' · ') + '</div>' : '') +
                errorHtml;
        }

        return '<tr' + (overridden ? ' class="rs-row-override"' : '') + '>' +
            // The key keeps nowrap: it is an identifier, and overflow-wrap
            // "anywhere" once crushed this column into vertical letter-soup.
            // The description below it wraps freely.
            '<td class="rs-cell"><div class="rs-key">' + escapeHtml(setting.key) + '</div>' +
                '<div class="rs-desc">' + escapeHtml(descText(setting)) + '</div></td>' +
            '<td class="rs-cell">' + valueCell + '</td>' +
            '<td class="rs-cell rs-actions">' +
                (editingKey === setting.key
                    ? ''
                    : '<button type="button" class="btn btn-sm" data-rs-edit="' + escapeHtml(setting.key) + '">' + wwIcon('pencil') + ' ' + t('rs.action.edit') + '</button>') +
            '</td></tr>';
    }

    function matches(setting) {
        if (!query) return true;
        const q = query.toLowerCase();
        return String(setting.key).toLowerCase().indexOf(q) >= 0 ||
            String(setting.description || '').toLowerCase().indexOf(q) >= 0 ||
            descText(setting).toLowerCase().indexOf(q) >= 0;
    }

    function render() {
        const host = document.getElementById('rsTableHost');
        if (!host) return;

        if (!settings.length) {
            host.innerHTML = '<div class="empty-state" style="text-align: center; padding: 60px; color: var(--text-secondary);">' +
                '<div style="font-size: 48px; margin-bottom: 16px;">' + wwIcon('sliders') + '</div>' +
                '<div class="empty-title">' + escapeHtml(t('rs.empty.title')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('rs.empty.text')) + '</div></div>';
            return;
        }

        const visible = settings.filter(matches);
        const groups = {};
        visible.forEach(function (setting) {
            const domain = String(setting.domain || 'other');
            (groups[domain] = groups[domain] || []).push(setting);
        });
        const domains = DOMAIN_ORDER.filter(function (domain) { return groups[domain]; });
        Object.keys(groups).sort().forEach(function (domain) {
            if (domains.indexOf(domain) < 0) domains.push(domain);
        });

        let html = '<div class="rs-wrap"><table class="rs-table">';
        html += '<thead><tr>' +
            '<th>' + t('rs.col.key') + '</th>' +
            '<th>' + t('rs.col.value') + '</th>' +
            '<th></th>' +
            '</tr></thead><tbody>';
        if (!visible.length) {
            html += '<tr><td colspan="3" class="rs-cell" style="color: var(--text-secondary);">' +
                escapeHtml(t('rs.search.noMatch')) + '</td></tr>';
        }
        domains.forEach(function (domain) {
            html += '<tr class="rs-group"><td colspan="3">' + escapeHtml(domainLabel(domain)) + '</td></tr>';
            groups[domain].slice().sort(function (a, b) {
                return String(a.key).localeCompare(String(b.key));
            }).forEach(function (setting) {
                html += renderRow(setting);
            });
        });
        html += '</tbody></table></div>';
        host.innerHTML = html;
        bindRowActions(host);
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
            showToast(t('rs.alert.saved'), 'info');
        } catch (error) {
            // Keep the row in edit mode with the typed value; the backend's
            // validation message is shown verbatim under the input.
            editingValue = value;
            rowError = { key: key, message: error.message || String(error) };
            render();
        }
    }

    async function clearOverride(key) {
        if (!(await wwConfirm(t('rs.confirm.clear')))) return;
        setRowBusy();
        try {
            const result = await API.clearRuntimeSetting(key);
            editingKey = null;
            editingValue = null;
            rowError = null;
            applyUpdated(result);
            showToast(t('rs.alert.cleared'), 'info');
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
        query = '';
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        try {
            const result = await API.getRuntimeSettings();
            settings = (result && result.data && result.data.settings) || [];
            // The search box lives OUTSIDE the re-rendered table host so
            // typing never rebuilds (and un-focuses) the input itself.
            container.innerHTML =
                '<div class="rs-toolbar"><input id="rsSearch" class="filter-input" type="search" ' +
                'placeholder="' + escapeHtml(t('rs.search.placeholder')) + '" autocomplete="off"></div>' +
                '<div id="rsTableHost"></div>';
            const search = document.getElementById('rsSearch');
            if (search) {
                search.addEventListener('input', function () {
                    query = String(search.value || '').trim();
                    render();
                });
            }
            render();
        } catch (error) {
            container.innerHTML = '<div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">' +
                '<p>' + escapeHtml(t('common.loadFailed')) + ': ' + escapeHtml(error.message || String(error)) + '</p>' +
                '<button class="btn" data-act="RuntimeSettingsModule.load" data-args="" style="margin-top: 10px;">' + t('common.retry') + '</button></div>';
        }
    }

    return { load: load };
})();

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="RuntimeSettingsModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('RuntimeSettingsModule', RuntimeSettingsModule);
