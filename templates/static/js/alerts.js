/**
 * 告警列表模块
 * 处理告警的加载、筛选、分页、展示和交互
 */

const AlertsModule = {
    currentPage: 1,
    pageSize: 20,
    alerts: [],
    filteredAlerts: [],
    totalCount: 0,
    nextCursor: null,
    hasMore: false,
    _loadingMore: false,
    currentForwardId: null,
    currentTabByAlert: {},

    /**
     * 初始化告警模块
     */
    init() {
        this.loadAlerts();
        this.bindEvents();
    },

    /**
     * 绑定事件处理
     */
    bindEvents() {
        // 搜索和筛选事件
        const searchInput = document.getElementById('searchInput');
        const importanceFilter = document.getElementById('importanceFilter');
        const sourceFilter = document.getElementById('sourceFilter');
        const duplicateFilter = document.getElementById('duplicateFilter');
        const pageSizeSelect = document.getElementById('pageSize');

        if (searchInput) {
            searchInput.addEventListener('input', () => this.filterAlerts());
        }
        if (importanceFilter) {
            importanceFilter.addEventListener('change', () => this.filterAlerts());
        }
        if (sourceFilter) {
            sourceFilter.addEventListener('change', () => this.filterAlerts());
        }
        if (duplicateFilter) {
            duplicateFilter.addEventListener('change', () => this.filterAlerts());
        }
        if (pageSizeSelect) {
            pageSizeSelect.addEventListener('change', () => this.changePageSize());
        }

        // 事件委托处理告警项交互
        document.addEventListener('click', (e) => this.handleAlertClick(e));
    },

    /**
     * 处理告警相关的点击事件
     */
    handleAlertClick(e) {
        // 优先处理按钮操作
        const btn = e.target.closest('button[data-action]');
        if (btn) {
            e.stopPropagation();
            const action = btn.getAttribute('data-action');
            const id = btn.getAttribute('data-id');
            console.log('按钮点击:', action, id);

            if (action === 'reanalyze') {
                this.reanalyzeAlert(id);
            } else if (action === 'deep-analyze') {
                this.deepAnalyzeAlert(id);
            } else if (action === 'forward') {
                this.openForwardModal(id);
            }
            return;
        }

        // Tab 切换
        if (e.target.closest('.tab')) {
            const tab = e.target.closest('.tab');
            const tabName = tab.getAttribute('data-tab');
            const alertItem = tab.closest('.alert-item');
            const webhookId = tab.getAttribute('data-id');

            // 切换 tab 激活状态
            alertItem.querySelectorAll('.tab').forEach(function(t) {
                t.classList.remove('active');
            });
            tab.classList.add('active');

            // 切换内容显示
            alertItem.querySelectorAll('.tab-content').forEach(function(content) {
                const contentTab = content.getAttribute('data-tab-content');
                if (contentTab === tabName) {
                    content.classList.add('active');
                } else {
                    content.classList.remove('active');
                }
            });

            // 如果切换到深度分析标签，加载数据
            if (tabName === 'deep-analysis' && webhookId) {
                this.loadDeepAnalyses(webhookId);
            }
            return;
        }

        // 告警展开/收起
        if (e.target.closest('.alert-header')) {
            const header = e.target.closest('.alert-header');
            // 如果点击的是按钮或按钮内的元素，不处理
            if (e.target.closest('button')) return;

            const alertItem = header.closest('.alert-item');
            const isExpanding = !alertItem.classList.contains('expanded');
            alertItem.classList.toggle('expanded');

            // 如果是展开操作，且数据是摘要模式，加载完整数据
            if (isExpanding) {
                const webhookId = alertItem.getAttribute('data-id');
                const webhook = this.alerts.find(w => w.id == webhookId);

                // 检查是否需要加载完整数据
                if (webhook && !webhook.parsed_data && !webhook.ai_analysis) {
                    this.loadFullAlertData(webhookId, alertItem);
                }
            }
        }
    },

    /**
     * 加载告警数据
     */
    async loadAlerts() {
        try {
            // 显示加载提示
            const alertList = document.getElementById('alertList');
            alertList.innerHTML = '<div class="loading"><div class="spinner"></div><p>正在加载数据...</p></div>';

            // 使用纯游标模式加载最新数据（避免 offset/count）
            const result = await API.getWebhooks({ use_cursor: true, limit: 200, fields: 'summary', cursor_id: null });

            if (!result.success || !result.data) {
                throw new Error('数据格式错误');
            }

            this.alerts = result.data;
            this.nextCursor = result.cursor ? result.cursor.next_cursor : null;
            this.hasMore = result.cursor ? !!result.cursor.has_more : false;
            this.totalCount = null;

            console.log('✅ 数据加载完成:', this.alerts.length, '条（总共', this.totalCount, '条）');

            this.updateStats();
            this.currentPage = 1;
            this.filterAlerts(true);

            document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('zh-CN');
        } catch (error) {
            console.error('加载失败:', error);
            showError('加载失败: ' + error.message);
        }
    },

    async loadMoreAlerts() {
        if (!this.hasMore || this._loadingMore) return;
        this._loadingMore = true;
        try {
            const btn = document.getElementById('loadMoreBtn');
            if (btn) {
                btn.disabled = true;
                btn.textContent = '加载中...';
            }

            const result = await API.getWebhooks({ use_cursor: true, limit: 200, fields: 'summary', cursor_id: this.nextCursor });
            if (!result.success || !result.data) {
                throw new Error('数据格式错误');
            }

            this.alerts = this.alerts.concat(result.data);
            this.nextCursor = result.cursor ? result.cursor.next_cursor : null;
            this.hasMore = result.cursor ? !!result.cursor.has_more : false;

            this.updateStats();
            this.filterAlerts(false);
        } catch (error) {
            console.error('加载更多失败:', error);
            alert('加载更多失败: ' + error.message);
        } finally {
            const btn = document.getElementById('loadMoreBtn');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '加载更多';
            }
            this._loadingMore = false;
        }
    },

    /**
     * 更新统计信息
     */
    updateStats() {
        const totalEl = document.getElementById('totalCount');
        if (totalEl) {
            if (this.totalCount !== null && this.totalCount !== undefined) {
                totalEl.textContent = this.totalCount;
            } else {
                totalEl.textContent = this.hasMore ? (this.alerts.length + '+') : String(this.alerts.length);
            }
        }

        let highCount = 0, mediumCount = 0, duplicateCount = 0;

        this.alerts.forEach(function(w) {
            const importance = w.importance || 'low';
            if (importance === 'high') highCount++;
            else if (importance === 'medium') mediumCount++;

            if (w.is_duplicate === 1) duplicateCount++;
        });

        document.getElementById('highCount').textContent = highCount;
        document.getElementById('mediumCount').textContent = mediumCount;
        document.getElementById('duplicateCount').textContent = duplicateCount;
    },

    /**
     * 筛选告警
     */
    filterAlerts(resetPage = true) {
        const searchTerm = document.getElementById('searchInput').value.toLowerCase();
        const importanceFilter = document.getElementById('importanceFilter').value;
        const sourceFilter = document.getElementById('sourceFilter').value;
        const duplicateFilter = document.getElementById('duplicateFilter').value;

        // 筛选数据
        this.filteredAlerts = this.alerts.filter(function(webhook) {
            const matchSearch = !searchTerm || JSON.stringify(webhook).toLowerCase().indexOf(searchTerm) > -1;

            let matchImportance = true;
            if (importanceFilter) {
                const webhookImportance = webhook.importance || 'low';
                matchImportance = webhookImportance === importanceFilter;
            }

            const matchSource = !sourceFilter || webhook.source === sourceFilter;

            let matchDuplicate = true;
            if (duplicateFilter === 'original') {
                matchDuplicate = !webhook.is_duplicate || webhook.is_duplicate === 0;
            } else if (duplicateFilter === 'duplicate') {
                matchDuplicate = webhook.is_duplicate === 1;
            }

            return matchSearch && matchImportance && matchSource && matchDuplicate;
        });

        console.log('筛选结果:', this.filteredAlerts.length, '条（共', this.alerts.length, '条）');

        if (resetPage) {
            this.currentPage = 1;
        }

        // 显示当前页数据
        this.displayCurrentPage();
    },

    /**
     * 显示当前页数据（前端分页）
     */
    displayCurrentPage() {
        const totalFiltered = this.filteredAlerts.length;
        const totalPagesFiltered = Math.ceil(totalFiltered / this.pageSize);

        // 确保当前页在有效范围内
        if (this.currentPage > totalPagesFiltered && totalPagesFiltered > 0) {
            console.warn('⚠️  当前页码超出范围，重置到最后一页');
            this.currentPage = totalPagesFiltered;
        }

        // 计算当前页的数据范围
        const startIndex = (this.currentPage - 1) * this.pageSize;
        const endIndex = Math.min(startIndex + this.pageSize, totalFiltered);
        const currentPageData = this.filteredAlerts.slice(startIndex, endIndex);

        console.log('📄 显示第', this.currentPage, '页，共', totalPagesFiltered, '页');
        console.log('📊 数据范围:', startIndex, '-', endIndex, '，显示', currentPageData.length, '条');
        console.log('📈 筛选后总数:', totalFiltered, '条（原始数据', this.alerts.length, '条）');

        // 更新分页信息
        this.updatePagination(totalFiltered, totalPagesFiltered);

        // 显示数据
        this.renderAlerts(currentPageData);
    },

    /**
     * 渲染告警列表
     */
    renderAlerts(webhooks) {
        const container = document.getElementById('alertList');

        if (webhooks.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-title">暂无告警</div><div class="empty-text">没有符合筛选条件的告警</div></div>';
            return;
        }

        let html = '';
        webhooks.forEach((webhook) => {
            const importance = webhook.importance || 'low';
            const isDuplicate = webhook.is_duplicate === 1;
            // 兼容两种数据格式：完整模式(ai_analysis)和摘要模式(summary)
            const analysis = webhook.ai_analysis || {};
            const summary = webhook.summary || analysis.summary || '';

            html += '<div class="alert-item" data-id="' + escapeHtml(String(webhook.id)) + '">';
            html += '<div class="alert-header">';
            html += '<div class="alert-left">';
            html += '<div class="alert-title-row">';
            html += '<span class="alert-icon">' + getAlertIcon(importance) + '</span>';
            html += '<span class="alert-title">' + escapeHtml(String(summary || webhook.source || ('告警 #' + webhook.id))) + '</span>';
            html += '</div>';
            html += '<div class="alert-meta">';
            html += '<span class="alert-meta-item">📍 ' + escapeHtml(String(webhook.source || 'unknown')) + '</span>';

            // 显示主机信息（如果有）
            if (webhook.alert_info && webhook.alert_info.host) {
                html += '<span class="alert-meta-item">🖥️ ' + escapeHtml(String(webhook.alert_info.host)) + '</span>';
            }

            // 始终显示客户端 IP
            if (webhook.client_ip) {
                html += '<span class="alert-meta-item">🌐 ' + escapeHtml(String(webhook.client_ip)) + '</span>';
            }

            html += '<span class="alert-meta-item">🕐 ' + formatTime(webhook.timestamp) + '</span>';

            // 显示重复信息
            if (isDuplicate) {
                html += '<span class="alert-meta-item">🔗 原始 #' + webhook.duplicate_of + '</span>';
                // 显示上次告警 ID 和时间
                if (webhook.prev_alert_id) {
                    let prevText = '⏮️ 上次 #' + webhook.prev_alert_id;
                    if (webhook.prev_alert_timestamp) {
                        prevText += ' (' + timeAgo(webhook.prev_alert_timestamp) + ')';
                    }
                    html += '<span class="alert-meta-item">' + prevText + '</span>';
                }
            }
            html += '</div></div>';
            html += '<div class="alert-right">';
            html += '<span class="badge badge-' + importance + '">' + getImportanceText(importance) + '</span>';
            // 显示重复类型：窗口内 or 窗口外
            if (isDuplicate) {
                const isWithinWindow = webhook.is_within_window || false;
                const isBeyondWindow = webhook.beyond_time_window || false;

                if (isBeyondWindow) {
                    html += '<span class="badge badge-duplicate" title="超过24小时窗口的重复告警">窗口外重复</span>';
                } else if (isWithinWindow) {
                    html += '<span class="badge badge-duplicate" title="24小时窗口内的重复告警">窗口内重复</span>';
                } else {
                    html += '<span class="badge badge-duplicate">重复</span>';
                }
            }
            html += '<span class="alert-time">' + timeAgo(webhook.timestamp) + '</span>';
            html += '<div class="alert-actions">';
            html += '<button class="btn btn-sm" data-action="reanalyze" data-id="' + escapeHtml(String(webhook.id)) + '">🔄 重新分析</button>';
            html += '<button class="btn btn-sm" data-action="deep-analyze" data-id="' + escapeHtml(String(webhook.id)) + '">🔬 深度分析</button>';
            html += '<button class="btn btn-sm btn-primary" data-action="forward" data-id="' + escapeHtml(String(webhook.id)) + '">🚀 转发</button>';
            html += '</div></div></div>';

            html += '<div class="alert-details">';
            html += '<div class="details-tabs">';
            html += '<div class="tab active" data-tab="overview" data-id="' + webhook.id + '">概览</div>';
            html += '<div class="tab" data-tab="data" data-id="' + webhook.id + '">原始数据</div>';
            // AI 分析标签页
            if (analysis && Object.keys(analysis).length > 0) {
                html += '<div class="tab" data-tab="ai" data-id="' + webhook.id + '">AI 分析</div>';
            } else if (summary || webhook.importance) {
                html += '<div class="tab" data-tab="ai" data-id="' + webhook.id + '">AI 分析</div>';
            }
            // 深度分析标签页
            html += '<div class="tab" data-tab="deep-analysis" data-id="' + webhook.id + '">深度分析</div>';
            html += '</div>';

            html += '<div class="tab-content active" data-tab-content="overview">';
            html += this.renderOverview(webhook);
            html += '</div>';

            html += '<div class="tab-content" data-tab-content="data">';
            if (webhook.parsed_data) {
                html += renderJSONBlock(webhook.parsed_data, '原始数据');
            } else if (webhook.alert_info && Object.keys(webhook.alert_info).length > 0) {
                html += '<div class="info-grid">';
                Object.entries(webhook.alert_info).forEach(([key, value]) => {
                    if (value) {
                        html += '<div class="info-item"><div class="info-label">' + escapeHtml(String(key)) + '</div><div class="info-value">' + escapeHtml(String(value)) + '</div></div>';
                    }
                });
                html += '</div>';
                html += '<div style="margin-top: 1rem; padding: 0.75rem; background: #f0f9ff; border-left: 3px solid #0ea5e9; border-radius: 4px;">';
                html += '<p style="margin: 0; color: #0369a1; font-size: 0.9rem;">💡 首次展开时会自动加载完整原始数据</p>';
                html += '</div>';
            } else {
                html += '<div style="padding: 2rem; text-align: center; color: #94a3b8;">暂无数据</div>';
            }
            html += '</div>';

            // AI 分析内容
            if (analysis && Object.keys(analysis).length > 0) {
                html += '<div class="tab-content" data-tab-content="ai">';
                html += this.renderAIAnalysis(analysis);
                html += '</div>';
            } else if (summary || webhook.importance) {
                html += '<div class="tab-content" data-tab-content="ai">';
                html += '<div class="ai-section">';
                html += '<div class="ai-header">🤖 智能分析结果</div>';
                html += '<div class="ai-content">';
                if (summary) {
                    html += '<div class="ai-item"><div class="ai-label">摘要</div><div class="ai-value">' + escapeHtml(String(summary)) + '</div></div>';
                }
                if (webhook.importance) {
                    html += '<div class="ai-item"><div class="ai-label">重要性</div><div class="ai-value">' + getImportanceText(webhook.importance) + '</div></div>';
                }
                html += '</div></div>';
                html += '<div style="margin-top: 1rem; padding: 0.75rem; background: #f0f9ff; border-left: 3px solid #0ea5e9; border-radius: 4px;">';
                html += '<p style="margin: 0; color: #0369a1; font-size: 0.9rem;">💡 首次展开时会自动加载完整 AI 分析结果</p>';
                html += '</div>';
                html += '</div>';
            }

            // 深度分析内容面板
            html += '<div class="tab-content" data-tab-content="deep-analysis">';
            html += '<div id="deep-analysis-container-' + webhook.id + '">点击标签加载深度分析历史...</div>';
            html += '</div>';

            html += '</div></div>';
        });

        container.innerHTML = html;
    },

    /**
     * 渲染概览信息
     */
    renderOverview(webhook) {
        let html = '<div class="info-grid">';
        html += '<div class="info-item"><div class="info-label">告警 ID</div><div class="info-value">#' + webhook.id + '</div></div>';
        html += '<div class="info-item"><div class="info-label">来源</div><div class="info-value">' + escapeHtml(String(webhook.source || '-')) + '</div></div>';
        html += '<div class="info-item"><div class="info-label">客户端 IP</div><div class="info-value">' + escapeHtml(String(webhook.client_ip || '-')) + '</div></div>';
        html += '<div class="info-item"><div class="info-label">接收时间</div><div class="info-value">' + new Date(webhook.timestamp).toLocaleString('zh-CN') + '</div></div>';
        if (webhook.is_duplicate) {
            html += '<div class="info-item"><div class="info-label">原始告警</div><div class="info-value">#' + webhook.duplicate_of + '</div></div>';
            if (webhook.prev_alert_id) {
                let prevValue = '#' + webhook.prev_alert_id;
                if (webhook.prev_alert_timestamp) {
                    prevValue += ' (' + new Date(webhook.prev_alert_timestamp).toLocaleString('zh-CN') + ')';
                }
                html += '<div class="info-item"><div class="info-label">上次告警</div><div class="info-value">' + prevValue + '</div></div>';
            }
            html += '<div class="info-item"><div class="info-label">重复次数</div><div class="info-value">' + (webhook.duplicate_count || 1) + '</div></div>';

            // 显示重复类型
            const isWithinWindow = webhook.is_within_window || false;
            const isBeyondWindow = webhook.beyond_time_window || false;
            let duplicateType = '未知';
            if (isBeyondWindow) {
                duplicateType = '窗口外重复（超过24小时）';
            } else if (isWithinWindow) {
                duplicateType = '窗口内重复（24小时内）';
            }
            html += '<div class="info-item"><div class="info-label">重复类型</div><div class="info-value">' + escapeHtml(String(duplicateType)) + '</div></div>';
        }
        html += '</div>';
        return html;
    },

    /**
     * 渲染 AI 分析结果
     */
    renderAIAnalysis(analysis) {
        if (!analysis || Object.keys(analysis).length === 0) {
            return '<div style="padding: 2rem; text-align: center; color: #94a3b8;">暂无 AI 分析数据</div>';
        }

        let html = `
            <div class="ai-analysis" style="border-left: 4px solid #4f46e5; background: #ffffff; padding: 1.5rem; border-radius: 12px; box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.05); margin-bottom: 1rem;">
                <div class="ai-header" style="font-size: 1rem; font-weight: 600; color: #4f46e5; display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem;">
                    <span>🤖</span> AIOps 智能诊断报告
                    <span class="badge ${analysis._degraded ? 'badge-medium' : 'badge-low'}" style="margin-left: auto;">
                        ${escapeHtml(String(analysis._degraded ? '本地规则降级' : (analysis._route_type || '智能路由')))}
                    </span>
                </div>

                <div style="font-size: 1.1rem; color: #0f172a; font-weight: 600; margin-bottom: 1.5rem; line-height: 1.5; padding-bottom: 1rem; border-bottom: 1px solid #e2e8f0;">
                    ${escapeHtml(String(analysis.summary || '无分析摘要'))}
                </div>

                <div class="ai-details" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 2rem;">
        `;

        if (analysis.root_cause) {
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; letter-spacing: 0.05em;">🔍 根因定位</h4>
                    <p style="font-size: 0.95rem; color: #1e293b; margin: 0; line-height: 1.6;">${escapeHtml(String(analysis.root_cause))}</p>
                </div>
            `;
        } else if (analysis.event_type) {
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; letter-spacing: 0.05em;">🏷️ 事件类型</h4>
                    <p style="font-size: 0.95rem; color: #1e293b; margin: 0; line-height: 1.6;">${escapeHtml(String(analysis.event_type))}</p>
                </div>
            `;
        }

        if (analysis.impact || analysis.impact_scope) {
            const impact = analysis.impact || analysis.impact_scope;
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; letter-spacing: 0.05em;">💥 影响评估</h4>
                    <p style="font-size: 0.95rem; color: #1e293b; margin: 0; line-height: 1.6;">${escapeHtml(String(impact))}</p>
                </div>
            `;
        }

        const actions = analysis.recommendations || analysis.actions;
        if (actions && actions.length > 0) {
            html += `
                <div class="detail-section" style="grid-column: 1 / -1;">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; letter-spacing: 0.05em;">🛠️ 修复建议与操作</h4>
                    <ul style="font-size: 0.95rem; color: #1e293b; margin: 0; padding-left: 1.5rem; line-height: 1.6;">
                        ${actions.map(r => `<li style="margin-bottom: 0.5rem;">${escapeHtml(String(r))}</li>`).join('')}
                    </ul>
                </div>
            `;
        }

        if (analysis.risks && analysis.risks.length > 0) {
            html += `
                <div class="detail-section" style="grid-column: 1 / -1;">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; letter-spacing: 0.05em;">⚠️ 潜在风险</h4>
                    <ul style="font-size: 0.95rem; color: #1e293b; margin: 0; padding-left: 1.5rem; line-height: 1.6;">
                        ${analysis.risks.map(r => `<li style="margin-bottom: 0.5rem;">${escapeHtml(String(r))}</li>`).join('')}
                    </ul>
                </div>
            `;
        }

        html += `</div>`; // Close grid

        // Metadata footer
        html += `
            <div class="ai-meta" style="margin-top: 2rem; display: flex; flex-wrap: wrap; gap: 1rem; justify-content: space-between; font-size: 0.8rem; color: #64748b; background: #f8fafc; padding: 1rem; border-radius: 8px; border: 1px solid #e2e8f0;">
                <span>⚡ 重要性: <strong style="color: #0f172a;">${escapeHtml(String(analysis.importance || '未知'))}</strong></span>
        `;

        if (analysis.noise_reduction) {
            const nr = analysis.noise_reduction;
            const relationMap = { root_cause: '根因告警', derived: '衍生告警', standalone: '独立告警' };
            const relation = relationMap[nr.relation] || nr.relation || '未知';
            html += `<span>🛡️ 降噪判定: <strong style="color: #0f172a;">${escapeHtml(String(relation))}</strong> (置信度: ${Number(nr.confidence * 100).toFixed(1)}%)</span>`;
            if (nr.root_cause_event_id) {
                html += `<span>🔗 关联根因: <strong style="color: #4f46e5;">#${nr.root_cause_event_id}</strong></span>`;
            }
        }

        html += `<span>🔀 路由通道: <strong style="color: #0f172a;">${escapeHtml(String(analysis._route_type || '未知'))}</strong></span>`;
        if (analysis._cache_hit) {
            const hitCount = analysis._cache_hit_count || 1;
            html += `<span title="命中次数: ${escapeHtml(String(hitCount))}" style="color: #10b981; font-weight: 600;">🎯 缓存命中 (${escapeHtml(String(hitCount))}次)</span>`;
        }

        html += `
            </div>
        </div>
        `;

        // Render Raw JSON analysis below it for debugging
        if (typeof renderJSONBlock === 'function') {
            html += renderJSONBlock(analysis, '原始分析数据');
        }

        return html;
    },

    /**
     * 更新分页信息
     */
    updatePagination(totalFiltered, totalPagesFiltered) {
        const paginationDiv = document.getElementById('pagination');
        const loadMoreBtn = document.getElementById('loadMoreBtn');

        if (totalPagesFiltered > 0) {
            paginationDiv.style.display = 'flex';

            document.getElementById('currentPageNum').textContent = this.currentPage;
            document.getElementById('totalPages').textContent = totalPagesFiltered;
            document.getElementById('totalCount2').textContent = this.hasMore ? (totalFiltered + '+') : totalFiltered;

            document.getElementById('firstPage').disabled = this.currentPage === 1;
            document.getElementById('prevPage').disabled = this.currentPage === 1;
            document.getElementById('nextPage').disabled = (this.currentPage >= totalPagesFiltered) && !this.hasMore;
            document.getElementById('lastPage').disabled = this.hasMore || (this.currentPage >= totalPagesFiltered);

            if (loadMoreBtn) {
                loadMoreBtn.style.display = this.hasMore ? 'inline-block' : 'none';
                loadMoreBtn.disabled = this._loadingMore;
            }
        } else {
            paginationDiv.style.display = 'none';
            if (loadMoreBtn) loadMoreBtn.style.display = 'none';
        }
    },

    /**
     * 跳转到指定页
     */
    async goToPage(page) {
        const totalPagesFiltered = Math.ceil(this.filteredAlerts.length / this.pageSize);

        console.log('🔄 请求跳转到第', page, '页');
        console.log('   当前筛选数据:', this.filteredAlerts.length, '条');
        console.log('   每页显示:', this.pageSize, '条');
        console.log('   总页数:', totalPagesFiltered, '页');

        if (page < 1) {
            console.warn('❌ 页码小于1，忽略');
            return;
        }

        if (page > totalPagesFiltered) {
            if (this.hasMore) {
                await this.loadMoreAlerts();
                const updatedTotalPages = Math.ceil(this.filteredAlerts.length / this.pageSize);
                if (page > updatedTotalPages) {
                    console.warn('❌ 页码超出范围（最大', updatedTotalPages, '页），忽略');
                    return;
                }
            } else {
                console.warn('❌ 页码超出范围（最大', totalPagesFiltered, '页），忽略');
                return;
            }
        }

        this.currentPage = page;
        console.log('✅ 跳转到第', page, '页');
        this.displayCurrentPage();
    },

    /**
     * 改变每页显示数量
     */
    changePageSize() {
        this.pageSize = parseInt(document.getElementById('pageSize').value);
        this.currentPage = 1;
        this.displayCurrentPage();
    },

    /**
     * 加载单条告警的完整数据
     */
    async loadFullAlertData(webhookId, alertItem) {
        console.log('🔄 加载完整数据:', webhookId);

        // 显示加载状态
        const dataTab = alertItem.querySelector('[data-tab-content="data"]');
        const aiTab = alertItem.querySelector('[data-tab-content="ai"]');

        if (dataTab) {
            dataTab.innerHTML = '<div style="padding: 2rem; text-align: center;"><div class="spinner"></div><p>正在加载完整数据...</p></div>';
        }

        try {
            const result = await API.getWebhook(webhookId);

            if (result.success && result.data) {
                const fullData = result.data;

                // 更新 alerts 中的数据（合并）
                const index = this.alerts.findIndex(w => w.id == webhookId);
                if (index !== -1) {
                    this.alerts[index] = { ...this.alerts[index], ...fullData };
                }

                // 更新概览标签页
                const overviewTab = alertItem.querySelector('[data-tab-content="overview"]');
                if (overviewTab && index !== -1) {
                    overviewTab.innerHTML = this.renderOverview(this.alerts[index]);
                }

                // 更新原始数据标签页
                if (dataTab && fullData.parsed_data) {
                    dataTab.innerHTML = renderJSONBlock(fullData.parsed_data, '原始数据');
                }

                // 更新 AI 分析标签页
                if (aiTab && fullData.ai_analysis) {
                    aiTab.innerHTML = this.renderAIAnalysis(fullData.ai_analysis);
                } else if (aiTab) {
                    aiTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: #94a3b8;">暂无 AI 分析数据</div>';
                }

                console.log('✅ 完整数据加载成功');
            } else {
                throw new Error(result.error || '加载失败');
            }
        } catch (error) {
            console.error('❌ 加载完整数据失败:', error);
            if (dataTab) {
                dataTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: #ef4444;">❌ 加载失败: ' + escapeHtml(String(error.message || error)) + '</div>';
            }
        }
    },

    /**
     * 重新分析告警
     */
    async reanalyzeAlert(id) {
        console.log('开始重新分析 webhook:', id);

        if (!confirm('确认要重新分析这条告警吗？')) {
            return;
        }

        try {
            const result = await API.reanalyze(id);

            console.log('重新分析结果:', result);

            if (result.success) {
                alert('✅ 重新分析成功！');
                this.loadAlerts();
            } else {
                alert('❌ 分析失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            console.error('重新分析错误:', error);
            alert('❌ 请求失败: ' + error.message);
        }
    },

    /**
     * 打开转发模态框
     */
    openForwardModal(id) {
        console.log('打开转发模态框, webhook ID:', id);
        this.currentForwardId = id;

        // 获取配置的转发地址作为默认值
        const configUrl = document.getElementById('configForwardUrl');
        const forwardUrlInput = document.getElementById('forwardUrl');

        if (configUrl && forwardUrlInput) {
            forwardUrlInput.value = configUrl.value || '';
        }

        const modal = document.getElementById('forwardModal');
        if (modal) {
            modal.classList.add('active');
            console.log('转发模态框已打开');
        } else {
            console.error('找不到转发模态框元素');
        }
    },

    /**
     * 关闭转发模态框
     */
    closeForwardModal() {
        document.getElementById('forwardModal').classList.remove('active');
        this.currentForwardId = null;
    },

    /**
     * 确认转发
     */
    async confirmForward() {
        const url = document.getElementById('forwardUrl').value;
        if (!url) return alert('请输入转发地址');

        try {
            const result = await API.forward(this.currentForwardId, url);

            if (result.success) {
                alert('✅ 转发成功！');
                this.closeForwardModal();
            } else {
                alert('❌ 转发失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            alert('❌ 请求失败: ' + error.message);
        }
    },

    /**
     * 加载深度分析历史记录
     */
    async loadDeepAnalyses(webhookId) {
        const container = document.getElementById('deep-analysis-container-' + webhookId);
        if (!container) return;

        container.innerHTML = '<div style="padding: 2rem; text-align: center;"><div class="spinner"></div><p>正在加载深度分析历史...</p></div>';

        try {
            const result = await API.getDeepAnalyses(webhookId);
            const records = result.data || [];

            if (records.length === 0) {
                container.innerHTML = '<div style="text-align:center; padding:30px; color:#888;">' +
                    '<p>暂无深度分析记录</p>' +
                    '<button class="btn btn-primary" onclick="window.alertsModule.deepAnalyzeAlert(' + webhookId + ')">\ud83d\udd2c 立即分析</button>' +
                    '</div>';
                return;
            }

            let html = '';
            records.forEach(function(record) {
                const analysis = record.analysis_result || {};
                const engineLabel = record.engine === 'openclaw' ? '\ud83d\udc19 OpenClaw' : '\ud83e\udd16 \u672c\u5730 AI';
                const time = new Date(record.created_at).toLocaleString('zh-CN');
                const duration = record.duration_seconds ? record.duration_seconds.toFixed(1) + 's' : '-';

                html += '<div style="border:1px solid #e0e0e0; border-radius:8px; padding:16px; margin-bottom:12px; background:#fafafa;">';

                // \u5934\u90e8\uff1a\u5f15\u64ce\u3001\u65f6\u95f4\u3001\u8017\u65f6
                html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid #eee;">';
                html += '<span style="font-weight:600;">' + engineLabel + '</span>';
                html += '<span style="color:#888; font-size:0.85em;">' + time + ' | \u8017\u65f6 ' + duration + '</span>';
                html += '</div>';

                // \u7528\u6237\u95ee\u9898\uff08\u5982\u679c\u6709\uff09
                if (record.user_question) {
                    html += '<div style="margin-bottom:10px; padding:8px 12px; background:#e8f4fd; border-radius:4px; font-size:0.9em;">';
                    html += '<strong>\u7528\u6237\u95ee\u9898\uff1a</strong>' + record.user_question;
                    html += '</div>';
                }

                // 检查是否为 pending 状态（OpenClaw 异步等待结果）
                if (record.status === 'pending') {
                    // 分析中状态卡片
                    html += '<div style="text-align:center; padding:20px; background:linear-gradient(135deg, #f093fb 0%, #f5576c 100%); border-radius:8px; color:white;">';
                    html += '<div style="font-size:2em; margin-bottom:12px;">⏳</div>';
                    html += '<div style="font-size:1.1em; font-weight:600; margin-bottom:8px;">OpenClaw 正在分析中...</div>';
                    if (record.openclaw_run_id) {
                        html += '<div style="font-size:0.8em; color:rgba(255,255,255,0.7); margin-bottom:12px;">Run ID: ' + record.openclaw_run_id + '</div>';
                    }
                    html += '<div style="font-size:0.9em; color:rgba(255,255,255,0.9);">结果将自动更新，请稍后刷新页面</div>';
                    html += '</div>';
                } else if (analysis.status === 'triggered') {
                    // \u7279\u6b8a\u5361\u7247\u6837\u5f0f\uff1a\u5df2\u89e6\u53d1 OpenClaw \u5206\u6790
                    html += '<div style="text-align:center; padding:20px; background:linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius:8px; color:white;">';
                    html += '<div style="font-size:2em; margin-bottom:12px;">\ud83d\ude80</div>';
                    html += '<div style="font-size:1.1em; font-weight:600; margin-bottom:8px;">\u5df2\u89e6\u53d1 OpenClaw \u5206\u6790</div>';
                    if (analysis.runId) {
                        html += '<div style="font-size:0.8em; color:rgba(255,255,255,0.7); margin-bottom:12px;">Run ID: ' + analysis.runId + '</div>';
                    }
                    html += '<div style="font-size:0.9em; color:rgba(255,255,255,0.9);">\u5206\u6790\u7ed3\u679c\u8bf7\u5728 OpenClaw \u63a7\u5236\u53f0\u67e5\u770b</div>';
                    html += '</div>';
                } else {
                    // \u6b63\u5e38\u5206\u6790\u7ed3\u679c\u6e32\u67d3
                    // 如果有完整的 OpenClaw 文本，优先渲染 markdown
                    if (analysis._openclaw_text) {
                        html += '<pre style="white-space:pre-wrap; font-size:0.85em;">' + escapeHtml(String(analysis._openclaw_text)) + '</pre>';
                        // 如果有置信度，单独显示
                        if (analysis.confidence !== undefined) {
                            const pct = (analysis.confidence * 100).toFixed(0);
                            html += '<div style="margin-top:8px; color:#888; font-size:0.85em;">\u7f6e\u4fe1\u5ea6: ' + pct + '%</div>';
                        }
                    } else {
                        // 原有的 JSON 字段渲染逻辑
                        if (analysis.root_cause) {
                            html += '<div style="margin-bottom:8px;"><strong>\ud83d\udd0d \u6839\u56e0\u5206\u6790\uff1a</strong><p style="margin:4px 0; white-space:pre-wrap;">' + escapeHtml(String(analysis.root_cause)) + '</p></div>';
                        }
                        if (analysis.impact) {
                            html += '<div style="margin-bottom:8px;"><strong>\ud83d\udca5 \u5f71\u54cd\u8303\u56f4\uff1a</strong><p style="margin:4px 0; white-space:pre-wrap;">' + escapeHtml(String(analysis.impact)) + '</p></div>';
                        }
                        if (analysis.recommendations && Array.isArray(analysis.recommendations)) {
                            html += '<div style="margin-bottom:8px;"><strong>\u2705 \u4fee\u590d\u5efa\u8bae\uff1a</strong><ul style="margin:4px 0; padding-left:20px;">';
                            analysis.recommendations.forEach(function(rec) {
                                if (typeof rec === 'object' && rec !== null) {
                                    var label = (rec.priority ? '<strong>' + escapeHtml(String(rec.priority)) + '</strong>: ' : '') + escapeHtml(String(rec.action || JSON.stringify(rec)));
                                    html += '<li>' + label + '</li>';
                                } else {
                                    html += '<li>' + escapeHtml(String(rec)) + '</li>';
                                }
                            });
                            html += '</ul></div>';
                        }
                        if (analysis.confidence !== undefined) {
                            const pct = (analysis.confidence * 100).toFixed(0);
                            html += '<div style="margin-top:8px; color:#888; font-size:0.85em;">\u7f6e\u4fe1\u5ea6: ' + pct + '%</div>';
                        }

                        // \u5982\u679c\u6ca1\u6709\u7ed3\u6784\u5316\u5b57\u6bb5\uff0cfallback \u663e\u793a\u539f\u59cb JSON
                        if (!analysis.root_cause && !analysis.impact && !analysis.recommendations) {
                            html += '<pre style="background:#f5f5f5; padding:12px; border-radius:4px; overflow-x:auto; font-size:0.85em; max-height:300px;">' + escapeHtml(JSON.stringify(analysis, null, 2)) + '</pre>';
                        }
                    }
                }

                html += '</div>';
            });

            // 底部：再次分析按钮
            html += '<div style="text-align:center; margin-top:12px;">';
            html += '<button class="btn btn-sm" onclick="window.alertsModule.deepAnalyzeAlert(' + webhookId + ')">\ud83d\udd2c 再次分析</button>';
            html += '</div>';

            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = '<div style="color:red; padding:20px;">加载失败: ' + escapeHtml(String(e.message || e)) + '</div>';
        }
    },

    /**
     * 深度分析告警
     */
    async deepAnalyzeAlert(id) {
        // 让用户选择分析引擎
        const engineChoice = confirm('使用 OpenClaw Agent 深度分析？\n\n点击「确定」使用 OpenClaw（更深度）\n点击「取消」使用本地 AI');
        const engine = engineChoice ? 'openclaw' : 'local';

        const question = prompt('请输入您想问的问题（可选）:', '');
        if (question === null) return;  // 用户取消

        try {
            const result = await API.deepAnalyze(id, question, engine);
            if (result.success && result.data) {
                const analysisResult = result.data.analysis || {};

                // 检查是否为 triggered 状态（OpenClaw 异步触发）
                if (analysisResult.status === 'triggered') {
                    // 显示友好的浮层提示（不用 alert）
                    this.showTriggeredNotification(analysisResult.runId);
                }

                // 分析完成，切换到深度分析标签页并刷新数据
                const alertItem = document.querySelector('.alert-item[data-id="' + id + '"]');
                if (alertItem) {
                    // 确保详情展开
                    if (!alertItem.classList.contains('expanded')) {
                        alertItem.classList.add('expanded');
                    }

                    // 切换到深度分析 tab
                    const tabs = alertItem.querySelectorAll('.tab');
                    const contents = alertItem.querySelectorAll('.tab-content');
                    tabs.forEach(function(t) { t.classList.remove('active'); });
                    contents.forEach(function(c) { c.classList.remove('active'); });

                    const deepTab = alertItem.querySelector('[data-tab="deep-analysis"]');
                    const deepContent = alertItem.querySelector('[data-tab-content="deep-analysis"]');
                    if (deepTab) deepTab.classList.add('active');
                    if (deepContent) deepContent.classList.add('active');

                    // 加载深度分析历史记录
                    this.loadDeepAnalyses(id);
                } else {
                    // 如果告警项不在当前页面，显示简单提示
                    alert('✅ 分析完成！请展开告警详情查看深度分析结果。');
                }
            } else {
                alert('分析失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            alert('请求失败: ' + error.message);
        }
    },

    /**
     * 显示 OpenClaw 分析已触发的友好提示
     */
    showTriggeredNotification(runId) {
        // 创建浮层提示
        const notification = document.createElement('div');
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 16px 24px;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(102, 126, 234, 0.4);
            z-index: 10000;
            max-width: 360px;
            animation: slideIn 0.3s ease-out;
        `;
        notification.innerHTML = `
            <div style="display:flex; align-items:center; margin-bottom:8px;">
                <span style="font-size:1.5em; margin-right:10px;">\ud83d\ude80</span>
                <strong style="font-size:1.1em;">已触发 OpenClaw 分析</strong>
            </div>
            <div style="font-size:0.9em; color:rgba(255,255,255,0.9); margin-bottom:8px;">
                分析请求已发送，结果将在 OpenClaw 控制台展示
            </div>
            ${runId ? `<div style="font-size:0.8em; color:rgba(255,255,255,0.7);">Run ID: ${runId}</div>` : ''}
        `;

        // 添加动画样式
        if (!document.getElementById('triggered-notification-style')) {
            const style = document.createElement('style');
            style.id = 'triggered-notification-style';
            style.textContent = `
                @keyframes slideIn {
                    from { transform: translateX(100%); opacity: 0; }
                    to { transform: translateX(0); opacity: 1; }
                }
                @keyframes slideOut {
                    from { transform: translateX(0); opacity: 1; }
                    to { transform: translateX(100%); opacity: 0; }
                }
            `;
            document.head.appendChild(style);
        }

        document.body.appendChild(notification);

        // 4秒后自动消失
        setTimeout(() => {
            notification.style.animation = 'slideOut 0.3s ease-in forwards';
            setTimeout(() => notification.remove(), 300);
        }, 4000);
    }
};
