// The prompt fingerprints as they stand right now, fetched once per page.
// An analysis records the fingerprint it was produced under; that hex is the
// same string on every analysis until somebody edits a prompt, so on its own it
// is noise. Compared against these it answers the only question worth asking of
// it — is this report still explained by what the prompt says today?
let _promptVersionsNow = null;
async function ensurePromptVersions() {
    if (_promptVersionsNow !== null) return _promptVersionsNow;
    try {
        const resp = await API.authenticatedFetch('/v1/prompt/versions');
        const data = await resp.json();
        _promptVersionsNow = (data && data.versions) || {};
    } catch (e) {
        _promptVersionsNow = {};  // no basis for comparison; show the kind only
    }
    return _promptVersionsNow;
}

/**
 * Alert List Module
 * Handles loading, filtering, pagination, display, and interaction of alerts
 */

/**
 * A clickable reference to another alert.
 *
 * These identifiers were rendered as inert text everywhere, so a reader who
 * spotted "the original is #123" had to memorise the number and go hunting.
 * AlertsModule.focusAlertById already handled the hard part (clear filters,
 * paginate, fetch if absent) and was called from nowhere.
 */
function _alertLink(id, label) {
    if (!id) return escapeHtml(String(label == null ? '' : label));
    return '<a href="#/alerts/' + encodeURIComponent(id) + '"'
        + ' data-prevent="1" data-act="openAlert" data-args="' + Number(id) + '"'
        + ' style="color: var(--primary); text-decoration: none;">'
        + escapeHtml(String(label == null ? '#' + id : label)) + '</a>';
}

const AlertsModule = {
    currentPage: 1,
    pageSize: 20,
    alerts: [],
    filteredAlerts: [],
    totalCount: 0,
    nextCursor: null,
    hasMore: false,
    _loadingMore: false,
    _pendingActions: new Set(),
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
     * Initialize the alert module.
     *
     * Bind-only: the Inbox tab is hidden on the Overview landing page, so the
     * alert list is loaded lazily on first Inbox open (setInboxView -> loadAlerts),
     * mirroring the other tab modules and saving a fetch + render per page load.
     */
    init() {
        this.bindEvents();
    },

    /**
     * Bind event handlers
     */
    bindEvents() {
        this._bindBulkEvents();
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
            const handlers = {
                'reanalyze': () => this.reanalyzeAlert(id),
                'deep-analyze': () => this.deepAnalyzeAlert(id),
                'forward': () => this.openForwardModal(id),
                'replay-dl': () => this.replayDeadLetter(id),
                'quick-silence': () => this.quickSilence(id),
                'replay-dry': () => this.replayDryRun(id),
                'acknowledge': () => this.updateWorkflow(id, { workflow_status: 'acknowledged' }),
                // Resolve is terminal and closes the alert out of the queue, so it
                // asks first. Acknowledge stays one click: it is cheap to take back
                // and asking on every claim would train people to click through.
                'resolve': async () => {
                    if (!(await wwConfirm(t('alerts.workflow.confirmResolve')))) return;
                    return this.updateWorkflow(id, { workflow_status: 'resolved' });
                },
                'assign': () => this.assignWorkflow(id),
                'notes': () => this.manageNotes(id),
                'override-importance': () => this.overrideImportance(id),
                'cycle-workflow': () => this.chooseWorkflow(id)
            };
            const handler = handlers[action];
            if (handler) {
                const menu = btn.closest('.alert-action-menu');
                if (menu) menu.removeAttribute('open');
                void this._runButtonAction(btn, action + ':' + id, handler);
            }
            return;
        }

        // The toolbar is interactive but must not expand/collapse the card.
        if (e.target.closest('.alert-toolbar')) return;

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

    async _runButtonAction(button, key, handler) {
        if (this._pendingActions.has(key)) return;
        this._pendingActions.add(key);
        if (button) {
            button.disabled = true;
            button.classList.add('is-busy');
            button.setAttribute('aria-busy', 'true');
        }
        try {
            await Promise.resolve(handler());
        } catch (error) {
            console.error('Alert action failed:', key, error);
            showError(t('alerts.msg.requestFailed') + ': ' + (error && error.message || error));
        } finally {
            this._pendingActions.delete(key);
            if (button) {
                button.disabled = false;
                button.classList.remove('is-busy');
                button.removeAttribute('aria-busy');
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

            // A shared #/alerts?… link restores its filters before anything
            // is fetched; absent keys leave inputs alone so pre-filled drills
            // (gap cards, dead-letter arrivals) survive.
            this._applyFiltersFromHash();
            // The search BOX is the source of truth, not the debounce state:
            // a drill from another view lands here with the box pre-filled,
            // and a programmatic fill never fires the input event.
            this._searchTerm = ((document.getElementById('searchInput') || {}).value || '').trim();
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

            // The load path is the choke point search/window changes pass
            // through; selects sync in filterAlerts.
            this._syncFiltersToHash();

            this.updateStats();
            this.currentPage = 1;
            this.filterAlerts(true);

            document.getElementById('lastUpdate').textContent = formatTime(new Date());
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
            showToast(t('alerts.error.loadMoreFailed') + ': ' + error.message, 'error');
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
    // Filters ↔ URL: keys q/imp/src/dup/ps/win under #/alerts?…, so a filtered
    // investigation is a shareable, refresh-proof address. Reading applies
    // ONLY the keys present (pre-filled drills must survive a clean hash).
    _filterInputs() {
        return {
            q: document.getElementById('searchInput'),
            imp: document.getElementById('importanceFilter'),
            src: document.getElementById('sourceFilter'),
            dup: document.getElementById('duplicateFilter'),
            ps: document.getElementById('processingStatusFilter'),
            win: document.getElementById('timeWindowFilter')
        };
    },

    _applyFiltersFromHash() {
        if (typeof hashFilters !== 'function') return;
        const params = hashFilters();
        const inputs = this._filterInputs();
        Object.keys(inputs).forEach((key) => {
            if (inputs[key] && Object.prototype.hasOwnProperty.call(params, key)) {
                inputs[key].value = params[key];
            }
        });
    },

    _syncFiltersToHash() {
        if (typeof writeHashFilters !== 'function') return;
        const inputs = this._filterInputs();
        const out = {};
        Object.keys(inputs).forEach((key) => {
            const value = inputs[key] ? String(inputs[key].value || '') : '';
            // "all" is the time window's empty value; drop defaults from the URL.
            if (value && value !== 'all') out[key] = value;
        });
        writeHashFilters('alerts', out);
    },

    filterAlerts(resetPage = true) {
        this._syncFiltersToHash();
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
            console.warn('Current page number out of range, resetting to last page');
            this.currentPage = totalPagesFiltered;
        }

        // Calculate the data range for the current page
        const startIndex = (this.currentPage - 1) * this.pageSize;
        const endIndex = Math.min(startIndex + this.pageSize, totalFiltered);
        const currentPageData = this.filteredAlerts.slice(startIndex, endIndex);


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
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('inbox') + '</div><div class="empty-title">' + t('alerts.empty.title') + '</div><div class="empty-text">' + t('alerts.empty.text') + '</div></div>';
            return;
        }

        let html = '';
        webhooks.forEach((webhook) => {
            const importance = webhook.importance || 'low';
            const duplicateType = webhook.duplicate_type || 'new';
            const isDuplicate = duplicateType !== 'new' && !!webhook.is_duplicate;
            const analysis = webhook.ai_analysis || {};
            const summary = webhook.summary || analysis.summary || '';
            const summaryText = String(summary || '').trim();
            const webhookId = escapeHtml(String(webhook.id));

            html += '<div class="alert-item" data-id="' + webhookId + '">';
            html += '<div class="alert-header">';
            html += '<div class="alert-card-top">';
            html += '<div class="alert-left">';
            html += '<div class="alert-title-row">';
            html += '<input type="checkbox" class="alert-bulk-check" data-bulk-id="' + webhookId + '"' +
                (this._bulkSelected.has(String(webhookId)) ? ' checked' : '') +
                ' aria-label="' + escapeHtml(t('alerts.bulk.checkAria')) + '">';
            html += '<span class="alert-icon">' + getAlertIcon(importance) + '</span>';
            html += '<span class="alert-title' + (summaryText ? '' : ' is-muted') + '">' + escapeHtml(summaryText || t('alerts.summaryUnavailable', {id: webhook.id})) + '</span>';
            html += '</div>';
            html += '<div class="alert-meta">';
            html += '<span class="alert-meta-item">#' + webhookId + '</span>';
            html += '<span class="alert-meta-item">' + wwIcon('target') + ' ' + escapeHtml(String(webhook.source || 'unknown')) + '</span>';

            // Always show the client IP
            if (webhook.client_ip) {
                html += '<span class="alert-meta-item">' + wwIcon('globe') + ' ' + escapeHtml(String(webhook.client_ip)) + '</span>';
            }

            html += '<span class="alert-meta-item">' + wwIcon('clock') + ' ' + formatTime(webhook.timestamp) + '</span>';

            // Show duplicate information
            if (isDuplicate) {
                html += '<span class="alert-meta-item">' + wwIcon('link') + ' ' + _alertLink(webhook.duplicate_of, t('alerts.meta.original', {id: webhook.duplicate_of})) + '</span>';
                // Show previous alert ID and time
                if (webhook.prev_alert_id) {
                    let prevText = wwIcon('chevron-left') + ' ' + _alertLink(webhook.prev_alert_id, t('alerts.meta.previous', {id: webhook.prev_alert_id}));
                    if (webhook.prev_alert_timestamp) {
                        prevText += ' (' + timeAgo(webhook.prev_alert_timestamp) + ')';
                    }
                    html += '<span class="alert-meta-item">' + prevText + '</span>';
                }
            }
            html += '</div></div>';
            html += '<div class="alert-status">';
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
                html += '<span class="badge ' + fwdClass + '" title="' + t('alerts.fwd.statusTitle') + '">' + wwIcon('send') + ' ' + escapeHtml(fwdLabels[webhook.forward_status] || webhook.forward_status) + '</span>';
            }
            var workflowStatus = webhook.workflow_status || 'open';
            var workflowClass = workflowStatus === 'resolved' || workflowStatus === 'ignored' ? 'badge-low' : (workflowStatus === 'open' ? 'badge-high' : 'badge-medium');
            // Clickable, not decorative. Acknowledge and Resolve used to be
            // buried in the secondary actions and only reversible through a
            // toast that vanished — so the status you were looking at was the
            // one thing on the card you could not change by touching it.
            html += '<button type="button" class="badge badge-action ' + workflowClass +
                '" data-action="cycle-workflow" data-id="' + webhookId + '" title="' +
                escapeHtml(t('alerts.workflow.clickToChange')) + '">' +
                escapeHtml(t('alerts.workflow.' + workflowStatus)) + '</button>';
            html += '<span class="alert-time">' + timeAgo(webhook.timestamp) + '</span>';
            html += '</div></div>';

            html += '<div class="alert-toolbar">';
            html += '<div class="alert-primary-actions">';
            if (workflowStatus === 'open') {
                html += '<button type="button" class="btn btn-sm" data-action="acknowledge" data-id="' + webhookId + '">' + wwIcon('check') + ' ' + escapeHtml(t('alerts.action.acknowledge')) + '</button>';
            }
            if (workflowStatus !== 'resolved' && workflowStatus !== 'ignored') {
                html += '<button type="button" class="btn btn-sm btn-quiet-primary" data-action="resolve" data-id="' + webhookId + '">' + wwIcon('check') + ' ' + escapeHtml(t('alerts.action.resolve')) + '</button>';
            }
            html += '</div>';

            var secondaryActions = [];
            secondaryActions.push('<button type="button" class="btn btn-sm" data-action="reanalyze" data-id="' + webhookId + '">' + wwIcon('refresh') + ' ' + escapeHtml(t('alerts.action.reanalyze')) + '</button>');
            secondaryActions.push('<button type="button" class="btn btn-sm" data-action="deep-analyze" data-id="' + webhookId + '">' + wwIcon('flask') + ' ' + escapeHtml(t('alerts.action.deepAnalyze')) + '</button>');
            secondaryActions.push('<button type="button" class="btn btn-sm" data-action="forward" data-id="' + webhookId + '">' + wwIcon('send') + ' ' + escapeHtml(t('alerts.action.forward')) + '</button>');
            secondaryActions.push('<button type="button" class="btn btn-sm btn-warn" data-action="quick-silence" data-id="' + webhookId + '" title="' + escapeHtml(t('alerts.action.quickSilenceTitle')) + '">' + wwIcon('volume-x') + ' ' + escapeHtml(t('alerts.action.quickSilence')) + '</button>');
            secondaryActions.push('<button type="button" class="btn btn-sm" data-action="replay-dry" data-id="' + webhookId + '" title="' + escapeHtml(t('alerts.action.replayDryTitle')) + '">' + wwIcon('refresh') + ' ' + escapeHtml(t('alerts.action.replayDry')) + '</button>');
            secondaryActions.push('<button type="button" class="btn btn-sm" data-action="assign" data-id="' + webhookId + '">' + wwIcon('user') + ' ' + escapeHtml(t('alerts.action.assign')) + '</button>');
            secondaryActions.push('<button type="button" class="btn btn-sm" data-action="notes" data-id="' + webhookId + '">' + wwIcon('pencil') + ' ' + escapeHtml(t('alerts.action.notes')) + '</button>');
            secondaryActions.push('<button type="button" class="btn btn-sm" data-action="override-importance" data-id="' + webhookId + '">' + wwIcon('sliders') + ' ' + escapeHtml(t('alerts.action.overrideImportance')) + '</button>');
            if (webhook.processing_status === 'dead_letter') {
                secondaryActions.push('<button type="button" class="btn btn-sm btn-danger" data-action="replay-dl" data-id="' + webhookId + '">' + wwIcon('refresh') + ' ' + escapeHtml(t('alerts.action.replayDeadLetter')) + '</button>');
            }
            html += '<details class="alert-action-menu">';
            html += '<summary class="btn btn-sm alert-more-trigger">••• ' + escapeHtml(t('alerts.action.more')) + '</summary>';
            html += '<div class="alert-secondary-actions">' + secondaryActions.join('') + '</div>';
            html += '</details></div></div>';

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
            html += '<div class="tab" data-tab="timeline" data-id="' + webhook.id + '">' + wwIcon('clock') + ' ' + t('alerts.tab.timeline') + '</div>';
            html += '</div>';

            html += '<div class="tab-content active" data-tab-content="overview">';
            html += this.renderOverview(webhook);
            html += '</div>';

            html += '<div class="tab-content" data-tab-content="data">';
            if (webhook.parsed_data) {
                html += renderJSONBlock(webhook.parsed_data, t('alerts.tab.rawData'));
            } else {
                html += '<div style="padding: 2rem; text-align: center; color: var(--text-muted);">' + t('alerts.noData') + '</div>';
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
                html += '<div class="ai-header">' + wwIcon('sparkles') + ' ' + t('alerts.ai.resultsTitle') + '</div>';
                html += '<div class="ai-content">';
                if (summary) {
                    html += '<div class="ai-item"><div class="ai-label">' + t('alerts.ai.summary') + '</div><div class="ai-value">' + escapeHtml(String(summary)) + '</div></div>';
                }
                if (webhook.importance) {
                    html += '<div class="ai-item"><div class="ai-label">' + t('alerts.ai.importance') + '</div><div class="ai-value">' + getImportanceText(webhook.importance) + '</div></div>';
                }
                html += '</div></div>';
                html += '<div style="margin-top: 1rem; padding: 0.75rem; background: var(--info-bg); border-left: 3px solid var(--info); border-radius: 4px;">';
                html += '<p style="margin: 0; color: var(--info); font-size: 0.9rem;">' + wwIcon('lightbulb') + ' ' + t('alerts.ai.autoLoadHint') + '</p>';
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
            html += '<div id="timeline-container-' + webhook.id + '">' + wwIcon('clock') + ' ' + t('alerts.timeline.clickToLoad') + '</div>';
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
        html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.receivedAt') + '</div><div class="info-value">' + formatTimeFull(webhook.timestamp) + '</div></div>';
        const statusMap = { received: t('alerts.status.received'), analyzing: t('alerts.status.analyzing'), completed: t('alerts.status.completed'), failed: t('alerts.status.failed'), dead_letter: t('alerts.status.deadLetter') };
        const statusText = statusMap[webhook.processing_status] || String(webhook.processing_status || '-');
        html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.processingStatus') + '</div><div class="info-value">' + escapeHtml(statusText) + '</div></div>';
        html += '<div class="info-item"><div class="info-label">Workflow</div><div class="info-value">' + escapeHtml(String(webhook.workflow_status || 'open')) + '</div></div>';
        html += '<div class="info-item"><div class="info-label">Owner</div><div class="info-value">' + escapeHtml(String(webhook.assignee || 'Unassigned')) + (webhook.team ? ' · ' + escapeHtml(String(webhook.team)) : '') + '</div></div>';
        html += '<div class="info-item"><div class="info-label">SLA due</div><div class="info-value">' + escapeHtml(webhook.sla_due_at ? formatTimeFull(webhook.sla_due_at) : 'Not set') + '</div></div>';
        if (webhook.updated_at) {
            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.lastUpdated') + '</div><div class="info-value">' + formatTimeFull(webhook.updated_at) + '</div></div>';
        }
        if (webhook.processing_status === 'failed' || webhook.processing_status === 'dead_letter') {
            const failure = webhook.failure_reason || webhook.error_message || '-';
            html += '<div class="info-item" style="grid-column: 1 / -1;"><div class="info-label">' + t('alerts.overview.failureReason') + '</div><div class="info-value" style="color:var(--danger); white-space: pre-wrap;">' + escapeHtml(String(failure)) + '</div></div>';
        }
        if (webhook.is_duplicate) {
            html += '<div class="info-item"><div class="info-label">' + t('alerts.overview.originalAlert') + '</div><div class="info-value">' + _alertLink(webhook.duplicate_of) + '</div></div>';
            if (webhook.prev_alert_id) {
                let prevValue = _alertLink(webhook.prev_alert_id);
                if (webhook.prev_alert_timestamp) {
                    prevValue += ' (' + formatTimeFull(webhook.prev_alert_timestamp) + ')';
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
    /**
     * Render one investigation report from the deep-analysis gateway.
     *
     * The gateway hands back `_gateway_text`, the investigator's own reply.
     * It used to be printed verbatim on the assumption that it was markdown —
     * but the investigator answers in JSON, so the entire report landed on
     * screen as one wall of escaped braces and the structured renderer below
     * it never ran. Raw text is now the fallback for when the reply really is
     * prose, not the default.
     *
     * Field shapes come from the report contract and are defensive on purpose:
     * root_cause and impact arrive as objects today and were plain strings in
     * older records, and both are still in the table.
     */
    renderDeepReport(analysis) {
        const esc = (v) => escapeHtml(String(v === null || v === undefined ? '' : v));
        const section = (icon, label, body, wide) =>
            '<div class="detail-section' + (wide ? ' dar-wide' : '') + '">' +
            '<h4 class="ww-eyebrow">' + wwIcon(icon) + ' ' + label + '</h4>' + body + '</div>';
        const sub = (label, value) =>
            value ? '<span class="dar-sub">' + label + ': ' + esc(value) + '</span>' : '';
        const asList = (items, cls) => items.length
            ? '<ul class="dar-list' + (cls ? ' ' + cls : '') + '">' + items.join('') + '</ul>'
            : '';
        const strings = (value) => (Array.isArray(value) ? value : [])
            .map((item) => '<li>' + esc(typeof item === 'object' ? JSON.stringify(item) : item) + '</li>');

        let html = '';
        let sections = '';

        if (analysis.summary) {
            html += '<div class="dar-summary">' + esc(analysis.summary) + '</div>';
        }

        const rc = analysis.root_cause;
        if (rc) {
            const text = (typeof rc === 'object') ? (rc.description || rc.summary || '') : rc;
            const status = (typeof rc === 'object') ? rc.status : '';
            sections += section('search', t('alerts.deep.rootCause'),
                '<p class="dar-body">' + esc(text) + sub(t('alerts.deep.rcStatus'), status) + '</p>');
        }

        const impact = analysis.impact || analysis.impact_scope;
        if (impact) {
            const text = (typeof impact === 'object') ? (impact.description || '') : impact;
            const scope = (typeof impact === 'object') ? impact.scope : '';
            const severity = (typeof impact === 'object') ? impact.severity : '';
            sections += section('zap', t('alerts.deep.impactScope'),
                '<p class="dar-body">' + esc(text) + sub(t('alerts.deep.scope'), scope) +
                sub(t('alerts.deep.severity'), severity) + '</p>');
        }

        // Recommendations carry a priority, which is a state: one point of
        // colour on a neutral chip, never a filled badge.
        const recs = Array.isArray(analysis.recommendations) ? analysis.recommendations : [];
        if (recs.length > 0) {
            const dotFor = { P0: 'ww-dot-danger', P1: 'ww-dot-warning', P2: 'ww-dot-muted', P3: 'ww-dot-muted' };
            const items = recs.map((rec) => {
                if (typeof rec !== 'object' || rec === null) return '<li>' + esc(rec) + '</li>';
                const priority = rec.priority ? String(rec.priority).toUpperCase() : '';
                const chip = priority
                    ? '<span class="dar-pri"><span class="ww-dot ' + (dotFor[priority] || 'ww-dot-muted') + '"></span>' +
                      esc(priority) + '</span>'
                    : '';
                return '<li>' + chip + esc(rec.action || '') + sub(t('alerts.deep.reason'), rec.reason) + '</li>';
            });
            sections += section('wrench', t('alerts.deep.recommendations'), asList(items), true);
        }

        // Evidence states what was found AND what it supports. Dropping the
        // second half would leave assertions an operator cannot check.
        const evidence = Array.isArray(analysis.evidence) ? analysis.evidence : [];
        if (evidence.length > 0) {
            const items = evidence.map((item) => {
                if (typeof item !== 'object' || item === null) return '<li>' + esc(item) + '</li>';
                return '<li>' + esc(item.finding || '') +
                    sub(t('alerts.deep.supports'), item.supports) +
                    sub(t('alerts.deep.evSource'), item.source) + '</li>';
            });
            sections += section('list', t('alerts.deep.evidence'), asList(items), true);
        }

        const timeline = Array.isArray(analysis.timeline) ? analysis.timeline : [];
        if (timeline.length > 0) {
            const items = timeline.map((item) => {
                if (typeof item !== 'object' || item === null) return '<li>' + esc(item) + '</li>';
                const when = item.time ? '<span class="dar-time">' + esc(item.time) + '</span> ' : '';
                return '<li>' + when + esc(item.event || '') + '</li>';
            });
            sections += section('history', t('alerts.deep.timeline'), asList(items), true);
        }

        const nextChecks = strings(analysis.next_checks);
        if (nextChecks.length > 0) {
            sections += section('check', t('alerts.deep.nextChecks'), asList(nextChecks));
        }

        // What the investigation could NOT establish is a section, not an
        // omission: a report listing only findings reads more certain than it is.
        const unknowns = strings(analysis.unknowns);
        if (unknowns.length > 0) {
            sections += section('alert-circle', t('alerts.deep.unknowns'), asList(unknowns, 'dar-open'));
        }

        const assumptions = strings(analysis.assumptions);
        if (assumptions.length > 0) {
            sections += section('lightbulb', t('alerts.deep.assumptions'), asList(assumptions, 'dar-open'));
        }

        if (sections) {
            html += '<div class="dar-grid">' + sections + '</div>';
        }

        // Nothing recognised — show the payload rather than an empty card.
        if (!html) {
            const raw = analysis._gateway_text || JSON.stringify(analysis, null, 2);
            return '<pre class="ww-pre">' + esc(raw) + '</pre>';
        }

        if (analysis.confidence !== undefined && analysis.confidence !== null) {
            html += '<div class="ww-meta" style="margin-top: var(--sp-4);">' + wwIcon('gauge') + ' ' +
                t('alerts.deep.confidence') + ': <strong class="ww-mono">' +
                (Number(analysis.confidence) * 100).toFixed(0) + '%</strong></div>';
        }

        // Every field, including any the renderer does not know about yet —
        // collapsed, so a contract change is discoverable instead of invisible.
        html += '<details style="margin-top: var(--sp-3);"><summary class="ww-eyebrow" style="cursor:pointer;">' +
            t('alerts.deep.rawJson') + '</summary><pre class="ww-pre" style="margin-top: var(--sp-2);">' +
            esc(JSON.stringify(analysis, null, 2)) + '</pre></details>';

        return html;
    },

    // Act-now-vs-defer verdict as a dot chip (colour in the point, neutral
    // chip). Absent on analyses cached before the field existed — render
    // nothing rather than inventing a recommendation.
    renderTriageChip(analysis) {
        const verdict = String(analysis.triage_verdict || '').toLowerCase();
        const dotFor = { act_now: 'ww-dot-danger', monitor: 'ww-dot-warning', defer: 'ww-dot-muted' };
        if (!dotFor[verdict]) return '';
        const confidence = (typeof analysis.triage_confidence === 'number')
            ? ' · ' + Math.round(analysis.triage_confidence * 100) + '%'
            : '';
        return '<div style="margin-top: 0.6rem;"><span class="badge badge-outline" style="font-weight: 500;">' +
            '<span class="ww-dot ' + dotFor[verdict] + '"></span>' +
            escapeHtml(t('alerts.ai.triage.' + verdict)) + escapeHtml(confidence) + '</span></div>';
    },

    renderAIAnalysis(analysis) {
        if (!analysis || Object.keys(analysis).length === 0) {
            return '<div style="padding: 2rem; text-align: center; color: var(--text-muted);">' + t('alerts.ai.noData') + '</div>';
        }

        let html = `
            <div class="ai-analysis" style=" background: var(--bg-surface); border: 1px solid var(--border); padding: 1.5rem; border-radius: 12px; box-shadow: var(--shadow-sm); margin-bottom: 1rem;">
                <div class="ai-header" style="font-size: 1rem; font-weight: 600; color: var(--primary); display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem;">
                    <span>${wwIcon('sparkles')}</span> ${t('alerts.ai.reportTitle')}
                    <span class="badge ${analysis._degraded ? 'badge-medium' : 'badge-low'}" style="margin-left: auto;">
                        ${escapeHtml(String(analysis._degraded ? t('alerts.ai.localFallback') : (analysis._route_type || t('alerts.ai.smartRouting'))))}
                    </span>
                </div>

                <div style="font-size: 1.1rem; color: var(--text-main); font-weight: 600; margin-bottom: 1.5rem; line-height: 1.5; padding-bottom: 1rem; border-bottom: 1px solid var(--border);">
                    ${escapeHtml(String(analysis.summary || t('alerts.ai.noSummary')))}
                    ${this.renderTriageChip(analysis)}
                </div>

                <div class="ai-details" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 2rem;">
        `;

        if (analysis.root_cause) {
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">${wwIcon('search')} ${t('alerts.ai.rootCause')}</h4>
                    <p style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; line-height: 1.6;">${escapeHtml(String(analysis.root_cause))}</p>
                </div>
            `;
        } else if (analysis.event_type) {
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">${wwIcon('tag')} ${t('alerts.ai.eventType')}</h4>
                    <p style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; line-height: 1.6;">${escapeHtml(String(analysis.event_type))}</p>
                </div>
            `;
        }

        if (analysis.impact || analysis.impact_scope) {
            const impact = analysis.impact || analysis.impact_scope;
            html += `
                <div class="detail-section">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">${wwIcon('zap')} ${t('alerts.ai.impact')}</h4>
                    <p style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; line-height: 1.6;">${escapeHtml(String(impact))}</p>
                </div>
            `;
        }

        const actions = analysis.recommendations || analysis.actions;
        if (actions && actions.length > 0) {
            html += `
                <div class="detail-section" style="grid-column: 1 / -1;">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">${wwIcon('wrench')} ${t('alerts.ai.recommendations')}</h4>
                    <ul style="font-size: 0.95rem; color: var(--text-secondary); margin: 0; padding-left: 1.5rem; line-height: 1.6;">
                        ${actions.map(r => `<li style="margin-bottom: 0.5rem;">${escapeHtml(String(r))}</li>`).join('')}
                    </ul>
                </div>
            `;
        }

        if (analysis.risks && analysis.risks.length > 0) {
            html += `
                <div class="detail-section" style="grid-column: 1 / -1;">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; letter-spacing: 0.05em;">${wwIcon('alert-triangle')} ${t('alerts.ai.risks')}</h4>
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
                <span>${wwIcon('zap')} ${t('alerts.ai.importance')}: <strong style="color: var(--text-main);">${escapeHtml(String(analysis.importance || t('alerts.ai.unknown')))}</strong></span>
        `;

        if (analysis.noise_reduction) {
            const nr = analysis.noise_reduction;
            const relationMap = { root_cause: t('alerts.ai.relation.rootCause'), derived: t('alerts.ai.relation.derived'), standalone: t('alerts.ai.relation.standalone') };
            const relation = relationMap[nr.relation] || nr.relation || t('alerts.ai.unknown');
            html += `<span>${wwIcon('shield')} ${t('alerts.ai.noiseReduction')}: <strong style="color: var(--text-main);">${escapeHtml(String(relation))}</strong> (${t('alerts.ai.confidence')}: ${Number(nr.confidence * 100).toFixed(1)}%)</span>`;
            if (nr.root_cause_event_id) {
                html += `<span>${wwIcon('link')} ${t('alerts.ai.relatedRootCause')}: ${_alertLink(nr.root_cause_event_id)}</span>`;
            }
        }

        html += `<span>${wwIcon('send')} ${t('alerts.ai.routeChannel')}: <strong style="color: var(--text-main);">${escapeHtml(String(analysis._route_type || t('alerts.ai.unknown')))}</strong></span>`;

        // Which prompt produced this, answered as a comparison rather than a
        // hex: the fingerprint only means something next to today's.
        const promptVersion = analysis._prompt_version;
        if (promptVersion) {
            const kind = String(analysis._prompt_kind || 'user');
            const current = _promptVersionsNow ? _promptVersionsNow[kind] : null;
            const edited = current && current !== promptVersion;
            const verdict = !current ? '' : ` · ${edited ? t('alerts.ai.promptEdited') : t('alerts.ai.promptCurrent')}`;
            const tone = edited ? ' style="color: var(--warning);"' : '';
            html += `<span title="${escapeHtml(String(promptVersion))}"${tone}>${wwIcon('pencil')} ${t('alerts.ai.prompt')}: <strong style="color: var(--text-main);">${escapeHtml(kind)}</strong>${escapeHtml(verdict)}</span>`;
        }

        // What this analysis actually spent. Present only on the route that
        // really called the model — a reuse route carries no usage, so absence
        // here means "this alert cost nothing", not "we forgot to measure".
        const usage = analysis._usage;
        if (usage && usage.model) {
            html += `<span>${wwIcon('sparkles')} ${t('alerts.ai.model')}: <strong style="color: var(--text-main);">${escapeHtml(String(usage.model))}</strong></span>`;
            html += `<span>${wwIcon('layers')} ${t('alerts.ai.tokens')}: <strong class="ww-mono" style="color: var(--text-main);">${escapeHtml(String(usage.tokens_in || 0))}</strong> ${t('alerts.ai.tokensIn')} / <strong class="ww-mono" style="color: var(--text-main);">${escapeHtml(String(usage.tokens_out || 0))}</strong> ${t('alerts.ai.tokensOut')}</span>`;
            if (usage.cost_usd !== undefined && usage.cost_usd !== null) {
                html += `<span>${wwIcon('dollar')} ${t('alerts.ai.cost')}: <strong class="ww-mono" style="color: var(--text-main);">$${escapeHtml(Number(usage.cost_usd).toFixed(6))}</strong></span>`;
            }
        }
        if (analysis._cache_hit) {
            const hitCount = analysis._cache_hit_count || 1;
            html += `<span title="${t('alerts.ai.hitCount', {n: escapeHtml(String(hitCount))})}" style="color: var(--success); font-weight: 600;">${wwIcon('target')} ${t('alerts.ai.cacheHit', {n: escapeHtml(String(hitCount))})}</span>`;
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


        if (page < 1) {
            console.warn('Page number less than 1, ignoring');
            return;
        }

        if (page > totalPagesFiltered) {
            if (this.hasMore) {
                await this.loadMoreAlerts();
                const updatedTotalPages = Math.ceil(this.filteredAlerts.length / this.pageSize);
                if (page > updatedTotalPages) {
                    console.warn('Page number out of range (max', updatedTotalPages, 'pages), ignoring');
                    return;
                }
            } else {
                console.warn('Page number out of range (max', totalPagesFiltered, 'pages), ignoring');
                return;
            }
        }

        this.currentPage = page;
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

        // Show loading state
        const dataTab = alertItem.querySelector('[data-tab-content="data"]');
        const aiTab = alertItem.querySelector('[data-tab-content="ai"]');

        if (dataTab) {
            dataTab.innerHTML = '<div style="padding: 2rem; text-align: center;"><div class="spinner"></div><p>' + t('alerts.loadingFullData') + '</p></div>';
        }

        try {
            await ensurePromptVersions();
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
                        dataTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: var(--text-muted);">' + t('alerts.noData') + '</div>';
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
                    aiTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: var(--text-muted);">' + t('alerts.ai.noData') + '</div>';
                }

            } else {
                throw new Error(result.error || t('alerts.error.loadFailed'));
            }
        } catch (error) {
            console.error('Failed to load full data:', error);
            if (dataTab) {
                dataTab.innerHTML = '<div style="padding: 2rem; text-align: center; color: var(--danger);">' + wwIcon('x') + ' ' + t('alerts.error.loadFailed') + ': ' + escapeHtml(String(error.message || error)) + '</div>';
            }
        }
    },

    /**
     * Replay a dead letter
     */
    async replayDeadLetter(id) {

        if (!(await wwConfirm(t('alerts.confirm.replayDeadLetter')))) {
            return;
        }

        try {
            const result = await API.replayDeadLetter(id);
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
                d.should_forward ? 'Would FORWARD' : 'Would SKIP',
                d.skip_reason ? 'Reason: ' + d.skip_reason : '',
                'Rules matched: ' + (d.matched_rule_count || 0) + ' of ' + (d.rules_evaluated || 0),
                d.matched_rules && d.matched_rules.length ? 'Matching: ' + d.matched_rules.join(', ') : ''
            ].filter(Boolean);
            showToast(lines.join('\n'), 'info');
        } catch (e) {
            showToast(t('common.requestFailed') + ': ' + (e && e.message || e), 'error');
        }
    },

    /**
     * Reanalyze an alert
     */
    async reanalyzeAlert(id) {

        if (!(await wwConfirm(t('alerts.confirm.reanalyze')))) {
            return;
        }

        try {
            const result = await API.reanalyze(id);


            if (result.success) {
                showToast(t('alerts.msg.reanalyzeSuccess'), 'info');
                this.loadAlerts();
            } else {
                showToast(t('alerts.msg.analysisFailed') + ': ' + (result.error || t('alerts.msg.unknownError')), 'error');
            }
        } catch (error) {
            console.error('Reanalysis error:', error);
            showToast(t('alerts.msg.requestFailed') + ': ' + error.message, 'error');
        }
    },

    /**
     * Open the forward modal
     */
    openForwardModal(id) {
        this.currentForwardId = id;

        const forwardUrlInput = document.getElementById('forwardUrl');
        if (forwardUrlInput) {
            forwardUrlInput.value = '';
        }

        const modal = document.getElementById('forwardModal');
        if (modal) {
            modal.classList.add('active');
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
        if (!url) return showToast(t('alerts.msg.enterForwardUrl'), 'info');
        const id = this.currentForwardId;
        const button = document.getElementById('confirmForwardBtn');

        await this._runButtonAction(button, 'forward-confirm:' + id, async () => {
            try {
                const result = await API.forward(id, url);

                if (result.success) {
                    showToast(t('alerts.msg.forwardSuccess'), 'info');
                    this.closeForwardModal();
                } else {
                    showToast(t('alerts.msg.forwardFailed') + ': ' + (result.error || t('alerts.msg.unknownError')), 'error');
                }
            } catch (error) {
                showToast(t('alerts.msg.requestFailed') + ': ' + error.message, 'error');
            }
        });
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
                container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">' + t('alerts.decision.none') + '</div>';
                return;
            }
            if (typeof DecisionTraceModule !== 'undefined' && DecisionTraceModule.renderDetails) {
                container.innerHTML = '<div class="da-card da-card-expanded" style="margin:0;">' + DecisionTraceModule.renderDetails(result.data) + '</div>';
            } else {
                container.innerHTML = '<div style="padding:1rem; color:var(--text-muted);">' + t('alerts.decision.none') + '</div>';
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
                container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">' + t('alerts.timeline.empty') + '</div>';
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
        const impEmoji = { high: '<span class="ww-dot ww-dot-danger"></span>', medium: '<span class="ww-dot ww-dot-warning"></span>', low: '<span class="ww-dot ww-dot-success"></span>' };
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
            var borderColor = isAnchor ? 'var(--primary, var(--primary))' : (isCaused ? 'var(--warning, var(--warning))' : 'var(--border, var(--text-secondary))');
            var bg = isAnchor ? 'var(--primary-bg)' : 'transparent';

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
            if (isAnchor) html += '';
            html += '<a href="javascript:void(0)" data-act="AlertsModule._scrollToAlert" data-args="' + ev.id + '" style="color:var(--text-main); text-decoration:none;">#' + ev.id + '</a>';
            html += ' <span style="color:var(--text-muted); font-weight:400;">' + escapeHtml(ev.source) + '</span>';
            html += ' <span>' + (impEmoji[ev.importance] || '<span class="ww-dot ww-dot-muted"></span>') + ' ' + escapeHtml(ev.importance) + '</span>';
            if (ev.is_duplicate) html += ' <span class="badge badge-outline" style="font-size:0.6rem;">' + t('alerts.status.duplicate') + '</span>';
            if (ev.forward_status === 'sent') html += ' <span class="badge badge-success" style="font-size:0.6rem;"></span>';
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
        if (!item) {
            // The target is filtered out or on another page: the silent no-op
            // here was audit finding #3. focusAlertById clears filters,
            // paginates, and fetches from the API if needed.
            this.focusAlertById(eventId);
            return;
        }
        item.scrollIntoView({ behavior: 'smooth', block: 'center' });
        if (!item.classList.contains('expanded')) {
            item.classList.add('expanded');
            // Trigger data load if needed
            var header = item.querySelector('.alert-header');
            if (header) header.click();
        }
        item.style.boxShadow = '0 0 0 3px var(--primary, var(--primary))';
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
                container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">' +
                    '<p>' + t('alerts.deep.noRecords') + '</p>' +
                    '<button class="btn btn-primary" data-act="AlertsModule.deepAnalyzeAlert" data-args="' + webhookId + '">' + wwIcon('flask') + ' ' + t('alerts.deep.analyzeNow') + '</button>' +
                    '</div>';
                return;
            }

            let html = '';
            // Arrow, not function(): the body calls this.renderDeepReport.
            records.forEach((record) => {
                const analysis = record.analysis_result || {};
                // The engine is whatever platform answered; show its own name rather than
                // assuming one product. t() falls back to the raw slug for a
                // gateway newer than the dictionary, which beats mislabelling it.
                const engineLabel = record.engine
                    ? t('deep.engine.' + record.engine, {}, record.engine)
                    : t('deep.engine.local');
                const time = formatTimeFull(record.created_at);
                const duration = record.duration_seconds ? record.duration_seconds.toFixed(1) + 's' : '-';

                html += '<div style="border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:12px; background:var(--bg-subtle);">';

                // Header: engine, time, duration
                html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--bg-subtle);">';
                html += '<span style="font-weight:600;">' + engineLabel + '</span>';
                html += '<span style="color:var(--text-muted); font-size:0.85em;">' + time + ' | ' + t('alerts.deep.duration') + ' ' + duration + '</span>';
                html += '</div>';

                // User question (if any)
                if (record.user_question) {
                    html += '<div style="margin-bottom:10px; padding:8px 12px; background:var(--info-bg); border-radius:4px; font-size:0.9em;">';
                    html += '<strong>' + t('alerts.deep.userQuestion') + ': </strong>' + escapeHtml(String(record.user_question));
                    html += '</div>';
                }

                // Check whether the status is pending (the gateway answers asynchronously)
                if (record.status === 'pending') {
                    // Analyzing-state card
                    html += '<div style="text-align:center; padding:20px; background:var(--info-bg); border:1px solid var(--info-bg); border-radius:8px; color:var(--info);">';
                    html += '<div style="font-size:2em; margin-bottom:12px;"></div>';
                    html += '<div style="font-size:1.1em; font-weight:600; margin-bottom:8px;">' + t('alerts.deep.gatewayAnalyzing') + '</div>';
                    if (record.gateway_run_id) {
                        html += '<div style="font-size:0.8em; opacity:0.7; margin-bottom:12px;">' + t('alerts.deep.runId') + ': ' + escapeHtml(String(record.gateway_run_id)) + '</div>';
                    }
                    html += '<div style="font-size:0.9em; opacity:0.85;">' + t('alerts.deep.willUpdate') + '</div>';
                    html += '</div>';
                } else {
                    html += this.renderDeepReport(analysis);
                }

                html += '</div>';
            });

            // Footer: re-analyze button
            html += '<div style="text-align:center; margin-top:12px;">';
            html += '<button class="btn btn-sm" data-act="AlertsModule.deepAnalyzeAlert" data-args="' + webhookId + '">' + wwIcon('flask') + ' ' + t('alerts.deep.analyzeAgain') + '</button>';
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
        const question = await wwPrompt(t('alerts.deep.questionPrompt'), '');
        if (question === null) return;  // User cancelled

        try {
            const result = await API.deepAnalyze(id, question, 'auto');
            if (result.success && result.data) {
                const record = result.data;
                const analysisResult = record.analysis_result || {};

                // Check whether the status is pending (the gateway answers asynchronously)
                if (record.status === 'pending' || analysisResult._pending) {
                    this.showTriggeredNotification(analysisResult._gateway_run_id || record.gateway_run_id);
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
                    showToast(t('alerts.deep.completeNotice'), 'info');
                }
            } else {
                showToast(t('alerts.msg.analysisFailed') + ': ' + (result.error || t('alerts.msg.unknownError')), 'error');
            }
        } catch (error) {
            showToast(t('alerts.msg.requestFailed') + ': ' + error.message, 'error');
        }
    },

    /**
     * Show a friendly notification that deep analysis has been triggered
     */
    showTriggeredNotification(runId) {
        // Create the overlay notification
        const notification = document.createElement('div');
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            background: var(--primary);
            color: white;
            padding: 16px 24px;
            border-radius: 12px;
            box-shadow: var(--shadow-lg);
            z-index: 10000;
            max-width: 360px;
            animation: slideIn 0.3s ease-out;
        `;
        notification.innerHTML = `
            <div style="display:flex; align-items:center; margin-bottom:8px;">
                
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
    },

    // ── Bulk workflow actions ──────────────────────────────────────────
    // Clearing twenty stale alerts was forty clicks. Selection is a Set of
    // ids surviving re-renders; the bar lives above the list and executes
    // sequentially through the same single-item endpoint.
    _bulkSelected: new Set(),

    _renderBulkBar() {
        const bar = document.getElementById('alertsBulkBar');
        if (!bar) return;
        const n = this._bulkSelected.size;
        if (!n) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
        bar.style.display = 'flex';
        bar.innerHTML = '<span class="bulk-count">' + escapeHtml(t('alerts.bulk.selected', { n: n })) + '</span>' +
            '<button type="button" class="btn btn-sm" data-bulk="page">' + escapeHtml(t('alerts.bulk.selectPage')) + '</button>' +
            '<button type="button" class="btn btn-sm btn-quiet-primary" data-bulk="ack">' + wwIcon('check') + ' ' + escapeHtml(t('alerts.bulk.ack')) + '</button>' +
            '<button type="button" class="btn btn-sm btn-quiet-primary" data-bulk="resolve">' + wwIcon('check') + ' ' + escapeHtml(t('alerts.bulk.resolve')) + '</button>' +
            '<button type="button" class="btn btn-sm" data-bulk="clear">' + escapeHtml(t('alerts.bulk.clear')) + '</button>';
    },

    _bindBulkEvents() {
        const list = document.getElementById('alertList');
        if (list && !list._bulkBound) {
            list._bulkBound = true;
            list.addEventListener('change', (e) => {
                const box = e.target.closest('.alert-bulk-check');
                if (!box) return;
                const id = String(box.getAttribute('data-bulk-id'));
                if (box.checked) this._bulkSelected.add(id); else this._bulkSelected.delete(id);
                this._renderBulkBar();
            });
            list.addEventListener('click', (e) => {
                if (e.target.closest('.alert-bulk-check')) e.stopPropagation();
            }, true);
        }
        const bar = document.getElementById('alertsBulkBar');
        if (bar && !bar._bulkBound) {
            bar._bulkBound = true;
            bar.addEventListener('click', (e) => {
                const btn = e.target.closest('[data-bulk]');
                if (!btn) return;
                const kind = btn.getAttribute('data-bulk');
                if (kind === 'clear') {
                    this._bulkSelected.clear();
                    document.querySelectorAll('.alert-bulk-check').forEach((b) => { b.checked = false; });
                    this._renderBulkBar();
                } else if (kind === 'page') {
                    document.querySelectorAll('.alert-bulk-check').forEach((b) => {
                        b.checked = true;
                        this._bulkSelected.add(String(b.getAttribute('data-bulk-id')));
                    });
                    this._renderBulkBar();
                } else {
                    this._runBulk(kind === 'ack' ? 'acknowledged' : 'resolved', btn);
                }
            });
        }
    },

    async _runBulk(status, btn) {
        const ids = Array.from(this._bulkSelected);
        if (!ids.length || !(await wwConfirm(t('alerts.bulk.confirm', { n: ids.length })))) return;
        btn.disabled = true;
        let failed = 0;
        for (const id of ids) {
            try {
                const response = await API.authenticatedFetch('/v1/webhooks/' + id + '/workflow', {
                    method: 'PUT', body: JSON.stringify({ workflow_status: status })
                });
                if (!response.ok) failed++;
            } catch (e) { failed++; }
        }
        this._bulkSelected.clear();
        if (typeof showToast === 'function') {
            showToast(t(failed ? 'alerts.bulk.doneWithFailures' : 'alerts.bulk.done', { n: ids.length - failed, failed: failed }), failed ? 'warning' : 'success');
        }
        this._renderBulkBar();
        await this.loadAlerts();
    },

    async updateWorkflow(id, patch) {
        try {
            const response = await API.authenticatedFetch('/v1/webhooks/' + id + '/workflow', {
                method: 'PUT', body: JSON.stringify(patch)
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            const target = this.alerts.find(function (item) { return String(item.id) === String(id); });
            if (target) Object.assign(target, payload.data || {});
            this.filterAlerts(false);
            // No undo toast: the badge itself is now the control, so putting
            // the status back is the same gesture as setting it — visible on
            // the card, available whenever you notice rather than for twelve
            // seconds after the click.
            if (patch && patch.workflow_status && typeof showToast === 'function') {
                showToast(t('alerts.workflow.changedTo', {
                    status: t('alerts.workflow.' + patch.workflow_status)
                }), 'success');
            }
        } catch (error) {
            showToast('Workflow update failed: ' + (error.message || String(error)), 'error');
        }
    },

    async undoWorkflow(id) {
        try {
            const response = await API.authenticatedFetch('/v1/webhooks/' + id + '/workflow/undo', { method: 'POST' });
            const payload = await response.json();
            if (!response.ok || !payload.success) {
                // The refusal is the useful part: it means somebody else moved
                // the alert on, and saying so is better than silently winning.
                if (typeof showToast === 'function') {
                    showToast(payload.error || t('alerts.workflow.undoFailed'), 'warning');
                }
                await this.loadAlerts();
                return;
            }
            const target = this.alerts.find(function (item) { return String(item.id) === String(id); });
            if (target) Object.assign(target, payload.data || {});
            this.filterAlerts(false);
            if (typeof showToast === 'function') showToast(t('alerts.workflow.undone'), 'success');
        } catch (error) {
            if (typeof showToast === 'function') {
                showToast(t('alerts.workflow.undoFailed') + ': ' + (error.message || String(error)), 'error');
            }
        }
    },

    async assignWorkflow(id) {
        const target = this.alerts.find(function (item) { return String(item.id) === String(id); }) || {};
        const assignee = await wwPrompt('Assignee (leave empty to unassign)', target.assignee || '');
        if (assignee === null) return;
        const team = await wwPrompt('Team (leave empty to clear)', target.team || '');
        if (team === null) return;
        const sla = await wwPrompt('SLA in minutes (leave empty to keep current SLA)', '');
        if (sla === null) return;
        const patch = { assignee: assignee, team: team };
        if (sla.trim()) patch.sla_minutes = Number(sla);
        await this.updateWorkflow(id, patch);
    },

    async manageNotes(id) {
        try {
            const response = await API.authenticatedFetch('/v1/webhooks/' + id + '/notes');
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            const history = (payload.data || []).map(function (note) {
                return '[' + note.actor + '] ' + note.body;
            }).join('\n');
            const body = await wwPrompt((history ? 'Existing notes:\n' + history + '\n\n' : '') + 'Add a note (Cancel to close)', '');
            if (!body || !body.trim()) return;
            const createResponse = await API.authenticatedFetch('/v1/webhooks/' + id + '/notes', {
                method: 'POST', body: JSON.stringify({ body: body.trim(), actor: 'dashboard' })
            });
            if (!createResponse.ok) throw new Error('HTTP ' + createResponse.status);
        } catch (error) {
            showToast('Notes failed: ' + (error.message || String(error)), 'error');
        }
    },

    // Replaces the old correct/incorrect feedback pair. "Correct" only ever
    // incremented an agreement percentage computed from self-selected samples,
    // and "incorrect" sent a free-text comment nothing ever read — it did not
    // even send the corrected importance the backend has always supported. One
    // action that changes something beats two that imply they teach the model.
    async chooseWorkflow(id) {
        const target = this.alerts.find(function (item) { return String(item.id) === String(id); }) || {};
        const current = target.workflow_status || 'open';
        const states = ['open', 'acknowledged', 'in_progress', 'resolved', 'ignored'];
        const choice = await wwChoose(t('alerts.workflow.choose'), states.map(function (s) {
            return { value: s, label: t('alerts.workflow.' + s), active: s === current };
        }));
        if (!choice || choice === current) return;
        await this.updateWorkflow(id, { workflow_status: choice });
    },

    async overrideImportance(id) {
        const target = this.alerts.find(function (item) { return String(item.id) === String(id); }) || {};
        const current = target.importance || '';
        const value = await wwPrompt(t('alerts.action.overrideImportancePrompt'), current);
        if (value === null) return;
        const next = String(value).trim().toLowerCase();
        if (!next) return;
        if (['high', 'medium', 'low'].indexOf(next) === -1) {
            if (typeof showToast === 'function') showToast(t('alerts.action.overrideImportanceBad'), 'warning');
            return;
        }
        if (next === String(current).toLowerCase()) return;
        try {
            const response = await API.authenticatedFetch('/v1/webhooks/' + id + '/feedback', {
                method: 'POST',
                body: JSON.stringify({
                    verdict: 'incorrect',
                    corrected_importance: next,
                    actor: 'dashboard'
                })
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            if (target) target.importance = next;
            this.filterAlerts(false);
            if (typeof showToast === 'function') showToast(t('alerts.action.overrideImportanceOk'), 'success');
        } catch (error) {
            if (typeof showToast === 'function') {
                showToast(t('alerts.action.overrideImportanceFail') + ': ' + (error.message || String(error)), 'error');
            }
        }
    }
};

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="AlertsModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('AlertsModule', AlertsModule);
