/**
 * API call wrapper module
 * Unifies all backend API calls, providing consistent error handling and response parsing
 */

const API = {
    _tokenCache: {
        read: '',
        write: ''
    },
    _legacyStorageCleared: false,
    /**
     * Get the read-only API Token
     */
    getToken() {
        return this.getReadToken();
    },

    getReadToken() {
        return this._tokenCache.read || '';
    },

    getWriteToken() {
        return this._tokenCache.write || '';
    },

    async setReadToken(token) {
        this._tokenCache.read = String(token || '');
    },

    async setWriteToken(token) {
        this._tokenCache.write = String(token || '');
    },

    async clearTokens() {
        this._tokenCache.read = '';
        this._tokenCache.write = '';
        this.clearLegacyPersistedTokens();
    },

    getTokenStatus() {
        return {
            read: Boolean(this.getReadToken()),
            write: Boolean(this.getWriteToken())
        };
    },

    async initAuthStorage() {
        // Credentials intentionally live only in page memory. Persisting both
        // ciphertext and its decrypting key under one origin does not protect
        // against same-origin script compromise.
        this.clearLegacyPersistedTokens();
    },

    clearLegacyPersistedTokens() {
        if (this._legacyStorageCleared) return;
        this._legacyStorageCleared = true;
        try {
            window.localStorage?.removeItem('webhook_api_key');
            window.localStorage?.removeItem('webhook_admin_write_key');
        } catch (_error) {
            // Storage can be unavailable under strict privacy settings.
        }
        try {
            window.indexedDB?.deleteDatabase('webhookwise_auth_crypto');
        } catch (_error) {
            // Best-effort cleanup of the obsolete local encryption key.
        }
    },

    getAuthMode(options = {}) {
        if (options.authMode) return options.authMode;
        const method = String(options.method || 'GET').toUpperCase();
        return method === 'GET' || method === 'HEAD' ? 'read' : 'write';
    },

    getTokenForMode(mode) {
        return mode === 'write' ? this.getWriteToken() : this.getReadToken();
    },

    // Global auth lock, prevents multiple input prompts from appearing on concurrent requests
    _authPromises: {
        read: null,
        write: null
    },

    /**
     * Wraps fetch, automatically adding the Auth header and handling 401
     */
    async authenticatedFetch(url, options = {}, retryState = {}) {
        const authMode = this.getAuthMode(options);
        await this.initAuthStorage();

        // If an auth prompt is already in progress, wait for it to finish
        if (this._authPromises[authMode]) {
            await this._authPromises[authMode];
        }

        const token = this.getTokenForMode(authMode);
        const headers = {
            ...options.headers,
            'Content-Type': 'application/json'
        };
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }
        if (authMode === 'write') {
            const readToken = this.getReadToken();
            if (readToken) {
                headers['x-api-key'] = readToken;
            }
        }

        const { authMode: _ignoredAuthMode, ...fetchOptions } = options;
        const response = await fetch(url, { ...fetchOptions, headers });

        if (await this.shouldPromptForAuth(response, authMode)) {
            // Before prompting, check once more whether another concurrent request already handled it
            const currentToken = this.getTokenForMode(authMode);
            if (currentToken && currentToken !== token) {
                // Token has been updated; retry directly with the new Token
                return this.authenticatedFetch(url, options, retryState);
            }

            if (retryState[authMode]) {
                return response;
            }

            if (!this._authPromises[authMode]) {
                // Create a Promise lock and block other concurrent requests
                this._authPromises[authMode] = new Promise((resolve) => {
                    // Use setTimeout to avoid deadlocking the UI thread and give the browser a chance to render
                    setTimeout(async () => {
                        const key = prompt(this.authPromptText(authMode));
                        if (key) {
                            try {
                                if (authMode === 'write') {
                                    await this.setWriteToken(key);
                                } else {
                                    await this.setReadToken(key);
                                }
                                if (typeof updateAuthButtonState === 'function') {
                                    updateAuthButtonState();
                                }
                            } catch (error) {
                                console.error('Failed to load credentials into page memory', error);
                                resolve(null);
                                this._authPromises[authMode] = null;
                                return;
                            }
                        }
                        resolve(key);
                        this._authPromises[authMode] = null;
                    }, 50);
                });
            }

            const newKey = await this._authPromises[authMode];
            if (newKey) {
                // Now that we have a new Token, recursively retry this request
                return this.authenticatedFetch(url, options, { ...retryState, [authMode]: true });
            }
        }

        return response;
    },

    authPromptText(mode) {
        if (mode === 'write') {
            return 'Enter the WebhookWise ADMIN_WRITE_KEY (used for write operations such as save, forward, and retry):';
        }
        return 'Enter the WebhookWise API_KEY (used for read-only Dashboard queries):';
    },

    async shouldPromptForAuth(response, mode) {
        if (mode === 'read') {
            return response.status === 401;
        }
        if (response.status === 401) {
            return true;
        }
        if (response.status !== 403) {
            return false;
        }
        const body = await response.clone().json().catch(() => null);
        return [
            'Admin write permission required',
            'Admin write permission required. Missing ADMIN_WRITE_KEY.',
            'Admin write token required. API key is insufficient for this endpoint.'
        ].includes(body?.detail);
    },

    async parseJsonResponse(response) {
        const payload = await response.json().catch(() => null);
        if (!response.ok) {
            const rawDetail = payload && (payload.error || payload.detail || payload.message);
            const detail = typeof rawDetail === 'string' ? rawDetail : (rawDetail ? JSON.stringify(rawDetail) : 'HTTP ' + response.status);
            const error = new Error(detail);
            error.status = response.status;
            error.retryAfter = response.headers.get('Retry-After');
            throw error;
        }
        return payload;
    },

    // ========== Alert-related API ==========

    /**
     * Get the alert list
     * @param {object} params - Query parameters
     * @param {number} params.page - Page number
     * @param {number} params.page_size - Items per page
     * @param {number} params.cursor - Cursor for the next page
     * @returns {Promise<object>} Alert list data
     */
    async getWebhooks(params = {}) {
        const queryParams = new URLSearchParams();
        if (params.cursor !== null && params.cursor !== undefined) queryParams.append('cursor', params.cursor);
        if (params.page_size) queryParams.append('page_size', params.page_size);
        if (params.page) queryParams.append('page', params.page);
        if (params.importance) queryParams.append('importance', params.importance);
        if (params.source) queryParams.append('source', params.source);
        if (params.window) queryParams.append('window', params.window);
        if (params.search) queryParams.append('search', params.search);
        if (params.processing_status) queryParams.append('processing_status', params.processing_status);

        const response = await this.authenticatedFetch('/v1/webhooks?' + queryParams.toString());
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Get the details of a single alert
     * @param {number} id - Alert ID
     * @returns {Promise<object>} Alert detail data
     */
    async getWebhook(id) {
        const response = await this.authenticatedFetch('/v1/webhooks/' + id);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Re-analyze an alert
     * @param {number} id - Alert ID
     * @returns {Promise<object>} Analysis result
     */
    async reanalyze(id) {
        const response = await this.authenticatedFetch('/v1/reanalyze/' + id, { method: 'POST' });
        return await this.parseJsonResponse(response);
    },

    /**
     * Forward an alert
     * @param {number} id - Alert ID
     * @param {string} url - Forwarding target URL
     * @returns {Promise<object>} Forwarding result
     */
    async forward(id, url) {
        const response = await this.authenticatedFetch('/v1/forward/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_url: url })
        });
        return await this.parseJsonResponse(response);
    },

    /**
     * Dry-run a webhook payload through the pre-AI pipeline (the Sandbox).
     * Returns what WW would extract and decide; no enqueue / AI / persistence.
     * @param {string} source - source hint (e.g. "volcengine")
     * @param {object} payload - the raw alert payload object
     */
    async testWebhookPayload(source, payload) {
        const response = await this.authenticatedFetch('/v1/sandbox/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: source, payload: payload })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== AI-related API ==========

    /**
     * Get AI usage statistics
     * @param {string} period - Statistics period (day/week/month)
     * @returns {Promise<object>} AI usage statistics data
     */
    async getAIUsage(period = 'day') {
        const response = await this.authenticatedFetch('/v1/ai-usage?period=' + period);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== Overview API ==========

    /**
     * Get the one-screen overview summary (today's volume / forward rate /
     * skip distribution / delivery success / top sources).
     * @param {string} period - day/week/month
     */
    async getOverview(period = 'day') {
        const response = await this.authenticatedFetch('/v1/overview?period=' + period);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== Decision Trace API ==========

    /**
     * Get decision-trace aggregate stats (forwarded vs skipped + skip reasons)
     * @param {string} period - Statistics period (day/week/month)
     * @returns {Promise<object>} Decision-trace stats
     */
    async getDecisionTraceStats(period = 'day') {
        const response = await this.authenticatedFetch('/v1/decision-traces/stats?period=' + period);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Get decision-trace AI-judgment quality stats (override/degradation/importance)
     * @param {string} period - Statistics period (day/week/month)
     * @returns {Promise<object>} Quality stats
     */
    async getDecisionTraceQualityStats(period = 'day') {
        const response = await this.authenticatedFetch('/v1/decision-traces/quality-stats?period=' + period);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * List incidents (cursor-paginated, filterable by status)
     * @param {object} params - Query params (status, page_size, cursor)
     * @returns {Promise<object>} Incident list
     */
    async getIncidents(params = {}) {
        const queryParams = new URLSearchParams();
        if (params.status) queryParams.append('status', params.status);
        if (params.page_size) queryParams.append('page_size', params.page_size);
        if (params.cursor != null) queryParams.append('cursor', params.cursor);
        const response = await this.authenticatedFetch('/v1/incidents?' + queryParams.toString());
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Get the decision-trace list (newest first), each row carrying its full chain
     * @param {object} params - Query parameters (cursor, page_size, outcome, skip_code, source)
     * @returns {Promise<object>} Decision-trace list data
     */
    async getDecisionTraces(params = {}) {
        const queryParams = new URLSearchParams();
        if (params.cursor !== null && params.cursor !== undefined) queryParams.append('cursor', params.cursor);
        if (params.page_size) queryParams.append('page_size', params.page_size);
        if (params.outcome) queryParams.append('outcome', params.outcome);
        if (params.skip_code) queryParams.append('skip_code', params.skip_code);
        if (params.source) queryParams.append('source', params.source);
        if (params.delivery) queryParams.append('delivery', params.delivery);
        const response = await this.authenticatedFetch('/v1/decision-traces?' + queryParams.toString());
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Get the decision trace for a single webhook event (chain + delivery).
     * Returns {success:false} on 404 (no trace) rather than throwing — "no
     * trace yet" is a normal state for a just-ingested alert.
     * @param {number} webhookId
     */
    async getDecisionTraceByEvent(webhookId) {
        const response = await this.authenticatedFetch('/v1/decision-traces/by-event/' + webhookId);
        if (response.status === 404) return { success: false, data: null };
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== Deep Analysis API ==========

    /**
     * Get all deep analysis records (paginated + filtered)
     */
    async getAllDeepAnalyses(page = 1, perPage = 20, status = '', engine = '', cursor = null) {
        const params = new URLSearchParams({ page: page, per_page: perPage });
        if (cursor !== null && cursor !== undefined) params.set('cursor', cursor);
        if (status) params.set('status', status);
        if (engine) params.set('engine', engine);
        const response = await this.authenticatedFetch('/v1/deep-analyses?' + params.toString());
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Get the full content of a single deep analysis (includes normalized_report + the raw analysis_result)
     * The list only returns a lightweight summary; this endpoint is called on demand when expanding an entry.
     * @param {number} analysisId - Deep analysis record ID
     */
    async getDeepAnalysisDetail(analysisId) {
        const response = await this.authenticatedFetch('/v1/deep-analyses/detail/' + analysisId);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Get deep analysis history records
     * @param {number} webhookId - Alert ID
     * @returns {Promise<object>} List of deep analysis history records
     */
    async getDeepAnalyses(webhookId) {
        const response = await this.authenticatedFetch('/v1/deep-analyses/' + webhookId);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Run a deep analysis
     * @param {number} id - Alert ID
     * @param {string} question - Analysis question
     * @param {string} engine - Analysis engine ('openclaw'/'auto')
     * @returns {Promise<object>} Analysis result
     */
    async deepAnalyze(id, question, engine = 'auto') {
        const response = await this.authenticatedFetch('/v1/deep-analyze/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_question: question,
                engine: engine
            })
        });
        return await this.parseJsonResponse(response);
    },

    /**
     * Forward a deep analysis result
     * @param {number} analysisId - Deep analysis record ID
     * @param {string} targetUrl - Forwarding target URL
     * @returns {Promise<object>} Forwarding result
     */
    async forwardDeepAnalysis(analysisId, targetUrl) {
        const response = await this.authenticatedFetch('/v1/deep-analyses/' + analysisId + '/forward', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_url: targetUrl })
        });
        return await this.parseJsonResponse(response);
    },

    /**
     * Re-fetch a failed deep analysis result
     * @param {number} analysisId - Deep analysis record ID
     * @returns {Promise<object>} Retry result
     */
    async retryDeepAnalysis(analysisId) {
        const response = await this.authenticatedFetch('/v1/deep-analyses/' + analysisId + '/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        return await this.parseJsonResponse(response);
    },

    // ========== Forwarding Rules API ==========

    /**
     * Get the forwarding rules list
     * @returns {Promise<object>} Rules list
     */
    async getForwardRules(options = {}) {
        const includeSensitive = !!options.includeSensitive;
        const response = await this.authenticatedFetch(
            includeSensitive ? '/v1/forward-rules/sensitive' : '/v1/forward-rules',
            includeSensitive ? { authMode: 'write' } : {}
        );
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Create a forwarding rule
     * @param {object} ruleData - Rule data
     * @returns {Promise<object>} Creation result
     */
    async createForwardRule(ruleData) {
        const response = await this.authenticatedFetch('/v1/forward-rules', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ruleData)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Update a forwarding rule
     * @param {number} id - Rule ID
     * @param {object} ruleData - Rule data
     * @returns {Promise<object>} Update result
     */
    async updateForwardRule(id, ruleData) {
        const response = await this.authenticatedFetch('/v1/forward-rules/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ruleData)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Delete a forwarding rule
     * @param {number} id - Rule ID
     * @returns {Promise<object>} Deletion result
     */
    async deleteForwardRule(id) {
        const response = await this.authenticatedFetch('/v1/forward-rules/' + id, {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Test a forwarding rule
     * @param {number} id - Rule ID
     * @returns {Promise<object>} Test result
     */
    async testForwardRule(id) {
        const response = await this.authenticatedFetch('/v1/forward-rules/' + id + '/test', {
            method: 'POST'
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== Silences API ==========

    /**
     * Get the silences list
     * @param {object} params - { activeOnly }
     * @returns {Promise<object>} Silences list
     */
    async getSilences(params = {}) {
        const q = new URLSearchParams();
        if (params.activeOnly) q.append('active_only', 'true');
        const query = q.toString();
        const response = await this.authenticatedFetch('/v1/silences' + (query ? '?' + query : ''));
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Create a silence
     * @param {object} silenceData - Silence data
     * @returns {Promise<object>} Creation result
     */
    async createSilence(silenceData) {
        const response = await this.authenticatedFetch('/v1/silences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(silenceData)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Update a silence
     * @param {number} id - Silence ID
     * @param {object} silenceData - Silence data
     * @returns {Promise<object>} Update result
     */
    async updateSilence(id, silenceData) {
        const response = await this.authenticatedFetch('/v1/silences/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(silenceData)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Lift (deactivate) a silence
     * @param {number} id - Silence ID
     * @returns {Promise<object>} Lift result
     */
    async liftSilence(id) {
        const response = await this.authenticatedFetch('/v1/silences/' + id + '/lift', {
            method: 'POST'
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Delete a silence
     * @param {number} id - Silence ID
     * @returns {Promise<object>} Deletion result
     */
    async deleteSilence(id) {
        const response = await this.authenticatedFetch('/v1/silences/' + id, {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Backtest a proposed silence rule
     * @param {object} silenceData - Silence data including lookback_days
     * @returns {Promise<object>} Backtest result
     */
    async backtestSilence(silenceData) {
        const response = await this.authenticatedFetch('/v1/silences/backtest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(silenceData)
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== Forwarding Queue API ==========

    /**
     * Get forwarding queue records
     */
    async getOutbox(params = {}) {
        const q = new URLSearchParams();
        if (params.page) q.append('page', params.page);
        if (params.page_size) q.append('page_size', params.page_size);
        if (params.cursor !== null && params.cursor !== undefined) q.append('cursor', params.cursor);
        if (params.status) q.append('status', params.status);
        if (params.event_type) q.append('event_type', params.event_type);
        const response = await this.authenticatedFetch('/v1/outbox?' + q.toString(), { authMode: "write" });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    /**
     * Retry a failed forwarding record
     */
    async retryOutbox(id) {
        const response = await this.authenticatedFetch('/v1/admin/outbox/' + id + '/retry', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    // ========== Dead Letter Queue API ==========

    async getDeadLetters(params = {}) {
        const q = new URLSearchParams();
        if (params.page) q.append('page', params.page);
        if (params.page_size) q.append('page_size', params.page_size);
        if (params.source) q.append('source', params.source);
        if (params.search) q.append('search', params.search);
        if (params.time_from) q.append('time_from', params.time_from);
        if (params.time_to) q.append('time_to', params.time_to);
        const query = q.toString();
        const response = await this.authenticatedFetch('/v1/admin/dead-letters' + (query ? '?' + query : ''));
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    async getDeadLetterDetail(eventId) {
        const response = await this.authenticatedFetch('/v1/admin/dead-letters/' + eventId);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    async replayDeadLetter(eventId) {
        const response = await this.authenticatedFetch('/v1/admin/dead-letters/' + eventId + '/replay', {
            method: 'POST'
        });
        return await this.parseJsonResponse(response);
    },

    async replayDeadLettersByIds(eventIds) {
        const response = await this.authenticatedFetch('/v1/admin/dead-letters/replay-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_ids: eventIds })
        });
        return await this.parseJsonResponse(response);
    },

    async replayAllDeadLetters(batchSize) {
        const q = batchSize ? '?batch_size=' + encodeURIComponent(batchSize) : '';
        const response = await this.authenticatedFetch('/v1/admin/dead-letters/replay-all' + q, { method: 'POST' });
        return await this.parseJsonResponse(response);
    }
};
