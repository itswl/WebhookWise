/**
 * Alert List Module
 * Handles loading, filtering, pagination, display, and interaction of alerts
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
    _searchTerm: '',
    _searchDebounce: null,
    currentTabByAlert: {},
    _extractCursorMeta(result) {
        const pag = result ? (result.cursor || result.pagination) : null;
        const nextCursor = pag ? (pag.next_cursor ?? null) : null;
        const hasMore = pag ? !!pag.has_more : false;
        return { nextCursor, hasMore };
    },
    // Time-window filter value (server-side). '' / 'all' = no bound.
    _windowValue() {
        const el = document.getElementById('timeWindowFilter');
        return el ? el.value : '';
    },

    /**
     * Initialize the alert module
     */
    init() {
        this.loadAlerts();
        this.bindEvents();
    },

    /**
     * Bind event handlers
     */
    bindEvents() {
        // Search and filter events
        const searchInput = document.getElementById('searchInput');
        const importanceFilter = document.getElementById('importanceFilter');
        const sourceFilter = document.getElementById('sourceFilter');
        const duplicateFilter = document.getElementById('duplicateFilter');
        const processingStatusFilter = document.getElementById('processingStatusFilter');
        const timeWindowFilter = document.getElementById('timeWindowFilter');
        const pageSizeSelect = document.getElementById('pageSize');

        if (searchInput) {
            searchInput.addEventListener('input', () => {
                // Server-side full-text search debounced at 300 ms.
                clearTimeout(this._searchDebounce);
                this._searchDebounce = setTimeout(() => {
                    this._searchTerm = searchInput.value.trim();
                    this.loadAlerts();
                }, 300);
            });
        }
        if (importanceFilter) {
            importanceFilter.addEventListener('change', () => this.filterAlerts());
        }
        if (sourceFilter) {
            sourceFilter.addEventListener('change', () => this.filterAlerts());
        }
        if (processingStatusFilter) {
            processingStatusFilter.addEventListener('change', () => this.filterAlerts());
        }
        if (duplicateFilter) {
            duplicateFilter.addEventListener('change', () => this.filterAlerts());
        }
        if (timeWindowFilter) {
            // Window is server-side → reload from the API rather than client-filter.
            timeWindowFilter.addEventListener('change', () => this.loadAlerts());
        }
        if (pageSizeSelect) {
            pageSizeSelect.addEventListener('change', () => this.changePageSize());
        }

        // Event delegation for alert item interactions
        document.addEventListener('click', (e) => this.handleAlertClick(e));
    },

    /**
     * Handle alert-related click events
     */
    handleAlertClick(e) {
        // Handle button actions first
        const btn = e.target.closest('button[data-action]');
        if (btn) {
            e.stopPropagation();
            const action = btn.getAttribute('data-action');
            const id = btn.getAttribute('data-id');
            console.log('Button click:', action, id);

            if (action === 'reanalyze') {
                this.reanalyzeAlert(id);
            } else if (action === 'deep-analyze') {
                this.deepAnalyzeAlert(id);
            } else if (action === 'forward') {
                this.openForwardModal(id);
            } else if (action === 'replay-dl') {
                this.replayDeadLetter(id);
            } else if (action === 'quick-silence') {
                this.quickSilence(id);
            } else if (action === 'replay-dry') {
                this.replayDryRun(id);
            }
            return;
        }

        // Tab switching
        if (e.target.closest('.tab')) {
            const tab = e.target.closest('.tab');
            const tabName = tab.getAttribute('data-tab');
            const alertItem = tab.closest('.alert-item');
            const webhookId = tab.getAttribute('data-id');

            // Toggle tab active state
            alertItem.querySelectorAll('.tab').forEach(function(t) {
                t.classList.remove('active');
            });
            tab.classList.add('active');

            // Toggle content display
            alertItem.querySelectorAll('.tab-content').forEach(function(content) {
                const contentTab = content.getAttribute('data-tab-content');
                if (contentTab === tabName) {
                    content.classList.add('active');
                } else {
                    content.classList.remove('active');
                }
            });

            // If switching to the deep analysis tab, load data
            if (tabName === 'deep-analysis' && webhookId) {
                this.loadDeepAnalyses(webhookId);
            }
            // If switching to the decision tab, load the trace + delivery
            if (tabName === 'decision' && webhookId) {
                this.loadDecisionTrace(webhookId);
            }
            // If switching to the timeline tab, load the incident timeline
            if (tabName === 'timeline' && webhookId) {
                this.loadTimeline(webhookId);
            }
            return;
        }

        // Expand/collapse alert
        if (e.target.closest('.alert-header')) {
            const header = e.target.closest('.alert-header');
            // If a button or an element inside a button was clicked, do nothing
            if (e.target.closest('button')) return;

            const alertItem = header.closest('.alert-item');
            const isExpanding = !alertItem.classList.contains('expanded');
            alertItem.classList.toggle('expanded');

            // If expanding and data is in summary mode, load full data
            if (isExpanding) {
                const webhookId = alertItem.getAttribute('data-id');
                const webhook = this.alerts.find(w => w.id == webhookId);

                // Check whether full data needs to be loaded
                if (webhook && !webhook.parsed_data && !webhook.ai_analysis) {
                    this.loadFullAlertData(webhookId, alertItem);
                }
            }
        }
    },

    /**
     * Load alert data
     */
    async loadAlerts() {
        try {
            // Show loading indicator
            const alertList = document.getElementById('alertList');
            alertList.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('alerts.loadingData') + '</p></div>';

            const params = { page_size: 200, cursor: null, window: this._windowValue() };
            if (this._searchTerm) params.search = this._searchTerm;
            const result = await API.getWebhooks(params);

            if (!result.success || !result.data) {
                throw new Error(t('alerts.error.invalidData'));
            }

            this.alerts = result.data;
            const meta = this._extractCursorMeta(result);
            this.nextCursor = meta.nextCursor;
            this.hasMore = meta.hasMore;
            // Real server-side total for the active window (null when unknown).
            this.totalCount = (result.pagination && result.pagination.total != null) ? result.pagination.total : null;

            console.log('✅ Data loaded:', this.alerts.length, 'items (total', this.totalCount, 'items)');

            this.updateStats();
            this.currentPage = 1;
            this.filterAlerts(true);

            document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('zh-CN');
        } catch (error) {
            console.error('Load failed:', error);
            showError(t('alerts.error.loadFailed') + ': ' + error.message);
        }
    },

    async loadMoreAlerts() {
        if (!this.hasMore || this._loadingMore) return;
        this._loadingMore = true;
        try {
            const btn = document.getElementById('loadMoreBtn');
            if (btn) {
                btn.disabled = true;
                btn.textContent = t('common.loading');
            }

            const loadMoreParams = { page_size: 200, cursor: this.nextCursor, window: this._windowValue() };
            if (this._searchTerm) loadMoreParams.search = this._searchTerm;
            const result = await API.getWebhooks(loadMoreParams);
            if (!result.success || !result.data) {
                throw new Error(t('alerts.error.invalidData'));
            }

            this.alerts = this.alerts.concat(result.data);
            const meta = this._extractCursorMeta(result);
            this.nextCursor = meta.nextCursor;
            this.hasMore = meta.hasMore;

            this.updateStats();
            this.filterAlerts(false);
        } catch (error) {
            console.error('Load more failed:', error);
            alert(t('alerts.error.loadMoreFailed') + ': ' + error.message);
        } finally {
            const btn = document.getElementById('loadMoreBtn');
            if (btn) {
                btn.disabled = false;
                btn.textContent = t('alerts.page.loadMore');
            }
            this._loadingMore = false;
        }
    },

    /**
     * Update statistics
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

            if (!!w.is_duplicate) duplicateCount++;
        });

        document.getElementById('highCount').textContent = highCount;
        document.getElementById('mediumCount').textContent = mediumCount;
        document.getElementById('duplicateCount').textContent = duplicateCount;
    },

    /**
     * Filter alerts
     */
    filterAlerts(resetPage = true) {
        const importanceFilter = document.getElementById('importanceFilter').value;
        const sourceFilter = document.getElementById('sourceFilter').value;
        const duplicateFilter = document.getElementById('duplicateFilter').value;
        const processingStatusFilter = document.getElementById('processingStatusFilter') ? document.getElementById('processingStatusFilter').value : '';

        // Filter data (search is server-side; these filters are instant on loaded data).
        this.filteredAlerts = this.alerts.filter(function(webhook) {
            let matchImportance = true;
            if (importanceFilter) {
                const webhookImportance = webhook.importance || 'low';
                matchImportance = webhookImportance === importanceFilter;
            }

            const matchSource = !sourceFilter || webhook.source === sourceFilter;

            let matchDuplicate = true;
            if (duplicateFilter === 'original') {
                matchDuplicate = !webhook.is_duplicate;
            } else if (duplicateFilter === 'duplicate') {
                matchDuplicate = !!webhook.is_duplicate;
            }

            const matchProcessingStatus = !processingStatusFilter || webhook.processing_status === processingStatusFilter;

            return matchImportance && matchSource && matchDuplicate && matchProcessingStatus;
        });

        console.log('Filter results:', this.filteredAlerts.length, 'items (of', this.alerts.length, 'items)');

        if (resetPage) {
            this.currentPage = 1;
        }

        // Display current page data
        this.displayCurrentPage();
    },

    /**
     * Display current page data (client-side pagination)
     */
    displayCurrentPage() {
        const totalFiltered = this.filteredAlerts.length;
        const totalPagesFiltered = Math.ceil(totalFiltered / this.pageSize);

        // Ensure the current page is within the valid range
        if (this.currentPage > totalPagesFiltered && totalPagesFiltered > 0) {
            console.warn('⚠️  Current page number out of range, resetting to last page');
            this.currentPage = totalPagesFiltered;
        }

        // Calculate the data range for the current page
        const startIndex = (this.currentPage - 1) * this.pageSize;
        const endIndex = Math.min(startIndex + this.pageSize, totalFiltered);
        const currentPageData = this.filteredAlerts.slice(startIndex, endIndex);

        console.log('📄 Showing page', this.currentPage, 'of', totalPagesFiltered, 'pages');
        console.log('📊 Data range:', startIndex, '-', endIndex, ', showing', currentPageData.length, 'items');
        console.log('📈 Total after filtering:', totalFiltered, 'items (original data', this.alerts.length, 'items)');

        // Update pagination info
        this.updatePagination(totalFiltered, totalPagesFiltered);

        // Display data
        this.renderAlerts(currentPageData);
    },

    /**
     * Render the alert list
     */
    renderAlerts(webhooks) {
        const container = document.getElementById('alertList');

        if (webhooks.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-title">' + t('alerts.empty.title') + '</div><div class="empty-text">' + t('alerts.empty.text') + '</div></div>';
            return;
        }

        let html = '';
        webhooks.forEach((webhook) => {
            const importance = webhook.importance || 'low';
            const duplicateType = webhook.duplicate_type || 'new';
            const isDuplicate = duplicateType !== 'new' && !!webhook.is_duplicate;
            const analysis = webhook.ai_analysis || {};
            const summary = webhook.summary || analysis.summary || '';

            html += '<div class="alert-item" data-id="' + escapeHtml(String(webhook.id)) + '">';
            html += '<div class="alert-header">';
            html += '<div class="alert-left">';
            html += '<div class="alert-title-row">';
            html += '<span class="alert-icon">' + getAlertIcon(importance) + '</span>';
            html += '<span class="alert-title">' + escapeHtml(String(summary || webhook.source || t('alerts.titleFallback', {id: webhook.id}))) + '</span>';
            html += '</div>';
            html += '<div class="alert-meta">';
            html += '<span class="alert-meta-item">🆔 #' + escapeHtml(String(webhook.id)) + '</span>';
            html += '<span class="alert-meta-item">📍 ' + escapeHtml(String(webhook.source || 'unknown')) + '</span>';

            // Always show the client IP
            if (webhook.client_ip) {
                html += '<span class="alert-meta-item">🌐 ' + escapeHtml(String(webhook.client_ip)) + '</span>';
            }

            html += '<span class="alert-meta-item">🕐 ' + formatTime(webhook.timestamp) + '</span>';

            // Show duplicate information
            if (isDuplicate) {
                html += '<span class="alert-meta-item">🔗 ' + t('alerts.meta.original', {id: webhook.duplicate_of}) + '</span>';
                // Show previous alert ID and time
                if (webhook.prev_alert_id) {
                    let prevText = '⏮️ ' + t('alerts.meta.previous', {id: webhook.prev_alert_id});
                    if (webhook.prev_alert_timestamp) {
                        prevText += ' (' + timeAgo(webhook.prev_alert_timestamp) + ')';
                    }
                    html += '<span class="alert-meta-item">' + prevText + '</span>';
                }
            }
            html += '</div></div>';
            html += '<div class="alert-right">';
            html += '<span class="badge badge-' + importance + '">' + getImportanceText(importance) + '</span>';
            if (isDuplicate) {
                html += '<span class="badge badge-duplicate" title="' + t('alerts.badge.duplicate') + '">' + t('alerts.badge.duplicate') + '</span>';
            } else {
                html += '<span class="badge badge-new">' + t('alerts.badge.new') + '</span>';
            }
            // Forward status badge
            if (webhook.forward_status) {
                var fwdLabels = { 'pending': t('alerts.fwd.pending'), 'queued': t('alerts.fwd.queued'), 'skipped': t('alerts.fwd.skipped'), 'forwarded': t('alerts.fwd.forwarded'), 'sent': t('alerts.fwd.sent'), 'failed': t('alerts.fwd.failed'), 'success': t('alerts.fwd.sent') };
                var fwdClass = (webhook.forward_status === 'sent' || webhook.forward_status === 'success' || webhook.forward_status === 'forwarded') ? 'badge-low' : ((webhook.forward_status === 'failed') ? 'badge-high' : 'badge-medium');
                html += '<span class="badge ' + fwdClass + '" title="' + t('alerts.fwd.statusTitle') + '">📤 ' + escapeHtml(fwdLabels[webhook.forward_status] || webhook.forward_status) + '</span>';
            }
            html += '<span class="alert-time">' + timeAgo(webhook.timestamp) + '</span>';
            html += '<div class="alert-actions">';
            html += '<button class="btn btn-sm" data-action="reanalyze" data-id="' + escapeHtml(String(webhook.id)) + '">🔄 ' + t('alerts.action.reanalyze') + '</button>';
            html += '<button class="btn btn-sm" data-action="deep-analyze" data-id="' + escapeHtml(String(webhook.id)) + '">🔬 ' + t('alerts.action.deepAnalyze') + '</button>';
            html += '<button class="btn btn-sm btn-primary" data-action="forward" data-id="' + escapeHtml(String(webhook.id)) + '">🚀 ' + t('alerts.action.forward') + '</button>';
            html += '<button class="btn btn-sm btn-warn" data-action="quick-silence" data-id="' + escapeHtml(String(webhook.id)) + '" title="' + t('alerts.action.quickSilenceTitle') + '">🔕 ' + t('alerts.action.quickSilence') + '</button>';
            html += '<button class="btn btn-sm" data-action="replay-dry" data-id="' + escapeHtml(String(webhook.id)) + '" title="' + t('alerts.action.replayDryTitle') + '">🔁 ' + t('alerts.action.replayDry') + '</button>';
            if (webhook.processing_status === 'dead_letter') {
                html += '<button class="btn btn-sm btn-danger" data-action="replay-dl" data-id="' + escapeHtml(String(webhook.id)) + '">🔄 ' + t('alerts.action.replayDeadLetter') + '</button>';
            }
            html += '</div></div></div>';

            html += '<div class="alert-details">';
            html += '<div class="details-tabs">';
            html += '<div class="tab active" data-tab="overview" data-id="' + webhook.id + '">' + t('alerts.tab.overview') + '</div>';
            html += '<div class="tab" data-tab="data" data-id="' + webhook.id + '">' + t('alerts.tab.rawData') + '</div>';
            // AI Analysis tab
            if (analysis && Object.keys(analysis).length > 0) {
                html += '<div class="tab" data-tab="ai" data-id="' + webhook.id + '">' + t('alerts.tab.ai') + '</div>';
            } else if (summary || webhook.importance) {
                html += '<div class="tab" data-tab="ai" data-id="' + webhook.id + '">' + t('alerts.tab.ai') + '</div>';
            }
            // Deep Analysis tab
            html += '<div class="tab" data-tab="deep-analysis" data-id="' + webhook.id + '">' + t('alerts.tab.deep') + '</div>';
            // Decision / Delivery tab (why forwarded/skipped + did it deliver)
            html += '<div class="tab" data-tab="decision" data-id="' + webhook.id + '">' + t('alerts.tab.decision') + '</div>';
            // Incident Timeline tab
            html += '<div class="tab" data-tab="timeline" data-id="' + webhook.id + '">📅 ' + t('alerts.tab.timeline') + '</div>';
            html += '</div>';

            html += '<div class="tab-content active" data-tab-content="overview">';
            html += this.renderOverview(webhook);
            html += '</div>';

            html += '<div class="tab-content" data-tab-content="data">';
            if (webhook.parsed_data) {
                html += renderJSONBlock(webhook.parsed_data, t('alerts.tab.rawData'));
            } else {
                html += '<div style="padding: 2rem; text-align: center; color: #94a3b8;">' + t('alerts.noData') + '</div>';
            }
            html += '</div>';

            // AI analysis content
            if (analysis && Object.keys(analysis).length > 0) {
                html += '<div class="tab-content" data-tab-content="ai">';
                html += this.renderAIAnalysis(analysis);
                html += '</div>';
            } else if (summary || webhook.importance) {
                html += '<div class="tab-content" data-tab-content="ai">';
                html += '<div class="ai-section">';
                html += '<div class="ai-header">🤖 ' + t('alerts.ai.resultsTitle') + '</div>';
                html += '<div class="ai-content">';
                if (summary) {
                    html += '<div class="ai-item"><div class="ai-label">' + t('alerts.ai.summary') + '</div><div class="ai-value">' + escapeHtml(String(summary)) + '</div></div>';
                }
                if (webhook.importance) {
                    html += '<div class="ai-item"><div class="ai-label">' + t('alerts.ai.importance') + '</div><div class="ai-value">' + getImportanceText(webhook.importance) + '</div></div>';
                }
                html += '</div></div>';
                html += '<div style="margin-top: 1rem; padding: 0.75rem; background: #f0f9ff; border-left: 3px solid #0ea5e9; border-radius: 4px;">';
                html += '<p style="margin: 0; color: #0369a1; font-size: 0.9rem;">💡 ' + t('alerts.ai.autoLoadHint') + '</p>';
                html += '</div>';
                html += '</div>';
            }

            // Deep analysis content panel
            html += '<div class="tab-content" data-tab-content="deep-analysis">';
            html += '<div id="deep-analysis-container-' + webhook.id + '">' + t('alerts.deep.clickToLoad') + '</div>';
            html += '</div>';

            // Decision / delivery content panel (lazy-loaded on tab click)
            html += '<div class="tab-content" data-tab-content="decision">';
            html += '<div id="decision-container-' + webhook.id + '">' + t('alerts.decision.clickToLoad') + '</div>';
            html += '</div>';

            // Incident timeline panel (lazy-loaded on tab click)
            html += '<div class="tab-content" data-tab-content="timeline">';
            html += '<div id="timeline-container-' + webhook.id + '">📅 ' + t('alerts.timeline.clickToLoad') + '</div>';
            html += '</div>';

            html += '</div></div>';
        });

        container.innerHTML = html;
    },

    /**
     * Render overview information
     */
    renderOverview(webhook) {
        let html = '<div class="info-grid">';
        html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.alertId') + '</div><div class="info-value">#' + webhook.id + '</div></div>';
        html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.source') + '</div><div class="info-value">' + escapeHtml(String(webhook.source || '-')) + '</div></div>';
        if (webhook.request_id) {
            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.requestId') + '</div><div class="info-value" style="font-size:0.75rem;word-break:break-all;">' + escapeHtml(String(webhook.request_id)) + '</div></div>';
        }
        if (webhook.alert_hash) {
            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.fingerprint') + '</div><div class="info-value" style="font-size:0.75rem;">' + escapeHtml(String(webhook.alert_hash).substring(0, 16) + '…') + '</div></div>';
        }
        html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.clientIp') + '</div><div class="info-value">' + escapeHtml(String(webhook.client_ip || '-')) + '</div></div>';
        html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.receivedAt') + '</div><div class="info-value">' + new Date(webhook.timestamp).toLocaleString('zh-CN') + '</div></div>';
        const statusMap = { received: t('alerts.status.received'), analyzing: t('alerts.status.analyzing'), completed: t('alerts.status.completed'), failed: t('alerts.status.failed'), dead_letter: t('alerts.status.deadLetter') };
        const statusText = statusMap[webhook.processing_status] || String(webhook.processing_status || '-');
        html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.processingStatus') + '</div><div class="info-value">' + escapeHtml(statusText) + '</div></div>';
        if (webhook.updated_at) {
            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.lastUpdated') + '</div><div class="info-value">' + new Date(webhook.updated_at).toLocaleString('zh-CN') + '</div></div>';
        }
        if (webhook.processing_status === 'failed' || webhook.processing_status === 'dead_letter') {
            const failure = webhook.failure_reason || webhook.error_message || '-';
            html += '<div class="info-item" style="grid-column: 1 / -1;"><div class="info-label">' + t('alerts.overview.failureReason') + '</div><div class="info-value" style="color:#ef4444; white-space: pre-wrap;">' + escapeHtml(String(failure)) + '</div></div>';
        }
        if (webhook.is_duplicate) {
            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.originalAlert') + '</div><div class="info-value">#' + webhook.duplicate_of + '</div></div>';
            if (webhook.prev_alert_id) {
                let prevValue = '#' + webhook.prev_alert_id;
                if (webhook.prev_alert_timestamp) {
                    prevValue += ' (' + new Date(webhook.prev_alert_timestamp).toLocaleString('zh-CN') + ')';
                }
                html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.previousAlert') + '</div><div class="info-value">' + prevValue + '</div></div>';
            }
            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.duplicateCount') + '</div><div class="info-value">' + (webhook.duplicate_count || 1) + '</div></div>';

            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.duplicateType') + '</div><div class="info-value">' + t('alerts.badge.duplicate') + '</div></div>';
        }
        html += '</div>';
        return html;
    },

    /**
     * Render AI analysis results
     */
    renderAIAnalysis(analysis) {
        if (!analysis || Object.keys(analysis).length === 0) {
            return '<div style="padding: 2rem; text-align: center; color: var(--text-muted);">' + t('alerts.ai.noData') + '</div>';
        }

        let html = `
            <div class="ai-analysis" style="border-left: 4px solid var(--primary); background: var(--bg-surface); border: 1px solid var(--border); padding: 1.5rem; border-radius: 12px; box-shadow: var(--shadow-sm); margin-bottom: 1rem;">
                <div class="ai-header" style="font-size: 1rem; font-weight: 600; color: var(--primary); display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem;">
                    <span>🤖</span> ${t('alerts.ai.reportTitle')}
                    <span class="badge ${analysis._degraded ? 'badge-medium' : 'badge-low'}" style="margin-left: auto;">
                        ${escapeHtml(String(analysis._degraded ? t('alerts.ai.localFallback') : (analysis._route_type || t('alerts.ai.smartRouting'))))}
                    </span>
                </div>

                <div style="font-size: 1.1rem; color: var(--text-main); font-weight: 600; margin-bottom: 1.5rem; line-height: 1.5; padding-bottom: 1rem; border-bottom: 1px solid var(--border);">
                    ${escapeHtml(String(analysis.summary || t('alerts.ai.noSummary')))}
                </div>

                <div class="ai-details" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 2rem;">
        `;

        if (analysis.root_cause) {
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">🔍 ${t('alerts.ai.rootCause')}</h4>
                    <p style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; line-height: 1.6;">${escapeHtml(String(analysis.root_cause))}</p>
                </div>
            `;
        } else if (analysis.event_type) {
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">🏷️ ${t('alerts.ai.eventType')}</h4>
                    <p style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; line-height: 1.6;">${escapeHtml(String(analysis.event_type))}</p>
                </div>
            `;
        }

        if (analysis.impact || analysis.impact_scope) {
            const impact = analysis.impact || analysis.impact_scope;
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">💥 ${t('alerts.ai.impact')}</h4>
                    <p style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; line-height: 1.6;">${escapeHtml(String(impact))}</p>
                </div>
            `;
        }

        const actions = analysis.recommendations || analysis.actions;
        if (actions && actions.length > 0) {
            html += `
                <div class="detail-section" style="grid-column: 1 / -1;">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">🛠️ ${t('alerts.ai.recommendations')}</h4>
                    <ul style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; padding-left: 1.5rem; line-height: 1.6;">
                        ${actions.map(r => `<li style="margin-bottom: 0.5rem;">${escapeHtml(String(r))}</li>`).join('')}
                    </ul>
                </div>
            `;
        }

        if (analysis.risks && analysis.risks.length > 0) {
            html += `
                <div class="detail-section" style="grid-column: 1 / -1;">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">⚠️ ${t('alerts.ai.risks')}</h4>
                    <ul style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; padding-left: 1.5rem; line-height: 1.6;">
                        ${analysis.risks.map(r => `<li style="margin-bottom: 0.5rem;">${escapeHtml(String(r))}</li>`).join('')}
                    </ul>
                </div>
            `;
        }

        html += `</div>`; // Close grid

        // Metadata footer
        html += `
            <div class="ai-meta" style="margin-top: 2rem; display: flex; flex-wrap: wrap; gap: 1rem; justify-content: space-between; font-size: 0.8rem; color: var(--text-secondary); background: var(--bg-base); padding: 1rem; border-radius: 8px; border: 1px solid var(--border);">
                <span>⚡ ${t('alerts.ai.importance')}: <strong style="color: var(--text-main);">${escapeHtml(String(analysis.importance || t('alerts.ai.unknown')))}</strong></span>
        `;

        if (analysis.noise_reduction) {
            const nr = analysis.noise_reduction;
            const relationMap = { root_cause: t('alerts.ai.relation.rootCause'), derived: t('alerts.ai.relation.derived'), standalone: t('alerts.ai.relation.standalone') };
            const relation = relationMap[nr.relation] || nr.relation || t('alerts.ai.unknown');
            html += `<span>🛡️ ${t('alerts.ai.noiseReduction')}: <strong style="color: var(--text-main);">${escapeHtml(String(relation))}</strong> (${t('alerts.ai.confidence')}: ${Number(nr.confidence * 100).toFixed(1)}%)</span>`;
            if (nr.root_cause_event_id) {
                html += `<span>🔗 ${t('alerts.ai.relatedRootCause')}: <strong style="color: var(--primary);">#${nr.root_cause_event_id}</strong></span>`;
            }
        }

        html += `<span>🔀 ${t('alerts.ai.routeChannel')}: <strong style="color: var(--text-main);">${escapeHtml(String(analysis._route_type || t('alerts.ai.unknown')))}</strong></span>`;
        if (analysis._cache_hit) {
            const hitCount = analysis._cache_hit_count || 1;
            html += `<span title="${t('alerts.ai.hitCount', {n: escapeHtml(String(hitCount))})}" style="color: #10b981; font-weight: 600;">🎯 ${t('alerts.ai.cacheHit', {n: escapeHtml(String(hitCount))})}</span>`;
        }

        html += `
            </div>
        </div>
        `;

        // Render Raw JSON analysis below it for debugging
        if (typeof renderJSONBlock === 'function') {
            html += renderJSONBlock(analysis, t('alerts.ai.rawAnalysisData'));
        }

        return html;
    },

    /**
     * Update pagination info
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
     * Jump to a specific page
     */
    async goToPage(page) {
        const totalPagesFiltered = Math.ceil(this.filteredAlerts.length / this.pageSize);

        console.log('🔄 Requested jump to page', page);
        console.log('   Current filtered data:', this.filteredAlerts.length, 'items');
        console.log('   Per page:', this.pageSize, 'items');
        console.log('   Total pages:', totalPagesFiltered, 'pages');

        if (page < 1) {
            console.warn('❌ Page number less than 1, ignoring');
            return;
        }

        if (page > totalPagesFiltered) {
            if (this.hasMore) {
                await this.loadMoreAlerts();
                const updatedTotalPages = Math.ceil(this.filteredAlerts.length / this.pageSize);
                if (page > updatedTotalPages) {
                    console.warn('❌ Page number out of range (max', updatedTotalPages, 'pages), ignoring');
                    return;
                }
            } else {
                console.warn('❌ Page number out of range (max', totalPagesFiltered, 'pages), ignoring');
                return;
            }
        }

        this.currentPage = page;
        console.log('✅ Jumped to page', page);
        this.displayCurrentPage();
    },

    /**
     * Change the number of items shown per page
     */
    changePageSize() {
        this.pageSize = parseInt(document.getElementById('pageSize').value);
        this.currentPage = 1;
        this.displayCurrentPage();
    },

    _clearFiltersForFocus() {
        const searchInput = document.getElementById('searchInput');
        const importanceFilter = document.getElementById('importanceFilter');
        const sourceFilter = document.getElementById('sourceFilter');
        const duplicateFilter = document.getElementById('duplicateFilter');
        const processingStatusFilter = document.getElementById('processingStatusFilter');
        if (searchInput) searchInput.value = '';
        if (importanceFilter) importanceFilter.value = '';
        if (sourceFilter) sourceFilter.value = '';
        if (duplicateFilter) duplicateFilter.value = '';
        if (processingStatusFilter) processingStatusFilter.value = '';
    },

    _revealAlertItem(id) {
        const alertItem = document.querySelector('.alert-item[data-id="' + id + '"]');
        if (!alertItem) return false;
        alertItem.scrollIntoView({ behavior: 'smooth', block: 'center' });
        if (!alertItem.classList.contains('expanded')) {
            alertItem.classList.add('expanded');
        }
        alertItem.classList.add('alert-focus');
        setTimeout(function() {
            alertItem.classList.remove('alert-focus');
        }, 1800);

        const webhook = this.alerts.find(w => w.id == id);
        if (webhook && !webhook.parsed_data && !webhook.ai_analysis) {
            this.loadFullAlertData(id, alertItem);
        }
        return true;
    },

    async focusAlertById(id) {
        if (!id) return false;
        if (this._revealAlertItem(id)) return true;

        let index = this.filteredAlerts.findIndex(w => w.id == id);
        if (index === -1) {
            this._clearFiltersForFocus();
            this.filteredAlerts = this.alerts.slice();
            index = this.filteredAlerts.findIndex(w => w.id == id);
        }

        if (index === -1) {
            try {
                const result = await API.getWebhook(id);
                if (result.success && result.data) {
                    this.alerts = [result.data].concat(this.alerts.filter(w => w.id != id));
                    this._clearFiltersForFocus();
                    this.filteredAlerts = this.alerts.slice();
                    index = 0;
                    this.updateStats();
                }
            } catch (error) {
                console.error('Failed to locate alert:', error);
                showError(t('alerts.error.locateFailed') + ': ' + error.message);
                return false;
            }
        }

        if (index === -1) {
            showError(t('alerts.error.notFound', {id: id}));
            return false;
        }

        this.currentPage = Math.floor(index / this.pageSize) + 1;
        this.displayCurrentPage();
        setTimeout(() => this._revealAlertItem(id), 50);
        return true;
    },

    /**
     * Load the full data for a single alert
     */
    async loadFullAlertData(webhookId, alertItem) {
        console.log('🔄 Loading full data:', webhookId);

        // Show loading state
        const dataTab = alertItem.querySelector('[data-tab-content="data"]');
        const aiTab = alertItem.querySelector('[data-tab-content="ai"]');

        if (dataTab) {
            dataTab.innerHTML = '<div style="padding: 2rem; text-align: center;"><div class="spinner"></div><p>' + t('alerts.loadingFullData') + '</p></div>';
        }

        try {
            const result = await API.getWebhook(webhookId);

            if (result.success && result.data) {
                const fullData = result.data;

                // Update the data in alerts (merge)
                const index = this.alerts.findIndex(w => w.id == webhookId);
                if (index !== -1) {
                    this.alerts[index] = { ...this.alerts[index], ...fullData };
                }

                // Update the overview tab
                const overviewTab = alertItem.querySelector('[data-tab-content="overview"]');
                if (overviewTab && index !== -1) {
                    overviewTab.innerHTML = this.renderOverview(this.alerts[index]);
                }

                // Update the raw data tab
                if (dataTab) {
                    if (fullData.parsed_data) {
                        dataTab.innerHTML = renderJSONBlock(fullData.parsed_data, t('alerts.tab.rawData'));
                    } else if (fullData.raw_payload) {
                        // parsed_data is null (zero-parse mode), use the decompressed raw_payload
                        let rawData;
                        try {
                            rawData = JSON.parse(fullData.raw_payload);
                        } catch (e) {
                            rawData = fullData.raw_payload;
                        }
                        dataTab.innerHTML = renderJSONBlock(rawData, t('alerts.tab.rawData'));
                    } else {
                        dataTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: #94a3b8;">' + t('alerts.noData') + '</div>';
                    }
                }

                // Append request headers display
                if (dataTab && fullData.headers && Object.keys(fullData.headers).length > 0) {
                    var filteredHeaders = {};
                    Object.keys(fullData.headers).forEach(function(k) {
                        if (!k.startsWith('x-forwarded') && k !== 'traceparent') filteredHeaders[k] = fullData.headers[k];
                    });
                    if (Object.keys(filteredHeaders).length > 0) {
                        dataTab.innerHTML += '<div style="margin-top:1rem;">' + renderJSONBlock(filteredHeaders, t('alerts.requestHeaders')) + '</div>';
                    }
                }

                // Update the AI analysis tab
                if (aiTab && fullData.ai_analysis) {
                    aiTab.innerHTML = this.renderAIAnalysis(fullData.ai_analysis);
                } else if (aiTab) {
                    aiTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: #94a3b8;">' + t('alerts.ai.noData') + '</div>';
                }

                console.log('✅ Full data loaded successfully');
            } else {
                throw new Error(result.error || t('alerts.error.loadFailed'));
            }
        } catch (error) {
            console.error('❌ Failed to load full data:', error);
            if (dataTab) {
                dataTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: #ef4444;">❌ ' + t('alerts.error.loadFailed') + ': ' + escapeHtml(String(error.message || error)) + '</div>';
            }
        }
    },

    /**
     * Replay a dead letter
     */
    async replayDeadLetter(id) {
        console.log('Starting replay of dead letter:', id);

        if (!confirm(t('alerts.confirm.replayDeadLetter'))) {
            return;
        }

        try {
            const result = await API.replayDeadLetter(id);
            console.log('Replay result:', result);
            if (result.success) {
                showToast(t('alerts.success.replayStarted'));
                setTimeout(() => this.loadAlerts(), 1500);
            } else {
                throw new Error(result.error || t('common.loadFailed'));
            }
        } catch (error) {
            console.error('Replay failed:', error);
            showError(t('alerts.error.replayFailed') + ': ' + error.message);
        }
    },

    /**
     * Quick-silence: open the silence form pre-filled with this alert's context,
     * with the duration set to 2 hours.
     */
    quickSilence(id) {
        var alert = this.alerts.find(function (w) { return w.id == id; });
        if (!alert) return;
        // Extract match fields from parsed_data — same extraction the decisioning
        // engine uses (extract_forward_match_fields). We approximate by reading
        // the parsed_data fields the silence form maps to.
        var pd = alert.parsed_data || {};
        if (typeof showQuickSilenceForm === 'function') {
            showQuickSilenceForm(
                alert.source || '',
                pd.Project || pd.project || '',
                pd.Region || pd.region || '',
                pd.environment || pd.env || '',
                pd.RuleName || pd.rule_name || ''
            );
        }
    },

    /**
     * What-if dry-run: replay current rules/silences against this alert.
     */
    async replayDryRun(id) {
        try {
            var resp = await API.authenticatedFetch('/v1/webhooks/' + id + '/replay-dry-run', { method: 'POST' });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var result = await resp.json();
            var d = result.data || {};
            var lines = [
                d.should_forward ? '✅ Would FORWARD' : '⏭️ Would SKIP',
                d.skip_reason ? 'Reason: ' + d.skip_reason : '',
                'Rules matched: ' + (d.matched_rule_count || 0) + ' of ' + (d.rules_evaluated || 0),
                d.matched_rules && d.matched_rules.length ? 'Matching: ' + d.matched_rules.join(', ') : ''
            ].filter(Boolean);
            alert(lines.join('\n'));
        } catch (e) {
            alert(t('common.requestFailed') + ': ' + (e && e.message || e));
        }
    },

    /**
     * Reanalyze an alert
     */
    async reanalyzeAlert(id) {
        console.log('Starting reanalysis of webhook:', id);

        if (!confirm(t('alerts.confirm.reanalyze'))) {
            return;
        }

        try {
            const result = await API.reanalyze(id);

            console.log('Reanalysis result:', result);

            if (result.success) {
                alert('✅ ' + t('alerts.msg.reanalyzeSuccess'));
                this.loadAlerts();
            } else {
                alert('❌ ' + t('alerts.msg.analysisFailed') + ': ' + (result.error || t('alerts.msg.unknownError')));
            }
        } catch (error) {
            console.error('Reanalysis error:', error);
            alert('❌ ' + t('alerts.msg.requestFailed') + ': ' + error.message);
        }
    },

    /**
     * Open the forward modal
     */
    openForwardModal(id) {
        console.log('Opening forward modal, webhook ID:', id);
        this.currentForwardId = id;

        const forwardUrlInput = document.getElementById('forwardUrl');
        if (forwardUrlInput) {
            forwardUrlInput.value = '';
        }

        const modal = document.getElementById('forwardModal');
        if (modal) {
            modal.classList.add('active');
            console.log('Forward modal opened');
        } else {
            console.error('Forward modal element not found');
        }
    },

    /**
     * Close the forward modal
     */
    closeForwardModal() {
        document.getElementById('forwardModal').classList.remove('active');
        this.currentForwardId = null;
    },

    /**
     * Confirm forward
     */
    async confirmForward() {
        const url = document.getElementById('forwardUrl').value;
        if (!url) return alert(t('alerts.msg.enterForwardUrl'));

        try {
            const result = await API.forward(this.currentForwardId, url);

            if (result.success) {
                alert('✅ ' + t('alerts.msg.forwardSuccess'));
                this.closeForwardModal();
            } else {
                alert('❌ ' + t('alerts.msg.forwardFailed') + ': ' + (result.error || t('alerts.msg.unknownError')));
            }
        } catch (error) {
            alert('❌ ' + t('alerts.msg.requestFailed') + ': ' + error.message);
        }
    },

    /**
     * Load the decision trace (why forwarded/skipped) + delivery status for an
     * alert, reusing the Decision Trace tab's renderer. Lazy-loaded on tab open.
     */
    async loadDecisionTrace(webhookId) {
        const container = document.getElementById('decision-container-' + webhookId);
        if (!container) return;
        if (container.dataset.loaded === 'true') return;  // already shown; don't reflow
        container.innerHTML = '<div style="padding: 2rem; text-align: center;"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';

        try {
            const result = await API.getDecisionTraceByEvent(webhookId);
            if (!result || !result.success || !result.data) {
                container.innerHTML = '<div style="text-align:center; padding:30px; color:#888;">' + t('alerts.decision.none') + '</div>';
                return;
            }
            if (typeof DecisionTraceModule !== 'undefined' && DecisionTraceModule.renderDetails) {
                container.innerHTML = '<div class="da-card da-card-expanded" style="margin:0;">' + DecisionTraceModule.renderDetails(result.data) + '</div>';
            } else {
                container.innerHTML = '<div style="padding:1rem; color:#888;">' + t('alerts.decision.none') + '</div>';
            }
            container.dataset.loaded = 'true';
        } catch (e) {
            container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--danger);">' + t('common.loadFailed') + ': ' + escapeHtml(String(e && e.message || e)) + '</div>';
        }
    },

    /**
     * Load the incident timeline for a webhook event.
     */
    async loadTimeline(webhookId) {
        const container = document.getElementById('timeline-container-' + webhookId);
        if (!container) return;
        if (container.dataset.loaded === 'true') return;
        container.innerHTML = '<div style="padding: 2rem; text-align: center;"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';

        try {
            const result = await this._fetchTimeline(webhookId);
            if (!result || !result.data || !result.data.events || !result.data.events.length) {
                container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">📅 ' + t('alerts.timeline.empty') + '</div>';
                return;
            }
            container.innerHTML = this._renderTimeline(result.data);
            container.dataset.loaded = 'true';
        } catch (e) {
            container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--danger);">' + t('common.loadFailed') + ': ' + escapeHtml(String(e && e.message || e)) + '</div>';
        }
    },

    async _fetchTimeline(eventId) {
        const response = await API.authenticatedFetch('/v1/webhooks/' + eventId + '/timeline');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    _renderTimeline(data) {
        const anchorId = data.anchor ? data.anchor.id : null;
        const impEmoji = { high: '🔴', medium: '🟠', low: '🟢' };
        // Build an index of event IDs for fast relationship lookups.
        var idIndex = {};
        data.events.forEach(function (ev) { idIndex[ev.id] = ev; });

        // Determine which events are causal parents of others.
        var causedBy = {};  // causedBy[childId] = parentId
        data.events.forEach(function (ev) {
            if (ev.duplicate_of && idIndex[ev.duplicate_of]) causedBy[ev.id] = ev.duplicate_of;
            if (ev.prev_alert_id && idIndex[ev.prev_alert_id] && !causedBy[ev.id]) causedBy[ev.id] = ev.prev_alert_id;
            if (ev.noise_root_cause_id && idIndex[ev.noise_root_cause_id] && !causedBy[ev.id]) causedBy[ev.id] = ev.noise_root_cause_id;
        });

        var html = '<div style="padding: 0.5rem 0;">';
        for (var i = 0; i < data.events.length; i++) {
            var ev = data.events[i];
            var isAnchor = ev.id === anchorId;
            var isCaused = causedBy[ev.id] !== undefined;
            var borderColor = isAnchor ? 'var(--primary, #6366f1)' : (isCaused ? 'var(--warning, #f59e0b)' : 'var(--border, #334155)');
            var bg = isAnchor ? 'var(--primary-bg, rgba(99,102,241,0.08))' : 'transparent';

            // Causal connector: if this event was caused by a previous one in the
            // timeline, show a small arrow link.
            var causalParent = causedBy[ev.id];
            var connectorHtml = '';
            if (causalParent) {
                var parentEv = idIndex[causalParent];
                connectorHtml = '<div style="font-size:0.65rem; color:var(--warning); margin-bottom:0.15rem; padding-left:0.25rem;">';
                connectorHtml += '↳ ' + t('alerts.timeline.causedBy', { id: causalParent });
                if (parentEv && parentEv.summary) {
                    connectorHtml += ' — <span style="opacity:0.7;">' + escapeHtml(parentEv.summary.slice(0, 60)) + '</span>';
                }
                connectorHtml += '</div>';
            }

            html += '<div style="display:flex; align-items:flex-start; gap:0.75rem; padding:0.625rem 0.5rem; margin-bottom:0.25rem; border-left:3px solid ' + borderColor + '; background:' + bg + '; border-radius:0 4px 4px 0;">';
            // Time
            html += '<div style="font-size:0.75rem; color:var(--text-muted); min-width:4.5rem; text-align:right; padding-top:0.15rem;">' + escapeHtml(ev.timestamp ? ev.timestamp.slice(11, 19) : '') + '</div>';
            // Content
            html += '<div style="flex:1; min-width:0;">';
            if (connectorHtml) html += connectorHtml;
            html += '<div style="font-size:0.8rem; font-weight:600; margin-bottom:0.15rem;">';
            if (isAnchor) html += '📍 ';
            html += '<a href="javascript:void(0)" onclick="AlertsModule._scrollToAlert(' + ev.id + ')" style="color:var(--text-main); text-decoration:none;">#' + ev.id + '</a>';
            html += ' <span style="color:var(--text-muted); font-weight:400;">' + escapeHtml(ev.source) + '</span>';
            html += ' <span>' + (impEmoji[ev.importance] || '⚪') + ' ' + escapeHtml(ev.importance) + '</span>';
            if (ev.is_duplicate) html += ' <span class="badge badge-outline" style="font-size:0.6rem;">' + t('alerts.status.duplicate') + '</span>';
            if (ev.forward_status === 'sent') html += ' <span class="badge badge-success" style="font-size:0.6rem;">📤</span>';
            if (isCaused) html += ' <span style="font-size:0.6rem; color:var(--warning);" title="' + t('alerts.timeline.derivedTitle') + '">↳ derived</span>';
            html += '</div>';
            if (ev.summary) {
                html += '<div style="font-size:0.82rem; color:var(--text-muted); line-height:1.4; white-space:pre-wrap;">' + escapeHtml(ev.summary) + '</div>';
            }
            html += '</div></div>';
        }
        html += '</div>';
        if (data.events.length >= 50) {
            html += '<div style="text-align:center; padding:0.5rem; font-size:0.78rem; color:var(--text-muted);">' + t('alerts.timeline.truncated', { n: 50 }) + '</div>';
        }
        return html;
    },

    /** Scroll to and expand the alert item with the given id. */
    _scrollToAlert(eventId) {
        var item = document.querySelector('.alert-item[data-id="' + eventId + '"]');
        if (!item) return;
        item.scrollIntoView({ behavior: 'smooth', block: 'center' });
        if (!item.classList.contains('expanded')) {
            item.classList.add('expanded');
            // Trigger data load if needed
            var header = item.querySelector('.alert-header');
            if (header) header.click();
        }
        item.style.boxShadow = '0 0 0 3px var(--primary, #6366f1)';
        setTimeout(function () { item.style.boxShadow = ''; }, 2000);
    },

    /**
     * Load deep analysis history records
     */
    async loadDeepAnalyses(webhookId) {
        const container = document.getElementById('deep-analysis-container-' + webhookId);
        if (!container) return;

        container.innerHTML = '<div style="padding: 2rem; text-align: center;"><div class="spinner"></div><p>' + t('alerts.deep.loadingHistory') + '</p></div>';

        try {
            const result = await API.getDeepAnalyses(webhookId);
            const records = result.data || [];

            if (records.length === 0) {
                container.innerHTML = '<div style="text-align:center; padding:30px; color:#888;">' +
                    '<p>' + t('alerts.deep.noRecords') + '</p>' +
                    '<button class="btn btn-primary" onclick="window.alertsModule.deepAnalyzeAlert(' + webhookId + ')">\ud83d\udd2c ' + t('alerts.deep.analyzeNow') + '</button>' +
                    '</div>';
                return;
            }

            let html = '';
            records.forEach(function(record) {
                const analysis = record.analysis_result || {};
                const engineLabel = record.engine === 'openclaw' ? '🦞 OpenClaw' : '\ud83e\udd16 ' + t('deep.engine.local');
                const time = new Date(record.created_at).toLocaleString('zh-CN');
                const duration = record.duration_seconds ? record.duration_seconds.toFixed(1) + 's' : '-';

                html += '<div style="border:1px solid #e0e0e0; border-radius:8px; padding:16px; margin-bottom:12px; background:#fafafa;">';

                // Header: engine, time, duration
                html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid #eee;">';
                html += '<span style="font-weight:600;">' + engineLabel + '</span>';
                html += '<span style="color:#888; font-size:0.85em;">' + time + ' | ' + t('alerts.deep.duration') + ' ' + duration + '</span>';
                html += '</div>';

                // User question (if any)
                if (record.user_question) {
                    html += '<div style="margin-bottom:10px; padding:8px 12px; background:#e8f4fd; border-radius:4px; font-size:0.9em;">';
                    html += '<strong>' + t('alerts.deep.userQuestion') + ': </strong>' + escapeHtml(String(record.user_question));
                    html += '</div>';
                }

                // Check whether the status is pending (OpenClaw asynchronously waiting for results)
                if (record.status === 'pending') {
                    // Analyzing-state card
                    html += '<div style="text-align:center; padding:20px; background:var(--info-bg); border:1px solid #bae6fd; border-radius:8px; color:var(--info);">';
                    html += '<div style="font-size:2em; margin-bottom:12px;">⏳</div>';
                    html += '<div style="font-size:1.1em; font-weight:600; margin-bottom:8px;">' + t('alerts.deep.openclawAnalyzing') + '</div>';
                    if (record.openclaw_run_id) {
                        html += '<div style="font-size:0.8em; opacity:0.7; margin-bottom:12px;">' + t('alerts.deep.runId') + ': ' + escapeHtml(String(record.openclaw_run_id)) + '</div>';
                    }
                    html += '<div style="font-size:0.9em; opacity:0.85;">' + t('alerts.deep.willUpdate') + '</div>';
                    html += '</div>';
                } else {
                    // Normal analysis result rendering
                    // If there is full OpenClaw text, render the markdown first
                    if (analysis._openclaw_text) {
                        html += '<pre style="white-space:pre-wrap; font-size:0.85em;">' + escapeHtml(String(analysis._openclaw_text)) + '</pre>';
                        // If there is a confidence score, display it separately
                        if (analysis.confidence !== undefined) {
                            const pct = (analysis.confidence * 100).toFixed(0);
                            html += '<div style="margin-top:8px; color:#888; font-size:0.85em;">' + t('alerts.deep.confidence') + ': ' + pct + '%</div>';
                        }
                    } else {
                        // Original JSON field rendering logic
                        if (analysis.root_cause) {
                            html += '<div style="margin-bottom:8px;"><strong>\ud83d\udd0d ' + t('alerts.deep.rootCause') + ': </strong><p style="margin:4px 0; white-space:pre-wrap;">' + escapeHtml(String(analysis.root_cause)) + '</p></div>';
                        }
                        if (analysis.impact) {
                            html += '<div style="margin-bottom:8px;"><strong>\ud83d\udca5 ' + t('alerts.deep.impactScope') + ': </strong><p style="margin:4px 0; white-space:pre-wrap;">' + escapeHtml(String(analysis.impact)) + '</p></div>';
                        }
                        if (analysis.recommendations && Array.isArray(analysis.recommendations)) {
                            html += '<div style="margin-bottom:8px;"><strong>\u2705 ' + t('alerts.deep.recommendations') + ': </strong><ul style="margin:4px 0; padding-left:20px;">';
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
                            html += '<div style="margin-top:8px; color:#888; font-size:0.85em;">' + t('alerts.deep.confidence') + ': ' + pct + '%</div>';
                        }

                        // If there are no structured fields, display the raw JSON directly
                        if (!analysis.root_cause && !analysis.impact && !analysis.recommendations) {
                            html += '<pre style="background:#f5f5f5; padding:12px; border-radius:4px; overflow-x:auto; font-size:0.85em; max-height:300px;">' + escapeHtml(JSON.stringify(analysis, null, 2)) + '</pre>';
                        }
                    }
                }

                html += '</div>';
            });

            // Footer: re-analyze button
            html += '<div style="text-align:center; margin-top:12px;">';
            html += '<button class="btn btn-sm" onclick="window.alertsModule.deepAnalyzeAlert(' + webhookId + ')">\ud83d\udd2c ' + t('alerts.deep.analyzeAgain') + '</button>';
            html += '</div>';

            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = '<div style="color:red; padding:20px;">' + t('alerts.error.loadFailed') + ': ' + escapeHtml(String(e.message || e)) + '</div>';
        }
    },

    /**
     * Deep-analyze an alert
     */
    async deepAnalyzeAlert(id) {
        const question = prompt(t('alerts.deep.questionPrompt'), '');
        if (question === null) return;  // User cancelled

        try {
            const result = await API.deepAnalyze(id, question, 'openclaw');
            if (result.success && result.data) {
                const record = result.data;
                const analysisResult = record.analysis_result || {};

                // Check whether the status is pending (OpenClaw asynchronously waiting for results)
                if (record.status === 'pending' || analysisResult._pending) {
                    this.showTriggeredNotification(analysisResult._openclaw_run_id || record.openclaw_run_id);
                }

                // Analysis complete, switch to the deep analysis tab and refresh data
                const alertItem = document.querySelector('.alert-item[data-id="' + id + '"]');
                if (alertItem) {
                    // Ensure details are expanded
                    if (!alertItem.classList.contains('expanded')) {
                        alertItem.classList.add('expanded');
                    }

                    // Switch to the deep analysis tab
                    const tabs = alertItem.querySelectorAll('.tab');
                    const contents = alertItem.querySelectorAll('.tab-content');
                    tabs.forEach(function(t) { t.classList.remove('active'); });
                    contents.forEach(function(c) { c.classList.remove('active'); });

                    const deepTab = alertItem.querySelector('[data-tab="deep-analysis"]');
                    const deepContent = alertItem.querySelector('[data-tab-content="deep-analysis"]');
                    if (deepTab) deepTab.classList.add('active');
                    if (deepContent) deepContent.classList.add('active');

                    // Load deep analysis history records
                    this.loadDeepAnalyses(id);
                } else {
                    // If the alert item is not on the current page, show a simple notice
                    alert('✅ ' + t('alerts.deep.completeNotice'));
                }
            } else {
                alert(t('alerts.msg.analysisFailed') + ': ' + (result.error || t('alerts.msg.unknownError')));
            }
        } catch (error) {
            alert(t('alerts.msg.requestFailed') + ': ' + error.message);
        }
    },

    /**
     * Show a friendly notification that OpenClaw analysis has been triggered
     */
    showTriggeredNotification(runId) {
        // Create the overlay notification
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
                <strong style="font-size:1.1em;">${t('alerts.deep.triggeredTitle')}</strong>
            </div>
            <div style="font-size:0.9em; color:rgba(255,255,255,0.9); margin-bottom:8px;">
                ${t('alerts.deep.triggeredDesc')}
            </div>
            ${runId ? `<div style="font-size:0.8em; color:rgba(255,255,255,0.7);">${t('alerts.deep.runId')}: ${escapeHtml(String(runId))}</div>` : ''}
        `;

        // Add animation styles
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

        // Auto-dismiss after 4 seconds
        setTimeout(() => {
            notification.style.animation = 'slideOut 0.3s ease-in forwards';
            setTimeout(() => notification.remove(), 300);
        }, 4000);
    }
};
