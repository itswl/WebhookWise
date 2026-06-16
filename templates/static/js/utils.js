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

    if (seconds < 60) return seconds + ' seconds ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' minutes ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' hours ago';
    return Math.floor(seconds / 86400) + ' days ago';
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
        btn.textContent = '✅ Copied';
        btn.style.background = '#28a745';
        btn.style.borderColor = '#28a745';

        setTimeout(() => {
            btn.textContent = originalText;
            btn.style.background = '';
            btn.style.borderColor = '';
        }, 2000);
    }).catch(err => {
        console.error('Copy failed:', err);
        alert('Copy failed');
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
    const texts = { high: 'High', medium: 'Medium', low: 'Low' };
    return texts[importance] || 'Low';
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
    html += '<button class="code-copy-btn" onclick="copyToClipboard(this)">📋 Copy</button>';
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
        '<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">Failed to load</div><div class="empty-text">' +
        escapeHtml(String(message || '')) + '</div><button class="btn btn-primary" onclick="AlertsModule.loadAlerts()">Retry</button></div>';
}

/**
 * Show a Toast notification
 * @param {string} message - Notification message
 * @param {string} type - Type: 'success' | 'error' | 'info'
 */
function showToast(message, type) {
    type = type || 'info';
    var toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(function() {
        toast.classList.add('toast-exit');
        setTimeout(function() { toast.remove(); }, 300);
    }, 3000);
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
        ? '<button data-action="load-more"' + (isLoading ? ' disabled' : '') + '>' + (isLoading ? 'Loading...' : ('Load ' + batchSize + ' more')) + '</button>'
        : '';

    container.innerHTML =
        '<div class="pagination compact-pagination">' +
            '<div class="pagination-info">' +
                'Loaded <strong>' + loaded + '</strong> / <strong>' + totalText + '</strong>' +
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
