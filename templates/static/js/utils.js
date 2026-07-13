/**
 * Utility functions module
 * Provides common utility functions such as time formatting, number formatting, JSON highlighting, and clipboard operations
 */

/**
 * Format a timestamp into a local time string
 * @param {number} timestamp - Timestamp (milliseconds)
 * @returns {string} The formatted time string (MM/DD HH:mm)
 */
function formatTime(timestamp) {
    if (timestamp === null || timestamp === undefined || timestamp === '') return '-';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return '-';
    return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
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
        btn.textContent = '✅ ' + t('common.copied');
        btn.style.background = '#28a745';
        btn.style.borderColor = '#28a745';

        setTimeout(() => {
            btn.textContent = originalText;
            btn.style.background = '';
            btn.style.borderColor = '';
        }, 2000);
    }).catch(err => {
        console.error('Copy failed:', err);
        alert(t('common.copyFailed'));
    });
}

/**
 * Escape HTML to prevent XSS attacks
 * @param {string} text - Raw text
 * @returns {string} The escaped, HTML-safe text
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Get the alert icon
 * @param {string} importance - Importance level (high/medium/low)
 * @returns {string} The corresponding emoji icon
 */
function getAlertIcon(importance) {
    const icons = { high: '🔴', medium: '🟡', low: '🟢' };
    return icons[importance] || '⚪';
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
    html += '<button class="code-copy-btn" onclick="copyToClipboard(this)">📋 ' + t('utils.copy') + '</button>';
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
        '<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">' + t('common.loadFailed') + '</div><div class="empty-text">' +
        escapeHtml(String(message || '')) + '</div><button class="btn btn-primary" onclick="AlertsModule.loadAlerts()">' + t('common.retry') + '</button></div>';
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
    
    let icon = 'ℹ️';
    let bgColor = 'var(--bg-elevated, #1e293b)';
    let borderColor = 'var(--border, #475569)';
    let textColor = 'var(--text-main, #f8fafc)';

    const msgLower = String(message).toLowerCase();
    if (msgLower.includes('✅') || msgLower.includes('success') || msgLower.includes('成功')) {
        icon = '✅';
        type = 'success';
        bgColor = 'rgba(16, 185, 129, 0.15)';
        borderColor = 'rgba(16, 185, 129, 0.4)';
        textColor = 'var(--success, #10b981)';
    } else if (msgLower.includes('❌') || msgLower.includes('failed') || msgLower.includes('error') || msgLower.includes('失败') || msgLower.includes('crashed')) {
        icon = '❌';
        type = 'error';
        bgColor = 'rgba(239, 68, 68, 0.15)';
        borderColor = 'rgba(239, 68, 68, 0.4)';
        textColor = 'var(--danger, #ef4444)';
    } else if (msgLower.includes('⚠️') || msgLower.includes('warning') || msgLower.includes('警告') || msgLower.includes('conflict')) {
        icon = '⚠️';
        type = 'warning';
        bgColor = 'rgba(245, 158, 11, 0.15)';
        borderColor = 'rgba(245, 158, 11, 0.4)';
        textColor = 'var(--warning, #f59e0b)';
    } else if (msgLower.includes('🚀') || msgLower.includes('🔄') || msgLower.includes('fresh') || msgLower.includes('started')) {
        icon = '🚀';
        type = 'info';
        bgColor = 'rgba(99, 102, 241, 0.15)';
        borderColor = 'rgba(99, 102, 241, 0.4)';
        textColor = 'var(--primary, #6366f1)';
    }

    // Clean prefix emojis
    let cleanMessage = String(message);
    if (cleanMessage.startsWith('✅') || cleanMessage.startsWith('❌') || cleanMessage.startsWith('⚠️') || cleanMessage.startsWith('🚀') || cleanMessage.startsWith('🔄') || cleanMessage.startsWith('🗑️') || cleanMessage.startsWith('✏️')) {
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
