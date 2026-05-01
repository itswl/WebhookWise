/**
 * 工具函数模块
 * 提供时间格式化、数字格式化、JSON 高亮、剪贴板操作等通用工具函数
 */

/**
 * 格式化时间戳为本地时间字符串
 * @param {number} timestamp - 时间戳（毫秒）
 * @returns {string} 格式化后的时间字符串 (MM/DD HH:mm)
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
 * 计算相对时间（多久前）
 * @param {number} timestamp - 时间戳（毫秒）
 * @returns {string} 相对时间字符串（如：5分钟前、2小时前）
 */
function timeAgo(timestamp) {
    if (timestamp === null || timestamp === undefined || timestamp === '') return '-';
    const now = new Date();
    const past = new Date(timestamp);
    if (Number.isNaN(past.getTime())) return '-';
    const seconds = Math.floor((now - past) / 1000);

    if (seconds < 60) return seconds + '秒前';
    if (seconds < 3600) return Math.floor(seconds / 60) + '分钟前';
    if (seconds < 86400) return Math.floor(seconds / 3600) + '小时前';
    return Math.floor(seconds / 86400) + '天前';
}

/**
 * 格式化数字（添加千分位分隔符）
 * @param {number} num - 数字
 * @returns {string} 格式化后的数字字符串
 */
function formatNumber(num) {
    return num.toLocaleString('zh-CN');
}

/**
 * JSON 语法高亮
 * @param {object|string} json - JSON 对象或字符串
 * @returns {string} 带有语法高亮 HTML 的字符串
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
 * 复制代码块内容到剪贴板
 * @param {HTMLElement} btn - 点击的复制按钮元素
 */
function copyToClipboard(btn) {
    const codeBlock = btn.closest('.code-wrapper').querySelector('pre');
    const text = codeBlock.textContent;

    navigator.clipboard.writeText(text).then(() => {
        const originalText = btn.textContent;
        btn.textContent = '✅ 已复制';
        btn.style.background = '#28a745';
        btn.style.borderColor = '#28a745';

        setTimeout(() => {
            btn.textContent = originalText;
            btn.style.background = '';
            btn.style.borderColor = '';
        }, 2000);
    }).catch(err => {
        console.error('复制失败:', err);
        alert('复制失败');
    });
}

/**
 * HTML 转义，防止 XSS 攻击
 * @param {string} text - 原始文本
 * @returns {string} 转义后的 HTML 安全文本
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * 获取告警图标
 * @param {string} importance - 重要性级别（high/medium/low）
 * @returns {string} 对应的 emoji 图标
 */
function getAlertIcon(importance) {
    const icons = { high: '🔴', medium: '🟡', low: '🟢' };
    return icons[importance] || '⚪';
}

/**
 * 获取重要性文本
 * @param {string} importance - 重要性级别（high/medium/low）
 * @returns {string} 对应的中文文本
 */
function getImportanceText(importance) {
    const texts = { high: '高', medium: '中', low: '低' };
    return texts[importance] || '低';
}

/**
 * 渲染格式化的 JSON 代码块
 * @param {object} data - 要渲染的数据
 * @param {string} title - 代码块标题
 * @returns {string} HTML 字符串
 */
function renderJSONBlock(data, title = 'JSON') {
    const jsonString = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    const highlighted = syntaxHighlightJSON(jsonString);

    let html = '<div class="code-wrapper">';
    html += '<div class="code-header">';
    html += '<span class="code-lang">' + title + '</span>';
    html += '<button class="code-copy-btn" onclick="copyToClipboard(this)">📋 复制</button>';
    html += '</div>';
    html += '<div class="code-block">';
    html += '<pre>' + highlighted + '</pre>';
    html += '</div>';
    html += '</div>';

    return html;
}

/**
 * 显示错误信息
 * @param {string} message - 错误消息
 */
function showError(message) {
    document.getElementById('alertList').innerHTML =
        '<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">加载失败</div><div class="empty-text">' +
        message + '</div><button class="btn btn-primary" onclick="AlertsModule.loadAlerts()">重试</button></div>';
}
