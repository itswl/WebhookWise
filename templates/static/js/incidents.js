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

    function renderDetail(data) {
        var impEmoji = { high: '🔴', medium: '🟠', low: '🟢' };
        var members = data.members || [];
        var html = '';

        // Summary analysis section
        var summary = data.summary_analysis || {};
        if (summary.summary) {
            html += '<div style="background:var(--bg-base); border-radius:6px; padding:0.75rem 1rem; margin-bottom:0.75rem; border-left:3px solid var(--primary);">';
            html += '<div style="font-weight:600; margin-bottom:0.25rem; font-size:0.85rem;">🧠 ' + t('incidents.llmSummary') + '</div>';
            html += '<div style="font-size:0.85rem; color:var(--text-main);">' + escapeHtml(String(summary.summary || '')) + '</div>';
            if (summary.root_cause) {
                html += '<div style="margin-top:0.4rem; font-size:0.82rem;"><span style="color:var(--text-muted);">' + t('incidents.rootCause') + ':</span> ' + escapeHtml(String(summary.root_cause)) + '</div>';
            }
            if (summary.confidence) {
                html += '<div style="margin-top:0.4rem; font-size:0.78rem; color:var(--text-muted);">' + t('incidents.confidence') + ': ' + Number(summary.confidence).toFixed(2) + '</div>';
            }
            html += '</div>';
        } else if (data.status === 'quiet') {
            html += '<div style="padding:0.5rem; margin-bottom:0.75rem; font-size:0.82rem; color:var(--text-muted);">💬 ' + t('incidents.summaryPending') + '</div>';
        }

        // Member alert timeline
        if (members.length) {
            html += '<div style="font-weight:600; font-size:0.8rem; color:var(--text-muted); margin-bottom:0.5rem; text-transform:uppercase; letter-spacing:0.04em;">📅 ' + t('incidents.timeline') + ' (' + members.length + ')</div>';
            for (var i = 0; i < members.length; i++) {
                var m = members[i];
                html += '<div style="display:flex; align-items:flex-start; gap:0.5rem; padding:0.35rem 0; border-left:2px solid var(--border); padding-left:0.75rem; margin-left:0.25rem;">';
                html += '<div style="font-size:0.7rem; color:var(--text-muted); min-width:3.5rem; text-align:right; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' + escapeHtml(m.timestamp ? m.timestamp.slice(11, 19) : '') + '</div>';
                html += '<div style="flex:1; min-width:0;">';
                html += '<span style="font-size:0.78rem; font-weight:500;">#' + m.id + '</span> ';
                html += '<span>' + (impEmoji[m.importance] || '') + ' ' + escapeHtml(m.importance || '') + '</span> ';
                html += '<span style="font-size:0.72rem; color:var(--text-muted);">' + escapeHtml(m.source || '') + '</span>';
                if (m.summary) {
                    html += '<div style="font-size:0.75rem; color:var(--text-muted); line-height:1.3; margin-top:0.1rem;">' + escapeHtml(m.summary.slice(0, 160)) + '</div>';
                }
                html += '</div></div>';
            }
        }

        return html;
    }

    function init() {
        // No auto-load on init; content is lazy-loaded when the Incidents tab is opened.
    }

    return {
        init: init,
        load: load,
        toggle: toggle,
        render: render
    };
})();
