/**
 * Inbound Setup — guided source credential creation and end-to-end validation.
 *
 * Source credentials are intentionally kept only in module memory. The server
 * returns the clear token on create/rotate only, and this module never persists
 * it to localStorage, sessionStorage, logs, or a query string.
 */
const IngressSetupModule = (function () {
    'use strict';

    const POLL_INTERVAL_MS = 2500;
    const POLL_LIMIT = 48;
    let sourceTypes = [];
    let existingSources = [];
    let selectedType = '';
    let connection = null;
    let setup = null;
    let sourceToken = '';
    let currentStep = 1;
    let pollCount = 0;
    let pollTimer = null;
    let dryRunResult = null;
    let forwardingResult = null;

    function sourceTypeValue(item) {
        if (typeof item === 'string') return item;
        return String(item && (item.id || item.source_type || item.value) || '');
    }

    function sourceTypeLabel(item) {
        if (typeof item === 'string') return item;
        return String(item && (item.name || item.label || item.id || item.source_type) || '');
    }

    function sourceTypeDescription(item) {
        return typeof item === 'object' && item
            ? String(item.description || item.hint || '')
            : '';
    }

    function responseData(payload) {
        return payload && payload.data || {};
    }

    function responseConnection(payload) {
        const data = responseData(payload);
        return data.connection || data.source || data;
    }

    function responseSetup(payload) {
        const data = responseData(payload);
        return data.setup || {};
    }

    function clearPoll() {
        if (pollTimer) window.clearTimeout(pollTimer);
        pollTimer = null;
    }

    function setStatus(message, kind) {
        const element = document.getElementById('ingressWizardStatus');
        if (!element) return;
        element.textContent = message || '';
        element.className = 'ingress-status' + (kind ? ' is-' + kind : '');
    }

    function setBusy(button, busy) {
        if (!button) return;
        button.disabled = !!busy;
        button.classList.toggle('is-busy', !!busy);
    }

    function stepper() {
        const labels = [
            t('ingress.step.source'),
            t('ingress.step.credential'),
            t('ingress.step.configure'),
            t('ingress.step.payload'),
            t('ingress.step.firstEvent'),
            t('ingress.step.forward')
        ];
        return '<ol class="ingress-stepper">' + labels.map(function (label, index) {
            const step = index + 1;
            const state = step < currentStep ? ' is-complete' : (step === currentStep ? ' is-current' : '');
            return '<li class="' + state + '"><span>' + (step < currentStep ? wwIcon('check') : step) +
                '</span><strong>' + escapeHtml(label) + '</strong></li>';
        }).join('') + '</ol>';
    }

    function typeCards() {
        if (!sourceTypes.length) {
            return '<div class="ingress-source-types"><button class="ingress-source-type is-selected" ' +
                'type="button" data-source-type="generic"><strong>' + wwIcon('link') + ' Generic JSON</strong></button></div>';
        }
        return '<div class="ingress-source-types">' + sourceTypes.map(function (item) {
            const value = sourceTypeValue(item);
            const selected = value === selectedType ? ' is-selected' : '';
            return '<button class="ingress-source-type' + selected + '" type="button" data-source-type="' +
                escapeHtml(value) + '"><strong>' + escapeHtml(sourceTypeLabel(item)) + '</strong>' +
                (sourceTypeDescription(item) ? '<span>' +
                    escapeHtml(sourceTypeDescription(item)) + '</span>' : '') + '</button>';
        }).join('') + '</div>';
    }

    function existingSourceCards() {
        if (!existingSources.length) return '';
        return '<details class="ingress-existing"><summary>' +
            escapeHtml(t('ingress.existing.title', { value: existingSources.length })) +
            '</summary><div>' + existingSources.map(function (item) {
                const status = item.onboarding_status || (item.first_event_at ? 'connected' : 'waiting_for_event');
                const credentialState = item.credential_state ||
                    (item.enabled === false ? 'disabled' : 'active');
                return '<button type="button" data-existing-source="' + escapeHtml(String(item.id)) + '">' +
                    '<span><strong>' + escapeHtml(item.name || item.source_type || '') + '</strong>' +
                    '<small>' + escapeHtml(item.source_type || '') + ' · ' +
                    escapeHtml(t('ingress.connection.' + status)) + ' · ' +
                    escapeHtml(t('ingress.credentialState.' + credentialState)) +
                    '</small></span><span>→</span></button>';
            }).join('') + '</div></details>';
    }

    function renderSourceStep() {
        return '<section class="ingress-step-card"><div class="ingress-step-heading"><span>1</span><div><h3>' +
            escapeHtml(t('ingress.source.title')) + '</h3><p>' +
            escapeHtml(t('ingress.source.hint')) + '</p></div></div>' +
            typeCards() +
            '<label class="form-group"><span class="form-label">' +
            escapeHtml(t('ingress.source.name')) +
            '</span><input class="form-input" id="ingressSourceName" type="text" maxlength="100" ' +
            'placeholder="' + escapeHtml(t('ingress.source.namePlaceholder')) + '"></label>' +
            '<div class="ingress-step-actions"><button class="btn btn-primary" id="ingressCreateSourceBtn" ' +
            'type="button">' + escapeHtml(t('ingress.source.create')) + '</button></div>' +
            existingSourceCards() + '</section>';
    }

    function webhookUrl() {
        if (!connection) return '';
        return String(
            setup && (setup.webhook_url || setup.url || setup.endpoint) ||
            window.location.origin + '/v1/source-webhooks/' + connection.public_id
        );
    }

    function authorizationCredentials() {
        return String(
            setup && setup.authorization && setup.authorization.credentials ||
            sourceToken ||
            ''
        );
    }

    function configurationText() {
        if (setup) {
            const value = setup.configuration || setup.config || setup.curl || setup.example;
            if (typeof value === 'string' && value) return value;
            if (value && typeof value === 'object') return JSON.stringify(value, null, 2);
        }
        const token = authorizationCredentials() || '<SOURCE_TOKEN>';
        return 'curl -X POST ' + webhookUrl() + '\n' +
            '  -H "Authorization: Bearer ' + token + '"\n' +
            '  -H "Content-Type: application/json"\n' +
            '  -d \'{"event":"alert","severity":"warning","message":"WebhookWise onboarding test"}\'';
    }

    function copyField(label, value, sensitive) {
        return '<div class="ingress-copy-field' + (sensitive ? ' is-sensitive' : '') + '"><span>' +
            escapeHtml(label) + '</span><div><code>' + escapeHtml(value) +
            '</code><button class="btn btn-sm" type="button" data-copy-value="' +
            escapeHtml(encodeURIComponent(value)) + '">' + escapeHtml(t('ingress.copy')) +
            '</button></div></div>';
    }

    function renderCredentialStep() {
        const token = authorizationCredentials();
        return '<section class="ingress-step-card"><div class="ingress-step-heading"><span>2</span><div><h3>' +
            escapeHtml(t('ingress.credential.title')) + '</h3><p>' +
            escapeHtml(t('ingress.credential.once')) + '</p></div></div>' +
            '<div class="ingress-token-warning">' + wwIcon('alert-triangle') + ' ' +
            escapeHtml(t('ingress.credential.warning')) + '</div>' +
            copyField(t('ingress.webhookUrl'), webhookUrl(), false) +
            copyField(t('ingress.authorization'), 'Authorization: Bearer ' + token, true) +
            '<div class="ingress-step-actions"><button class="btn btn-primary" type="button" ' +
            'data-ingress-next="3">' + escapeHtml(t('common.continue')) + '</button></div></section>';
    }

    function renderConfigureStep() {
        return '<section class="ingress-step-card"><div class="ingress-step-heading"><span>3</span><div><h3>' +
            escapeHtml(t('ingress.configure.title')) + '</h3><p>' +
            escapeHtml(t('ingress.configure.hint')) + '</p></div></div>' +
            '<div class="ingress-config-block"><pre><code>' +
            escapeHtml(configurationText()) + '</code></pre><button class="btn btn-sm" type="button" ' +
            'data-copy-value="' + escapeHtml(encodeURIComponent(configurationText())) + '">' +
            escapeHtml(t('ingress.copyConfig')) + '</button></div>' +
            '<div class="ingress-step-actions"><button class="btn" type="button" data-ingress-back="2">← ' +
            escapeHtml(t('common.back')) + '</button><button class="btn btn-primary" type="button" ' +
            'data-ingress-next="4">' + escapeHtml(t('common.continue')) + '</button></div></section>';
    }

    function selectedSourceType() {
        return selectedType || connection && connection.source_type || 'generic';
    }

    function samplePayload() {
        const definition = sourceTypes.find(function (item) {
            return sourceTypeValue(item) === selectedSourceType();
        });
        const sample = definition && typeof definition === 'object'
            ? definition.sample_payload || definition.sample || definition.example_payload
            : null;
        return JSON.stringify(sample || {
            event: 'alert',
            severity: 'warning',
            message: 'WebhookWise onboarding test',
            service: connection && connection.name || 'demo-service'
        }, null, 2);
    }

    function renderPayloadStep() {
        const dryRun = dryRunResult
            ? '<div class="ingress-test-result is-success">' + wwIcon('check') + ' ' +
                escapeHtml(t('ingress.payload.valid')) + '</div>'
            : '';
        return '<section class="ingress-step-card"><div class="ingress-step-heading"><span>4</span><div><h3>' +
            escapeHtml(t('ingress.payload.title')) + '</h3><p>' +
            escapeHtml(t('ingress.payload.hint')) + '</p></div></div>' +
            '<textarea class="form-input ingress-payload" id="ingressPayload" rows="14" spellcheck="false">' +
            escapeHtml(samplePayload()) + '</textarea>' + dryRun +
            (!sourceToken ? '<div class="ingress-token-warning">' + wwIcon('alert-triangle') + ' ' +
                escapeHtml(t('ingress.payload.rotateNeeded')) + '</div>' : '') +
            '<div class="ingress-step-actions"><button class="btn" type="button" id="ingressDryRunBtn">' + wwIcon('flask') + ' ' +
            escapeHtml(t('ingress.payload.dryRun')) + '</button>' +
            (!sourceToken
                ? '<button class="btn" type="button" id="ingressRotateBtn">↻ ' +
                    escapeHtml(t('ingress.credential.rotate')) + '</button>'
                : '<button class="btn btn-primary" type="button" id="ingressSendEventBtn">' +
                    escapeHtml(t('ingress.payload.send')) + '</button>') +
            '</div></section>';
    }

    function statusConnection(data) {
        return data && (data.connection || data.source || data) || {};
    }

    function renderFirstEventStep() {
        const connected = connection && (
            connection.onboarding_status === 'connected' || connection.first_event_at
        );
        return '<section class="ingress-step-card"><div class="ingress-step-heading"><span>5</span><div><h3>' +
            escapeHtml(t('ingress.firstEvent.title')) + '</h3><p>' +
            escapeHtml(connected ? t('ingress.firstEvent.connected') : t('ingress.firstEvent.waiting')) +
            '</p></div></div><div class="ingress-connection-state' +
            (connected ? ' is-connected' : '') + '"><span>' + (connected ? wwIcon('check') : '') +
            '</span><div><strong>' +
            escapeHtml(t('ingress.connection.' + (connected ? 'connected' : 'waiting_for_event'))) +
            '</strong><small>' +
            escapeHtml(connection && (connection.last_request_id || connection.first_event_at) || '') +
            '</small></div></div>' +
            '<div class="ingress-step-actions"><button class="btn" type="button" id="ingressPollBtn">' + wwIcon('refresh') + ' ' +
            escapeHtml(t('ingress.firstEvent.check')) + '</button>' +
            (connected ? '<button class="btn btn-primary" type="button" data-ingress-next="6">' +
                escapeHtml(t('common.continue')) + '</button>' : '') +
            '</div></section>';
    }

    function renderForwardStep() {
        const result = forwardingResult
            ? '<div class="ingress-test-result is-success">' + wwIcon('check') + ' ' +
                escapeHtml(t('ingress.forward.success')) + '</div>'
            : '';
        return '<section class="ingress-step-card"><div class="ingress-step-heading"><span>6</span><div><h3>' +
            escapeHtml(t('ingress.forward.title')) + '</h3><p>' +
            escapeHtml(t('ingress.forward.hint')) + '</p></div></div>' +
            '<div class="resolution-form-grid"><label class="form-group"><span class="form-label">' +
            escapeHtml(t('ingress.forward.channel')) +
            '</span><select class="form-input" id="ingressForwardTemplate">' +
            '<option value="feishu">Feishu</option><option value="generic_webhook">Webhook</option>' +
            '<option value="openclaw">OpenClaw</option></select></label>' +
            '<label class="form-group"><span class="form-label">' +
            escapeHtml(t('ingress.forward.url')) +
            '</span><input class="form-input" id="ingressForwardUrl" type="url" ' +
            'placeholder="https://..."></label></div>' + result +
            '<div class="ingress-step-actions"><button class="btn btn-primary" type="button" ' +
            'id="ingressForwardTestBtn">' + escapeHtml(t('ingress.forward.test')) +
            '</button></div></section>';
    }

    function renderConnectionManagement() {
        if (!connection || !connection.id || sourceToken) return '';
        const revoked = connection.enabled === false || connection.credential_state === 'revoked' ||
            Boolean(connection.revoked_at);
        return '<details class="ingress-connection-management"><summary>' +
            escapeHtml(t('ingress.manage.title')) + '</summary><div><p>' +
            escapeHtml(revoked ? t('ingress.manage.revokedHint') : t('ingress.manage.hint')) +
            '</p><div class="ingress-step-actions">' +
            '<button class="btn" type="button" id="ingressManageRotateBtn">↻ ' +
            escapeHtml(revoked ? t('ingress.manage.reconnect') : t('ingress.credential.rotate')) +
            '</button><button class="btn ingress-revoke-btn" type="button" id="ingressRevokeBtn"' +
            (revoked ? ' disabled' : '') + '>× ' + escapeHtml(t('ingress.manage.revoke')) +
            '</button></div></div></details>';
    }

    function currentContent() {
        var content;
        if (currentStep === 2) content = renderCredentialStep();
        else if (currentStep === 3) content = renderConfigureStep();
        else if (currentStep === 4) content = renderPayloadStep();
        else if (currentStep === 5) content = renderFirstEventStep();
        else if (currentStep === 6) content = renderForwardStep();
        else content = renderSourceStep();
        return content + (currentStep === 1 || currentStep === 2 ? '' : renderConnectionManagement());
    }

    function render() {
        const container = document.getElementById('ingressSetupWizard');
        if (!container) return;
        container.innerHTML = stepper() + '<div class="ingress-wizard-body">' +
            currentContent() + '<div id="ingressWizardStatus" class="ingress-status" role="status"></div></div>';
        bindRenderedActions();
    }

    function payloadFromEditor() {
        const editor = document.getElementById('ingressPayload');
        let payload;
        try {
            payload = JSON.parse(editor && editor.value || '');
        } catch (_error) {
            throw new Error(t('ingress.payload.invalid'));
        }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
            throw new Error(t('ingress.payload.invalid'));
        }
        return payload;
    }

    async function createSource(button) {
        const name = String(document.getElementById('ingressSourceName')?.value || '').trim();
        if (!selectedType || !name) {
            setStatus(t('ingress.source.required'), 'error');
            return;
        }
        setBusy(button, true);
        try {
            const payload = await API.createInboundSource({
                name: name,
                source_type: selectedType,
                actor: 'dashboard'
            });
            connection = responseConnection(payload);
            setup = responseSetup(payload);
            sourceToken = authorizationCredentials();
            currentStep = 2;
            render();
        } catch (error) {
            setStatus(error.message || String(error), 'error');
            setBusy(button, false);
        }
    }

    async function selectExisting(id) {
        clearPoll();
        const payload = await API.getInboundSource(id);
        connection = responseConnection(payload);
        selectedType = connection.source_type || '';
        setup = null;
        sourceToken = '';
        currentStep = connection.first_event_at ? 6 : 4;
        render();
    }

    async function rotateCredential(button) {
        if (!connection || !connection.id) return;
        setBusy(button, true);
        try {
            const payload = await API.rotateInboundSource(connection.id, 'dashboard');
            connection = responseConnection(payload);
            setup = responseSetup(payload);
            sourceToken = authorizationCredentials();
            currentStep = 2;
            render();
        } catch (error) {
            setStatus(error.message || String(error), 'error');
            setBusy(button, false);
        }
    }

    async function dryRun(button) {
        setBusy(button, true);
        try {
            const payload = payloadFromEditor();
            dryRunResult = await API.testWebhookPayload(selectedSourceType(), payload);
            const editorValue = document.getElementById('ingressPayload').value;
            render();
            document.getElementById('ingressPayload').value = editorValue;
            setStatus(t('ingress.payload.valid'), 'success');
        } catch (error) {
            setStatus(error.message || String(error), 'error');
            setBusy(button, false);
        }
    }

    async function sendEvent(button) {
        if (!connection || !connection.public_id || !sourceToken) {
            setStatus(t('ingress.payload.rotateNeeded'), 'error');
            return;
        }
        setBusy(button, true);
        try {
            const payload = payloadFromEditor();
            await API.sendInboundSourceEvent(connection.public_id, sourceToken, payload);
            // Clear the cleartext credential as soon as it is no longer needed.
            sourceToken = '';
            setup = null;
            currentStep = 5;
            pollCount = 0;
            render();
            await pollStatus();
        } catch (error) {
            setStatus(error.message || String(error), 'error');
            setBusy(button, false);
        }
    }

    async function pollStatus() {
        clearPoll();
        if (!connection || !connection.id) return;
        try {
            const payload = await API.getInboundSourceStatus(connection.id);
            connection = Object.assign({}, connection, statusConnection(responseData(payload)));
            render();
            const connected = connection.onboarding_status === 'connected' || connection.first_event_at;
            if (!connected && pollCount < POLL_LIMIT) {
                pollCount += 1;
                pollTimer = window.setTimeout(pollStatus, POLL_INTERVAL_MS);
            } else if (!connected) {
                setStatus(t('ingress.firstEvent.timeout'), 'error');
            }
        } catch (error) {
            setStatus(error.message || String(error), 'error');
        }
    }

    async function testForward(button) {
        const template = document.getElementById('ingressForwardTemplate')?.value || 'generic_webhook';
        const url = String(document.getElementById('ingressForwardUrl')?.value || '').trim();
        if (template !== 'openclaw' && !url) {
            setStatus(t('ingress.forward.urlRequired'), 'error');
            return;
        }
        setBusy(button, true);
        try {
            forwardingResult = await API.testIntegrationTarget(template, url);
            render();
            setStatus(t('ingress.forward.success'), 'success');
        } catch (error) {
            setStatus(error.message || String(error), 'error');
            setBusy(button, false);
        }
    }

    async function revokeCredential(button) {
        if (!connection || !connection.id || connection.enabled === false) return;
        if (!window.confirm(t('ingress.manage.revokeConfirm'))) return;
        setBusy(button, true);
        try {
            const payload = await API.revokeInboundSource(connection.id, 'dashboard');
            connection = responseConnection(payload);
            sourceToken = '';
            setup = null;
            render();
            setStatus(t('ingress.manage.revoked'), 'success');
        } catch (error) {
            setStatus(error.message || String(error), 'error');
            setBusy(button, false);
        }
    }

    function copyValue(button) {
        const value = decodeURIComponent(button.getAttribute('data-copy-value') || '');
        navigator.clipboard.writeText(value).then(function () {
            const original = button.textContent;
            button.textContent = t('ingress.copied');
            window.setTimeout(function () { button.textContent = original; }, 1500);
        }).catch(function () {
            setStatus(t('ingress.copyFailed'), 'error');
        });
    }

    function bindRenderedActions() {
        const container = document.getElementById('ingressSetupWizard');
        if (!container) return;
        container.querySelectorAll('[data-source-type]').forEach(function (button) {
            button.addEventListener('click', function () {
                selectedType = button.getAttribute('data-source-type') || '';
                render();
            });
        });
        container.querySelectorAll('[data-existing-source]').forEach(function (button) {
            button.addEventListener('click', function () {
                selectExisting(button.getAttribute('data-existing-source')).catch(function (error) {
                    setStatus(error.message || String(error), 'error');
                });
            });
        });
        container.querySelectorAll('[data-copy-value]').forEach(function (button) {
            button.addEventListener('click', function () { copyValue(button); });
        });
        container.querySelectorAll('[data-ingress-next]').forEach(function (button) {
            button.addEventListener('click', function () {
                currentStep = Number(button.getAttribute('data-ingress-next')) || currentStep;
                render();
            });
        });
        container.querySelectorAll('[data-ingress-back]').forEach(function (button) {
            button.addEventListener('click', function () {
                currentStep = Number(button.getAttribute('data-ingress-back')) || currentStep;
                render();
            });
        });
        document.getElementById('ingressCreateSourceBtn')?.addEventListener('click', function (event) {
            createSource(event.currentTarget);
        });
        document.getElementById('ingressRotateBtn')?.addEventListener('click', function (event) {
            rotateCredential(event.currentTarget);
        });
        document.getElementById('ingressManageRotateBtn')?.addEventListener('click', function (event) {
            rotateCredential(event.currentTarget);
        });
        document.getElementById('ingressRevokeBtn')?.addEventListener('click', function (event) {
            revokeCredential(event.currentTarget);
        });
        document.getElementById('ingressDryRunBtn')?.addEventListener('click', function (event) {
            dryRun(event.currentTarget);
        });
        document.getElementById('ingressSendEventBtn')?.addEventListener('click', function (event) {
            sendEvent(event.currentTarget);
        });
        document.getElementById('ingressPollBtn')?.addEventListener('click', function () {
            pollCount = 0;
            pollStatus();
        });
        document.getElementById('ingressForwardTestBtn')?.addEventListener('click', function (event) {
            testForward(event.currentTarget);
        });
    }

    async function load() {
        clearPoll();
        const container = document.getElementById('ingressSetupWizard');
        if (!container) return;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' +
            escapeHtml(t('common.loading')) + '</p></div>';
        try {
            const results = await Promise.all([
                API.getInboundSourceTypes(),
                API.getInboundSources()
            ]);
            const typesData = responseData(results[0]);
            const sourcesData = responseData(results[1]);
            sourceTypes = Array.isArray(typesData) ? typesData : (typesData.items || typesData.source_types || []);
            existingSources = Array.isArray(sourcesData) ? sourcesData : (sourcesData.items || sourcesData.sources || []);
            if (!selectedType && sourceTypes.length) selectedType = sourceTypeValue(sourceTypes[0]);
            if (!selectedType) selectedType = 'generic';
            render();
        } catch (error) {
            container.innerHTML = '<div class="empty-state response-empty is-error">' +
                escapeHtml(t('common.loadFailed')) + ': ' +
                escapeHtml(error.message || String(error)) + '</div>';
        }
    }

    function reset() {
        clearSensitive();
        connection = null;
        currentStep = 1;
        pollCount = 0;
        dryRunResult = null;
        forwardingResult = null;
        load();
    }

    function clearSensitive() {
        clearPoll();
        const discardedOneTimeToken = Boolean(
            sourceToken ||
            setup && setup.authorization && setup.authorization.credentials &&
            setup.authorization.credentials !== '<rotate-token-to-reveal>'
        );
        setup = null;
        sourceToken = '';
        if (discardedOneTimeToken && connection && currentStep < 4) {
            currentStep = 4;
        }
    }

    return {
        load: load,
        reset: reset,
        clearSensitive: clearSensitive,
        deactivate: clearSensitive
    };
})();
