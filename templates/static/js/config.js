/**
 * 系统配置管理模块 - 增强版
 * 支持热加载配置段、版本追踪、SSE 实时更新
 */

var ConfigModule = (function() {
    var sectionNames = {
        'all': '全部配置',
        'noise': '降噪配置',
        'retry': '重试配置',
        'ai': 'AI 配置',
        'circuit_breaker': '熔断器配置',
        'notifications': '通知配置',
        'maintenance': '维护配置',
        'tasks': '任务配置',
        'security': '安全配置',
        'openclaw': 'OpenClaw 配置'
    };

    var currentVersion = 0;
    var sectionMeta = [];

    function init() {
        loadVersion();
        startPolling();
    }

    function loadVersion() {
        API.getConfigVersion().then(function(res) {
            if (res.success && res.data) {
                currentVersion = res.data.version || 0;
                sectionMeta = res.data.sections || [];
                renderVersionInfo();
            }
        }).catch(function() {});
    }

    function renderVersionInfo() {
        var container = document.getElementById('configVersionInfo');
        if (!container) return;

        var html = '<div class="config-version-bar" style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:var(--bg-secondary);border-radius:6px;margin-bottom:16px;">';
        html += '<div><strong>当前配置版本</strong>: <code style="font-size:1.1rem;">v' + currentVersion + '</code></div>';
        html += '<div><button class="btn btn-sm" onclick="ConfigModule.refresh()">🔄 刷新版本</button></div>';
        html += '</div>';

        // Section metadata table
        if (sectionMeta.length > 0) {
            html += '<div style="margin-bottom:16px;"><strong>配置段列表</strong></div>';
            html += '<div class="outbox-table-wrap"><table class="outbox-table"><thead><tr><th>配置段</th><th>名称</th><th>热更新</th></tr></thead><tbody>';
            sectionMeta.forEach(function(s) {
                var hotBadge = s.hot_reloadable === 'yes'
                    ? '<span class="badge badge-success">支持</span>'
                    : '<span class="badge badge-new">需重启</span>';
                html += '<tr><td><code>' + escapeHtml(s.id) + '</code></td><td>' + escapeHtml(s.name) + '</td><td>' + hotBadge + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }

        // Reload buttons
        html += '<div style="margin-top:16px;"><strong>触发配置热加载</strong></div>';
        html += '<div id="configReloadButtons" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;">';
        var sectionOrder = ['noise', 'retry', 'ai', 'circuit_breaker', 'notifications', 'maintenance', 'tasks', 'security', 'openclaw'];
        sectionOrder.forEach(function(k) {
            var name = sectionNames[k] || k;
            html += '<button class="btn" onclick="ConfigModule.reload(\'' + k + '\')">♻️ ' + name + '</button>';
        });
        html += '<button class="btn btn-primary" onclick="ConfigModule.reload(\'all\')">♻️ 重载全部</button>';
        html += '</div>';

        container.innerHTML = html;
    }

    var pollInterval = null;

    function startPolling() {
        if (pollInterval) clearInterval(pollInterval);
        pollInterval = setInterval(function() {
            refresh();
        }, 15000);
    }

    function stopPolling() {
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    }

    function refresh() {
        loadVersion();
    }

    function reload(section) {
        var name = sectionNames[section] || section;
        if (!confirm('确认重新加载「' + name + '」？\n\n将从环境变量和 .env 文件重新读取配置项，无需重启进程。')) return;

        var buttons = document.querySelectorAll('#configReloadButtons .btn');
        buttons.forEach(function(b) { b.disabled = true; });
        var targetBtn = Array.from(buttons).find(function(b) { return b.textContent.includes(sectionNames[section] || section); });
        if (targetBtn) targetBtn.textContent = '⏳ 加载中...';

        API.reloadConfig(section).then(function(res) {
            if (res.success) {
                showToast(res.message || name + ' 已重新加载', 'success');
                loadVersion();
            } else {
                showToast(res.error || '加载失败', 'error');
            }
        }).catch(function(e) {
            showToast('请求异常: ' + e.message, 'error');
        }).finally(function() {
            buttons.forEach(function(b) { b.disabled = false; });
            renderVersionInfo();
        });
    }

    return {
        init: init,
        refresh: refresh,
        reload: reload
    };
})();
