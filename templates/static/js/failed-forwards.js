/**
 * 转发重试管理模块
 * 实现失败转发记录的查看、筛选、手动重试、删除
 */

/**
 * 加载失败转发列表
 */
async function loadFailedForwards() {
    console.log('🔄 加载失败转发列表...');
    const tbody = document.getElementById('failedForwardsTableBody');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; padding: 40px;"><div class="spinner"></div> 加载中...</td></tr>';

    try {
        const status = document.getElementById('ffStatusFilter')?.value || '';
        const targetType = document.getElementById('ffTargetTypeFilter')?.value || '';

        let url = '/api/failed-forwards?limit=100&offset=0';
        if (status) url += '&status=' + encodeURIComponent(status);
        if (targetType) url += '&target_type=' + encodeURIComponent(targetType);

        const resp = await API.authenticatedFetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const result = await resp.json();

        if (result.success) {
            const records = result.data || [];
            renderFailedForwards(records, result.total || records.length);
            console.log('✅ 加载了', records.length, '条失败转发记录');
        } else {
            tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; padding: 40px; color: var(--text-secondary);">❌ 加载失败: ${escapeHtml(result.error || '未知错误')}</td></tr>`;
        }
    } catch (error) {
        console.error('❌ 加载失败转发列表失败:', error);
        tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; padding: 40px; color: var(--text-secondary);">❌ 加载失败: ${escapeHtml(error.message || String(error))}</td></tr>`;
    }
}

/**
 * 渲染失败转发列表
 */
function renderFailedForwards(records, total) {
    const tbody = document.getElementById('failedForwardsTableBody');
    if (!tbody) return;

    if (!records || records.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; padding: 40px; color: #888;">📭 暂无失败转发记录</td></tr>';
        return;
    }

    let html = '';
    records.forEach(r => {
        const statusBadge = formatFFStatus(r.status);
        const targetUrl = r.target_url || '-';
        const truncatedUrl = targetUrl.length > 50 ? targetUrl.substring(0, 50) + '...' : targetUrl;
        const targetTypeText = formatFFTargetType(r.target_type);
        const nextRetry = r.next_retry_at ? formatFFTime(r.next_retry_at) : '-';
        const failureReason = r.failure_reason || r.error_message || '-';
        const truncatedReason = failureReason.length > 60 ? failureReason.substring(0, 60) + '...' : failureReason;

        html += `<tr>
            <td style="font-weight: 600; color: var(--text-muted);">#${r.id}</td>
            <td>${r.webhook_event_id || '-'}</td>
            <td>${targetTypeText}</td>
            <td title="${escapeHtml(targetUrl)}" style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(truncatedUrl)}</td>
            <td>${statusBadge}</td>
            <td>${r.retry_count || 0} / ${r.max_retries || '-'}</td>
            <td style="font-size: 0.85em; color: var(--text-muted); white-space: nowrap;">${nextRetry}</td>
            <td title="${escapeHtml(failureReason)}" style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.85em; color: var(--text-muted);">${escapeHtml(truncatedReason)}</td>
            <td style="white-space: nowrap;">
                ${r.status === 'exhausted' ? `<button class="btn" onclick="retryFailedForward(${r.id})" style="font-size: 0.8rem; padding: 4px 10px; color: #4338ca; border-color: #c7d2fe; background: #e0e7ff; font-weight: 600;">🔄 重试</button>` : ''}
                <button class="btn" onclick="deleteFailedForward(${r.id})" style="font-size: 0.8rem; padding: 4px 10px; color: #dc2626; border-color: #fecaca; background: #fef2f2; font-weight: 600;">🗑️ 删除</button>
            </td>
        </tr>`;
    });

    tbody.innerHTML = html;
}

/**
 * 加载重试统计
 */
async function loadRetryStats() {
    console.log('📊 加载重试统计...');
    try {
        const resp = await API.authenticatedFetch('/api/failed-forwards/stats');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const result = await resp.json();

        if (result.success && result.data) {
            const stats = result.data;
            const pendingEl = document.getElementById('ffStatPending');
            const retryingEl = document.getElementById('ffStatRetrying');
            const successEl = document.getElementById('ffStatSuccess');
            const exhaustedEl = document.getElementById('ffStatExhausted');

            if (pendingEl) pendingEl.textContent = stats.pending || 0;
            if (retryingEl) retryingEl.textContent = stats.retrying || 0;
            if (successEl) successEl.textContent = stats.success || 0;
            if (exhaustedEl) exhaustedEl.textContent = stats.exhausted || 0;
        }
    } catch (error) {
        console.error('❌ 加载重试统计失败:', error);
    }
}

/**
 * 手动重试
 */
async function retryFailedForward(id) {
    if (!confirm('确定要重试此转发记录吗？\n\n将重置为待重试状态。')) {
        return;
    }

    try {
        console.log('🔄 手动重试:', id);
        const resp = await API.authenticatedFetch(`/api/failed-forwards/${id}/retry`, { method: 'POST' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const result = await resp.json();

        if (result.success) {
            alert('✅ 已重置为待重试');
            loadFailedForwards();
            loadRetryStats();
        } else {
            alert('❌ 重试失败: ' + (result.error || '未知错误'));
        }
    } catch (error) {
        console.error('❌ 手动重试失败:', error);
        alert('❌ 重试失败: ' + error.message);
    }
}

/**
 * 删除失败转发记录
 */
async function deleteFailedForward(id) {
    if (!confirm('确定要删除此转发记录吗？\n\n此操作不可撤销。')) {
        return;
    }

    try {
        console.log('🗑️ 删除失败转发记录:', id);
        const resp = await API.authenticatedFetch(`/api/failed-forwards/${id}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const result = await resp.json();

        if (result.success) {
            alert('✅ 记录已删除');
            loadFailedForwards();
            loadRetryStats();
        } else {
            alert('❌ 删除失败: ' + (result.error || '未知错误'));
        }
    } catch (error) {
        console.error('❌ 删除失败转发记录失败:', error);
        alert('❌ 删除失败: ' + error.message);
    }
}

/**
 * 筛选失败转发
 */
function filterFailedForwards() {
    loadFailedForwards();
}

/**
 * 格式化状态显示
 */
function formatFFStatus(status) {
    const map = {
        'pending': '<span style="padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; background: #dbeafe; color: #1d4ed8;">待重试</span>',
        'retrying': '<span style="padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; background: #ffedd5; color: #c2410c;">重试中</span>',
        'success': '<span style="padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; background: #dcfce7; color: #15803d;">已成功</span>',
        'exhausted': '<span style="padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; background: #fee2e2; color: #dc2626;">已耗尽</span>'
    };
    return map[status] || `<span style="padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; background: #f1f5f9; color: #64748b;">${status || '未知'}</span>`;
}

/**
 * 格式化转发类型
 */
function formatFFTargetType(type) {
    const map = {
        'feishu': '飞书',
        'openclaw': 'OpenClaw',
        'webhook': 'Webhook'
    };
    return map[type] || type || '未知';
}

/**
 * 格式化时间
 */
function formatFFTime(timeStr) {
    if (!timeStr) return '-';
    try {
        const date = new Date(timeStr);
        if (isNaN(date.getTime())) return timeStr;
        const pad = n => String(n).padStart(2, '0');
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    } catch {
        return timeStr;
    }
}

// 导出模块
const FailedForwardsModule = {
    init: function() {
        console.log('🔄 转发重试模块初始化');
    },
    load: function() {
        loadFailedForwards();
        loadRetryStats();
    }
};
