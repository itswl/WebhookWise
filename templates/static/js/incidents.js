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

    var STATUS_BADGES = {
        active: { label: 'Active', cls: 'badge-high', icon: '🔥' },
        quiet: { label: 'Quiet', cls: 'badge-medium', icon: '🔇' },
        closed: { label: 'Closed', cls: 'badge-low', icon: '✅' }
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
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">✅</div><div class="empty-title">' + t('incidents.empty.title') + '</div><div class="empty-text">' + t('incidents.empty.text') + '</div></div>';
            return;
        }

        var html = '';
        for (var i = 0; i < _rows.length; i++) {
            var row = _rows[i];
            var badge = STATUS_BADGES[row.status] || { label: row.status, cls: 'badge-outline', icon: '❓' };
            html += '<div class="incident-card" id="incident-' + row.id + '" style="border:1px solid var(--border); border-radius:8px; padding:1rem 1.25rem; margin-bottom:0.75rem; background:var(--bg-surface); cursor:pointer;" onclick="IncidentsModule.toggle(' + row.id + ')">';
            html += '<div style="display:flex; align-items:center; gap:0.75rem;">';
            html += '<span style="font-size:1.5rem;">' + badge.icon + '</span>';
            html += '<div style="flex:1; min-width:0;">';
            html += '<div style="display:flex; align-items:center; gap:0.5rem;">';
            html += '<span style="font-weight:600; font-size:1rem; color:var(--text-main);">' + escapeHtml(row.title) + '</span>';
            html += '<span class="badge ' + badge.cls + '" style="font-size:0.65rem;">' + badge.label + '</span>';
            html += '<span class="badge badge-outline" style="font-size:0.65rem;">' + escapeHtml((row.workflow_status || 'open').replace('_', ' ')) + '</span>';
            html += '</div>';
            html += '<div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.2rem;">';
            html += '<span>' + escapeHtml(row.source || '') + '</span> · ';
            html += '<span>' + row.alert_count + ' alerts</span> · ';
            html += '<span>' + (row.started_at ? row.started_at.slice(0, 16).replace('T', ' ') : '?') + '</span>';
            if (row.top_importance) {
                html += ' · <span>' + (row.top_importance === 'high' ? '🔴 high' : row.top_importance === 'medium' ? '🟠 medium' : '🟢 low') + '</span>';
            }
            if (row.assignee || row.team) {
                html += ' · <span>👤 ' + escapeHtml(row.assignee || 'Unassigned') + (row.team ? ' / ' + escapeHtml(row.team) : '') + '</span>';
            }
            if (row.sla_due_at) html += ' · <span>⏰ ' + escapeHtml(row.sla_due_at.slice(0, 16).replace('T', ' ')) + '</span>';
            html += '</div>';
            html += '</div>';
            // Action buttons: close / reopen (stop propagation so they don't toggle the card)
            if (row.status === 'active' || row.status === 'quiet') {
                html += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.closeIncident(' + row.id + ')" title="' + t('incidents.action.closeTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;">✅</button>';
            }
            if (row.status === 'closed') {
                html += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.reopenIncident(' + row.id + ')" title="' + t('incidents.action.reopenTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;">🔄</button>';
            }
            html += '<span style="color:var(--text-muted); font-size:0.8rem;">▶</span>';
            html += '</div>';

            // Expandable detail (hidden by default)
            html += '<div class="incident-detail" id="incident-detail-' + row.id + '" onclick="event.stopPropagation()" style="display:none; margin-top:0.75rem; padding-top:0.75rem; border-top:1px solid var(--border-light);"></div>';
            html += '</div>';
        }

        container.innerHTML = html;
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
            _detailCache[id] = data;
            detailEl.innerHTML = renderDetail(data);
            loadIntelligence(id);
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

    function intelligenceScore(score) {
        return '<span class="incident-intelligence-score">' +
            t('incidents.intelligence.score', { value: Math.round(Number(score || 0) * 100) }) +
            '</span>';
    }

    function intelligenceFeedbackButtons(incidentId, recommendationType, candidateRef, verdict) {
        var encodedRef = encodedReference(candidateRef);
        var adoptionFeedback = recommendationType === 'runbook' ||
            recommendationType === 'similar_incident';
        var positiveVerdict = adoptionFeedback ? 'used' : 'relevant';
        var negativeVerdict = adoptionFeedback ? 'not_used' : 'irrelevant';
        var positiveLabel = adoptionFeedback ? 'incidents.intelligence.used' : 'incidents.intelligence.relevant';
        var negativeLabel = adoptionFeedback ? 'incidents.intelligence.notUsed' : 'incidents.intelligence.irrelevant';
        var relevantClass = verdict === 'relevant' || verdict === 'used' ? ' active' : '';
        var irrelevantClass = verdict === 'irrelevant' || verdict === 'not_used' ? ' active' : '';
        return '<div class="incident-intelligence-feedback">' +
            '<button class="btn btn-sm incident-intelligence-feedback-btn' + relevantClass + '" ' +
            'onclick="event.stopPropagation(); IncidentsModule.intelligenceFeedback(' + incidentId + ',\'' +
            recommendationType + '\',\'' + encodedRef + '\',\'' + positiveVerdict + '\')">✓ ' +
            t(positiveLabel) + '</button>' +
            '<button class="btn btn-sm incident-intelligence-feedback-btn' + irrelevantClass + '" ' +
            'onclick="event.stopPropagation(); IncidentsModule.intelligenceFeedback(' + incidentId + ',\'' +
            recommendationType + '\',\'' + encodedRef + '\',\'' + negativeVerdict + '\')">× ' +
            t(negativeLabel) + '</button>' +
            '</div>';
    }

    function renderSimilarIncident(item, incidentId) {
        var resolution = Array.isArray(item.resolution) ? item.resolution.join('；') : (item.resolution || '');
        var html = '<article class="incident-intelligence-card">';
        html += '<div class="incident-intelligence-card-head"><strong>#' + item.incident_id + ' ' +
            escapeHtml(item.title || '') + '</strong>' + intelligenceScore(item.score) + '</div>';
        if (item.root_cause) {
            html += '<p><span>' + t('incidents.rootCause') + ':</span> ' + escapeHtml(String(item.root_cause)) + '</p>';
        } else if (resolution) {
            html += '<p>' + escapeHtml(String(resolution)) + '</p>';
        }
        html += '<div class="incident-intelligence-reasons">' + intelligenceReasons(item.reasons) + '</div>';
        html += intelligenceFeedbackButtons(
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
            '</strong>' + intelligenceScore(item.score) + '</div>';
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
        html += intelligenceFeedbackButtons(incidentId, 'change', item.candidate_ref, item.feedback);
        return html + '</article>';
    }

    function renderRunbook(item, incidentId, data) {
        var execution = executionForCandidate(data, item.candidate_ref);
        var encodedRef = encodedReference(item.candidate_ref);
        var html = '<article class="incident-intelligence-card">';
        html += '<div class="incident-intelligence-card-head"><strong>' + escapeHtml(item.title || '') +
            '</strong>' + intelligenceScore(item.score) + '</div>';
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
                ',\'' + encodedRef + '\')">▶ ' + t('incidents.runbook.start') + '</button>';
        }
        html += intelligenceFeedbackButtons(incidentId, 'runbook', item.candidate_ref, item.feedback);
        return html + '</article>';
    }

    function renderIntelligence(data, embedded) {
        var heading = embedded ? '' : '<div class="incident-intelligence-title">✨ ' +
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
                icon: '🕘',
                title: t('incidents.intelligence.similar'),
                items: intelligence.similar_incidents || [],
                render: renderSimilarIncident
            },
            {
                icon: '🚀',
                title: t('incidents.intelligence.changes'),
                items: intelligence.related_changes || [],
                render: renderRelatedChange
            },
            {
                icon: '📘',
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
            '<summary>✨ ' + t('incidents.evidence.title') +
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
        html += '<div class="tree-indicator incident-timeline-change-indicator">🚀</div>';
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

    function renderIncidentToolbar(data, members) {
        var workflowStatus = data.workflow_status || 'open';
        var terminal = workflowStatus === 'resolved' || workflowStatus === 'ignored';
        var sources = uniqueIncidentSources(members);
        var secondaryActions = [];

        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.assign(' +
            data.id + ')">👤 ' + t('alerts.action.assign') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.feedback(' +
            data.id + ',\'correct\')">👍 ' + t('alerts.action.feedbackCorrect') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.feedback(' +
            data.id + ',\'grouping_wrong\')">👎 ' + t('incidents.action.groupingWrong') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.merge(' +
            data.id + ')">🔗 ' + t('incidents.action.merge') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.split(' +
            data.id + ')">✂️ ' + t('incidents.action.split') + '</button>'
        );
        secondaryActions.push(
            '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.exportPostmortem(' +
            data.id + ')">📄 ' + t('incidents.action.postmortem') + '</button>'
        );
        if (sources.length) {
            secondaryActions.push(
                '<button type="button" class="btn btn-sm btn-warn" onclick="event.stopPropagation(); ' +
                'IncidentsModule.silenceIncidentSources(' + data.id + ')">🔕 ' +
                t('incidents.action.silenceAll') + ' (' + sources.length + ')</button>'
            );
        }
        if (terminal || data.status === 'closed') {
            secondaryActions.push(
                '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.reopenIncident(' +
                data.id + ')">🔄 ' + t('incidents.action.reopen') + '</button>'
            );
        }

        var html = '<div class="incident-command-bar">';
        html += '<div class="incident-command-meta">';
        html += '<strong>' + escapeHtml(t('alerts.workflow.' + workflowStatus)) + '</strong>';
        html += '<span>👤 ' + t('incidents.owner') + ': ' +
            escapeHtml(data.assignee || t('incidents.unassigned')) +
            (data.team ? ' / ' + escapeHtml(data.team) : '') + '</span>';
        html += '<span>⏰ ' + t('incidents.sla') + ': ' +
            escapeHtml(data.sla_due_at ? data.sla_due_at.replace('T', ' ').slice(0, 19) : t('incidents.notSet')) +
            '</span></div>';
        html += '<div class="incident-command-actions alert-primary-actions">';
        if (workflowStatus === 'open') {
            html += '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); ' +
                'IncidentsModule.updateWorkflow(' + data.id + ',\'acknowledged\')">👋 ' +
                t('alerts.action.acknowledge') + '</button>';
        }
        if (!terminal) {
            html += '<button type="button" class="btn btn-sm btn-primary" onclick="event.stopPropagation(); ' +
                'IncidentsModule.updateWorkflow(' + data.id + ',\'resolved\')">✅ ' +
                t('alerts.action.resolve') + '</button>';
        }
        html += '<button type="button" class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.addNote(' +
            data.id + ')">📝 ' + t('alerts.action.notes') + '</button>';
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
        return '<div class="incident-service-profile"><strong>🧭 ' +
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
        html += '<div class="incident-command-grid">';

        html += '<article class="incident-command-card"><div class="incident-command-label">📣 ' +
            t('incidents.command.whatHappened') + '</div><div class="incident-command-body">' +
            escapeHtml(happened || t('incidents.command.noSummary')) + '</div>';
        var impactText = displayText(firstDefined(command, ['impact'])) || displayText(summary.impact);
        if (impactText) {
            html += '<div class="incident-command-card-meta"><strong>' + t('incidents.command.impact') +
                ':</strong> ' + escapeHtml(impactText) + '</div>';
        }
        html += '</article>';

        html += '<article class="incident-command-card"><div class="incident-command-label">🧩 ' +
            t('incidents.command.likelyCause') + '</div><div class="incident-command-body">' +
            escapeHtml(cause || t('incidents.command.noCause')) + '</div>';
        var confidence = firstDefined(command, ['confidence']);
        if (confidence == null) confidence = summary.confidence;
        if (confidence != null && Number.isFinite(Number(confidence))) {
            html += '<div class="incident-command-card-meta">' + t('incidents.confidence') + ': ' +
                Math.round(Number(confidence) * 100) + '%</div>';
        }
        html += '</article>';

        html += '<article class="incident-command-card"><div class="incident-command-label">🚀 ' +
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

        html += '<article class="incident-command-card"><div class="incident-command-label">🛠️ ' +
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
                    ',\'' + encodedReference(recommendedRunbook.candidate_ref) + '\')">▶ ' +
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
        html += '<div class="incident-runbook-execution-head"><div><span>📘</span><strong>' +
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
                    (completed ? '✓' : (index + 1)) + '</span><strong>' +
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
                'IncidentsModule.completeRunbookExecution(' + incidentId + ',' + executionId + ')">✅ ' +
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
                executionId + ',{effectiveness:\'effective\'})">👍 ' +
                t('incidents.runbook.effective') + '</button>';
            html += '<button type="button" class="btn btn-sm' +
                (execution.effectiveness === 'ineffective' ? ' active' : '') +
                '" onclick="event.stopPropagation(); IncidentsModule.updateRunbookExecution(' + incidentId + ',' +
                executionId + ',{effectiveness:\'ineffective\'})">👎 ' +
                t('incidents.runbook.ineffective') + '</button>';
        }
        html += '<span class="incident-runbook-manual-note">' +
            t('incidents.runbook.manualOnly') + '</span></div>';
        return html + '</article>';
    }

    function renderRunbookExecutions(data) {
        var executions = runbookExecutions(data);
        if (!executions.length) return '';
        var html = '<section class="incident-runbook-section"><div class="incident-section-title">📘 ' +
            t('incidents.runbook.executionTitle') + '</div>';
        executions.slice(0, 3).forEach(function (execution) {
            html += renderRunbookExecution(execution, data.id);
        });
        return html + '</section>';
    }

    function renderOperatorNotes(notes) {
        if (!notes.length) return '';
        var html = '<details class="incident-supporting-details incident-notes" onclick="event.stopPropagation()">' +
            '<summary>📝 ' + t('incidents.notes.title') +
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
            html += '<div class="incident-summary-state is-warning">⚠️ ' +
                t('incidents.summaryFailed') + '</div>';
        } else if (data.summary_status === 'pending' || data.summary_status === 'retrying' ||
                   data.summary_status === 'processing') {
            html += '<div class="incident-summary-state">💬 ' + t('incidents.summaryPending') + '</div>';
        }
        html += renderOperatorNotes(data.notes || []);
        html += renderSupportingEvidence(data);

        // Chronological alert and suspected-change timeline
        var relatedChanges = (data.intelligence && data.intelligence.related_changes) || [];
        var timelineItems = buildIncidentTimeline(members, relatedChanges);
        if (timelineItems.length) {
            html += '<div style="font-weight:600; font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem; text-transform:uppercase; letter-spacing:0.04em;">📅 ' +
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
                var dotColor = isRootAlert ? 'var(--primary, #6366f1)' : (m.importance === 'high' ? 'var(--danger, #ef4444)' : (m.importance === 'medium' ? 'var(--warning, #f59e0b)' : 'var(--success, #10b981)'));
                var dotIcon = isRootAlert ? '👑' : (m.importance === 'high' ? '🔴' : (m.importance === 'medium' ? '🟠' : '🟢'));
                var borderStyle = isRootAlert ? 'border: 2px solid var(--primary); box-shadow: 0 0 8px var(--primary);' : 'border: 1px solid var(--border);';
                
                html += '<div class="tree-node" style="position:relative;">';
                
                // Indicator dot
                html += '<div class="tree-indicator" style="position:absolute; left:-2.05rem; top:4px; width:1.1rem; height:1.1rem; border-radius:50%; background:' + dotColor + '; display:flex; align-items:center; justify-content:center; font-size:0.6rem; color:white; font-weight:bold; box-shadow:0 0 0 3px var(--bg-surface); ' + borderStyle + '">';
                html += isRootAlert ? '★' : '';
                html += '</div>';
                
                // Card contents
                var rootBadge = isRootAlert ? '<span class="badge badge-high" style="font-size:0.65rem; padding:1px 6px; background:var(--primary); color:white; font-weight:bold; border-radius:4px; margin-right:4px;">🏆 ' + t('incidents.timeline.rootAlert') + '</span>' : '';
                var dupBadge = m.is_duplicate ? '<span class="badge badge-outline" style="font-size:0.65rem; padding:1px 4px; margin-left:4px;">' + t('incidents.timeline.duplicate') + '</span>' : '';
                
                html += '<div style="background:var(--bg-subtle, #f8fafc); border:1px solid var(--border); border-radius:6px; padding:0.6rem 0.85rem; display:flex; flex-direction:column; gap:4px;">' +
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
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/close', { method: 'POST' });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            load();  // Refresh the list
        } catch (e) {
            alert(t('incidents.action.closeFailed') + ': ' + (e && e.message || e));
        }
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

    async function updateWorkflow(id, status) {
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/workflow', {
                method: 'PUT', body: JSON.stringify({ workflow_status: status })
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            delete _detailCache[id];
            await load();
        } catch (e) {
            alert('Workflow update failed: ' + (e.message || e));
        }
    }

    async function assign(id) {
        var data = _detailCache[id] || {};
        var assignee = prompt('Assignee (leave empty to unassign)', data.assignee || '');
        if (assignee === null) return;
        var team = prompt('Team (leave empty to clear)', data.team || '');
        if (team === null) return;
        var sla = prompt('SLA in minutes (leave empty to keep current SLA)', '');
        if (sla === null) return;
        var body = { assignee: assignee, team: team };
        if (sla.trim()) body.sla_minutes = Number(sla);
        try {
            var resp = await API.authenticatedFetch('/v1/incidents/' + id + '/workflow', { method: 'PUT', body: JSON.stringify(body) });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            delete _detailCache[id];
            await load();
        } catch (e) { alert('Assignment failed: ' + (e.message || e)); }
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

    async function updateRunbookExecution(id, executionId, changes) {
        var body = Object.assign({}, changes || {}, { actor: 'dashboard' });
        try {
            var resp = await API.authenticatedFetch(
                '/v1/incidents/' + id + '/runbook-executions/' + executionId,
                { method: 'PUT', body: JSON.stringify(body) }
            );
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            await loadIntelligence(id);
        } catch (e) {
            alert(t('incidents.runbook.updateFailed') + ': ' + (e.message || e));
        }
    }

    async function toggleRunbookStep(id, executionId, stepIndex, completed) {
        await updateRunbookExecution(id, executionId, {
            step_index: stepIndex,
            step_completed: completed
        });
    }

    async function completeRunbookExecution(id, executionId) {
        var notes = prompt(t('incidents.runbook.completionNotes'), '');
        if (notes === null) return;
        var changes = { status: 'completed', effectiveness: 'unknown' };
        if (notes.trim()) changes.notes = notes.trim();
        await updateRunbookExecution(id, executionId, changes);
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

    function search() {
        var term = (document.getElementById('incidentSearchInput') || {}).value || '';
        term = term.trim().toLowerCase();
        var container = document.getElementById('incidentsList');
        if (!container || !_rows.length) return;

        if (!term) { render(); return; }

        var filtered = _rows.filter(function (r) {
            return (r.title || '').toLowerCase().indexOf(term) >= 0 ||
                   (r.source || '').toLowerCase().indexOf(term) >= 0;
        });

        var html = '';
        for (var i = 0; i < filtered.length; i++) {
            // Reuse the same card rendering pattern from render()
            html += _cardHtml(filtered[i]);
        }
        if (!filtered.length) {
            html = '<div class="empty-state"><div class="empty-icon">🔍</div><div class="empty-title">' + t('incidents.search.empty') + '</div></div>';
        }
        container.innerHTML = html;
    }

    function _cardHtml(row) {
        var badge = STATUS_BADGES[row.status] || { label: row.status, cls: 'badge-outline', icon: '❓' };
        var h = '<div class="incident-card" id="incident-' + row.id + '" style="border:1px solid var(--border); border-radius:8px; padding:1rem 1.25rem; margin-bottom:0.75rem; background:var(--bg-surface); cursor:pointer;" onclick="IncidentsModule.toggle(' + row.id + ')">';
        h += '<div style="display:flex; align-items:center; gap:0.75rem;">';
        h += '<span style="font-size:1.5rem;">' + badge.icon + '</span>';
        h += '<div style="flex:1; min-width:0;">';
        h += '<div style="display:flex; align-items:center; gap:0.5rem;">';
        h += '<span style="font-weight:600; font-size:1rem; color:var(--text-main);">' + escapeHtml(row.title) + '</span>';
        h += '<span class="badge ' + badge.cls + '" style="font-size:0.65rem;">' + badge.label + '</span>';
        h += '<span class="badge badge-outline" style="font-size:0.65rem;">' + escapeHtml((row.workflow_status || 'open').replace('_', ' ')) + '</span>';
        h += '</div>';
        h += '<div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.2rem;">';
        h += '<span>' + escapeHtml(row.source || '') + '</span> · ';
        h += '<span>' + row.alert_count + ' alerts</span> · ';
        h += '<span>' + (row.started_at ? row.started_at.slice(0, 16).replace('T', ' ') : '?') + '</span>';
        h += '</div></div>';
        if (row.status === 'active' || row.status === 'quiet') {
            h += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.closeIncident(' + row.id + ')" title="' + t('incidents.action.closeTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;">✅</button>';
        }
        if (row.status === 'closed') {
            h += '<button class="btn btn-sm" onclick="event.stopPropagation(); IncidentsModule.reopenIncident(' + row.id + ')" title="' + t('incidents.action.reopenTitle') + '" style="font-size:0.7rem; margin-left:0.5rem;">🔄</button>';
        }
        h += '<span style="color:var(--text-muted); font-size:0.8rem;">▶</span>';
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
        merge: merge,
        split: split,
        exportPostmortem: exportPostmortem,
        silenceIncidentSources: silenceIncidentSources
    };
})();
