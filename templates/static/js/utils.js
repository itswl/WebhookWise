/**
 * Utility functions module
 * Provides common utility functions such as time formatting, number formatting, JSON highlighting, and clipboard operations
 */

/**
 * Format a timestamp into a local time string
 * @param {number} timestamp - Timestamp (milliseconds)
 * @returns {string} The formatted time string (MM/DD HH:mm)
 */
function _pad2(n) {
    return n < 10 ? '0' + n : String(n);
}

/* ONE time family for the whole dashboard (the views had four dialects:
   slashed locale dates, dashed ISO slices, truncated ISO, relative-only):
   - formatTime      → lists.  MM-DD HH:mm, year prefixed only when it differs.
   - formatTimeFull  → detail. YYYY-MM-DD HH:mm:ss.
   - timeAgo         → relative, next to an absolute or with it in the title.
   Deterministic output (no locale variance), sortable by eye. */
function formatTime(timestamp) {
    if (timestamp === null || timestamp === undefined || timestamp === '') return '-';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return '-';
    const base = _pad2(date.getMonth() + 1) + '-' + _pad2(date.getDate())
        + ' ' + _pad2(date.getHours()) + ':' + _pad2(date.getMinutes());
    return date.getFullYear() === new Date().getFullYear()
        ? base
        : date.getFullYear() + '-' + base;
}

function formatTimeFull(timestamp) {
    if (timestamp === null || timestamp === undefined || timestamp === '') return '-';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return '-';
    return date.getFullYear() + '-' + _pad2(date.getMonth() + 1) + '-' + _pad2(date.getDate())
        + ' ' + _pad2(date.getHours()) + ':' + _pad2(date.getMinutes()) + ':' + _pad2(date.getSeconds());
}

/**
 * Calculate relative time (how long ago)
 * @param {number} timestamp - Timestamp (milliseconds)
 * @returns {string} Relative time string (e.g.: 5 minutes ago, 2 hours ago)
 */
function timeAgo(timestamp) {
    if (timestamp === null || timestamp === undefined || timestamp === '') return '-';
    const now = new Date();
    const past = new Date(timestamp);
    if (Number.isNaN(past.getTime())) return '-';
    const seconds = Math.floor((now - past) / 1000);

    if (seconds < 60) return t('utils.timeAgo.seconds', { n: seconds });
    if (seconds < 3600) return t('utils.timeAgo.minutes', { n: Math.floor(seconds / 60) });
    if (seconds < 86400) return t('utils.timeAgo.hours', { n: Math.floor(seconds / 3600) });
    return t('utils.timeAgo.days', { n: Math.floor(seconds / 86400) });
}

/**
 * Format a number (add thousands separators)
 * @param {number} num - Number
 * @returns {string} The formatted number string
 */
function formatNumber(num) {
    return num.toLocaleString('zh-CN');
}

/**
 * JSON syntax highlighting
 * @param {object|string} json - JSON object or string
 * @returns {string} A string with syntax-highlighting HTML
 */
function syntaxHighlightJSON(json) {
    if (typeof json !== 'string') {
        json = JSON.stringify(json, null, 2);
    }

    json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function (match) {
        let cls = 'json-number';
        if (/^"/.test(match)) {
            if (/:$/.test(match)) {
                cls = 'json-key';
            } else {
                cls = 'json-string';
            }
        } else if (/true|false/.test(match)) {
            cls = 'json-boolean';
        } else if (/null/.test(match)) {
            cls = 'json-null';
        }
        return '<span class="' + cls + '">' + match + '</span>';
    });
}

/**
 * Copy code block content to the clipboard
 * @param {HTMLElement} btn - The clicked copy button element
 */
function copyToClipboard(btn) {
    const codeBlock = btn.closest('.code-wrapper').querySelector('pre');
    const text = codeBlock.textContent;

    navigator.clipboard.writeText(text).then(() => {
        const originalText = btn.textContent;
        btn.textContent = wwIcon('check') + ' ' + t('common.copied');
        btn.style.background = 'var(--success)';
        btn.style.borderColor = 'var(--success)';

        setTimeout(() => {
            btn.textContent = originalText;
            btn.style.background = '';
            btn.style.borderColor = '';
        }, 2000);
    }).catch(err => {
        console.error('Copy failed:', err);
        showToast(t('common.copyFailed'), 'error');
    });
}

/**
 * Escape HTML to prevent XSS attacks.
 *
 * The single shared implementation for the dashboard. Escapes quotes as well as
 * angle brackets/ampersands so the result is safe in both element-content and
 * attribute-value contexts. null/undefined become an empty string.
 * @param {*} value - Raw value (coerced to string)
 * @returns {string} The escaped, HTML-safe text
 */
function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

/**
 * Severity dot for an alert row. A uniform glyph whose COLOUR carries the
 * meaning — the tokens decide what "high" looks like, not the glyph.
 * Trusted markup: callers insert via innerHTML.
 */
function getAlertIcon(importance) {
    const tone = { high: 'danger', medium: 'warning', low: 'success' }[importance] || 'muted';
    return '<span class="ww-dot ww-dot-' + tone + '"></span>';
}

/**
 * Get the importance text
 * @param {string} importance - Importance level (high/medium/low)
 * @returns {string} The corresponding text
 */
function getImportanceText(importance) {
    const texts = { high: t('common.high'), medium: t('common.medium'), low: t('common.low') };
    return texts[importance] || t('common.low');
}

/**
 * Render a formatted JSON code block
 * @param {object} data - The data to render
 * @param {string} title - Code block title
 * @returns {string} HTML string
 */
function renderJSONBlock(data, title = 'JSON') {
    const jsonString = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    const highlighted = syntaxHighlightJSON(jsonString);

    let html = '<div class="code-wrapper">';
    html += '<div class="code-header">';
    html += '<span class="code-lang">' + title + '</span>';
    html += '<button class="code-copy-btn" data-act="copyToClipboard" data-self="1">' + t('utils.copy') + '</button>';
    html += '</div>';
    html += '<div class="code-block">';
    html += '<pre>' + highlighted + '</pre>';
    html += '</div>';
    html += '</div>';

    return html;
}

/**
 * Show an error message
 * @param {string} message - Error message
 */
function showError(message) {
    document.getElementById('alertList').innerHTML =
        '<div class="empty-state"><div class="empty-icon"></div><div class="empty-title">' + t('common.loadFailed') + '</div><div class="empty-text">' +
        escapeHtml(String(message || '')) + '</div><button class="btn btn-primary" data-act="AlertsModule.loadAlerts" data-args="">' + t('common.retry') + '</button></div>';
}

/**
 * Render a "Load more" pagination control consistent with the alert management view.
 */
function renderLoadMorePagination(container, options) {
    if (!container) return;

    options = options || {};
    var loaded = Math.max(0, parseInt(options.loaded, 10) || 0);
    var total = Math.max(0, parseInt(options.total, 10) || 0);
    var batchSize = Math.max(1, parseInt(options.batchSize, 10) || 200);
    var hasMore = !!options.hasMore;
    var isLoading = !!options.isLoading;
    var onLoadMore = options.onLoadMore;

    if (loaded <= 0 && total <= 0) {
        container.innerHTML = '';
        return;
    }

    var totalText = total || (hasMore ? (loaded + '+') : loaded);
    var buttonHtml = hasMore
        ? '<button data-action="load-more"' + (isLoading ? ' disabled' : '') + '>' + (isLoading ? t('common.loading') : t('utils.loadMore', { n: batchSize })) + '</button>'
        : '';

    container.innerHTML =
        '<div class="pagination compact-pagination">' +
            '<div class="pagination-info">' +
                t('utils.loadedOf', { loaded: '<strong>' + loaded + '</strong>', total: '<strong>' + totalText + '</strong>' }) +
            '</div>' +
            '<div class="pagination-buttons">' + buttonHtml + '</div>' +
        '</div>';

    var button = container.querySelector('button[data-action="load-more"]');
    if (button) {
        button.addEventListener('click', function() {
            if (button.disabled || typeof onLoadMore !== 'function') return;
            onLoadMore();
        });
    }
}

// ========== Global Elegant Toast Notification Override ==========

window.alert = function(message) {
    showToast(message);
};

function showToast(message, type = 'info') {
    let container = document.getElementById('toastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toastContainer';
        container.style.cssText = `
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            z-index: 10000;
            pointer-events: none;
        `;
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    
    let icon = '';
    let bgColor = 'var(--bg-elevated, var(--text-secondary))';
    let borderColor = 'var(--border, var(--text-secondary))';
    let textColor = 'var(--text-main, var(--bg-subtle))';

    const msgLower = String(message).toLowerCase();
    if (msgLower.includes('') || msgLower.includes('success') || msgLower.includes('成功')) {
        icon = '';
        type = 'success';
        bgColor = 'rgba(16, 185, 129, 0.15)';
        borderColor = 'rgba(16, 185, 129, 0.4)';
        textColor = 'var(--success, var(--success))';
    } else if (msgLower.includes('') || msgLower.includes('failed') || msgLower.includes('error') || msgLower.includes('失败') || msgLower.includes('crashed')) {
        icon = '';
        type = 'error';
        bgColor = 'rgba(239, 68, 68, 0.15)';
        borderColor = 'rgba(239, 68, 68, 0.4)';
        textColor = 'var(--danger, var(--danger))';
    } else if (msgLower.includes('') || msgLower.includes('warning') || msgLower.includes('警告') || msgLower.includes('conflict')) {
        icon = '';
        type = 'warning';
        bgColor = 'rgba(245, 158, 11, 0.15)';
        borderColor = 'rgba(245, 158, 11, 0.4)';
        textColor = 'var(--warning, var(--warning))';
    } else if (msgLower.includes('') || msgLower.includes('') || msgLower.includes('fresh') || msgLower.includes('started')) {
        icon = '';
        type = 'info';
        bgColor = 'rgba(99, 102, 241, 0.15)';
        borderColor = 'rgba(99, 102, 241, 0.4)';
        textColor = 'var(--primary, var(--primary))';
    }

    // Clean prefix emojis
    let cleanMessage = String(message);
    if (cleanMessage.startsWith('') || cleanMessage.startsWith('') || cleanMessage.startsWith('') || cleanMessage.startsWith('') || cleanMessage.startsWith('') || cleanMessage.startsWith('') || cleanMessage.startsWith('')) {
        cleanMessage = cleanMessage.substring(2).trim();
    }

    toast.style.cssText = `
        background: ${bgColor};
        color: ${textColor};
        border: 1px solid ${borderColor};
        padding: 0.85rem 1.35rem;
        border-radius: var(--radius-lg, 10px);
        box-shadow: var(--shadow-lg, 0 10px 15px -3px rgba(0,0,0,0.1));
        font-size: 0.9rem;
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 0.65rem;
        pointer-events: auto;
        animation: toastIn 0.35s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        max-width: 420px;
        box-sizing: border-box;
    `;

    const iconElement = document.createElement('span');
    iconElement.textContent = icon;
    const messageElement = document.createElement('span');
    messageElement.style.lineHeight = '1.45';
    messageElement.style.whiteSpace = 'pre-wrap';
    messageElement.textContent = cleanMessage;
    toast.append(iconElement, messageElement);
    container.appendChild(toast);

    // Auto remove toast
    setTimeout(() => {
        toast.style.animation = 'toastOut 0.3s ease-in forwards';
        setTimeout(() => {
            toast.remove();
        }, 300);
    }, 4500);
}

/**
 * Promise-based replacements for the browser's confirm() and prompt().
 *
 * The native ones are unstyled, unthemeable and place the app's name above
 * your text — they look like a browser error in the middle of a dark
 * dashboard. They also block the event loop, which is why they were easy to
 * reach for and why they cannot be made to match anything.
 *
 * These reuse the .modal component the dashboard already ships, so ESC to
 * close and the Tab focus trap in dashboard.js apply for free rather than
 * being reimplemented (and forgotten) here.
 */
function _wwDialog({ title, message, defaultValue, withInput, confirmLabel, cancelLabel, danger }) {
    return new Promise((resolve) => {
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');

        const content = document.createElement('div');
        content.className = 'modal-content';
        content.style.maxWidth = '460px';

        const header = document.createElement('div');
        header.className = 'modal-header';
        const heading = document.createElement('div');
        heading.className = 'modal-title';
        heading.textContent = title || '';
        header.appendChild(heading);

        const body = document.createElement('div');
        body.className = 'modal-body';
        if (message) {
            const text = document.createElement('div');
            text.style.cssText = 'line-height:1.6; white-space:pre-wrap; word-break:break-word;';
            text.textContent = message;
            body.appendChild(text);
        }

        let input = null;
        if (withInput) {
            input = document.createElement('input');
            input.type = 'text';
            input.className = 'form-input';
            input.value = defaultValue == null ? '' : String(defaultValue);
            input.style.cssText = 'width:100%; margin-top:0.85rem;';
            body.appendChild(input);
        }

        const footer = document.createElement('div');
        footer.className = 'modal-footer';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn-sm';
        cancel.textContent = cancelLabel || 'Cancel';
        const ok = document.createElement('button');
        ok.type = 'button';
        ok.className = 'btn btn-sm ' + (danger ? 'btn-danger' : 'btn-primary');
        ok.textContent = confirmLabel || 'OK';
        footer.append(cancel, ok);

        content.append(header, body, footer);
        modal.appendChild(content);
        document.body.appendChild(modal);

        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            modal.remove();
            document.removeEventListener('keydown', onKey, true);
            resolve(value);
        };
        // Cancel, not confirm, on every non-answer: dismissing a dialog you did
        // not read must never be the same as agreeing to it.
        const onKey = (event) => {
            if (event.key === 'Escape') { event.stopPropagation(); finish(withInput ? null : false); }
            if (event.key === 'Enter' && withInput) { event.preventDefault(); finish(input.value); }
        };
        document.addEventListener('keydown', onKey, true);
        cancel.addEventListener('click', () => finish(withInput ? null : false));
        ok.addEventListener('click', () => finish(withInput ? input.value : true));
        modal.addEventListener('click', (event) => {
            if (event.target === modal) finish(withInput ? null : false);
        });

        (input || ok).focus();
        if (input) input.select();
    });
}

/**
 * Pick one of a few values. Resolves the chosen value, or null if dismissed.
 *
 * A prompt() asking you to type "acknowledged" is a spelling test. These are
 * closed sets, so the choices should be the control.
 */
function wwChoose(message, choices) {
    return new Promise((resolve) => {
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');

        const content = document.createElement('div');
        content.className = 'modal-content';
        content.style.maxWidth = '380px';

        const header = document.createElement('div');
        header.className = 'modal-header';
        const heading = document.createElement('div');
        heading.className = 'modal-title';
        heading.textContent = message || '';
        header.appendChild(heading);

        const body = document.createElement('div');
        body.className = 'modal-body';
        body.style.cssText = 'display:flex; flex-direction:column; gap:0.5rem;';

        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            modal.remove();
            document.removeEventListener('keydown', onKey, true);
            resolve(value);
        };
        const onKey = (event) => {
            if (event.key === 'Escape') { event.stopPropagation(); finish(null); }
        };

        (choices || []).forEach((choice) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn' + (choice.active ? ' is-active' : '');
            button.style.cssText = 'justify-content:flex-start; text-align:left;';
            // The current value is marked, not hidden: knowing where you are is
            // half of choosing where to go. Marked with the sprite, not a check
            // emoji — every glyph must share one stroke voice and currentColor.
            button.textContent = choice.label;
            if (choice.active && typeof wwIcon === 'function') {
                button.insertAdjacentHTML('beforeend', ' ' + wwIcon('check'));
            }
            button.addEventListener('click', () => finish(choice.value));
            body.appendChild(button);
        });

        const footer = document.createElement('div');
        footer.className = 'modal-footer';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn-sm';
        cancel.textContent = typeof t === 'function' ? t('common.cancel') : 'Cancel';
        cancel.addEventListener('click', () => finish(null));
        footer.appendChild(cancel);

        content.append(header, body, footer);
        modal.appendChild(content);
        modal.addEventListener('click', (event) => { if (event.target === modal) finish(null); });
        document.addEventListener('keydown', onKey, true);
        document.body.appendChild(modal);
        const first = body.querySelector('button');
        if (first) first.focus();
    });
}

/** Styled confirm(). Resolves true/false — never throws, never blocks. */
function wwConfirm(message, options) {
    const opts = options || {};
    return _wwDialog({
        title: opts.title || (typeof t === 'function' ? t('common.confirmTitle') : 'Confirm'),
        message: message,
        withInput: false,
        confirmLabel: opts.confirmLabel || (typeof t === 'function' ? t('common.confirm') : 'Confirm'),
        cancelLabel: opts.cancelLabel || (typeof t === 'function' ? t('common.cancel') : 'Cancel'),
        danger: !!opts.danger
    });
}

/** Styled prompt(). Resolves the string, or null when dismissed. */
function wwPrompt(message, defaultValue, options) {
    const opts = options || {};
    return _wwDialog({
        title: opts.title || (typeof t === 'function' ? t('common.inputTitle') : 'Input'),
        message: message,
        defaultValue: defaultValue,
        withInput: true,
        confirmLabel: opts.confirmLabel || (typeof t === 'function' ? t('common.ok') : 'OK'),
        cancelLabel: opts.cancelLabel || (typeof t === 'function' ? t('common.cancel') : 'Cancel')
    });
}

/**
 * A toast that offers to take the last action back.
 *
 * Acknowledge and Resolve are a single click, and the click is easy to make by
 * accident — most easily of all from a phone. Offering the reversal at the
 * moment of the mistake is the only time the operator is still thinking about
 * it; finding the alert again ten minutes later to set the status by hand is
 * not the same affordance.
 *
 * Dwells longer than an ordinary toast (12s) because it is not a status
 * message, it is a decision. The handler is attached with addEventListener,
 * never an inline attribute: script-src-attr is 'none'.
 */
function showUndoToast(message, onUndo, undoLabel) {
    showToast(message, 'success');
    const container = document.getElementById('toastContainer');
    if (!container || typeof onUndo !== 'function') return;
    const toast = container.lastElementChild;
    if (!toast) return;

    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = undoLabel || 'Undo';
    button.style.cssText = `
        margin-left: auto;
        background: transparent;
        border: 1px solid currentColor;
        color: inherit;
        font: inherit;
        font-size: 0.8rem;
        padding: 0.15rem 0.6rem;
        border-radius: var(--radius-sm, 6px);
        cursor: pointer;
        flex-shrink: 0;
    `;
    let done = false;
    button.addEventListener('click', async () => {
        if (done) return;
        done = true;
        button.disabled = true;
        button.style.opacity = '0.6';
        try {
            await onUndo();
        } finally {
            toast.remove();
        }
    });
    toast.appendChild(button);

    // Outlive the 4.5s auto-remove that showToast scheduled: a reversal the
    // operator never got the chance to click is not an offer.
    const keepUntil = Date.now() + 12000;
    const guard = setInterval(() => {
        if (!toast.isConnected || Date.now() > keepUntil) {
            clearInterval(guard);
            if (toast.isConnected) toast.remove();
            return;
        }
        toast.style.animation = '';
        toast.style.opacity = '1';
    }, 250);
}

/**
 * Inline icon from the sprite in dashboard.html.
 *
 * Returns trusted markup — never pass the result through escapeHtml. Icons are
 * decorative (aria-hidden); meaning must always come from the adjacent text.
 */
function wwIcon(name, extraClass) {
    return '<svg class="icon' + (extraClass ? ' ' + extraClass : '') + '" aria-hidden="true">'
        + '<use href="#i-' + name + '"/></svg>';
}

/**
 * Trusted-markup wrapper for html`` interpolations: the fragment is spliced
 * verbatim instead of being escaped. Reserve it for markup a renderer built
 * itself (wwIcon output, nested html`` results) — never for payload data.
 */
function htmlRaw(markup) {
    return { __wwHtml: String(markup == null ? '' : markup) };
}

/**
 * Tagged template for HTML fragments: every interpolation is escaped unless
 * explicitly wrapped in htmlRaw(). This replaces manual concatenation for
 * new renderers — the "' + escapeHtml(x) + '" quoting dance is exactly where
 * the splice-defect class (wwIcon fragments shipping as page text, missed
 * escapes in attribute position) came from.
 */
function html(strings) {
    let out = strings[0];
    for (let i = 1; i < arguments.length; i++) {
        const value = arguments[i];
        out += (value && value.__wwHtml !== undefined)
            ? value.__wwHtml
            : (value === null || value === undefined) ? '' : escapeHtml(String(value));
        out += strings[i];
    }
    return out;
}

/**
 * Muted " · source" suffix for rule-grain labels (trusted markup; inputs
 * escaped here). Rule aggregates carry the sending system(s) alongside the
 * rule name; unidentified senders fall back to their source AS the name, in
 * which case repeating it would be noise — so the suffix self-suppresses.
 */
function wwSourceSuffix(name, sources) {
    var list = Array.isArray(sources) ? sources.filter(Boolean) : [];
    if (!list.length) return '';
    if (list.length === 1 && String(list[0]) === String(name)) return '';
    return '<span class="ww-muted"> · ' + escapeHtml(list.join(', ')) + '</span>';
}

/* ── Global delegated action dispatch ────────────────────────────────────
   The last mile to script-src-attr 'none': markup carries a function NAME
   and plain scalar args in data attributes, never code. A click resolves
   the name against an allowlist — an attacker-controlled data-act cannot
   reach anything the dashboard did not publish here.

     data-act="DecisionTraceModule.toggleExpand" data-args="123"
     data-act="navigateTo" data-args="trace"
     data-self="1"     → the clicked element is passed as the last argument
     data-stop / data-prevent → stopPropagation / preventDefault first
*/
var WW_ACTION_ROOTS = [
    'AlertsModule', 'DecisionTraceModule', 'DeepAnalysesModule', 'OverviewModule',
    'SilencesModule', 'RuntimeSettingsModule', 'IncidentsModule', 'ForwardRulesModule',
    'RoutingModule', 'ResponseCenterModule', 'KbDraftsModule', 'NoiseCenterModule',
    'ActionCenterModule', 'SandboxModule', 'IngressSetupModule', 'CommandPalette'
];
var WW_ACTION_GLOBALS = [
    'navigateTo', 'openAlert', 'openIncident', 'closeSidebarDrawer', 'copyToClipboard',
    'loadForwardRules', 'loadSilences', 'loadMaintenanceWindows', 'testRule',
    'showRuleForm', 'deleteRule', 'showSilenceForm', 'showMaintenanceWindowForm',
    'liftSilence', 'toggleTheme', 'openAuthModal',
    'navigateFromSidebar', 'drillSilenceToTrace',
    'loadInboundRules', 'showInboundRuleForm', 'hideInboundRuleForm', 'saveInboundRule', 'deleteInboundRule',
    // Registered late: these three rendered buttons whose clicks resolved to
    // nothing, in silences, maintenance windows and the forwarding page.
    'deleteSilence', 'deleteMaintenanceWindow', 'testRuleGateway'
];

function wwResolveAction(name) {
    if (!name) return null;
    var parts = String(name).split('.');
    if (parts.length === 1) {
        if (WW_ACTION_GLOBALS.indexOf(parts[0]) < 0) return null;
        var fn = window[parts[0]] || (typeof globalThis !== 'undefined' ? globalThis[parts[0]] : undefined);
        return typeof fn === 'function' ? fn : null;
    }
    if (parts.length !== 2 || WW_ACTION_ROOTS.indexOf(parts[0]) < 0) return null;
    var root = window[parts[0]];
    if (!root || typeof root[parts[1]] !== 'function') return null;
    return root[parts[1]].bind(root);
}

function wwParseArgs(raw) {
    if (raw === null || raw === undefined || raw === '') return [];
    return String(raw).split(',').map(function (token) {
        var value = token.trim();
        if (/^-?\d+$/.test(value)) return Number(value);
        if (value === 'true') return true;
        if (value === 'false') return false;
        return value;
    });
}

document.addEventListener('click', function (event) {
    var el = event.target.closest('[data-act], [data-stop]');
    if (!el) return;
    if (el.hasAttribute('data-stop')) event.stopPropagation();
    if (el.hasAttribute('data-prevent')) event.preventDefault();
    if (!el.hasAttribute('data-act')) return;
    var fn = wwResolveAction(el.getAttribute('data-act'));
    if (!fn) return;
    var args = wwParseArgs(el.getAttribute('data-args'));
    if (el.hasAttribute('data-self')) args.push(el);
    fn.apply(null, args);
});
