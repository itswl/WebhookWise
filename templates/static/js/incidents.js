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
            html += '</div>';
            html += '<div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.2rem;">';
            html += '<span>' + escapeHtml(row.source || '') + '</span> · ';
            html += '<span>' + row.alert_count + ' alerts</span> · ';
            html += '<span>' + (row.started_at ? row.started_at.slice(0, 16).replace('T', ' ') : '?') + '</span>';
            if (row.top_importance) {
                html += ' · <span>' + (row.top_importance === 'high' ? '🔴 high' : row.top_importance === 'medium' ? '🟠 medium' : '🟢 low') + '</span>';
            }
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
            html += '<div class="incident-detail" id="incident-detail-' + row.id + '" style="display:none; margin-top:0.75rem; padding-top:0.75rem; border-top:1px solid var(--border-light);"></div>';
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
            _detailCache[id] = data;
            detailEl.innerHTML = renderDetail(data);
        } catch (e) {
            detailEl.innerHTML = '<div style="color:var(--danger); padding:0.5rem;">' + t('common.loadFailed') + ': ' + escapeHtml(String(e && e.message || e)) + '</div>';
        }
    }

    function formatRelativeOffset(offsetSecs) {
        if (offsetSecs <= 0) return '+0s';
        if (offsetSecs < 60) return '+' + Math.round(offsetSecs) + 's';
        var mins = Math.floor(offsetSecs / 60);
        var secs = Math.round(offsetSecs % 60);
        if (mins < 60) {
            return '+' + mins + 'm' + (secs > 0 ? ' ' + secs + 's' : '');
        }
        var hours = Math.floor(mins / 60);
        mins = mins % 60;
        return '+' + hours + 'h' + (mins > 0 ? ' ' + mins + 'm' : '');
    }

    function impClass(importance) {
        if (importance === 'high') return 'danger';
        if (importance === 'medium') return 'medium';
        if (importance === 'low') return 'success';
        return 'outline';
    }

    function renderDetail(data) {
        var impEmoji = { high: '🔴', medium: '🟠', low: '🟢' };
        var members = data.members || [];
        var html = '';

        // Summary analysis section
        var summary = data.summary_analysis || {};
        if (summary.summary) {
            html += '<div style="background:var(--bg-base); border-radius:6px; padding:0.75rem 1rem; margin-bottom:1rem; border-left:3px solid var(--primary);">';
            html += '<div style="font-weight:600; margin-bottom:0.25rem; font-size:0.85rem;">🧠 ' + t('incidents.llmSummary') + '</div>';
            html += '<div style="font-size:0.85rem; color:var(--text-main); line-height:1.4;">' + escapeHtml(String(summary.summary || '')) + '</div>';
            if (summary.root_cause) {
                html += '<div style="margin-top:0.4rem; font-size:0.82rem;"><span style="color:var(--text-muted);">' + t('incidents.rootCause') + ':</span> <strong>' + escapeHtml(String(summary.root_cause)) + '</strong></div>';
            }
            if (summary.confidence) {
                html += '<div style="margin-top:0.4rem; font-size:0.78rem; color:var(--text-muted);">' + t('incidents.confidence') + ': ' + Number(summary.confidence).toFixed(2) + '</div>';
            }
            html += '</div>';
        } else if (data.status === 'quiet') {
            html += '<div style="padding:0.5rem; margin-bottom:1rem; font-size:0.82rem; color:var(--text-muted);">💬 ' + t('incidents.summaryPending') + '</div>';
        }

        // Member alert timeline
        if (members.length) {
            html += '<div style="font-weight:600; font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem; text-transform:uppercase; letter-spacing:0.04em;">📅 ' + t('incidents.timeline') + ' (' + members.length + ')</div>';
            
            // Timeline tree list
            html += '<div class="incident-tree" style="display:flex; flex-direction:column; gap:0.75rem; position:relative; padding-left:1.5rem; border-left:2px solid var(--border); margin-left:0.75rem;">';
            
            var firstMember = members[0];
            
            for (var i = 0; i < members.length; i++) {
                var m = members[i];
                var isRootAlert = (i === 0);
                
                // Calculate relative offset
                var offsetSecs = 0;
                if (m.timestamp && firstMember.timestamp) {
                    offsetSecs = (new Date(m.timestamp) - new Date(firstMember.timestamp)) / 1000;
                }
                var offsetStr = isRootAlert ? 'Root 首发' : formatRelativeOffset(offsetSecs);
                
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
                var rootBadge = isRootAlert ? '<span class="badge badge-high" style="font-size:0.65rem; padding:1px 6px; background:var(--primary); color:white; font-weight:bold; border-radius:4px; margin-right:4px;">🏆 Root Cause 首发</span>' : '';
                var dupBadge = m.is_duplicate ? '<span class="badge badge-outline" style="font-size:0.65rem; padding:1px 4px; margin-left:4px;">duplicate</span>' : '';
                
                html += '<div style="background:var(--bg-subtle, #f8fafc); border:1px solid var(--border); border-radius:6px; padding:0.6rem 0.85rem; display:flex; flex-direction:column; gap:4px;">' +
                    '<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">' +
                    '<div style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;">' +
                    rootBadge +
                    '<span style="font-size:0.8rem; font-weight:600; color:var(--text-main);">#' + m.id + '</span>' +
                    '<span class="badge badge-' + impClass(m.importance) + '" style="font-size:0.7rem; font-weight:600;">' + dotIcon + ' ' + escapeHtml(m.importance || 'unknown') + '</span>' +
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
                    '<span>Status: ' + escapeHtml(m.forward_status || 'ingested') + '</span>' +
                    '</div>';
                
                html += '</div></div>'; // close tree-node and card contents
            }
            
            html += '</div>'; // close incident-tree timeline
        }

        // Action: silence all sources in this incident
        if (members.length) {
            var sources = {};
            members.forEach(function (m) {
                var s = (m.source || '').trim();
                if (s) sources[s] = true;
            });
            var uniqueSources = Object.keys(sources);
            if (uniqueSources.length) {
                html += '<div style="margin-top:1rem; padding-top:0.75rem; border-top:1px solid var(--border-light);">';
                html += '<button class="btn btn-sm btn-warn" onclick="IncidentsModule.silenceIncidentSources(' + data.id + ')" style="font-size:0.75rem;">🔕 ' + t('incidents.action.silenceAll') + ' (' + uniqueSources.length + ')</button>';
                html += '</div>';
            }
        }

        return html;
    }

    function silenceIncidentSources(id) {
        var data = _detailCache[id];
        if (!data) return;
        var members = data.members || [];
        var sources = {};
        members.forEach(function (m) {
            var s = (m.source || '').trim();
            if (s) sources[s] = true;
        });
        var uniqueSources = Object.keys(sources);
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
        h += '<div class="incident-detail" id="incident-detail-' + row.id + '" style="display:none; margin-top:0.75rem; padding-top:0.75rem; border-top:1px solid var(--border-light);"></div>';
        h += '</div>';
        return h;
    }

    return {
        init: init,
        load: load,
        toggle: toggle,
        render: render,
        search: search,
        closeIncident: closeIncident,
        reopenIncident: reopenIncident,
        silenceIncidentSources: silenceIncidentSources
    };
})();
