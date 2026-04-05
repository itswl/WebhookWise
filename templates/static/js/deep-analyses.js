/**
 * 深度分析页面模块
 */
var DeepAnalysesModule = (function() {
    var currentPage = 1;
    var perPage = 20;
    var autoRefreshTimer = null;
    
    /**
     * 加载深度分析列表
     */
    async function load(page) {
        if (page) currentPage = page;
        
        var statusFilter = document.getElementById('daStatusFilter');
        var engineFilter = document.getElementById('daEngineFilter');
        var status = statusFilter ? statusFilter.value : '';
        var engine = engineFilter ? engineFilter.value : '';
        
        var container = document.getElementById('deepAnalysesList');
        if (!container) return;
        
        container.innerHTML = '<div style="text-align: center; padding: 40px;"><div class="spinner"></div><p>加载中...</p></div>';
        
        try {
            var result = await API.getAllDeepAnalyses(currentPage, perPage, status, engine);
            if (!result.success || !result.data) {
                container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">暂无深度分析记录</div>';
                return;
            }
            
            var items = result.data.items || [];
            var total = result.data.total || 0;
            
            if (items.length === 0) {
                container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">暂无深度分析记录</div>';
                renderPagination(0);
                return;
            }
            
            var html = '';
            var hasPending = false;
            
            items.forEach(function(record) {
                var analysis = record.analysis_result || {};
                var statusLabel = getStatusLabel(record.status);
                var engineLabel = record.engine === 'openocta' ? '🐙 OpenOcta' : '🤖 本地 AI';
                var time = record.created_at ? new Date(record.created_at).toLocaleString('zh-CN') : '-';
                var duration = record.duration_seconds ? record.duration_seconds.toFixed(1) + 's' : '-';
                var source = record.source || 'unknown';
                
                if (record.status === 'pending') hasPending = true;
                
                html += '<div style="border:1px solid #e0e0e0; border-radius:8px; padding:16px; margin-bottom:12px; background:#fafafa;">';
                
                // 头部：状态、引擎、来源、时间
                html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid #eee;">';
                html += '<div style="display:flex; gap:12px; align-items:center;">';
                html += statusLabel;
                html += '<span style="font-weight:600;">' + engineLabel + '</span>';
                html += '<span style="color:#666; font-size:0.85em;">来源: ' + source + '</span>';
                html += '<span style="color:#888; font-size:0.85em;">告警 #' + record.webhook_event_id + '</span>';
                if (record.openocta_run_id) {
                    html += '<span style="color:#999; font-size:0.8em; font-family:monospace;">Run: ' + escapeHtml(record.openocta_run_id) + '</span>';
                }
                if (record.openocta_session_key) {
                    html += '<span style="color:#999; font-size:0.8em; font-family:monospace;">Session: ' + escapeHtml(record.openocta_session_key) + '</span>';
                }
                html += '</div>';
                html += '<span style="color:#888; font-size:0.85em;">' + time + ' | 耗时 ' + duration + '</span>';
                html += '</div>';
                
                // 用户问题
                if (record.user_question) {
                    html += '<div style="margin-bottom:10px; padding:8px 12px; background:#e8f4fd; border-radius:4px; font-size:0.9em;">';
                    html += '<strong>用户问题：</strong>' + escapeHtml(record.user_question);
                    html += '</div>';
                }
                
                // 内容区
                if (record.status === 'pending') {
                    html += '<div style="text-align:center; padding:20px; background:linear-gradient(135deg, #f093fb 0%, #f5576c 100%); border-radius:8px; color:white;">';
                    html += '<div style="font-size:1.5em; margin-bottom:8px;">⏳</div>';
                    html += '<div style="font-weight:600;">OpenOcta 正在分析中...</div>';
                    if (record.openocta_run_id) {
                        html += '<div style="font-size:0.8em; color:rgba(255,255,255,0.7); margin-top:4px;">Run ID: ' + record.openocta_run_id + '</div>';
                    }
                    html += '</div>';
                } else if (record.status === 'failed') {
                    html += '<div style="padding:12px; background:#fff3f3; border-radius:4px; color:#d32f2f;">';
                    html += '<strong>分析失败</strong>';
                    if (analysis.root_cause) html += '<p style="margin:4px 0;">' + escapeHtml(analysis.root_cause) + '</p>';
                    if (record.openocta_session_key) {
                        html += '<button onclick="DeepAnalysesModule.retryAnalysis(' + record.id + ')" style="margin-top:8px; padding:6px 16px; background:#1976d2; color:white; border:none; border-radius:4px; cursor:pointer; font-size:13px;">🔄 重新拉取</button>';
                    }
                    html += '</div>';
                } else {
                    // completed - 展示分析结果
                    // 如果有完整的 OpenOcta 文本，优先渲染 markdown
                    if (analysis._openocta_text) {
                        if (typeof marked !== 'undefined') {
                            html += '<div class="openocta-analysis-content">' + marked.parse(analysis._openocta_text) + '</div>';
                        } else {
                            // fallback: 用 pre 显示
                            html += '<pre style="white-space:pre-wrap; font-size:0.85em;">' + escapeHtml(analysis._openocta_text) + '</pre>';
                        }
                        // 如果有置信度且不在 markdown 中，单独显示
                        if (analysis.confidence !== undefined && analysis.confidence !== null) {
                            var pct = (analysis.confidence * 100).toFixed(0);
                            html += '<div style="margin-top:8px; color:#888; font-size:0.85em;">置信度: ' + pct + '%</div>';
                        }
                    } else {
                        // 原有的 JSON 字段渲染逻辑
                        if (analysis.root_cause) {
                            html += '<div style="margin-bottom:8px;"><strong>🔍 根因分析：</strong><p style="margin:4px 0; white-space:pre-wrap;">' + escapeHtml(analysis.root_cause) + '</p></div>';
                        }
                        if (analysis.impact) {
                            html += '<div style="margin-bottom:8px;"><strong>💥 影响范围：</strong><p style="margin:4px 0; white-space:pre-wrap;">' + escapeHtml(analysis.impact) + '</p></div>';
                        }
                        if (analysis.recommendations && Array.isArray(analysis.recommendations) && analysis.recommendations.length > 0) {
                            html += '<div style="margin-bottom:8px;"><strong>✅ 修复建议：</strong><ul style="margin:4px 0; padding-left:20px;">';
                            analysis.recommendations.forEach(function(rec) {
                                if (typeof rec === 'object' && rec !== null) {
                                    var label = (rec.priority ? '<strong>' + escapeHtml(rec.priority) + '</strong>: ' : '') + escapeHtml(rec.action || JSON.stringify(rec));
                                    html += '<li>' + label + '</li>';
                                } else {
                                    html += '<li>' + escapeHtml(String(rec)) + '</li>';
                                }
                            });
                            html += '</ul></div>';
                        }
                        if (analysis.confidence !== undefined && analysis.confidence !== null) {
                            var pct = (analysis.confidence * 100).toFixed(0);
                            html += '<div style="margin-top:8px; color:#888; font-size:0.85em;">置信度: ' + pct + '%</div>';
                        }
                        // fallback: 没有结构化字段时显示原始 JSON
                        if (!analysis.root_cause && !analysis.impact && !analysis.recommendations) {
                            html += '<pre style="background:#f5f5f5; padding:12px; border-radius:4px; overflow-x:auto; font-size:0.85em; max-height:300px;">' + escapeHtml(JSON.stringify(analysis, null, 2)) + '</pre>';
                        }
                    }
                    
                    // 转发按钮（仅 completed 状态）
                    html += '<div style="margin-top:10px; padding-top:8px; border-top:1px solid #eee;">';
                    html += '<button onclick="DeepAnalysesModule.forwardAnalysis(' + record.id + ')" style="background:#4a90d9; color:#fff; border:none; padding:5px 14px; border-radius:4px; cursor:pointer; font-size:0.85em;">📤 转发</button>';
                    html += '</div>';
                }
                
                html += '</div>';
            });
            
            container.innerHTML = html;
            renderPagination(total);
            
            // 如果有 pending 记录，启动自动刷新
            if (hasPending) {
                startAutoRefresh();
            } else {
                stopAutoRefresh();
            }
            
        } catch (e) {
            container.innerHTML = '<div style="color:red; padding:20px; text-align:center;">加载失败: ' + e.message + '</div>';
        }
    }
    
    function getStatusLabel(status) {
        switch(status) {
            case 'pending':
                return '<span style="display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.8em; background:#fff3e0; color:#e65100;">⏳ 分析中</span>';
            case 'completed':
                return '<span style="display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.8em; background:#e8f5e9; color:#2e7d32;">✅ 已完成</span>';
            case 'failed':
                return '<span style="display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.8em; background:#ffebee; color:#c62828;">❌ 失败</span>';
            default:
                return '<span style="display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.8em; background:#f5f5f5; color:#666;">' + (status || 'unknown') + '</span>';
        }
    }
    
    function escapeHtml(text) {
        if (!text) return '';
        var div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    function renderPagination(total) {
        var container = document.getElementById('deepAnalysesPagination');
        if (!container) return;
        
        var totalPages = Math.ceil(total / perPage);
        if (totalPages <= 1) {
            container.innerHTML = '';
            return;
        }
        
        var html = '';
        if (currentPage > 1) {
            html += '<button class="btn btn-sm" onclick="DeepAnalysesModule.load(' + (currentPage - 1) + ')">上一页</button>';
        }
        html += '<span style="padding:6px 12px; color:#666;">第 ' + currentPage + ' / ' + totalPages + ' 页 (共 ' + total + ' 条)</span>';
        if (currentPage < totalPages) {
            html += '<button class="btn btn-sm" onclick="DeepAnalysesModule.load(' + (currentPage + 1) + ')">下一页</button>';
        }
        container.innerHTML = html;
    }
    
    function startAutoRefresh() {
        if (autoRefreshTimer) return;
        autoRefreshTimer = setInterval(function() {
            load();
        }, 15000); // 每 15 秒刷新
    }
    
    function stopAutoRefresh() {
        if (autoRefreshTimer) {
            clearInterval(autoRefreshTimer);
            autoRefreshTimer = null;
        }
    }
    
    async function forwardAnalysis(analysisId) {
        var url = prompt('请输入转发目标 URL：\n\n飞书: https://open.feishu.cn/open-apis/bot/v2/hook/xxx\n通用 Webhook: https://your-server.com/hook', '');
        if (url === null) return;
        url = url.trim();
        if (!url) return alert('URL 不能为空');
        if (!url.startsWith('http://') && !url.startsWith('https://')) return alert('请输入有效的 HTTP/HTTPS URL');
        
        try {
            var result = await API.forwardDeepAnalysis(analysisId, url);
            if (result.success) {
                alert('✅ ' + (result.message || '转发成功'));
            } else {
                alert('❌ ' + (result.message || '转发失败'));
            }
        } catch (e) {
            alert('❌ 请求失败: ' + e.message);
        }
    }
    
    // 筛选器变化时重新加载
    document.addEventListener('DOMContentLoaded', function() {
        var statusFilter = document.getElementById('daStatusFilter');
        var engineFilter = document.getElementById('daEngineFilter');
        if (statusFilter) statusFilter.addEventListener('change', function() { load(1); });
        if (engineFilter) engineFilter.addEventListener('change', function() { load(1); });
    });
    
    async function retryAnalysis(analysisId) {
        if (!confirm('确定要重新拉取此分析结果吗？')) return;

        try {
            var result = await API.retryDeepAnalysis(analysisId);
            if (result.success) {
                alert('已重新开始拉取，请等待结果');
                load();
                startAutoRefresh();
            } else {
                alert('重试失败: ' + (result.error || '未知错误'));
            }
        } catch (e) {
            alert('请求失败: ' + e.message);
        }
    }

    return {
        load: load,
        stopAutoRefresh: stopAutoRefresh,
        forwardAnalysis: forwardAnalysis,
        retryAnalysis: retryAnalysis
    };
})();
