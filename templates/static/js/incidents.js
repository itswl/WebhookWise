/**
 * Incidents Module — operational incident list, detail, and timeline.
 *
 * An incident groups related alerts that fired close together (same source,
 * within a 15-minute window). This module provides a chronological list view
 * with expandable detail showing the member alert timeline.
 */
const IncidentsModule = (function () {
    'use strict';

    var _rows = [];
    var _statusFilter = '';
    var _page = 1;
    var _pageSize = 30;
    var _nextCursor = null;
    var _hasMore = false;
    var _loaded = false;
    var _focusIncidentId = null;

    var STATUS_BADGES = {
        active: { label: 'Active', cls: 'badge-high', icon: wwIcon('flame') },
        quiet: { label: 'Quiet', cls: 'badge-medium', icon: wwIcon('volume-x') },
        closed: { label: 'Closed', cls: 'badge-low', icon: wwIcon('check') }
    };

    async function load() {
        var container = document.getElementById('incidentsList');
        if (!container) return;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        _page = 1;
        _nextCursor = null;
        _hasMore = false;
        _rows = [];

        try {
            var params = new URLSearchParams();
            params.set('page_size', String(_pageSize));
            if (_statusFilter) params.set('status', _statusFilter);
            var resp = await API.authenticatedFetch('/v1/incidents?' + params.toString());
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var result = await resp.json();
            _rows = result.data || [];
            _nextCursor = (result.pagination && result.pagination.next_cursor) || null;
            _hasMore = !!(result.pagination && result.pagination.has_more);
            _loaded = true;
            render();
        } catch (e) {
            container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--danger);">' + t('common.loadFailed') + ': ' + escapeHtml(String(e && e.message || e)) + '</div>';
        }
    }

    function toggleStatus() {
        var el = document.getElementById('incidentStatusFilter');
        _statusFilter = el ? el.value : '';
        load();
    }

    function render() {
        var container = document.getElementById('incidentsList');
        if (!container) return;

        if (!_rows.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('check') + '</div><div class="empty-title">' + t('incidents.empty.title') + '</div><div class="empty-text">' + t('incidents.empty.text') + '</div></div>';
            return;
        }

        var html = '';
        for (var i = 0; i < _rows.length; i++) {
            var row = _rows[i];
            var badge = STATUS_BADGES[row.status] || { label: row.status, cls: 'badge-outline', icon: wwIcon('info') };
            html += '<div class="incident-card" id="incident-' + row.id + '" style="border:1px solid var(--border); border-radius:8px; padding:1rem 1.25rem; margin-bottom:0.75rem; background:var(--bg-surface); cursor:pointer;" onclick="IncidentsModule.toggle(' + row.id + ')">';
            html += '<div style="display:flex; align-items:center; gap:0.75rem;">';
            html += '<span style="font-size:1.5rem;">' + badge.icon + '</span>';
            html += '<div style="flex:1; min-width:0;">';
            html += '<div style="display:flex; align-items:center; gap:0.5rem;">';
            html += '<span style="font-weight:600; font-size:1rem; color:var(--text-main);">' + escapeHtml(row.title) + '</span>';
            html += '<span class="badge ' + badge.cls + '" style="font-size:0.65rem;">' + badge.label + '</span>';
            html += '<span class="badge badge-outline" style="font-size:0.65rem;">' + escapeHtml((row.workflow_status || 'open').replace('_', ' ')) + '</span>';
            html += renderRecurrenceBadge(row.recurrence || row.recurrence_candidate);
            html += '</div>';
            html += '<div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.2rem;">';
            html += '<span>' + escapeHtml(row.source || '') + '</span> · ';
            html += '<span>' + row.alert_count + ' alerts</span> · ';
            html += '<span>' + (row.started_at ? row.started_at.slice(0, 16).replace('T', ' ') : '?') + '</span>';
            if (row.top_importance) {
                html += ' · <span>' + (row.top_importance === 'high' ? '<span class="ww-dot ww-dot-danger"></span> high' : row.top_importance === 'medium' ? '<span class="ww-dot ww-dot-warning"></span> medium' : '<span class="ww-dot ww-dot-success"></span> low') + '</span>';
            }
            if (row.assignee || row.team) {
                html += ' · <span>' + wwIcon('user') + ' ' + escapeHtml(row.assignee || 'Unassigned') + (row.team ? ' / ' + escapeHtml(row.team) : '') + '</span>';
            }
            if (row.sla_due_at) html += ' · <span>' + wwIcon('clock') + ' ' + escapeHtml(row.sla_due_at.slice(0, 16).replace('T', ' ')) + '</span>';
            html += '</div>';
            html += '</div>';
            // Action buttons: close / reopen (stop propagation so they don't toggle the card)
            if (row.status === 'active' || row.status === 'quiet') {
                html += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.openResolutionModal(' + row.id + ')" title="' + t('incidents.action.closeTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;"></button>';
            }
            if (row.status === 'closed') {
                html += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.openResolutionModal(' + row.id + ')" title="' + t('resolution.edit') + '" style="font-size:0.7rem; margin-left:0.5rem;"></button>';
                html += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.reopenIncident(' + row.id + ')" title="' + t('incidents.action.reopenTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;"></button>';
            }
            html += '<span style="color:var(--text-muted); font-size:0.8rem;"></span>';
            html += '</div>';

            // Expandable detail (hidden by default)
            html += '<div class="incident-detail" id="incident-detail-' + row.id + '" onclick="event.stopPropagation()" style="display:none; margin-top:0.75rem; padding-top:0.75rem; border-top:1px solid var(--border-light);"></div>';
            html += '</div>';
        }

        container.innerHTML = html;
        if (_focusIncidentId) {
            var focusId = _focusIncidentId;
            _focusIncidentId = null;
            focusLoadedIncident(focusId);
        }
    }

    var _detailCache = {};

    async function toggle(id) {
        var detailEl = document.getElementById('incident-detail-' + id);
        if (!detailEl) return;

        // Already expanded — collapse.
        if (detailEl.style.display !== 'none') {
            detailEl.style.display = 'none';
            return;
        }

        // Show placeholder while loading.
        detailEl.style.display = 'block';
        detailEl.innerHTML = '<div style="padding:1rem; text-align:center;"><div class="spinner"></div></div>';

        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var result = await resp.json();
            var data = result.data || {};
            data.intelligenceLoading = true;
            data.recurrenceLoading = true;
            _detailCache[id] = data;
            detailEl.innerHTML = renderDetail(data);
            loadIntelligence(id);
            loadRecurrence(id);
        } catch (e) {
            detailEl.innerHTML = '<div style="color:var(--danger); padding:0.5rem;">' + t('common.loadFailed') + ': ' + escapeHtml(String(e && e.message || e)) + '</div>';
        }
    }

    async function loadIntelligence(id) {
        var data = _detailCache[id];
        if (!data) return;
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/intelligence');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var result = await resp.json();
            data.intelligence = result.data || {};
            data.intelligenceError = false;
        } catch (e) {
            data.intelligence = {};
            data.intelligenceError = true;
        }
        data.intelligenceLoading = false;
        var detailEl = document.getElementById('incident-detail-' + id);
        if (detailEl) detailEl.innerHTML = renderDetail(data);
    }

    async function loadRecurrence(id) {
        var data = _detailCache[id];
        if (!data) return;
        try {
            var result = await API.getIncidentRecurrence(id);
            var recurrence = result.data || null;
            data.recurrence = recurrence && recurrence.recurrence === null ? null : recurrence;
            data.recurrenceError = false;
        } catch (_error) {
            data.recurrence = null;
            data.recurrenceError = true;
        }
        data.recurrenceLoading = false;
        var detailEl = document.getElementById('incident-detail-' + id);
        if (detailEl) detailEl.innerHTML = renderDetail(data);
    }

    function formatRelativeOffset(offsetSecs) {
        if (!Number.isFinite(offsetSecs) || Math.abs(offsetSecs) < 1) return '+0s';
        var sign = offsetSecs < 0 ? '-' : '+';
        var absoluteSecs = Math.abs(offsetSecs);
        if (absoluteSecs < 60) return sign + Math.round(absoluteSecs) + 's';
        var mins = Math.floor(absoluteSecs / 60);
        var secs = Math.round(absoluteSecs % 60);
        if (mins < 60) {
            return sign + mins + 'm' + (secs > 0 ? ' ' + secs + 's' : '');
        }
        var hours = Math.floor(mins / 60);
        mins = mins % 60;
        return sign + hours + 'h' + (mins > 0 ? ' ' + mins + 'm' : '');
    }

    function impClass(importance) {
        if (importance === 'high') return 'danger';
        if (importance === 'medium') return 'medium';
        if (importance === 'low') return 'success';
        return 'outline';
    }

    function displayText(value) {
        if (value == null) return '';
        if (Array.isArray(value)) {
            return value.map(displayText).filter(Boolean).join('；');
        }
        if (typeof value === 'object') {
            return displayText(
                value.summary || value.text || value.title || value.description || value.label || value.value
            );
        }
        return String(value);
    }

    function firstDefined(object, keys) {
        if (!object || typeof object !== 'object') return null;
        for (var i = 0; i < keys.length; i++) {
            if (object[keys[i]] !== undefined && object[keys[i]] !== null) {
                return object[keys[i]];
            }
        }
        return null;
    }

    function finiteNumber(value) {
        if (value === null || value === undefined || value === '') return null;
        var number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function encodedReference(value) {
        return encodeURIComponent(String(value || '')).replace(/'/g, '%27');
    }

    function runbookExecutions(data) {
        var value = data && data.intelligence && data.intelligence.runbook_executions;
        if (!value) return [];
        return Array.isArray(value) ? value : [value];
    }

    function runbookStepText(step, index) {
        return displayText(step && (step.text || step.title || step.label || step.description)) ||
            t('incidents.runbook.stepFallback', { value: index + 1 });
    }

    function isRunbookStepComplete(step) {
        return !!(step && (step.completed === true || step.done === true || step.status === 'completed'));
    }

    function runbookProgress(execution) {
        var steps = Array.isArray(execution && execution.steps) ? execution.steps : [];
        var completed = steps.filter(isRunbookStepComplete).length;
        return {
            steps: steps,
            completed: completed,
            total: steps.length,
            percent: steps.length ? Math.round(completed * 100 / steps.length) : 0
        };
    }

    function activeRunbookExecution(data) {
        var executions = runbookExecutions(data);
        for (var i = 0; i < executions.length; i++) {
            if (executions[i].status === 'in_progress') return executions[i];
        }
        return null;
    }

    function executionForCandidate(data, candidateRef) {
        var executions = runbookExecutions(data);
        for (var i = 0; i < executions.length; i++) {
            if (String(executions[i].candidate_ref || '') === String(candidateRef || '')) {
                return executions[i];
            }
        }
        return null;
    }

    function changeImpactLevel(assessment) {
        var raw = String(firstDefined(assessment, ['level', 'risk_level', 'risk']) || 'unknown').toLowerCase();
        return ['high', 'medium', 'low'].indexOf(raw) >= 0 ? raw : 'unknown';
    }

    function renderChangeImpact(change, compact) {
        var assessment = change && change.impact_assessment;
        if (!assessment || typeof assessment !== 'object') {
            return compact
                ? '<span class="incident-impact-badge impact-unknown">' +
                    t('incidents.changeImpact.pending') + '</span>'
                : '';
        }

        var level = changeImpactLevel(assessment);
        var summary = displayText(firstDefined(assessment, ['summary', 'conclusion', 'description']));
        var before = finiteNumber(firstDefined(assessment, ['before_alert_count', 'alerts_before']));
        var after = finiteNumber(firstDefined(assessment, ['after_alert_count', 'alerts_after']));
        var deltaValue = firstDefined(assessment, ['alert_delta', 'alert_count_delta']);
        var delta = finiteNumber(deltaValue);
        if (delta === null && before !== null && after !== null) {
            delta = after - before;
        }

        var html = '<div class="incident-change-impact' + (compact ? ' is-compact' : '') + '">';
        html += '<div class="incident-change-impact-head"><span class="incident-impact-badge impact-' + level + '">' +
            t('incidents.changeImpact.level.' + level) + '</span>';
        if (summary) html += '<span class="incident-change-impact-summary">' + escapeHtml(summary) + '</span>';
        html += '</div>';

        if (!compact) {
            var metrics = [];
            if (before !== null && after !== null) {
                metrics.push(t('incidents.changeImpact.beforeAfter', { before: before, after: after }));
            }
            if (delta !== null) {
                metrics.push(t('incidents.changeImpact.alertDelta', {
                    value: (delta > 0 ? '+' : '') + delta
                }));
            }
            var newIdentities = finiteNumber(firstDefined(
                assessment, ['new_identity_count', 'new_alert_identity_count']
            ));
            if (newIdentities !== null) {
                metrics.push(t('incidents.changeImpact.newIdentities', { value: newIdentities }));
            }
            var linkedIncidents = finiteNumber(firstDefined(
                assessment, ['linked_incident_count', 'incident_count']
            ));
            if (linkedIncidents !== null) {
                metrics.push(t('incidents.changeImpact.linkedIncidents', { value: linkedIncidents }));
            }
            if (assessment.recovered_after_rollback === true) {
                metrics.push(t('incidents.changeImpact.rollbackRecovered'));
            }
            if (metrics.length) {
                html += '<div class="incident-change-impact-metrics">' +
                    metrics.map(function (metric) {
                        return '<span>' + escapeHtml(metric) + '</span>';
                    }).join('') + '</div>';
            }
        }
        return html + '</div>';
    }

    function intelligenceReason(reason) {
        var code = String((reason && reason.code) || '');
        var value = reason && reason.value != null ? String(reason.value) : '';
        return t('incidents.intelligence.reason.' + code, { value: escapeHtml(value) });
    }

    function intelligenceReasons(reasons) {
        return (reasons || []).slice(0, 3).map(function (reason) {
            return '<span class="incident-intelligence-reason">' + intelligenceReason(reason) + '</span>';
        }).join('');
    }

    function calibrationExplanation(item) {
        var calibration = item && item.calibration;
        if (!calibration || typeof calibration !== 'object') return '';
        var parts = [];
        var reasonCode = String(calibration.reason || '');
        var knownReasons = {
            missing_service_scope: true,
            insufficient_sample: true,
            bounded_bayesian_adjustment: true
        };
        var explanation = knownReasons[reasonCode]
            ? t('incidents.intelligence.calibration.reason.' + reasonCode)
            : displayText(calibration.explanation || calibration.summary);
        if (explanation) parts.push(explanation);
        var sampleSize = finiteNumber(firstDefined(calibration, [
            'sample_size', 'samples', 'feedback_sample_size'
        ]));
        if (sampleSize !== null) {
            parts.push(t('incidents.intelligence.calibration.samples', { value: sampleSize }));
        }
        var adjustment = finiteNumber(firstDefined(calibration, ['adjustment', 'score_adjustment']));
        if (adjustment !== null && adjustment !== 0) {
            parts.push(t('incidents.intelligence.calibration.adjustment', {
                value: (adjustment > 0 ? '+' : '') + Math.round(adjustment * 100)
            }));
        }
        var strategy = String(calibration.strategy || '').toLowerCase();
        if (calibration.applied === true &&
                (strategy.indexOf('shrink') >= 0 || strategy.indexOf('bayesian') >= 0)) {
            parts.push(t('incidents.intelligence.calibration.shrunk'));
        }
        return parts.join(' · ');
    }

    function intelligenceScore(item) {
        var score = typeof item === 'object' ? item.score : item;
        var rawScore = typeof item === 'object' ? finiteNumber(item.raw_score) : null;
        var explanation = typeof item === 'object' ? calibrationExplanation(item) : '';
        var label = t('incidents.intelligence.score', {
            value: Math.round(Number(score || 0) * 100)
        });
        if (!explanation && rawScore === null) {
            return '<span class="incident-intelligence-score">' + label + '</span>';
        }
        var detail = explanation;
        if (rawScore !== null) {
            detail = t('incidents.intelligence.calibration.raw', {
                value: Math.round(rawScore * 100)
            }) + (detail ? ' · ' + detail : '');
        }
        var calibrationLabel = item && item.calibration && item.calibration.applied === false
            ? t('incidents.intelligence.notAdjusted')
            : t('incidents.intelligence.calibrated');
        return '<details class="incident-intelligence-calibration" onclick="event.stopPropagation()">' +
            '<summary class="incident-intelligence-score">' + label + ' · ' +
            calibrationLabel + '</summary><p>' +
            escapeHtml(detail || t('incidents.intelligence.calibration.default')) +
            '</p></details>';
    }

    function intelligenceFeedbackState(verdict) {
        if (!verdict) return '';
        var key = {
            relevant: 'incidents.intelligence.relevant',
            irrelevant: 'incidents.intelligence.irrelevant',
            used: 'incidents.intelligence.used',
            not_used: 'incidents.intelligence.notUsed'
        }[verdict];
        return key ? '<span class="incident-intelligence-feedback-state">' + wwIcon('check') + ' ' + t(key) + '</span>' : '';
    }

    function intelligenceFeedbackControls(incidentId, recommendationType, candidateRef, verdict) {
        if (!candidateRef) return '';
        var change = recommendationType === 'change';
        var positive = change ? 'relevant' : 'used';
        var negative = change ? 'irrelevant' : 'not_used';
        var encoded = encodedReference(candidateRef);
        return '<details class="incident-intelligence-feedback" onclick="event.stopPropagation()">' +
            '<summary>' + t('incidents.intelligence.feedbackAction') + '</summary><div>' +
            '<button type="button" class="btn btn-sm' + (verdict === positive ? ' active' : '') +
            '" onclick="IncidentsModule.intelligenceFeedback(' + incidentId + ',\'' +
            recommendationType + '\',\'' + encoded + '\',\'' + positive + '\')">' + wwIcon('thumbs-up') + ' ' +
            t(change ? 'incidents.intelligence.relevant' : 'incidents.intelligence.used') + '</button>' +
            '<button type="button" class="btn btn-sm' + (verdict === negative ? ' active' : '') +
            '" onclick="IncidentsModule.intelligenceFeedback(' + incidentId + ',\'' +
            recommendationType + '\',\'' + encoded + '\',\'' + negative + '\')">' + wwIcon('thumbs-down') + ' ' +
            t(change ? 'incidents.intelligence.irrelevant' : 'incidents.intelligence.notUsed') +
            '</button></div></details>';
    }

    function renderSimilarIncident(item, incidentId) {
        var resolution = Array.isArray(item.resolution) ? item.resolution.join('；') : (item.resolution || '');
        var html = '<article class="incident-intelligence-card">';
        html += '<div class="incident-intelligence-card-head"><strong>'
            + '<a href="#/incidents/' + encodeURIComponent(item.incident_id) + '"'
            + ' onclick="event.preventDefault(); openIncident(' + Number(item.incident_id) + ');"'
            + ' style="color: var(--primary); text-decoration: none;">#' + item.incident_id + '</a> '
            + escapeHtml(item.title || '') + '</strong>' + intelligenceScore(item) + '</div>';
        if (item.root_cause) {
            html += '<p><span>' + t('incidents.rootCause') + ':</span> ' + escapeHtml(String(item.root_cause)) + '</p>';
        } else if (resolution) {
            html += '<p>' + escapeHtml(String(resolution)) + '</p>';
        }
        html += '<div class="incident-intelligence-reasons">' + intelligenceReasons(item.reasons) + '</div>';
        html += intelligenceFeedbackState(item.feedback);
        html += intelligenceFeedbackControls(
            incidentId, 'similar_incident', item.candidate_ref, item.feedback
        );
        return html + '</article>';
    }

    function changeTitle(item) {
        var identity = item.service || item.external_id || ('#' + item.change_id);
        return escapeHtml(String(item.change_type || 'change')) + ' · ' + escapeHtml(String(identity));
    }

    function renderRelatedChange(item, incidentId) {
        var html = '<article class="incident-intelligence-card">';
        html += '<div class="incident-intelligence-card-head"><strong>' + changeTitle(item) +
            '</strong>' + intelligenceScore(item) + '</div>';
        if (item.version_from || item.version_to) {
            html += '<p><span>' + t('incidents.intelligence.version') + ':</span> ' +
                escapeHtml(item.version_from || '—') + ' → ' + escapeHtml(item.version_to || '—') + '</p>';
        }
        if (item.actor) {
            html += '<p><span>' + t('incidents.intelligence.actor') + ':</span> ' +
                escapeHtml(item.actor) + '</p>';
        }
        html += renderChangeImpact(item, false);
        html += '<div class="incident-intelligence-reasons">' + intelligenceReasons(item.reasons) + '</div>';
        html += intelligenceFeedbackState(item.feedback);
        html += intelligenceFeedbackControls(
            incidentId, 'change', item.candidate_ref, item.feedback
        );
        return html + '</article>';
    }

    function renderRunbook(item, incidentId, data) {
        var execution = executionForCandidate(data, item.candidate_ref);
        var encodedRef = encodedReference(item.candidate_ref);
        var html = '<article class="incident-intelligence-card">';
        html += '<div class="incident-intelligence-card-head"><strong>' + escapeHtml(item.title || '') +
            '</strong>' + intelligenceScore(item) + '</div>';
        if (item.excerpt) html += '<p class="incident-intelligence-excerpt">' + escapeHtml(item.excerpt) + '</p>';
        html += '<div class="incident-intelligence-reasons">' + intelligenceReasons(item.reasons) + '</div>';
        if (execution) {
            var progress = runbookProgress(execution);
            html += '<div class="incident-runbook-card-progress">' +
                t('incidents.runbook.progress', { completed: progress.completed, total: progress.total }) +
                '</div>';
        } else {
            html += '<button type="button" class="btn btn-sm incident-runbook-start" ' +
                'onclick="event.stopPropagation(); IncidentsModule.startRunbookExecution(' + incidentId +
                ',\'' + encodedRef + '\')">' + wwIcon('play') + ' ' + t('incidents.runbook.start') + '</button>';
        }
        html += intelligenceFeedbackState(item.feedback);
        html += intelligenceFeedbackControls(
            incidentId, 'runbook', item.candidate_ref, item.feedback
        );
        return html + '</article>';
    }

    function renderIntelligence(data, embedded) {
        var heading = embedded ? '' : '<div class="incident-intelligence-title">' + wwIcon('sparkles') + ' ' +
            t('incidents.intelligence.title') + '</div>';
        if (data.intelligenceLoading) {
            return '<section class="incident-intelligence">' + heading + '<div class="incident-intelligence-loading">' +
                t('common.loading') + '</div></section>';
        }
        if (data.intelligenceError) {
            return '<section class="incident-intelligence">' + heading + '<div class="incident-intelligence-empty">' +
                t('incidents.intelligence.unavailable') + '</div></section>';
        }
        var intelligence = data.intelligence || {};
        var groups = [
            {
                icon: wwIcon('history'),
                title: t('incidents.intelligence.similar'),
                items: intelligence.similar_incidents || [],
                render: renderSimilarIncident
            },
            {
                icon: wwIcon('send'),
                title: t('incidents.intelligence.changes'),
                items: intelligence.related_changes || [],
                render: renderRelatedChange
            },
            {
                icon: wwIcon('book-open'),
                title: t('incidents.intelligence.runbooks'),
                items: intelligence.recommended_runbooks || [],
                render: renderRunbook
            }
        ];
        var html = '<section class="incident-intelligence">';
        html += heading;
        html += '<p class="incident-intelligence-note">' + t('incidents.intelligence.note') + '</p>';
        html += '<div class="incident-intelligence-grid">';
        groups.forEach(function (group) {
            html += '<div class="incident-intelligence-group"><h4>' + group.icon + ' ' + group.title + '</h4>';
            if (!group.items.length) {
                html += '<div class="incident-intelligence-empty">' + t('incidents.intelligence.empty') + '</div>';
            } else {
                group.items.forEach(function (item) {
                    html += group.render(item, data.id, data);
                });
            }
            html += '</div>';
        });
        return html + '</div></section>';
    }

    function renderSupportingEvidence(data) {
        var intelligence = data.intelligence || {};
        var count = (intelligence.similar_incidents || []).length +
            (intelligence.related_changes || []).length +
            (intelligence.recommended_runbooks || []).length;
        return '<details class="incident-supporting-details" onclick="event.stopPropagation()">' +
            '<summary>' + wwIcon('sparkles') + ' ' + t('incidents.evidence.title') +
            '<span class="incident-supporting-count">' + count + '</span></summary>' +
            renderIntelligence(data, true) + '</details>';
    }

    function safeHttpUrl(value) {
        if (!value) return '';
        try {
            var parsed = new URL(String(value));
            return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : '';
        } catch (e) {
            return '';
        }
    }

    function timestampOffsetSeconds(timestamp, rootTimestamp) {
        var current = Date.parse(timestamp || '');
        var root = Date.parse(rootTimestamp || '');
        return Number.isFinite(current) && Number.isFinite(root) ? (current - root) / 1000 : 0;
    }

    function buildIncidentTimeline(members, changes) {
        var timeline = [];
        members.forEach(function (member) {
            timeline.push({ kind: 'alert', timestamp: member.timestamp, value: member });
        });
        changes.forEach(function (change) {
            timeline.push({ kind: 'change', timestamp: change.started_at, value: change });
        });
        timeline.sort(function (left, right) {
            var leftTime = Date.parse(left.timestamp || '');
            var rightTime = Date.parse(right.timestamp || '');
            if (!Number.isFinite(leftTime)) return 1;
            if (!Number.isFinite(rightTime)) return -1;
            if (leftTime === rightTime) return left.kind === 'change' ? -1 : 1;
            return leftTime - rightTime;
        });
        return timeline;
    }

    function renderChangeTimelineNode(change, rootTimestamp) {
        var offset = formatRelativeOffset(timestampOffsetSeconds(change.started_at, rootTimestamp));
        var sourceUrl = safeHttpUrl(change.source_url);
        var html = '<div class="tree-node incident-timeline-change-node">';
        html += '<div class="tree-indicator incident-timeline-change-indicator">' + wwIcon('send') + '</div>';
        html += '<article class="incident-timeline-change">';
        html += '<div class="incident-timeline-change-head"><div><span class="incident-timeline-change-badge">' +
            t('incidents.timeline.changeMarker') + '</span><strong>' + changeTitle(change) + '</strong></div>' +
            '<span class="incident-timeline-offset">' + escapeHtml(offset) + '</span></div>';
        html += renderChangeImpact(change, true);
        if (change.version_from || change.version_to) {
            html += '<div class="incident-timeline-change-version"><span>' +
                t('incidents.intelligence.version') + '</span> ' +
                escapeHtml(change.version_from || '—') + ' → ' + escapeHtml(change.version_to || '—') + '</div>';
        }
        var metadata = [change.source, change.environment, change.actor].filter(Boolean);
        if (metadata.length) {
            html += '<div class="incident-timeline-change-meta">' +
                metadata.map(function (value) { return escapeHtml(value); }).join(' · ') + '</div>';
        }
        html += '<div class="incident-timeline-change-footer"><span>' +
            escapeHtml(change.started_at ? change.started_at.replace('T', ' ').slice(0, 19) : '') +
            (change.status ? ' · ' + escapeHtml(change.status) : '') + '</span><span>' +
            t('incidents.intelligence.score', { value: Math.round(Number(change.score || 0) * 100) });
        if (sourceUrl) {
            html += ' · <a href="' + escapeHtml(sourceUrl) +
                '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();">' +
                t('incidents.timeline.viewChange') + '</a>';
        }
        return html + '</span></div></article></div>';
    }

    function uniqueIncidentSources(members) {
        var sources = {};
        (members || []).forEach(function (member) {
            var source = String(member.source || '').trim();
            if (source) sources[source] = true;
        });
        return Object.keys(sources);
    }

    function recurrenceStatus(value) {
        if (!value || typeof value !== 'object') return '';
        return String(value.status || value.review_status || 'pending').toLowerCase();
    }

    function renderRecurrenceBadge(recurrence) {
        if (!recurrence || typeof recurrence !== 'object') return '';
        var status = recurrenceStatus(recurrence);
        return '<span class="incident-recurrence-badge status-' +
            escapeHtml(status || 'pending') + '">↻ ' +
            t('incidents.recurrence.badge.' + (status || 'pending')) + '</span>';
    }

    function renderRecurrenceReview(data) {
        var recurrence = data.recurrence;
        if (!recurrence || typeof recurrence !== 'object') return '';
        var status = recurrenceStatus(recurrence);
        var previous = recurrence.previous_incident || {};
        var html = '<div class="incident-recurrence-review">' +
            '<div class="incident-recurrence-copy">' + renderRecurrenceBadge(recurrence) +
            '<div><strong>' + escapeHtml(t('incidents.recurrence.title')) + '</strong><span>' +
            escapeHtml(t('incidents.recurrence.previous', {
                id: previous.id || '—',
                title: previous.title || t('incidents.recurrence.unknownPrevious')
            })) + '</span></div></div>';
        if (status === 'pending') {
            html += '<div class="incident-recurrence-actions">' +
                '<button type="button" class="btn btn-sm btn-primary" onclick="event.stopPropagation(); ' +
                'IncidentsModule.reviewRecurrence(' + data.id + ',\'confirm\')">' + wwIcon('check') + ' ' +
                t('incidents.recurrence.confirm') + '</button>' +
                '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); ' +
                'IncidentsModule.reviewRecurrence(' + data.id + ',\'dismiss\')">× ' +
                t('incidents.recurrence.dismiss') + '</button></div>';
        } else if (recurrence.reviewed_by) {
            html += '<span class="incident-recurrence-reviewed">' +
                escapeHtml(t('incidents.recurrence.reviewedBy', {
                    value: recurrence.reviewed_by
                })) + '</span>';
        }
        return html + '</div>';
    }

    function renderIncidentToolbar(data, members) {
        var workflowStatus = data.workflow_status || 'open';
        var terminal = workflowStatus === 'resolved' || workflowStatus === 'ignored';
        var sources = uniqueIncidentSources(members);
        var secondaryActions = [];

        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.assign(' +
            data.id + ')">' + wwIcon('user') + ' ' + t('alerts.action.assign') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.feedback(' +
            data.id + ',\'correct\')">' + wwIcon('thumbs-up') + ' ' + t('alerts.action.feedbackCorrect') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.feedback(' +
            data.id + ',\'grouping_wrong\')">' + wwIcon('thumbs-down') + ' ' + t('incidents.action.groupingWrong') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.merge(' +
            data.id + ')">' + wwIcon('link') + ' ' + t('incidents.action.merge') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.split(' +
            data.id + ')">' + wwIcon('x') + ' ' + t('incidents.action.split') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.exportPostmortem(' +
            data.id + ')">' + wwIcon('file-text') + ' ' + t('incidents.action.postmortem') + '</button>'
        );
        if (sources.length) {
            secondaryActions.push(
                '<button type="button" class="btn btn-sm btn-warn" onclick="event.stopPropagation(); ' +
                'IncidentsModule.silenceIncidentSources(' + data.id + ')">' + wwIcon('volume-x') + ' ' +
                t('incidents.action.silenceAll') + ' (' + sources.length + ')</button>'
            );
        }
        if (terminal || data.status === 'closed') {
            secondaryActions.push(
                '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.openResolutionModal(' +
                data.id + ')">' + wwIcon('pencil') + ' ' + t('resolution.edit') + '</button>'
            );
            secondaryActions.push(
                '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.reopenIncident(' +
                data.id + ')">' + wwIcon('refresh') + ' ' + t('incidents.action.reopen') + '</button>'
            );
        }

        var html = '<div class="incident-command-bar">';
        html += '<div class="incident-command-meta">';
        html += '<strong>' + escapeHtml(t('alerts.workflow.' + workflowStatus)) + '</strong>';
        html += '<span>' + wwIcon('user') + ' ' + t('incidents.owner') + ': ' +
            escapeHtml(data.assignee || t('incidents.unassigned')) +
            (data.team ? ' / ' + escapeHtml(data.team) : '') + '</span>';
        html += '<span>' + wwIcon('clock') + ' ' + t('incidents.sla') + ': ' +
            escapeHtml(data.sla_due_at ? data.sla_due_at.replace('T', ' ').slice(0, 19) : t('incidents.notSet')) +
            '</span></div>';
        html += '<div class="incident-command-actions alert-primary-actions">';
        if (workflowStatus === 'open') {
            html += '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); ' +
                'IncidentsModule.updateWorkflow(' + data.id + ',\'acknowledged\')">' + wwIcon('check') + ' ' +
                t('alerts.action.acknowledge') + '</button>';
        }
        if (!terminal) {
            html += '<button type="button" class="btn btn-sm btn-primary" onclick="event.stopPropagation(); ' +
                'IncidentsModule.openResolutionModal(' + data.id + ')">' + wwIcon('check') + ' ' +
                t('alerts.action.resolve') + '</button>';
        }
        html += '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.addNote(' +
            data.id + ')">' + wwIcon('pencil') + ' ' + t('alerts.action.notes') + '</button>';
        html += '<details class="alert-action-menu incident-action-menu" onclick="event.stopPropagation()">' +
            '<summary class="btn btn-sm alert-more-trigger">••• ' + t('alerts.action.more') +
            '<span class="alert-action-count">' + secondaryActions.length + '</span></summary>' +
            '<div class="alert-secondary-actions">' + secondaryActions.join('') + '</div></details>';
        return html + '</div></div>';
    }

    function renderProgressBar(progress) {
        return '<div class="incident-runbook-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" ' +
            'aria-valuenow="' + progress.percent + '">' +
            '<div class="incident-runbook-progress-track"><span style="width:' + progress.percent +
            '%"></span></div><strong>' +
            escapeHtml(t('incidents.runbook.progress', {
                completed: progress.completed,
                total: progress.total
            })) + '</strong></div>';
    }

    function renderServiceProfile(profile) {
        if (!profile || typeof profile !== 'object' || !Object.keys(profile).length) return '';
        var service = displayText(firstDefined(profile, ['service', 'name'])) || t('incidents.serviceProfile.unknown');
        var facts = [];
        var alertCount = firstDefined(profile, ['alert_count_30d', 'alerts_30d', 'alert_count']);
        var incidentCount = firstDefined(profile, ['incident_count_30d', 'incidents_30d', 'incident_count']);
        var mttr = firstDefined(profile, [
            'average_mttr_minutes', 'mttr_minutes', 'mean_time_to_resolve_minutes'
        ]);
        var owner = displayText(firstDefined(profile, ['historical_owners', 'owner', 'team']));
        var rootCause = displayText(firstDefined(profile, ['common_root_causes', 'top_root_causes', 'common_root_cause']));
        if (alertCount != null) facts.push(t('incidents.serviceProfile.alerts', { value: alertCount }));
        if (incidentCount != null) facts.push(t('incidents.serviceProfile.incidents', { value: incidentCount }));
        if (mttr != null) facts.push(t('incidents.serviceProfile.mttr', { value: mttr }));
        if (owner) facts.push(t('incidents.serviceProfile.owner', { value: owner }));
        if (rootCause) facts.push(t('incidents.serviceProfile.rootCause', { value: rootCause }));
        var health = profile.health && typeof profile.health === 'object' ? profile.health : {};
        var healthLabel = displayText(health.label);
        return '<div class="incident-service-profile"><strong>' + wwIcon('target') + ' ' +
            t('incidents.serviceProfile.title') + ' · ' + escapeHtml(service) +
            (healthLabel ? ' <span class="incident-service-health health-' +
                escapeHtml(healthLabel) + '">' +
                t('incidents.serviceProfile.health.' + healthLabel) +
                (health.score != null ? ' ' + escapeHtml(health.score) : '') + '</span>' : '') +
            '</strong><div>' +
            facts.slice(0, 5).map(function (fact) {
                return '<span>' + escapeHtml(fact) + '</span>';
            }).join('') + '</div></div>';
    }

    function renderCommandSummary(data) {
        var intelligence = data.intelligence || {};
        var command = intelligence.command_summary || {};
        var summary = data.summary_analysis || {};
        var members = data.members || [];
        var firstMember = members[0] || {};
        var changes = intelligence.related_changes || [];
        var change = changes[0] || null;
        var executions = runbookExecutions(data);
        var execution = activeRunbookExecution(data);
        var runbooks = intelligence.recommended_runbooks || [];
        var recommendedRunbook = null;
        for (var runbookIndex = 0; runbookIndex < runbooks.length; runbookIndex++) {
            if (!executionForCandidate(data, runbooks[runbookIndex].candidate_ref)) {
                recommendedRunbook = runbooks[runbookIndex];
                break;
            }
        }

        var happened = displayText(firstDefined(command, ['what_happened', 'summary'])) ||
            displayText(summary.summary) || displayText(firstMember.summary) || displayText(data.title);
        var cause = displayText(firstDefined(command, ['likely_cause', 'most_likely_cause', 'root_cause'])) ||
            displayText(summary.root_cause);
        var nextAction = displayText(firstDefined(command, [
            'next_action', 'next_actions', 'next_steps', 'recommended_action', 'recommendation'
        ]));
        if (!nextAction && Array.isArray(summary.recommendations)) {
            nextAction = displayText(summary.recommendations[0]);
        }
        if (!nextAction && recommendedRunbook) nextAction = displayText(recommendedRunbook.title);

        var html = '<section class="incident-command-summary" id="incident-command-' + data.id + '">';
        html += renderIncidentToolbar(data, members);
        html += renderRecurrenceReview(data);
        html += '<div class="incident-command-grid">';

        html += '<article class="incident-command-card"><div class="incident-command-label">' + wwIcon('send') + ' ' +
            t('incidents.command.whatHappened') + '</div><div class="incident-command-body">' +
            escapeHtml(happened || t('incidents.command.noSummary')) + '</div>';
        var impactText = displayText(firstDefined(command, ['impact'])) || displayText(summary.impact);
        if (impactText) {
            html += '<div class="incident-command-card-meta"><strong>' + t('incidents.command.impact') +
                ':</strong> ' + escapeHtml(impactText) + '</div>';
        }
        html += '</article>';

        html += '<article class="incident-command-card"><div class="incident-command-label">' + wwIcon('layers') + ' ' +
            t('incidents.command.likelyCause') + '</div><div class="incident-command-body">' +
            escapeHtml(cause || t('incidents.command.noCause')) + '</div>';
        var confidence = firstDefined(command, ['confidence']);
        if (confidence == null) confidence = summary.confidence;
        if (confidence != null && Number.isFinite(Number(confidence))) {
            html += '<div class="incident-command-card-meta">' + t('incidents.confidence') + ': ' +
                Math.round(Number(confidence) * 100) + '%</div>';
        }
        html += '</article>';

        html += '<article class="incident-command-card"><div class="incident-command-label">' + wwIcon('send') + ' ' +
            t('incidents.command.recentChange') + '</div>';
        if (change) {
            html += '<div class="incident-command-body">' + changeTitle(change) + '</div>';
            if (change.version_from || change.version_to) {
                html += '<div class="incident-command-card-meta">' +
                    escapeHtml(change.version_from || '—') + ' → ' + escapeHtml(change.version_to || '—') +
                    '</div>';
            }
            html += renderChangeImpact(change, true);
        } else {
            var commandChange = displayText(firstDefined(
                command, ['recent_change', 'recent_related_change', 'related_change']
            ));
            html += '<div class="incident-command-body is-muted">' +
                escapeHtml(commandChange || (
                    data.intelligenceLoading ? t('common.loading') : t('incidents.command.noChange')
                )) + '</div>';
        }
        html += '</article>';

        html += '<article class="incident-command-card"><div class="incident-command-label">' + wwIcon('wrench') + ' ' +
            t('incidents.command.nextAction') + '</div>';
        if (execution) {
            var progress = runbookProgress(execution);
            html += '<div class="incident-command-body">' + escapeHtml(execution.title || nextAction ||
                t('incidents.runbook.untitled')) + '</div>' + renderProgressBar(progress);
        } else {
            html += '<div class="incident-command-body' + (nextAction ? '' : ' is-muted') + '">' +
                escapeHtml(nextAction || (
                    data.intelligenceLoading ? t('common.loading') : t('incidents.command.noAction')
                )) + '</div>';
            if (recommendedRunbook) {
                html += '<button type="button" class="btn btn-sm incident-runbook-start" ' +
                    'onclick="event.stopPropagation(); IncidentsModule.startRunbookExecution(' + data.id +
                    ',\'' + encodedReference(recommendedRunbook.candidate_ref) + '\')">' + wwIcon('play') + ' ' +
                    t('incidents.runbook.start') + '</button>';
            }
        }
        if (executions.length > 1) {
            html += '<div class="incident-command-card-meta">' +
                t('incidents.runbook.executionCount', { value: executions.length }) + '</div>';
        }
        html += '</article></div>';
        html += renderServiceProfile(intelligence.service_profile);
        return html + '</section>';
    }

    function renderRunbookExecution(execution, incidentId) {
        var progress = runbookProgress(execution);
        var executionId = Number(execution.id);
        var terminal = ['completed', 'failed', 'abandoned'].indexOf(execution.status) >= 0;
        var html = '<article class="incident-runbook-execution">';
        html += '<div class="incident-runbook-execution-head"><div><span>' + wwIcon('book-open') + '</span><strong>' +
            escapeHtml(execution.title || t('incidents.runbook.untitled')) + '</strong></div>' +
            '<span class="incident-runbook-status status-' +
            escapeHtml(String(execution.status || 'in_progress')) + '">' +
            t('incidents.runbook.status.' + (execution.status || 'in_progress')) + '</span></div>';
        html += '<div class="incident-runbook-meta">' +
            escapeHtml(execution.actor || 'operator') +
            (execution.started_at ? ' · ' + escapeHtml(execution.started_at.replace('T', ' ').slice(0, 19)) : '') +
            '</div>';
        html += renderProgressBar(progress);
        html += '<div class="incident-runbook-steps">';
        if (!progress.steps.length) {
            html += '<div class="incident-intelligence-empty">' + t('incidents.runbook.noSteps') + '</div>';
        } else {
            progress.steps.forEach(function (step, index) {
                var completed = isRunbookStepComplete(step);
                html += '<button type="button" class="incident-runbook-step' +
                    (completed ? ' is-complete' : '') + '" aria-pressed="' + completed + '"' +
                    (terminal ? ' disabled' : '') +
                    ' onclick="event.stopPropagation(); IncidentsModule.toggleRunbookStep(' + incidentId + ',' +
                    executionId + ',' + index + ',' + (!completed) + ')"><span>' +
                    (completed ? wwIcon('check') : (index + 1)) + '</span><strong>' +
                    escapeHtml(runbookStepText(step, index)) + '</strong></button>';
            });
        }
        html += '</div>';
        if (execution.notes) {
            html += '<p class="incident-runbook-notes">' + escapeHtml(execution.notes) + '</p>';
        }
        html += '<div class="incident-runbook-actions">';
        if (!terminal) {
            html += '<button type="button" class="btn btn-sm btn-primary" onclick="event.stopPropagation(); ' +
                'IncidentsModule.completeRunbookExecution(' + incidentId + ',' + executionId + ')">' + wwIcon('check') + ' ' +
                t('incidents.runbook.complete') + '</button>';
            html += '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); ' +
                'IncidentsModule.updateRunbookExecution(' + incidentId + ',' + executionId +
                ',{status:\'abandoned\'})">× ' + t('incidents.runbook.abandon') + '</button>';
        } else if (execution.status === 'failed') {
            html += '<button type="button" class="btn btn-sm btn-primary" ' +
                'onclick="event.stopPropagation(); IncidentsModule.updateRunbookExecution(' + incidentId + ',' +
                executionId + ',{status:\'in_progress\'})">↻ ' +
                t('incidents.runbook.resume') + '</button>';
        } else if (execution.status === 'completed') {
            html += '<button type="button" class="btn btn-sm' +
                (execution.effectiveness === 'effective' ? ' active' : '') +
                '" onclick="event.stopPropagation(); IncidentsModule.updateRunbookExecution(' + incidentId + ',' +
                executionId + ',{effectiveness:\'effective\'})">' + wwIcon('thumbs-up') + ' ' +
                t('incidents.runbook.effective') + '</button>';
            html += '<button type="button" class="btn btn-sm' +
                (execution.effectiveness === 'ineffective' ? ' active' : '') +
                '" onclick="event.stopPropagation(); IncidentsModule.updateRunbookExecution(' + incidentId + ',' +
                executionId + ',{effectiveness:\'ineffective\'})">' + wwIcon('thumbs-down') + ' ' +
                t('incidents.runbook.ineffective') + '</button>';
        }
        html += '<span class="incident-runbook-manual-note">' +
            t('incidents.runbook.manualOnly') + '</span></div>';
        return html + '</article>';
    }

    function renderRunbookExecutions(data) {
        var executions = runbookExecutions(data);
        if (!executions.length) return '';
        var html = '<section class="incident-runbook-section"><div class="incident-section-title">' + wwIcon('book-open') + ' ' +
            t('incidents.runbook.executionTitle') + '</div>';
        executions.slice(0, 3).forEach(function (execution) {
            html += renderRunbookExecution(execution, data.id);
        });
        return html + '</section>';
    }

    function renderOperatorNotes(notes) {
        if (!notes.length) return '';
        var html = '<details class="incident-supporting-details incident-notes" onclick="event.stopPropagation()">' +
            '<summary>' + wwIcon('pencil') + ' ' + t('incidents.notes.title') +
            '<span class="incident-supporting-count">' + notes.length + '</span></summary><div>';
        notes.forEach(function (note) {
            html += '<div class="incident-note"><span>' + escapeHtml(note.actor) + ':</span> ' +
                escapeHtml(note.body) + '</div>';
        });
        return html + '</div></details>';
    }

    function renderDetail(data) {
        var members = data.members || [];
        var html = '';

        html += renderCommandSummary(data);
        html += renderRunbookExecutions(data);
        if (data.summary_status === 'failed') {
            html += '<div class="incident-summary-state is-warning">' + wwIcon('alert-triangle') + ' ' +
                t('incidents.summaryFailed') + '</div>';
        } else if (data.summary_status === 'pending' || data.summary_status === 'retrying' ||
                   data.summary_status === 'processing') {
            html += '<div class="incident-summary-state">' + wwIcon('message') + ' ' + t('incidents.summaryPending') + '</div>';
        }
        html += renderOperatorNotes(data.notes || []);
        html += renderSupportingEvidence(data);

        // Chronological alert and suspected-change timeline
        var relatedChanges = (data.intelligence && data.intelligence.related_changes) || [];
        var timelineItems = buildIncidentTimeline(members, relatedChanges);
        if (timelineItems.length) {
            html += '<div style="font-weight:600; font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem; text-transform:uppercase; letter-spacing:0.04em;">' + wwIcon('clock') + ' ' +
                t('incidents.timeline') + ' · ' + t('incidents.timeline.counts', {
                    alerts: members.length,
                    changes: relatedChanges.length
                }) + '</div>';
            
            // Timeline tree list
            html += '<div class="incident-tree" style="display:flex; flex-direction:column; gap:0.75rem; position:relative; padding-left:1.5rem; border-left:2px solid var(--border); margin-left:0.75rem;">';
            
            var firstMember = members[0];
            var rootTimestamp = firstMember ? firstMember.timestamp : data.started_at;
            
            for (var i = 0; i < timelineItems.length; i++) {
                var timelineItem = timelineItems[i];
                if (timelineItem.kind === 'change') {
                    html += renderChangeTimelineNode(timelineItem.value, rootTimestamp);
                    continue;
                }
                var m = timelineItem.value;
                var isRootAlert = !!firstMember && m.id === firstMember.id;
                
                // Calculate relative offset
                var offsetSecs = timestampOffsetSeconds(m.timestamp, rootTimestamp);
                var offsetStr = isRootAlert ? t('incidents.timeline.root') : formatRelativeOffset(offsetSecs);
                
                // Icon and color
                var dotColor = isRootAlert ? 'var(--primary, var(--primary))' : (m.importance === 'high' ? 'var(--danger, var(--danger))' : (m.importance === 'medium' ? 'var(--warning, var(--warning))' : 'var(--success, var(--success))'));
                var dotIcon = isRootAlert
                    ? wwIcon('target', 'icon-warning')
                    : (m.importance === 'high' ? '<span class="ww-dot ww-dot-danger"></span>'
                        : (m.importance === 'medium' ? '<span class="ww-dot ww-dot-warning"></span>' : '<span class="ww-dot ww-dot-success"></span>'));
                var borderStyle = isRootAlert ? 'border: 2px solid var(--primary); box-shadow: 0 0 8px var(--primary);' : 'border: 1px solid var(--border);';
                
                html += '<div class="tree-node" style="position:relative;">';
                
                // Indicator dot
                html += '<div class="tree-indicator" style="position:absolute; left:-2.05rem; top:4px; width:1.1rem; height:1.1rem; border-radius:50%; background:' + dotColor + '; display:flex; align-items:center; justify-content:center; font-size:0.6rem; color:white; font-weight:bold; box-shadow:0 0 0 3px var(--bg-surface); ' + borderStyle + '">';
                html += isRootAlert ? wwIcon('target', 'icon-warning icon-sm') : '';
                html += '</div>';
                
                // Card contents
                var rootBadge = isRootAlert ? '<span class="badge badge-outline" style="font-size:0.65rem; padding:1px 6px; margin-right:4px;">' + wwIcon('target', 'icon-primary') + ' ' + t('incidents.timeline.rootAlert') + '</span>' : '';
                var dupBadge = m.is_duplicate ? '<span class="badge badge-outline" style="font-size:0.65rem; padding:1px 4px; margin-left:4px;">' + t('incidents.timeline.duplicate') + '</span>' : '';
                
                html += '<div style="background:var(--bg-subtle, var(--bg-subtle)); border:1px solid var(--border); border-radius:6px; padding:0.6rem 0.85rem; display:flex; flex-direction:column; gap:4px;">' +
                    '<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">' +
                    '<div style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;">' +
                    rootBadge +
                    '<span style="font-size:0.8rem; font-weight:600; color:var(--text-main);">#' + m.id + '</span>' +
                    '<span class="badge badge-' + impClass(m.importance) + '" style="font-size:0.7rem; font-weight:600;">' + dotIcon + ' ' + escapeHtml(m.importance || t('common.unknown')) + '</span>' +
                    dupBadge +
                    '<span style="font-size:0.72rem; color:var(--text-muted); font-weight:500;">' + escapeHtml(m.source || '') + '</span>' +
                    '</div>' +
                    '<div style="font-size:0.72rem; font-weight:600; color:var(--text-muted); background:var(--bg-base); padding:2px 6px; border-radius:4px; border:1px solid var(--border);">' +
                    escapeHtml(offsetStr) +
                    '</div>' +
                    '</div>';
                
                if (m.summary) {
                    html += '<div style="font-size:0.78rem; color:var(--text-main); line-height:1.4; font-weight:500;">' + escapeHtml(m.summary) + '</div>';
                }
                
                html += '<div style="font-size:0.7rem; color:var(--text-muted); display:flex; justify-content:space-between;">' +
                    '<span>' + escapeHtml(m.timestamp ? m.timestamp.replace('T', ' ').slice(0, 19) : '') + '</span>' +
                    '<span>' + t('incidents.timeline.status') + ': ' + escapeHtml(m.forward_status || 'ingested') + '</span>' +
                    '</div>';
                
                html += '</div></div>'; // close tree-node and card contents
            }
            
            html += '</div>'; // close incident-tree timeline
        }

        return html;
    }

    function silenceIncidentSources(id) {
        var data = _detailCache[id];
        if (!data) return;
        var uniqueSources = uniqueIncidentSources(data.members || []);
        // Open the silence form pre-filled with the first source. The operator
        // can add more criteria before saving.
        if (typeof showQuickSilenceForm === 'function' && uniqueSources.length) {
            showQuickSilenceForm(uniqueSources[0], '', '', '', '');
        }
    }

    function init() {
        // No auto-load on init; content is lazy-loaded when the Incidents tab is opened.
    }

    async function closeIncident(id) {
        return openResolutionModal(id);
    }

    async function reopenIncident(id) {
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/reopen', { method: 'POST' });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            load();  // Refresh the list
        } catch (e) {
            alert(t('incidents.action.reopenFailed') + ': ' + (e && e.message || e));
        }
    }

    async function updateWorkflow(id, status, skipReload) {
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/workflow', {
                method: 'PUT', body: JSON.stringify({ workflow_status: status })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            delete _detailCache[id];
            if (!skipReload) await load();
            return true;
        } catch (e) {
            alert('Workflow update failed: ' + (e.message || e));
            throw e;
        }
    }

    async function assign(id, suggestedOwner, skipReload) {
        var data = _detailCache[id] || {};
        var assignee = String(suggestedOwner || '').trim();
        var body;
        if (assignee) {
            body = { assignee: assignee };
        } else {
            assignee = prompt('Assignee (leave empty to unassign)', data.assignee || '');
            if (assignee === null) return false;
            var team = prompt('Team (leave empty to clear)', data.team || '');
            if (team === null) return false;
            var sla = prompt('SLA in minutes (leave empty to keep current SLA)', '');
            if (sla === null) return false;
            body = { assignee: assignee, team: team };
            if (sla.trim()) body.sla_minutes = Number(sla);
        }
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/workflow', { method: 'PUT', body: JSON.stringify(body) });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            delete _detailCache[id];
            if (!skipReload) await load();
            return true;
        } catch (e) {
            alert('Assignment failed: ' + (e.message || e));
            throw e;
        }
    }

    function operatorIdentity(data) {
        try {
            var saved = window.localStorage.getItem('webhookwise_operator_name');
            if (saved) return saved;
        } catch (_error) {
            // Browser storage is optional; fall back to incident ownership.
        }
        return String(data && (data.assignee || data.team) || 'dashboard').trim() || 'dashboard';
    }

    function setResolutionValue(id, value) {
        var element = document.getElementById(id);
        if (element) element.value = value == null ? '' : String(value);
    }

    function resolutionEnvelope(payload) {
        var data = payload && payload.data || {};
        return data.resolution || data;
    }

    function renderResolutionCompleteness(completeness) {
        var container = document.getElementById('incidentResolutionProgress');
        if (!container) return;
        if (!completeness || typeof completeness !== 'object') {
            container.innerHTML = '';
            return;
        }
        var percent = Math.max(0, Math.min(100, Number(completeness.percent || 0)));
        var missing = Array.isArray(completeness.missing_fields)
            ? completeness.missing_fields.length
            : 0;
        container.innerHTML = '<div><span style="width:' + percent + '%"></span></div><strong>' +
            escapeHtml(t('resolution.completeness', {
                percent: Math.round(percent),
                missing: missing
            })) + '</strong>';
    }

    function relatedChangeLabel(change) {
        var identity = change.service || change.external_id || ('#' + change.change_id);
        var version = change.version_from || change.version_to
            ? ' · ' + (change.version_from || '—') + ' → ' + (change.version_to || '—')
            : '';
        return String(change.change_type || 'change') + ' · ' + String(identity) + version;
    }

    function resolutionFeedbackHtml(data, record) {
        var intelligence = data.intelligence || {};
        var similar = (intelligence.similar_incidents || [])[0];
        var executions = runbookExecutions(data);
        var execution = executions.find(function (item) {
            return item.status === 'completed' && (!item.effectiveness || item.effectiveness === 'unknown');
        });
        if (!similar && !execution) return '';
        var html = '<details class="resolution-feedback"><summary>' + wwIcon('sparkles') + ' ' +
            t('resolution.feedback.title') + '</summary><div>';
        if (similar) {
            html += '<label class="form-group"><span class="form-label">' +
                t('resolution.feedback.similar', { id: similar.incident_id }) +
                '</span><select class="form-input" id="incidentResolutionSimilarFeedback" data-candidate-ref="' +
                escapeHtml(encodedReference(similar.candidate_ref)) + '">' +
                '<option value="">' + t('resolution.feedback.notSure') + '</option>' +
                '<option value="used"' + (similar.feedback === 'used' ? ' selected' : '') + '>' +
                t('incidents.intelligence.used') + '</option>' +
                '<option value="not_used"' + (similar.feedback === 'not_used' ? ' selected' : '') + '>' +
                t('incidents.intelligence.notUsed') + '</option></select></label>';
        }
        if (execution) {
            html += '<label class="form-group"><span class="form-label">' +
                t('resolution.feedback.runbook', {
                    value: escapeHtml(execution.title || t('incidents.runbook.untitled'))
                }) + '</span><select class="form-input" id="incidentResolutionRunbookFeedback" ' +
                'data-execution-id="' + Number(execution.id) + '">' +
                '<option value="unknown">' + t('runbookCompletion.unknown') + '</option>' +
                '<option value="effective">' + t('incidents.runbook.effective') + '</option>' +
                '<option value="ineffective">' + t('incidents.runbook.ineffective') +
                '</option></select></label>';
        }
        return html + '<p>' + t('resolution.feedback.hint') + '</p></div></details>';
    }

    function populateResolutionModal(data, envelope) {
        var record = envelope.record || {};
        var intelligence = data.intelligence || {};
        var changes = intelligence.related_changes || [];
        var association = record.change_association || 'unknown';
        var selectedChangeId = record.related_change_id || '';

        setResolutionValue('incidentResolutionId', data.id);
        var categorySelector = document.getElementById('incidentResolutionCategory');
        if (categorySelector && record.root_cause_category &&
                !Array.from(categorySelector.options).some(function (option) {
                    return option.value === record.root_cause_category;
                })) {
            var savedCategory = document.createElement('option');
            savedCategory.value = record.root_cause_category;
            savedCategory.textContent = record.root_cause_category;
            categorySelector.appendChild(savedCategory);
        }
        setResolutionValue('incidentResolutionCategory', record.root_cause_category || '');
        setResolutionValue('incidentResolutionRootCause', record.root_cause || '');
        setResolutionValue('incidentResolutionBody', record.resolution || '');
        setResolutionValue('incidentResolutionImpact', record.impact || '');
        setResolutionValue(
            'incidentResolutionOwner',
            record.owner || data.assignee || data.team || ''
        );
        setResolutionValue('incidentResolutionRecoveryEvidence', record.recovery_evidence || '');
        setResolutionValue(
            'incidentResolutionFollowUps',
            Array.isArray(record.follow_ups) ? record.follow_ups.join('\n') : ''
        );
        setResolutionValue('incidentResolutionChangeAssociation', association);

        var selector = document.getElementById('incidentResolutionRelatedChangeId');
        if (selector) {
            selector.innerHTML = '<option value="">' +
                escapeHtml(t('resolution.relatedChangeNone')) + '</option>';
            changes.forEach(function (change) {
                if (!change.change_id) return;
                var option = document.createElement('option');
                option.value = String(change.change_id);
                option.textContent = relatedChangeLabel(change);
                option.dataset.candidateRef = String(change.candidate_ref || '');
                selector.appendChild(option);
            });
            if (selectedChangeId && !Array.from(selector.options).some(function (option) {
                return option.value === String(selectedChangeId);
            })) {
                var savedChange = document.createElement('option');
                savedChange.value = String(selectedChangeId);
                savedChange.textContent = t('resolution.relatedChangeSaved', {
                    id: selectedChangeId
                });
                savedChange.dataset.candidateRef = 'change:' + selectedChangeId;
                selector.appendChild(savedChange);
            }
            selector.value = String(selectedChangeId || '');
            selector.disabled = association === 'ruled_out' || association === 'unknown';
        }
        var associationSelector = document.getElementById('incidentResolutionChangeAssociation');
        if (associationSelector) {
            associationSelector.onchange = function () {
                var value = associationSelector.value;
                selector.disabled = value === 'ruled_out' || value === 'unknown';
                if (selector.disabled) selector.value = '';
            };
        }
        var subtitle = document.getElementById('incidentResolutionSubtitle');
        if (subtitle) subtitle.textContent = '#' + data.id + ' · ' + (data.title || '');
        var feedback = document.getElementById('incidentResolutionRecommendationFeedback');
        if (feedback) feedback.innerHTML = resolutionFeedbackHtml(data, record);
        renderResolutionCompleteness(envelope.completeness);
    }

    async function openResolutionModal(id) {
        var modal = document.getElementById('incidentResolutionModal');
        var error = document.getElementById('incidentResolutionError');
        if (!modal) return;
        if (error) error.textContent = '';
        modal.classList.add('active');
        var subtitle = document.getElementById('incidentResolutionSubtitle');
        if (subtitle) subtitle.textContent = t('common.loading');
        try {
            var detail = _detailCache[id];
            if (!detail) {
                var detailPayload = await API.getIncident(id);
                detail = detailPayload.data || {};
                _detailCache[id] = detail;
            }
            if (!detail.intelligence) {
                try {
                    var intelligenceResponse = await API.authenticatedFetch(
                        '/v1/incidents/' + id + '/intelligence'
                    );
                    if (intelligenceResponse.ok) {
                        var intelligencePayload = await intelligenceResponse.json();
                        detail.intelligence = intelligencePayload.data || {};
                    }
                } catch (_error) {
                    detail.intelligence = {};
                }
            }
            var resolutionPayload = await API.getIncidentResolution(id);
            populateResolutionModal(detail, resolutionEnvelope(resolutionPayload));
            document.getElementById('incidentResolutionRootCause')?.focus();
        } catch (e) {
            if (error) error.textContent = t('common.loadFailed') + ': ' + (e.message || String(e));
        }
    }

    function closeResolutionModal() {
        document.getElementById('incidentResolutionModal')?.classList.remove('active');
    }

    function resolutionFormPayload() {
        var id = Number(document.getElementById('incidentResolutionId')?.value || 0);
        var data = _detailCache[id] || {};
        var association = document.getElementById('incidentResolutionChangeAssociation')?.value || 'unknown';
        var rawChangeId = document.getElementById('incidentResolutionRelatedChangeId')?.value || '';
        var relatedChangeId = rawChangeId ? Number(rawChangeId) : null;
        if (['confirmed', 'suspected'].includes(association) && !relatedChangeId) {
            throw new Error(t('resolution.relatedChangeRequired'));
        }
        if (association === 'ruled_out' || association === 'unknown') relatedChangeId = null;
        var followUps = String(document.getElementById('incidentResolutionFollowUps')?.value || '')
            .split('\n')
            .map(function (item) { return item.trim(); })
            .filter(Boolean);
        return {
            root_cause_category: document.getElementById('incidentResolutionCategory')?.value || null,
            root_cause: document.getElementById('incidentResolutionRootCause')?.value.trim() || null,
            resolution: document.getElementById('incidentResolutionBody')?.value.trim() || null,
            impact: document.getElementById('incidentResolutionImpact')?.value.trim() || null,
            change_association: association,
            related_change_id: relatedChangeId,
            recovery_evidence: document.getElementById('incidentResolutionRecoveryEvidence')?.value.trim() || null,
            owner: document.getElementById('incidentResolutionOwner')?.value.trim() || null,
            follow_ups: Array.from(new Set(followUps)),
            actor: operatorIdentity(data)
        };
    }

    function setResolutionBusy(busy) {
        ['incidentResolutionDraftBtn', 'incidentResolutionCloseBtn'].forEach(function (id) {
            var button = document.getElementById(id);
            if (!button) return;
            button.disabled = !!busy;
            button.classList.toggle('is-busy', !!busy);
        });
    }

    async function saveResolutionDraft() {
        var id = Number(document.getElementById('incidentResolutionId')?.value || 0);
        var error = document.getElementById('incidentResolutionError');
        if (error) error.textContent = '';
        setResolutionBusy(true);
        try {
            var result = await API.saveIncidentResolution(id, resolutionFormPayload());
            renderResolutionCompleteness(resolutionEnvelope(result).completeness);
            if (error) {
                error.classList.add('is-success');
                error.textContent = t('resolution.saved');
            }
        } catch (e) {
            if (error) {
                error.classList.remove('is-success');
                error.textContent = e.message || String(e);
            }
        } finally {
            setResolutionBusy(false);
        }
    }

    async function submitResolutionFeedback(id, payload) {
        var data = _detailCache[id] || {};
        var tasks = [];
        var association = payload.change_association;
        var changeSelector = document.getElementById('incidentResolutionRelatedChangeId');
        var selectedOption = changeSelector && changeSelector.selectedOptions[0];
        var changeRef = selectedOption && selectedOption.dataset.candidateRef;
        if (changeRef && ['confirmed', 'ruled_out'].includes(association)) {
            tasks.push(API.recordIncidentIntelligenceFeedback(id, {
                recommendation_type: 'change',
                candidate_ref: changeRef,
                verdict: association === 'confirmed' ? 'relevant' : 'irrelevant',
                actor: operatorIdentity(data)
            }));
        }
        var similar = document.getElementById('incidentResolutionSimilarFeedback');
        if (similar && similar.value) {
            tasks.push(API.recordIncidentIntelligenceFeedback(id, {
                recommendation_type: 'similar_incident',
                candidate_ref: decodeURIComponent(similar.dataset.candidateRef || ''),
                verdict: similar.value,
                actor: operatorIdentity(data)
            }));
        }
        var runbook = document.getElementById('incidentResolutionRunbookFeedback');
        if (runbook && runbook.value !== 'unknown') {
            tasks.push(updateRunbookExecution(
                id,
                Number(runbook.dataset.executionId),
                { status: 'completed', effectiveness: runbook.value },
                true
            ));
        }
        var results = await Promise.allSettled(tasks);
        return results.filter(function (item) { return item.status === 'rejected'; }).length;
    }

    async function submitResolution() {
        var id = Number(document.getElementById('incidentResolutionId')?.value || 0);
        var error = document.getElementById('incidentResolutionError');
        if (error) error.textContent = '';
        setResolutionBusy(true);
        try {
            var payload = resolutionFormPayload();
            var result = await API.closeIncident(id, payload);
            var feedbackFailures = await submitResolutionFeedback(id, payload);
            delete _detailCache[id];
            closeResolutionModal();
            await load();
            if (typeof ResponseCenterModule !== 'undefined') {
                ResponseCenterModule.loadWorkQueue();
            }
            if (feedbackFailures) {
                window.alert(t('resolution.feedback.partialFailure', {
                    value: feedbackFailures
                }));
            }
            return result;
        } catch (e) {
            if (error) {
                error.classList.remove('is-success');
                error.textContent = e.message || String(e);
            }
            return null;
        } finally {
            setResolutionBusy(false);
        }
    }

    async function reviewRecurrence(id, decision) {
        var data = _detailCache[id] || {};
        try {
            var result = await API.reviewIncidentRecurrence(id, decision, {
                actor: operatorIdentity(data)
            });
            data.recurrence = result.data || null;
            _detailCache[id] = data;
            var detail = document.getElementById('incident-detail-' + id);
            if (detail) detail.innerHTML = renderDetail(data);
        } catch (e) {
            alert(t('incidents.recurrence.reviewFailed') + ': ' + (e.message || e));
        }
    }

    async function addNote(id) {
        var body = prompt('Operator note', '');
        if (!body || !body.trim()) return;
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/notes', {
                method: 'POST', body: JSON.stringify({ body: body.trim(), actor: 'dashboard' })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            delete _detailCache[id];
            var detail = document.getElementById('incident-detail-' + id);
            if (detail) detail.style.display = 'none';
            await toggle(id);
        } catch (e) { alert('Adding note failed: ' + (e.message || e)); }
    }

    async function feedback(id, verdict) {
        var comment = verdict === 'correct' ? '' : prompt('What should be corrected?', '');
        if (comment === null) return;
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/feedback', {
                method: 'POST', body: JSON.stringify({ verdict: verdict, comment: comment || null, actor: 'dashboard' })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            alert('Feedback recorded');
        } catch (e) { alert('Feedback failed: ' + (e.message || e)); }
    }

    async function intelligenceFeedback(id, recommendationType, encodedCandidateRef, verdict) {
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/intelligence/feedback', {
                method: 'POST',
                body: JSON.stringify({
                    recommendation_type: recommendationType,
                    candidate_ref: decodeURIComponent(encodedCandidateRef),
                    verdict: verdict,
                    actor: 'dashboard'
                })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            await loadIntelligence(id);
        } catch (e) {
            alert(t('incidents.intelligence.feedbackFailed') + ': ' + (e.message || e));
        }
    }

    async function startRunbookExecution(id, encodedCandidateRef) {
        var candidateRef = decodeURIComponent(encodedCandidateRef || '');
        if (!candidateRef) return;
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/runbook-executions', {
                method: 'POST',
                body: JSON.stringify({ candidate_ref: candidateRef, actor: 'dashboard' })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            await loadIntelligence(id);
        } catch (e) {
            alert(t('incidents.runbook.startFailed') + ': ' + (e.message || e));
        }
    }

    async function updateRunbookExecution(id, executionId, changes, skipReload) {
        var body = Object.assign({}, changes || {}, { actor: 'dashboard' });
        try {
            var resp = await API.authenticatedFetch(
                '/v1/incidents/' + id + '/runbook-executions/' + executionId,
                { method: 'PUT', body: JSON.stringify(body) }
            );
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            if (!skipReload) await loadIntelligence(id);
            return true;
        } catch (e) {
            if (!skipReload) {
                alert(t('incidents.runbook.updateFailed') + ': ' + (e.message || e));
            }
            throw e;
        }
    }

    async function toggleRunbookStep(id, executionId, stepIndex, completed) {
        await updateRunbookExecution(id, executionId, {
            step_index: stepIndex,
            step_completed: completed
        });
    }

    function completeRunbookExecution(id, executionId) {
        setResolutionValue('runbookCompletionIncidentId', id);
        setResolutionValue('runbookCompletionExecutionId', executionId);
        setResolutionValue('runbookCompletionEffectiveness', 'unknown');
        var data = _detailCache[id] || {};
        var execution = runbookExecutions(data).find(function (item) {
            return Number(item.id) === Number(executionId);
        });
        setResolutionValue('runbookCompletionNotes', execution && execution.notes || '');
        var error = document.getElementById('runbookCompletionError');
        if (error) error.textContent = '';
        document.getElementById('runbookCompletionModal')?.classList.add('active');
        document.getElementById('runbookCompletionEffectiveness')?.focus();
    }

    function closeRunbookCompletionModal() {
        document.getElementById('runbookCompletionModal')?.classList.remove('active');
    }

    async function submitRunbookCompletion() {
        var id = Number(document.getElementById('runbookCompletionIncidentId')?.value || 0);
        var executionId = Number(document.getElementById('runbookCompletionExecutionId')?.value || 0);
        var effectiveness = document.getElementById('runbookCompletionEffectiveness')?.value || 'unknown';
        var notes = document.getElementById('runbookCompletionNotes')?.value.trim() || '';
        var button = document.getElementById('runbookCompletionSubmitBtn');
        var error = document.getElementById('runbookCompletionError');
        if (error) error.textContent = '';
        if (button) {
            button.disabled = true;
            button.classList.add('is-busy');
        }
        try {
            var changes = {
                status: 'completed',
                effectiveness: effectiveness
            };
            if (notes) changes.notes = notes;
            await updateRunbookExecution(id, executionId, changes);
            closeRunbookCompletionModal();
        } catch (e) {
            if (error) error.textContent = e.message || String(e);
        } finally {
            if (button) {
                button.disabled = false;
                button.classList.remove('is-busy');
            }
        }
    }

    async function merge(id) {
        var value = prompt('Incident IDs to merge into #' + id + ' (comma separated)', '');
        if (!value) return;
        var ids = value.split(',').map(function (item) { return Number(item.trim()); }).filter(Number.isInteger);
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/merge', {
                method: 'POST', body: JSON.stringify({ source_incident_ids: ids })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            await load();
        } catch (e) { alert('Merge failed: ' + (e.message || e)); }
    }

    async function exportPostmortem(id) {
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/postmortem');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var blob = await resp.blob();
            var url = URL.createObjectURL(blob);
            var link = document.createElement('a');
            link.href = url;
            // Mirrors the backend Content-Disposition filename.
            link.download = 'postmortem-incident-' + id + '.md';
            document.body.appendChild(link);
            link.click();
            link.remove();
            // Delay revocation so the click-initiated download keeps its blob.
            setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        } catch (e) {
            alert(t('incidents.action.postmortemFailed') + ': ' + (e && e.message || e));
        }
    }

    async function split(id) {
        var value = prompt('Alert IDs to split into a new incident (comma separated)', '');
        if (!value) return;
        var ids = value.split(',').map(function (item) { return Number(item.trim()); }).filter(Number.isInteger);
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/split', {
                method: 'POST', body: JSON.stringify({ event_ids: ids })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            await load();
        } catch (e) { alert('Split failed: ' + (e.message || e)); }
    }

    async function focusLoadedIncident(id) {
        var data = _detailCache[id];
        var row = _rows.find(function (item) { return Number(item.id) === Number(id); });
        if (!data && !row) {
            try {
                var payload = await API.getIncident(id);
                data = payload.data || {};
                _detailCache[id] = data;
                _rows.unshift({
                    id: data.id,
                    title: data.title,
                    status: data.status,
                    workflow_status: data.workflow_status,
                    source: data.source,
                    alert_count: data.alert_count,
                    started_at: data.started_at,
                    top_importance: data.top_importance,
                    assignee: data.assignee,
                    team: data.team,
                    sla_due_at: data.sla_due_at
                });
                render();
                row = _rows[0];
            } catch (e) {
                alert(t('common.loadFailed') + ': ' + (e.message || e));
                return;
            }
        }
        var detail = document.getElementById('incident-detail-' + id);
        if (!detail) return;
        if (!data) {
            await toggle(id);
        } else {
            data.intelligenceLoading = !data.intelligence;
            data.recurrenceLoading = !data.recurrence;
            detail.style.display = 'block';
            detail.innerHTML = renderDetail(data);
            if (data.intelligenceLoading) loadIntelligence(id);
            if (data.recurrenceLoading) loadRecurrence(id);
        }
        document.getElementById('incident-' + id)?.scrollIntoView({
            behavior: 'smooth',
            block: 'start'
        });
    }

    // Arm the scroll-to target without navigating; the caller has already
    // arrived (or is about to). render() consumes _focusIncidentId.
    function focusIncident(id) {
        _focusIncidentId = Number(id);
    }

    function openFromQueue(id) {
        if (typeof openIncident === 'function') {
            openIncident(id);
            return;
        }
        focusIncident(id);
        if (typeof switchMainTab === 'function') switchMainTab('alerts');
        if (typeof setInboxView === 'function') setInboxView('incidents');
    }

    function search() {
        var term = (document.getElementById('incidentSearchInput') || {}).value || '';
        term = term.trim().toLowerCase();
        var container = document.getElementById('incidentsList');
        if (!container || !_rows.length) return;

        if (!term) { render(); return; }

        var filtered = _rows.filter(function (r) {
            return (r.title || '').toLowerCase().indexOf(term) >= 0 ||
                   (r.source || '').toLowerCase().indexOf(term) >= 0 ||
                   String(r.id) === term.replace(/^#/, '');
        });

        var html = '';
        for (var i = 0; i < filtered.length; i++) {
            // Reuse the same card rendering pattern from render()
            html += _cardHtml(filtered[i]);
        }
        if (!filtered.length) {
            html = '<div class="empty-state"><div class="empty-icon">' + wwIcon('search') + '</div><div class="empty-title">' + t('incidents.search.empty') + '</div></div>';
        }
        container.innerHTML = html;
    }

    function _cardHtml(row) {
        var badge = STATUS_BADGES[row.status] || { label: row.status, cls: 'badge-outline', icon: wwIcon('info') };
        var h = '<div class="incident-card" id="incident-' + row.id + '" style="border:1px solid var(--border); border-radius:8px; padding:1rem 1.25rem; margin-bottom:0.75rem; background:var(--bg-surface); cursor:pointer;" onclick="IncidentsModule.toggle(' + row.id + ')">';
        h += '<div style="display:flex; align-items:center; gap:0.75rem;">';
        h += '<span style="font-size:1.5rem;">' + badge.icon + '</span>';
        h += '<div style="flex:1; min-width:0;">';
        h += '<div style="display:flex; align-items:center; gap:0.5rem;">';
        h += '<span style="font-weight:600; font-size:1rem; color:var(--text-main);">' + escapeHtml(row.title) + '</span>';
        h += '<span class="badge ' + badge.cls + '" style="font-size:0.65rem;">' + badge.label + '</span>';
        h += '<span class="badge badge-outline" style="font-size:0.65rem;">' + escapeHtml((row.workflow_status || 'open').replace('_', ' ')) + '</span>';
        h += renderRecurrenceBadge(row.recurrence || row.recurrence_candidate);
        h += '</div>';
        h += '<div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.2rem;">';
        h += '<span>' + escapeHtml(row.source || '') + '</span> · ';
        h += '<span>' + row.alert_count + ' alerts</span> · ';
        h += '<span>' + (row.started_at ? row.started_at.slice(0, 16).replace('T', ' ') : '?') + '</span>';
        h += '</div></div>';
        if (row.status === 'active' || row.status === 'quiet') {
            h += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.openResolutionModal(' + row.id + ')" title="' + t('incidents.action.closeTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;">' + wwIcon('check') + '</button>';
        }
        if (row.status === 'closed') {
            h += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.openResolutionModal(' + row.id + ')" title="' + t('resolution.edit') + '" style="font-size:0.7rem; margin-left:0.5rem;">' + wwIcon('pencil') + '</button>';
            h += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.reopenIncident(' + row.id + ')" title="' + t('incidents.action.reopenTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;">' + wwIcon('refresh') + '</button>';
        }
        h += '<span style="color:var(--text-muted); font-size:0.8rem;">' + wwIcon('play') + '</span>';
        h += '</div>';
        h += '<div class="incident-detail" id="incident-detail-' + row.id + '" onclick="event.stopPropagation()" style="display:none; margin-top:0.75rem; padding-top:0.75rem; border-top:1px solid var(--border-light);"></div>';
        h += '</div>';
        return h;
    }

    return {
        init: init,
        load: load,
        toggle: toggle,
        render: render,
        search: search,
        toggleStatus: toggleStatus,
        closeIncident: closeIncident,
        openResolutionModal: openResolutionModal,
        closeResolutionModal: closeResolutionModal,
        saveResolutionDraft: saveResolutionDraft,
        submitResolution: submitResolution,
        reopenIncident: reopenIncident,
        updateWorkflow: updateWorkflow,
        assign: assign,
        addNote: addNote,
        feedback: feedback,
        intelligenceFeedback: intelligenceFeedback,
        startRunbookExecution: startRunbookExecution,
        updateRunbookExecution: updateRunbookExecution,
        toggleRunbookStep: toggleRunbookStep,
        completeRunbookExecution: completeRunbookExecution,
        closeRunbookCompletionModal: closeRunbookCompletionModal,
        submitRunbookCompletion: submitRunbookCompletion,
        reviewRecurrence: reviewRecurrence,
        openFromQueue: openFromQueue,
        focusIncident: focusIncident,
        merge: merge,
        split: split,
        exportPostmortem: exportPostmortem,
        silenceIncidentSources: silenceIncidentSources
    };
})();
