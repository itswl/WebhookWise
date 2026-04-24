/**
 * Dashboard 核心逻辑模块
 * 负责初始化所有模块、全局事件绑定和自动刷新
 */

// 全局变量
let autoRefreshInterval = null;
let currentTab = 'alerts';

/**
 * 初始化 Dashboard
 */
document.addEventListener('DOMContentLoaded', () => {
    initDashboard();
});

/**
 * Dashboard 初始化函数
 */
function initDashboard() {
    console.log('🚀 初始化 Dashboard...');

    // 初始化各模块
    if (typeof AlertsModule !== 'undefined') {
        AlertsModule.init();
        // 设置全局引用，供 onclick 回调使用
        window.alertsModule = AlertsModule;
    }
    if (typeof AICostModule !== 'undefined') {
        AICostModule.init();
    }
    if (typeof ForwardRulesModule !== 'undefined') {
        ForwardRulesModule.init();
    }

    // 绑定全局事件
    bindGlobalEvents();

    // 启动自动刷新
    startAutoRefresh();

    // 强制清空搜索框（防止浏览器自动填充）
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.value = '';
        // 延迟再清一次，确保浏览器自动填充后也能清空
        setTimeout(() => {
            searchInput.value = '';
        }, 100);
    }

    console.log('✅ Dashboard 初始化完成');
}

/**
 * 绑定全局事件
 */
function bindGlobalEvents() {
    // Tab 切换
    document.querySelectorAll('.nav-tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            const tabId = e.target.getAttribute('data-tab');
            if (tabId) {
                switchMainTab(tabId);
            }
        });
    });

    // 自动刷新按钮
    const autoRefreshBtn = document.getElementById('autoRefreshBtn');
    if (autoRefreshBtn) {
        autoRefreshBtn.addEventListener('click', toggleAutoRefresh);
    }

    // 刷新按钮
    const refreshBtn = document.getElementById('refreshBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            if (typeof AlertsModule !== 'undefined') {
                AlertsModule.loadAlerts();
            }
        });
    }

    // 配置按钮
    const configBtn = document.getElementById('configBtn');
    if (configBtn) {
        configBtn.addEventListener('click', openConfigModal);
    }

    // 模态框外部点击关闭
    document.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal')) {
            e.target.classList.remove('active');
        }
    });

    // 键盘快捷键
    document.addEventListener('keydown', (e) => {
        // ESC 关闭模态框
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal.active').forEach(modal => {
                modal.classList.remove('active');
            });
        }

        // Ctrl/Cmd + R 刷新
        if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
            e.preventDefault();
            if (typeof AlertsModule !== 'undefined') {
                AlertsModule.loadAlerts();
            }
        }
    });

    // 移除了重复的分页按钮监听器，因为在 HTML 中已经绑定了 onclick 事件
}

/**
 * 切换主 Tab
 * @param {string} tabId - Tab ID
 */
function switchMainTab(tabId) {
    console.log('切换 Tab:', tabId);
    currentTab = tabId;

    // 更新导航栏激活状态
    document.querySelectorAll('.nav-tab').forEach(tab => {
        if (tab.getAttribute('data-tab') === tabId) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });

    // 显示/隐藏内容区域
    const tabContents = {
        'alerts': 'alertsTab',
        'ai-cost': 'aiCostTab',
        'deep-analyses': 'deepAnalysesTab',
        'forward-rules': 'forwardRulesTab'
    };

    Object.entries(tabContents).forEach(([id, elementId]) => {
        const element = document.getElementById(elementId);
        if (element) {
            element.style.display = id === tabId ? 'block' : 'none';
        }
    });

    // 触发 Tab 特定的初始化
    switch (tabId) {
        case 'alerts':
            // 切换到告警 Tab 时停止深度分析自动刷新
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            break;
        case 'ai-cost':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof AICostModule !== 'undefined') {
                AICostModule.loadStats('day');
            }
            break;
        case 'deep-analyses':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.load();
            }
            break;
        case 'forward-rules':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof loadForwardRules === 'function') {
                loadForwardRules();
            }
            break;
    }
}

/**
 * 启动自动刷新
 */
function startAutoRefresh() {
    // 默认不启动自动刷新，等待用户手动开启
    console.log('⏸️ 自动刷新已就绪（点击刷新按钮启动）');
}

/**
 * 切换自动刷新状态
 */
function toggleAutoRefresh() {
    const icon = document.getElementById('autoRefreshIcon');
    const text = document.getElementById('autoRefreshText');

    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
        if (icon) icon.textContent = '⏸️';
        if (text) text.textContent = '自动刷新';
        console.log('⏸️ 自动刷新已停止');
    } else {
        autoRefreshInterval = setInterval(() => {
            if (typeof AlertsModule !== 'undefined') {
                AlertsModule.loadAlerts();
            }
        }, 10000);
        if (icon) icon.textContent = '⏵️';
        if (text) text.textContent = '刷新中...';
        console.log('⏵️ 自动刷新已启动（每10秒）');
    }
}

/**
 * 打开配置模态框
 */
async function openConfigModal() {
    try {
        const result = await API.getConfig();

        if (result.success) {
            const c = result.data;
            document.getElementById('configForwardUrl').value = c.forward_url || '';
            document.getElementById('configOpenaiApiKey').value = c.openai_api_key === '已配置' ? '' : c.openai_api_key || '';
            document.getElementById('configOpenaiApiUrl').value = c.openai_api_url || '';
            document.getElementById('configOpenaiModel').value = c.openai_model || '';
            document.getElementById('configDuplicateWindow').value = c.duplicate_alert_time_window || 24;
            document.getElementById('configEnableForward').checked = c.enable_forward;
            document.getElementById('configEnableAi').checked = c.enable_ai_analysis;
            document.getElementById('configForwardDuplicate').checked = c.forward_duplicate_alerts;
            document.getElementById('configEnableNoiseReduction').checked = c.enable_alert_noise_reduction;
            document.getElementById('configSuppressDerivedForward').checked = c.suppress_derived_alert_forward;
            document.getElementById('configNoiseWindow').value = c.noise_reduction_window_minutes || 5;
            document.getElementById('configRootCauseConfidence').value = c.root_cause_min_confidence ?? 0.65;

            document.getElementById('configModal').classList.add('active');
        }
    } catch (error) {
        alert('获取配置失败: ' + error.message);
    }
}

/**
 * 关闭配置模态框
 */
function closeConfigModal() {
    document.getElementById('configModal').classList.remove('active');
}

/**
 * 保存配置
 */
async function saveConfig() {
    try {
        // 获取表单值
        const forwardUrl = document.getElementById('configForwardUrl').value;
        const apiKey = document.getElementById('configOpenaiApiKey').value;
        const apiUrl = document.getElementById('configOpenaiApiUrl').value;
        const model = document.getElementById('configOpenaiModel').value;

        // 构建数据对象（只包含非空值）
        const data = {
            duplicate_alert_time_window: parseInt(document.getElementById('configDuplicateWindow').value) || 24,
            enable_forward: document.getElementById('configEnableForward').checked,
            enable_ai_analysis: document.getElementById('configEnableAi').checked,
            forward_duplicate_alerts: document.getElementById('configForwardDuplicate').checked,
            enable_alert_noise_reduction: document.getElementById('configEnableNoiseReduction').checked,
            suppress_derived_alert_forward: document.getElementById('configSuppressDerivedForward').checked,
            noise_reduction_window_minutes: parseInt(document.getElementById('configNoiseWindow').value) || 5,
            root_cause_min_confidence: parseFloat(document.getElementById('configRootCauseConfidence').value) || 0.65
        };

        // 只有当用户输入了值时才添加到请求中（避免覆盖已有配置）
        if (forwardUrl && forwardUrl.trim()) {
            data.forward_url = forwardUrl.trim();
        }
        if (apiKey && apiKey.trim()) {
            data.openai_api_key = apiKey.trim();
        }
        if (apiUrl && apiUrl.trim()) {
            data.openai_api_url = apiUrl.trim();
        }
        if (model && model.trim()) {
            data.openai_model = model.trim();
        }

        console.log('📤 保存配置:', data);

        const result = await API.saveConfig(data);
        console.log('📥 服务器响应:', result);

        if (result.success) {
            alert('✅ 配置保存成功！');
            closeConfigModal();
        } else {
            const errorMsg = result.error || '未知错误';
            console.error('❌ 保存失败:', errorMsg);

            // 提供更友好的错误提示
            if (errorMsg.includes('权限') || errorMsg.includes('Permission')) {
                alert('❌ 保存失败: 权限错误\n\n' +
                      '可能原因：\n' +
                      '1. .env 文件被锁定或只读\n' +
                      '2. Docker 容器内没有写入权限\n' +
                      '3. 文件被其他程序占用\n\n' +
                      '建议：\n' +
                      '- 检查 .env 文件权限\n' +
                      '- 或使用环境变量配置（不写入文件）\n' +
                      '- 或使用 docker-compose.yml 配置');
            } else {
                alert('❌ 保存失败: ' + errorMsg);
            }
        }
    } catch (error) {
        console.error('❌ 请求失败:', error);
        alert('❌ 保存失败: ' + error.message);
    }
}

/**
 * 确认转发（由转发模态框调用）
 */
async function confirmForward() {
    if (typeof AlertsModule !== 'undefined') {
        await AlertsModule.confirmForward();
    }
}

/**
 * 关闭转发模态框
 */
function closeForwardModal() {
    if (typeof AlertsModule !== 'undefined') {
        AlertsModule.closeForwardModal();
    }
}

// ========== 全局函数包装器 ==========
// 用于 HTML onclick 事件调用模块方法

// 告警模块
function loadWebhooks() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.loadAlerts();
}

function filterAlerts() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.filterAlerts();
}

function changePageSize() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.changePageSize();
}

function goToPage(page) {
    if (typeof AlertsModule !== 'undefined') {
        // 支持特殊值
        if (page === 'prev') {
            AlertsModule.goToPage(AlertsModule.currentPage - 1);
        } else if (page === 'next') {
            AlertsModule.goToPage(AlertsModule.currentPage + 1);
        } else if (page === 'last') {
            const totalPages = Math.ceil(AlertsModule.filteredAlerts.length / AlertsModule.pageSize);
            AlertsModule.goToPage(totalPages);
        } else {
            AlertsModule.goToPage(page);
        }
    }
}
