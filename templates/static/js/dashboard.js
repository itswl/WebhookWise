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
    initDashboard().catch((error) => {
        console.error('Dashboard 初始化失败', error);
    });
});

/**
 * Dashboard 初始化函数
 */
async function initDashboard() {
    console.log('🚀 初始化 Dashboard...');

    if (typeof API !== 'undefined') {
        await API.initAuthStorage();
    }
    updateAuthButtonState();

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
        'outbox': 'outboxTab',
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
        case 'outbox':
            if (typeof DeepAnalysesModule !== 'undefined') {
                DeepAnalysesModule.stopAutoRefresh();
            }
            if (typeof OutboxModule !== 'undefined') {
                OutboxModule.load();
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

function openAuthModal() {
    const apiKeyInput = document.getElementById('authApiKey');
    const adminWriteKeyInput = document.getElementById('authAdminWriteKey');
    if (apiKeyInput) apiKeyInput.value = '';
    if (adminWriteKeyInput) adminWriteKeyInput.value = '';
    updateAuthButtonState();
    document.getElementById('authModal').classList.add('active');
}

function closeAuthModal() {
    document.getElementById('authModal').classList.remove('active');
}

async function saveAuthKeys() {
    const apiKey = document.getElementById('authApiKey')?.value.trim() || '';
    const adminWriteKey = document.getElementById('authAdminWriteKey')?.value.trim() || '';

    try {
        if (apiKey) {
            await API.setReadToken(apiKey);
        }
        if (adminWriteKey) {
            await API.setWriteToken(adminWriteKey);
        }
    } catch (error) {
        console.error('凭证加密保存失败', error);
        alert(error.message || '凭证加密保存失败，请确认当前浏览器支持 Web Crypto。');
        return;
    }

    updateAuthButtonState();
    closeAuthModal();
}

async function clearAuthKeys() {
    await API.clearTokens();
    const apiKeyInput = document.getElementById('authApiKey');
    const adminWriteKeyInput = document.getElementById('authAdminWriteKey');
    if (apiKeyInput) apiKeyInput.value = '';
    if (adminWriteKeyInput) adminWriteKeyInput.value = '';
    updateAuthButtonState();
}

function updateAuthButtonState() {
    if (typeof API === 'undefined') return;
    const status = API.getTokenStatus();
    const readStatus = document.getElementById('authReadStatus');
    const writeStatus = document.getElementById('authWriteStatus');
    const authBtnText = document.getElementById('authBtnText');

    if (readStatus) {
        readStatus.textContent = `API_KEY：${status.read ? '已保存' : '未保存'}`;
    }
    if (writeStatus) {
        writeStatus.textContent = `ADMIN_WRITE_KEY：${status.write ? '已保存' : '未保存'}`;
    }
    if (authBtnText) {
        authBtnText.textContent = status.read && status.write ? '凭证已保存' : '凭证';
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

function loadMoreWebhooks() {
    if (typeof AlertsModule !== 'undefined') AlertsModule.loadMoreAlerts();
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
