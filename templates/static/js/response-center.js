/**
 * Response Center — compact incident work queue and read-only knowledge gaps.
 *
 * The work queue deliberately renders only the context needed to choose the
 * next operator action. Full evidence stays in the incident view.
 */
const ResponseCenterModule = (function () {
    'use strict';

    const OPERATOR_STORAGE_KEY = 'webhookwise_operator_name';
    const WORK_QUEUE_PAGE_SIZE = 30;
    let currentBucket = 'active';
    let initialized = false;
    let workQueueItems = [];
    let workQueueMeta = {};

    function dataItems(payload) {
        const data = payload && payload.data;
        if (Array.isArray(data)) return data;
        if (!data || typeof data !== 'object') return [];
        return data.items || data.incidents || data.gaps || [];
    }

    function operatorName() {
        const input = document.getElementById('responseQueueActor');
        return String(input && input.value || '').trim();
    }

    function savedOperatorName() {
        try {
            return window.localStorage.getItem(OPERATOR_STORAGE_KEY) || '';
        } catch (_error) {
            return '';
        }
    }

    function saveOperatorName(value) {
        try {
            if (value) {
                window.localStorage.setItem(OPERATOR_STORAGE_KEY, value);
            } else {
                window.localStorage.removeItem(OPERATOR_STORAGE_KEY);
            }
        } catch (_error) {
            // A blocked localStorage must not make the work queue unusable.
        }
    }

    function displayValue(value) {
        if (value === null || value === undefined || value === '') return '';
        if (Array.isArray(value)) return value.map(displayValue).filter(Boolean).join(', ');
        if (typeof value === 'object') {
            return displayValue(value.summary || value.label || value.title || value.level || value.value);
        }
        return String(value);
    }

    function incidentId(item) {
        return Number(item.incident_id || item.id || 0);
    }

    function serviceName(item) {
        return displayValue(item.service || item.service_name || item.resource || item.title) ||
            t('response.queue.unknownService');
    }

    function impactText(item) {
        const impact = displayValue(
            item.impact_summary || item.impact || item.top_importance || item.importance || item.severity
        );
        return impact || t('response.queue.impactUnknown');
    }

    function ownerText(item) {
        const owner = displayValue(item.assignee || item.owner);
        const team = displayValue(item.team);
        if (owner && team) return owner + ' / ' + team;
        return owner || team || t('incidents.unassigned');
    }

    function durationLabel(milliseconds) {
        const totalMinutes = Math.max(0, Math.round(Math.abs(milliseconds) / 60000));
        if (totalMinutes < 60) return t('response.queue.minutes', { value: totalMinutes });
        const hours = Math.floor(totalMinutes / 60);
        const minutes = totalMinutes % 60;
        return t('response.queue.hours', {
            hours: hours,
            minutes: minutes
        });
    }

    function countdownMs(item) {
        if (Number.isFinite(Number(item.sla_minutes_remaining))) {
            return Number(item.sla_minutes_remaining) * 60000;
        }
        const deadline = item.sla_due_at || item.deadline || item.recovery_check_due_at;
        if (!deadline) return null;
        const dueAt = Date.parse(deadline);
        if (!Number.isFinite(dueAt)) return null;
        return dueAt - Date.now();
    }

    function countdown(item) {
        const ms = countdownMs(item);
        if (ms === null) {
            const deadline = item.sla_due_at || item.deadline || item.recovery_check_due_at;
            return deadline ? displayValue(deadline) : t('incidents.notSet');
        }
        return ms < 0
            ? t('response.queue.overdue', { value: durationLabel(ms) })
            : t('response.queue.dueIn', { value: durationLabel(ms) });
    }

    // Tone matches the clock: overdue (or due right now) is red, the final
    // 30 minutes amber, anything else neutral.
    function countdownTone(item) {
        const ms = countdownMs(item);
        if (ms === null) return '';
        if (ms <= 0) return ' is-overdue';
        if (ms <= 30 * 60000) return ' is-soon';
        return '';
    }

    function nextAction(item) {
        const supplied = item.next_action;
        if (supplied && typeof supplied === 'object') {
            const code = supplied.code || supplied.action || supplied.key || supplied.type || 'investigate';
            const labels = {
                acknowledge: 'response.action.acknowledge',
                assign_owner: 'response.action.assign',
                confirm_recovery: 'response.action.verifyRecovery',
                escalate_response: 'response.action.escalate',
                investigate: 'response.action.investigate'
            };
            return {
                action: code,
                label: labels[code] ? t(labels[code]) :
                    (displayValue(supplied.label || supplied.title) || t('response.action.open'))
            };
        }
        if (typeof supplied === 'string' && supplied) {
            return {
                action: supplied,
                label: t('response.action.' + supplied)
            };
        }
        if (currentBucket === 'unassigned' || !displayValue(item.assignee || item.owner || item.team)) {
            return { action: 'assign', label: t('response.action.assign') };
        }
        if (currentBucket === 'needs_recovery') {
            return { action: 'verify_recovery', label: t('response.action.verifyRecovery') };
        }
        if ((item.workflow_status || 'open') === 'open') {
            return { action: 'acknowledge', label: t('response.action.acknowledge') };
        }
        return { action: 'open', label: t('response.action.continue') };
    }

    function priorityReason(reason) {
        if (!reason || !reason.code) return '';
        return t('response.queue.reason.' + reason.code, {
            value: displayValue(reason.value)
        });
    }

    // Counts render INSIDE the bucket pills (one row of taxonomy, not two):
    // the old summary strip repeated every bucket name right under the
    // buttons that already said them.
    function renderQueueSummary(meta) {
        const summary = meta.summary || {};
        const counts = summary.counts || {};
        document.querySelectorAll('[data-bucket-count]').forEach(function (el) {
            const key = el.getAttribute('data-bucket-count');
            const n = counts[key];
            if (n === null || n === undefined) { el.hidden = true; return; }
            el.textContent = String(n);
            el.hidden = false;
        });
        return summary.truncated
            ? '<div class="response-queue-summary"><em>' + wwIcon('alert-triangle') + ' ' + escapeHtml(t('response.queue.truncated')) + '</em></div>'
            : '';
    }

    function renderWorkQueue(items, meta) {
        const container = document.getElementById('responseWorkQueueList');
        if (!container) return;
        if (!items.length) {
            // Still refresh the pill counts: this bucket being empty says
            // nothing about the others.
            renderQueueSummary(meta || {});
            container.innerHTML = '<div class="empty-state response-empty">' +
                '<div class="empty-icon">' + wwIcon('check') + '</div><div class="empty-title">' +
                escapeHtml(t('response.queue.emptyTitle')) + '</div><div class="empty-text">' +
                escapeHtml(t('response.queue.emptyText')) + '</div></div>';
            return;
        }

        container.innerHTML = renderQueueSummary(meta || {}) +
            '<div class="response-queue-list">' + items.map(function (item) {
            const id = incidentId(item);
            const action = nextAction(item);
            const urgency = String(item.urgency || item.top_importance || item.importance || '').toLowerCase();
            const urgencyClass = ['high', 'critical'].includes(urgency) ? ' is-critical' :
                (urgency === 'medium' || urgency === 'warning' ? ' is-warning' : '');
            const reasons = (item.priority_reasons || []).slice(0, 2)
                .map(priorityReason).filter(Boolean);
            return '<article class="response-queue-row' + urgencyClass + '" data-incident-id="' + id + '">' +
                '<div class="response-queue-cell response-queue-service"><span>' +
                escapeHtml(t('response.queue.service')) + '</span><strong>' +
                escapeHtml(serviceName(item)) + '</strong></div>' +
                '<div class="response-queue-cell"><span>' +
                escapeHtml(t('response.queue.impact')) + '</span><strong>' +
                escapeHtml(impactText(item)) + '</strong></div>' +
                '<div class="response-queue-cell"><span>' +
                escapeHtml(t('incidents.owner')) + '</span><strong>' +
                escapeHtml(ownerText(item)) + '</strong></div>' +
                '<div class="response-queue-cell response-queue-countdown' + countdownTone(item) + '"><span>' +
                escapeHtml(t('incidents.sla')) + '</span><strong>' +
                escapeHtml(countdown(item)) + '</strong></div>' +
                '<button class="btn btn-primary response-next-action" type="button" data-response-action="' +
                escapeHtml(action.action) + '">' + escapeHtml(action.label) + '</button>' +
                '<div class="response-priority-explain"><span>' +
                escapeHtml(t('response.queue.priorityScore', {
                    value: Math.round(Number(item.priority_score || 0))
                })) + '</span>' + reasons.map(function (reason) {
                    return '<small>' + escapeHtml(reason) + '</small>';
                }).join('') + '</div></article>';
        }).join('') + '</div>' +
            (meta && meta.has_more ? '<div class="response-load-more"><button class="btn" type="button" ' +
                'id="responseQueueLoadMore">' + escapeHtml(t('response.queue.loadMore')) +
                '</button><span>' + escapeHtml(t('response.queue.showing', {
                    shown: items.length,
                    total: meta.total_matches == null ? '—' : meta.total_matches
                })) + '</span></div>' : '');

        container.querySelectorAll('[data-response-action]').forEach(function (button) {
            button.addEventListener('click', function () {
                runNextAction(button).catch(function (error) {
                    button.disabled = false;
                    button.classList.remove('is-busy');
                    window.alert(t('response.action.failed') + ': ' + (error.message || String(error)));
                });
            });
        });
        document.getElementById('responseQueueLoadMore')?.addEventListener('click', function () {
            loadWorkQueue(true);
        });
    }

    async function loadWorkQueue(append) {
        const container = document.getElementById('responseWorkQueueList');
        if (!container) return;
        const shouldAppend = append === true;
        const actor = operatorName();
        saveOperatorName(actor);
        if (currentBucket === 'my' && !actor) {
            container.innerHTML = '<div class="empty-state response-empty">' +
                '<div class="empty-icon">' + wwIcon('user') + '</div><div class="empty-title">' +
                escapeHtml(t('response.queue.actorNeededTitle')) + '</div><div class="empty-text">' +
                escapeHtml(t('response.queue.actorNeededText')) + '</div></div>';
            document.getElementById('responseQueueActor')?.focus();
            return;
        }
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' +
            escapeHtml(t('common.loading')) + '</p></div>';
        try {
            const payload = await API.getResponseWorkQueue({
                bucket: currentBucket,
                actor: actor,
                limit: WORK_QUEUE_PAGE_SIZE,
                offset: shouldAppend ? (workQueueMeta.next_offset ?? workQueueItems.length) : 0
            });
            const data = payload.data || {};
            const incoming = dataItems(payload);
            workQueueItems = shouldAppend ? workQueueItems.concat(incoming) : incoming;
            workQueueMeta = {
                summary: data.summary || {},
                has_more: Boolean(data.has_more),
                next_offset: data.next_offset,
                total_matches: data.total_matches
            };
            renderWorkQueue(workQueueItems, workQueueMeta);
        } catch (error) {
            container.innerHTML = '<div class="empty-state response-empty is-error">' +
                escapeHtml(t('common.loadFailed')) + ': ' +
                escapeHtml(error.message || String(error)) + '</div>';
        }
    }

    function setBucket(bucket) {
        currentBucket = bucket || 'active';
        workQueueItems = [];
        workQueueMeta = {};
        document.querySelectorAll('[data-response-bucket]').forEach(function (button) {
            const selected = button.getAttribute('data-response-bucket') === currentBucket;
            button.classList.toggle('active', selected);
            button.setAttribute('aria-selected', String(selected));
        });
        loadWorkQueue();
    }

    async function runNextAction(button) {
        const row = button.closest('[data-incident-id]');
        const id = Number(row && row.getAttribute('data-incident-id'));
        const action = button.getAttribute('data-response-action') || 'open';
        if (!id) return;
        if (['assign', 'assign_owner', 'claim'].includes(action) && !operatorName()) {
            window.alert(t('response.queue.actorNeededText'));
            document.getElementById('responseQueueActor')?.focus();
            return;
        }
        button.disabled = true;
        button.classList.add('is-busy');

        if (action === 'assign' || action === 'assign_owner' || action === 'claim') {
            await IncidentsModule.assign(id, operatorName(), true);
        } else if (action === 'acknowledge' || action === 'ack') {
            await IncidentsModule.updateWorkflow(id, 'acknowledged', true);
        } else if (action === 'resolve' || action === 'verify_recovery' ||
                   action === 'confirm_recovery' || action === 'close') {
            await IncidentsModule.openResolutionModal(id);
        } else {
            await IncidentsModule.openFromQueue(id);
        }
        button.disabled = false;
        button.classList.remove('is-busy');
        if (['assign', 'assign_owner', 'claim', 'acknowledge', 'ack'].includes(action)) {
            await loadWorkQueue();
        }
    }

    function gapPriority(item) {
        const score = Number(item.priority_score);
        if (Number.isFinite(score)) {
            return score >= 60 ? 'high' : (score < 30 ? 'low' : 'medium');
        }
        const value = String(item.priority || item.severity || item.level || 'medium').toLowerCase();
        return ['critical', 'high'].includes(value) ? 'high' :
            (['low', 'info'].includes(value) ? 'low' : 'medium');
    }

    function gapReasons(item) {
        const raw = item.gaps || item.gap_types || item.missing || item.reasons ||
            item.priority_reasons || [];
        const values = Array.isArray(raw) ? raw : [raw];
        return values.map(function (reason) {
            if (reason && typeof reason === 'object' && reason.code) {
                return t('knowledgeGaps.reason.' + reason.code, {
                    value: displayValue(reason.value)
                });
            }
            return displayValue(reason);
        }).filter(Boolean);
    }

    // Rendered gap items, index-addressed by the create-runbook buttons
    // (patterns carry Chinese/quotes; attributes are the wrong transport).
    let lastGapItems = [];

    function renderKnowledgeGaps(items) {
        const container = document.getElementById('knowledgeGapsList');
        if (!container) return;
        lastGapItems = items;
        if (!items.length) {
            container.innerHTML = '<div class="empty-state response-empty"><div class="empty-icon">' + wwIcon('book-open') + '</div>' +
                '<div class="empty-title">' + escapeHtml(t('knowledgeGaps.emptyTitle')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('knowledgeGaps.emptyText')) + '</div></div>';
            return;
        }
        container.innerHTML = '<div class="knowledge-gap-list">' + items.map(function (item, gapIndex) {
            const priority = gapPriority(item);
            const reasons = gapReasons(item);
            const occurrences = item.incident_count || item.occurrences || item.recurrence_count;
            const status = item.knowledge_status || 'missing_runbook';
            const next = item.next_action || {};
            const description = displayValue(item.summary || item.reason || item.recommendation) ||
                t('knowledgeGaps.status.' + status);
            // The PATTERN is the card's identity (it is also what the drills
            // search for); service/source step back to a muted suffix. With
            // the pattern hidden, two "unknown service" cards were
            // indistinguishable — and clicking searched for text the operator
            // had never seen.
            const patternName = displayValue(item.alert_pattern) || serviceName(item);
            const originParts = [item.service, item.source].filter(function (part, idx, arr) {
                return part && String(part) !== patternName && arr.indexOf(part) === idx;
            });
            // Title owns a full-width line; badges live on a meta row below.
            // Long patterns crammed inline next to the badges wrapped one
            // character per line inside the leftover sliver.
            return '<article class="knowledge-gap-card">' +
                '<div class="knowledge-gap-title">' + escapeHtml(patternName) +
                (originParts.length ? '<span class="ww-muted"> · ' + escapeHtml(originParts.join(', ')) + '</span>' : '') +
                '</div>' +
                '<div class="knowledge-gap-meta"><span class="badge badge-' +
                (priority === 'high' ? 'danger' : priority) + '">' +
                escapeHtml(t('knowledgeGaps.priority.' + priority)) + '</span><span class="knowledge-gap-score">' +
                escapeHtml(t('knowledgeGaps.priorityScore', {
                    value: Math.round(Number(item.priority_score || 0))
                })) + '</span>' +
                (occurrences != null ? '<span class="knowledge-gap-count">' +
                    escapeHtml(t('knowledgeGaps.occurrences', { value: occurrences })) + '</span>' : '') +
                '</div>' +
                (description ? '<p>' + escapeHtml(description) + '</p>' : '') +
                (reasons.length ? '<div class="knowledge-gap-tags">' + reasons.map(function (reason) {
                    return '<span>' + escapeHtml(reason) + '</span>';
                }).join('') + '</div>' : '') +
                '<div class="knowledge-gap-next"><span>' +
                escapeHtml(t('knowledgeGaps.nextAction')) + '</span>' +
                // "Create a runbook" is executable, so it is a BUTTON opening
                // the inline authoring form; the validate suggestion stays
                // advisory text (its proof comes from runbook executions).
                (next.code !== 'validate_or_improve_runbook'
                    ? '<button type="button" class="btn btn-sm btn-quiet-primary" data-gap-create="' + gapIndex + '">' +
                      wwIcon('plus') + ' ' + escapeHtml(t('knowledgeGaps.action.create_runbook')) + '</button>'
                    : '<strong>' + escapeHtml(t('knowledgeGaps.action.validate_or_improve_runbook')) + '</strong>') +
                '</div>' +
                '<div class="knowledge-gap-form" id="gapRunbookForm' + gapIndex + '" style="display:none;"></div>' +
                // The gap names a PATTERN; the evidence lives in the incidents
                // and alerts that formed it. A card you cannot leave is a
                // dead end, and the runbook gets written FROM that evidence.
                '<div class="knowledge-gap-drills">' +
                '<button type="button" class="btn btn-sm" data-gap-incidents="' + escapeHtml(item.alert_pattern || '') + '">' +
                wwIcon('flame') + ' ' + escapeHtml(t('knowledgeGaps.drill.incidents')) + '</button>' +
                '<button type="button" class="btn btn-sm" data-gap-alerts="' + escapeHtml(item.alert_pattern || '') + '">' +
                wwIcon('bell') + ' ' + escapeHtml(t('knowledgeGaps.drill.alerts')) + '</button>' +
                '</div>' +
                '</article>';
        }).join('') + '</div>';

        container.querySelectorAll('[data-gap-create]').forEach(function (button) {
            button.addEventListener('click', function () {
                openRunbookForm(Number(button.getAttribute('data-gap-create')));
            });
        });
        container.querySelectorAll('[data-gap-incidents]').forEach(function (button) {
            button.addEventListener('click', function () {
                drillFromGap('incidents', 'incidentSearchInput', button.getAttribute('data-gap-incidents'));
            });
        });
        container.querySelectorAll('[data-gap-alerts]').forEach(function (button) {
            button.addEventListener('click', function () {
                drillFromGap('alerts', 'searchInput', button.getAttribute('data-gap-alerts'));
            });
        });
    }

    // Inline runbook authoring. Publishing tags the document so the gap
    // detector recognises it deterministically (kind=runbook +
    // alert_pattern equal to this group's pattern); the card then flips from
    // "missing runbook" to "validate or improve" instead of lying around.
    function openRunbookForm(gapIndex) {
        const item = lastGapItems[gapIndex];
        const panel = document.getElementById('gapRunbookForm' + gapIndex);
        if (!item || !panel) return;
        if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
        panel.style.display = 'block';
        const pattern = displayValue(item.alert_pattern) || serviceName(item);
        panel.innerHTML =
            '<input type="text" class="filter-input" data-rb-title style="width:100%; margin-bottom:8px;">' +
            '<textarea class="filter-input" data-rb-content style="width:100%; min-height:220px; font-size:0.85rem; line-height:1.6; resize:vertical; font-family:inherit;"></textarea>' +
            '<div style="display:flex; gap:8px; margin-top:8px;">' +
            '<button type="button" class="btn btn-sm btn-quiet-primary" data-rb-save>' + wwIcon('check') + ' ' + escapeHtml(t('knowledgeGaps.form.publish')) + '</button>' +
            '<button type="button" class="btn btn-sm" data-rb-cancel>' + escapeHtml(t('common.cancel')) + '</button></div>';
        panel.querySelector('[data-rb-title]').value = 'Runbook: ' + pattern;
        panel.querySelector('[data-rb-content]').value = t('knowledgeGaps.form.template');
        panel.querySelector('[data-rb-cancel]').addEventListener('click', function () {
            panel.style.display = 'none';
        });
        panel.querySelector('[data-rb-save]').addEventListener('click', async function () {
            const title = panel.querySelector('[data-rb-title]').value.trim();
            const content = panel.querySelector('[data-rb-content]').value.trim();
            if (!title || !content) return;
            const saveBtn = panel.querySelector('[data-rb-save]');
            saveBtn.disabled = true;
            try {
                const tags = { kind: 'runbook', alert_pattern: pattern };
                if (item.service) tags.service = String(item.service);
                if (item.environment) tags.environment = String(item.environment);
                if (item.source) tags.source = String(item.source);
                await API.ingestKbDocument({
                    title: title,
                    content: content,
                    source_ref: 'runbook:' + pattern,
                    tags: tags
                });
                if (typeof showToast === 'function') showToast(t('knowledgeGaps.form.published'), 'success');
                await loadKnowledgeGaps();
            } catch (error) {
                saveBtn.disabled = false;
                showToast(t('knowledgeGaps.form.publishFail') + ': ' + (error.message || String(error)), 'error');
            }
        });
    }

    // Land on the destination ALREADY filtered to the pattern: both list
    // views read their search box on load (alerts server-side, incidents via
    // the post-load search hook).
    function drillFromGap(slug, inputId, pattern) {
        const input = document.getElementById(inputId);
        if (input) input.value = pattern || '';
        if (typeof navigateTo === 'function') navigateTo(slug);
    }

    async function loadKnowledgeGaps() {
        const container = document.getElementById('knowledgeGapsList');
        if (!container) return;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' +
            escapeHtml(t('common.loading')) + '</p></div>';
        const selector = document.getElementById('knowledgeGapWindow');
        try {
            const payload = await API.getKnowledgeGaps({
                window_days: Number(selector && selector.value || 30),
                limit: 100
            });
            renderKnowledgeGaps(dataItems(payload));
        } catch (error) {
            container.innerHTML = '<div class="empty-state response-empty is-error">' +
                escapeHtml(t('common.loadFailed')) + ': ' +
                escapeHtml(error.message || String(error)) + '</div>';
        }
    }

    function init() {
        if (initialized) return;
        initialized = true;
        const actor = document.getElementById('responseQueueActor');
        if (actor) {
            actor.value = savedOperatorName();
            actor.addEventListener('change', function () {
                loadWorkQueue(false);
            });
            actor.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') loadWorkQueue();
            });
        }
        document.querySelectorAll('[data-response-bucket]').forEach(function (button) {
            button.addEventListener('click', function () {
                setBucket(button.getAttribute('data-response-bucket'));
            });
        });
    }

    return {
        init: init,
        loadWorkQueue: loadWorkQueue,
        loadKnowledgeGaps: loadKnowledgeGaps,
        setBucket: setBucket
    };
})();

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="ResponseCenterModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('ResponseCenterModule', ResponseCenterModule);
