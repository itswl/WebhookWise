/**
 * Rule Audit Module — surfaces zombie, pure-noise, and flapping alert rules.
 *
 * Read-only aggregation over webhook_events + decision_trace. Fires on the
 * "Audit" sub-view under the Routing tab.
 */
const RuleAuditModule = (function () {
    'use strict';

    function flagBadge(flag) {
        var map = {
            zombie: { label: t('audit.flag.zombie'), cls: 'badge-medium' },
            pure_noise: { label: t('audit.flag.pureNoise'), cls: 'badge-low' },
            flapping: { label: t('audit.flag.flapping'), cls: 'badge-high' }
        };
        var f = map[flag] || { label: flag, cls: 'badge-outline' };
        return '<span class="badge ' + f.cls + '" style="font-size:0.7rem; margin-right:4px;">' + f.label + '</span>';
    }

    async function load() {
        var container = document.getElementById('ruleAuditResult');
        if (!container) return;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';

        var daysEl = document.getElementById('auditWindowDays');
        var windowDays = daysEl ? parseInt(daysEl.value, 10) || 30 : 30;

        try {
            var resp = await API.authenticatedFetch('/v1/forward-rules/audit?window_days=' + windowDays + '&min_events=3');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var result = await resp.json();
            var rows = result.data || [];
            render(container, rows);
        } catch (e) {
            container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--danger);">' + t('common.loadFailed') + ': ' + escapeHtml(String(e && e.message || e)) + '</div>';
        }
    }

    function render(container, rows) {
        if (!rows.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">✅</div><div class="empty-title" data-i18n="audit.empty.title">All rules look healthy</div><div class="empty-text" data-i18n="audit.empty.text">No zombie, pure-noise, or flapping rules detected in the selected window.</div></div>';
            return;
        }

        var flagged = rows.filter(function (r) { return r.flags && r.flags.length > 0; });
        var html = '';

        // Summary bar
        var zombies = rows.filter(function (r) { return r.flags.indexOf('zombie') >= 0; }).length;
        var noises = rows.filter(function (r) { return r.flags.indexOf('pure_noise') >= 0; }).length;
        var flaps = rows.filter(function (r) { return r.flags.indexOf('flapping') >= 0; }).length;
        html += '<div class="stats-grid" style="margin-bottom: 1.5rem;">';
        html += '<div class="stat-card"><div class="stat-label" style="color:var(--text-muted);">🧟 ' + t('audit.summary.zombies') + '</div><div class="stat-value" style="color:var(--warning);">' + zombies + '</div></div>';
        html += '<div class="stat-card"><div class="stat-label" style="color:var(--text-muted);">🔇 ' + t('audit.summary.noise') + '</div><div class="stat-value" style="color:var(--text-muted);">' + noises + '</div></div>';
        html += '<div class="stat-card"><div class="stat-label" style="color:var(--text-muted);">📈 ' + t('audit.summary.flapping') + '</div><div class="stat-value" style="color:var(--danger);">' + flaps + '</div></div>';
        html += '<div class="stat-card"><div class="stat-label" style="color:var(--text-muted);">📋 ' + t('audit.summary.total') + '</div><div class="stat-value">' + rows.length + '</div></div>';
        html += '</div>';

        // Table
        html += '<div style="overflow-x:auto;">';
        html += '<table class="data-table" style="width:100%; border-collapse:collapse; font-size:0.88rem;">';
        html += '<thead><tr style="border-bottom:2px solid var(--border); text-align:left; color:var(--text-muted); font-size:0.78rem; text-transform:uppercase; letter-spacing:0.04em;">';
        html += '<th style="padding:0.5rem 0.75rem;">' + t('audit.col.source') + '</th>';
        html += '<th style="padding:0.5rem 0.75rem;">' + t('audit.col.ruleName') + '</th>';
        html += '<th style="padding:0.5rem 0.75rem; text-align:right;">' + t('audit.col.events') + '</th>';
        html += '<th style="padding:0.5rem 0.75rem; text-align:right;">' + t('audit.col.duplicatePct') + '</th>';
        html += '<th style="padding:0.5rem 0.75rem; text-align:right;">' + t('audit.col.skipped') + '</th>';
        html += '<th style="padding:0.5rem 0.75rem;">' + t('audit.col.lastSeen') + '</th>';
        html += '<th style="padding:0.5rem 0.75rem;">' + t('audit.col.flags') + '</th>';
        html += '</tr></thead><tbody>';

        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var lastSeen = r.last_seen ? timeAgo(r.last_seen) : '—';
            var flagsHtml = (r.flags || []).map(flagBadge).join('');
            var rowStyle = (r.flags && r.flags.indexOf('zombie') >= 0) ? 'background:rgba(245,158,11,0.06);' : '';

            html += '<tr style="border-bottom:1px solid var(--border-light);' + rowStyle + '">';
            html += '<td style="padding:0.5rem 0.75rem; color:var(--text-muted);">' + escapeHtml(String(r.source)) + '</td>';
            html += '<td style="padding:0.5rem 0.75rem; font-weight:500;">' + escapeHtml(String(r.rule_name)) + '</td>';
            html += '<td style="padding:0.5rem 0.75rem; text-align:right; font-variant-numeric:tabular-nums;">' + r.total + '</td>';
            html += '<td style="padding:0.5rem 0.75rem; text-align:right; font-variant-numeric:tabular-nums;">' + r.duplicate_pct + '%</td>';
            html += '<td style="padding:0.5rem 0.75rem; text-align:right; font-variant-numeric:tabular-nums; color:' + (r.forwarded === 0 && r.total > 0 ? 'var(--warning)' : 'inherit') + ';">' + r.skipped + '</td>';
            html += '<td style="padding:0.5rem 0.75rem; color:var(--text-muted); font-size:0.82rem; white-space:nowrap;">' + lastSeen + '</td>';
            html += '<td style="padding:0.5rem 0.75rem;">' + (flagsHtml || '—') + '</td>';
            html += '</tr>';
        }
        html += '</tbody></table></div>';
        container.innerHTML = html;
    }

    return { load: load };
})();
