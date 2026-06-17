/**
 * API call wrapper module
 * Unifies all backend API calls, providing consistent error handling and response parsing
 */

const READ_TOKEN_KEY = 'webhook_api_key';
const WRITE_TOKEN_KEY = 'webhook_admin_write_key';
const AUTH_CRYPTO_DB = 'webhookwise_auth_crypto';
const AUTH_CRYPTO_STORE = 'keys';
const AUTH_CRYPTO_KEY_ID = 'dashboard-token-key';
const AUTH_TOKEN_RECORD_VERSION = 1;

const API = {
    _tokenCache: {
        read: '',
        write: ''
    },
    _authStorageReady: null,
    _cryptoKeyPromise: null,

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
        if (token) {
            await this.setEncryptedToken(READ_TOKEN_KEY, 'read', token);
        }
    },

    async setWriteToken(token) {
        if (token) {
            await this.setEncryptedToken(WRITE_TOKEN_KEY, 'write', token);
        }
    },

    async clearTokens() {
        localStorage.removeItem(READ_TOKEN_KEY);
        localStorage.removeItem(WRITE_TOKEN_KEY);
        this._tokenCache.read = '';
        this._tokenCache.write = '';
        this._authStorageReady = null;
        this._cryptoKeyPromise = null;
        try {
            await this.deleteStoredCryptoKey();
        } catch (error) {
            console.warn('Failed to clear the local credential encryption key', error);
        }
    },

    getTokenStatus() {
        return {
            read: Boolean(this.getReadToken() || this.hasEncryptedToken(READ_TOKEN_KEY)),
            write: Boolean(this.getWriteToken() || this.hasEncryptedToken(WRITE_TOKEN_KEY))
        };
    },

    async initAuthStorage() {
        if (!this._authStorageReady) {
            this._authStorageReady = (async () => {
                this._tokenCache.read = await this.loadEncryptedToken(READ_TOKEN_KEY);
                this._tokenCache.write = await this.loadEncryptedToken(WRITE_TOKEN_KEY);
            })();
        }
        return this._authStorageReady;
    },

    hasEncryptedToken(storageKey) {
        return this.isEncryptedTokenRecord(localStorage.getItem(storageKey));
    },

    async loadEncryptedToken(storageKey) {
        const storedValue = localStorage.getItem(storageKey);
        if (!storedValue) return '';

        if (!this.isEncryptedTokenRecord(storedValue)) {
            localStorage.removeItem(storageKey);
            return '';
        }

        try {
            const record = JSON.parse(storedValue);
            return await this.decryptToken(record);
        } catch (error) {
            console.warn('Failed to read the local encrypted credentials; the invalid credentials have been cleared', error);
            localStorage.removeItem(storageKey);
            return '';
        }
    },

    async setEncryptedToken(storageKey, cacheName, token) {
        const record = await this.encryptToken(token);
        localStorage.setItem(storageKey, JSON.stringify(record));
        this._tokenCache[cacheName] = token;
    },

    isEncryptedTokenRecord(value) {
        if (!value) return false;
        try {
            const record = JSON.parse(value);
            return record?.v === AUTH_TOKEN_RECORD_VERSION
                && record?.alg === 'AES-GCM'
                && typeof record?.iv === 'string'
                && typeof record?.data === 'string';
        } catch (_error) {
            return false;
        }
    },

    async encryptToken(token) {
        const key = await this.getOrCreateCryptoKey();
        const iv = window.crypto.getRandomValues(new Uint8Array(12));
        const encodedToken = new TextEncoder().encode(token);
        const encrypted = await window.crypto.subtle.encrypt(
            { name: 'AES-GCM', iv },
            key,
            encodedToken
        );

        return {
            v: AUTH_TOKEN_RECORD_VERSION,
            alg: 'AES-GCM',
            iv: this.arrayBufferToBase64(iv),
            data: this.arrayBufferToBase64(encrypted)
        };
    },

    async decryptToken(record) {
        const key = await this.getOrCreateCryptoKey();
        const iv = new Uint8Array(this.base64ToArrayBuffer(record.iv));
        const encrypted = this.base64ToArrayBuffer(record.data);
        const decrypted = await window.crypto.subtle.decrypt(
            { name: 'AES-GCM', iv },
            key,
            encrypted
        );
        return new TextDecoder().decode(decrypted);
    },

    async getOrCreateCryptoKey() {
        this.assertCryptoStorageAvailable();
        if (!this._cryptoKeyPromise) {
            this._cryptoKeyPromise = (async () => {
                const storedKey = await this.readStoredCryptoKey();
                if (storedKey) return storedKey;

                const newKey = await window.crypto.subtle.generateKey(
                    { name: 'AES-GCM', length: 256 },
                    false,
                    ['encrypt', 'decrypt']
                );
                await this.writeStoredCryptoKey(newKey);
                return newKey;
            })();
        }
        return this._cryptoKeyPromise;
    },

    assertCryptoStorageAvailable() {
        if (!window.crypto?.subtle || !window.indexedDB) {
            throw new Error('The current browser does not support Web Crypto / IndexedDB; credentials cannot be saved encrypted');
        }
    },

    async readStoredCryptoKey() {
        return await this.withCryptoStore('readonly', (store) => store.get(AUTH_CRYPTO_KEY_ID));
    },

    async writeStoredCryptoKey(key) {
        await this.withCryptoStore('readwrite', (store) => store.put(key, AUTH_CRYPTO_KEY_ID));
    },

    async deleteStoredCryptoKey() {
        if (!window.indexedDB) return;
        await this.withCryptoStore('readwrite', (store) => store.delete(AUTH_CRYPTO_KEY_ID));
    },

    async withCryptoStore(mode, action) {
        const db = await this.openCryptoDb();
        return await new Promise((resolve, reject) => {
            const transaction = db.transaction(AUTH_CRYPTO_STORE, mode);
            const store = transaction.objectStore(AUTH_CRYPTO_STORE);
            const request = action(store);
            let result;

            request.onsuccess = () => {
                result = request.result;
            };
            request.onerror = () => reject(request.error);
            transaction.oncomplete = () => {
                db.close();
                resolve(result);
            };
            transaction.onerror = () => {
                db.close();
                reject(transaction.error);
            };
        });
    },

    async openCryptoDb() {
        return await new Promise((resolve, reject) => {
            const request = window.indexedDB.open(AUTH_CRYPTO_DB, 1);

            request.onupgradeneeded = () => {
                const db = request.result;
                if (!db.objectStoreNames.contains(AUTH_CRYPTO_STORE)) {
                    db.createObjectStore(AUTH_CRYPTO_STORE);
                }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    },

    arrayBufferToBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = '';
        bytes.forEach((byte) => {
            binary += String.fromCharCode(byte);
        });
        return window.btoa(binary);
    },

    base64ToArrayBuffer(value) {
        const binary = window.atob(value);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes.buffer;
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
                                console.error('Failed to save the encrypted credentials', error);
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
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
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
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
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
        return await response.json();
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
        return await response.json();
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
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    async replayDeadLettersByIds(eventIds) {
        const response = await this.authenticatedFetch('/v1/admin/dead-letters/replay-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_ids: eventIds })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    },

    async replayAllDeadLetters(batchSize) {
        const q = batchSize ? '?batch_size=' + encodeURIComponent(batchSize) : '';
        const response = await this.authenticatedFetch('/v1/admin/dead-letters/replay-all' + q, { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    }
};
