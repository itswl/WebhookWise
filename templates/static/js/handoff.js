/**
 * Handoff & Audit — what happened, and who changed what.
 *
 * Both endpoints have worked for a long time and neither had a way in. The
 * handoff brief in particular is already written as markdown, ready to paste
 * into a chat at shift change, and it was reachable only with curl. The audit
 * log is worse: it is written from a dozen places in the product and had no
 * reader at all, which is how a runtime setting could be changed with no trace
 * anyone could see.
 */

let handoffHours = 8;

async function loadHandoff() {
    const brief = document.getElementById('handoffBrief');
    const audit = document.getElementById('auditLogList');
    if (!brief || !audit) return;

    brief.innerHTML = `<div class="loading"><div class="spinner"></div><p>${t('common.loading')}</p></div>`;
    audit.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;

    await Promise.all([_loadHandoffBrief(brief), _loadAuditLog(audit)]);
}

async function _loadHandoffBrief(container) {
    try {
        const result = await API.getHandoff(handoffHours);
        const data = (result && result.data) || {};
        // Standard stat-card idiom (same as Overview/alerts) instead of the
        // bespoke handoff-stat row; colour stays in the value point.
        const stat = (labelKey, value, tone) =>
            `<div class="stat-card">
                <div class="stat-label">${escapeHtml(t(labelKey))}</div>
                <div class="stat-value"${tone ? ` style="color:${tone}"` : ''}>${escapeHtml(String(value))}</div>
            </div>`;

        const sources = (data.top_sources || [])
            .map((row) => `${escapeHtml(String(row.source || ''))} <strong>${escapeHtml(String(row.count || 0))}</strong>`)
            .join(' · ');

        // 8/12/24h mirror the shift lengths a handoff brief is written for
        // (one shift, a half day, a full day) — the same segmented-control
        // idiom as the trace header's Day/Week/Month.
        container.innerHTML =
            `<div class="handoff-window">
                <span class="btn-seg-group" role="group" aria-label="${escapeHtml(t('handoff.windowLabel'))}">` +
            [8, 12, 24].map((hours) =>
                `<button class="btn${hours === handoffHours ? ' active' : ''}" data-act="setHandoffWindow" ` +
                `data-args="${hours}">${escapeHtml(t('handoff.hours', { n: hours }))}</button>`).join('') +
            `</span></div>` +
            `<div class="stats-grid" style="margin-bottom: 1rem;">` +
            stat('handoff.stat.alerts', data.total_alerts || 0) +
            stat('handoff.stat.high', data.high_alerts || 0, (data.high_alerts || 0) > 0 ? 'var(--warning)' : '') +
            stat('handoff.stat.activeIncidents', data.active_incidents || 0,
                (data.active_incidents || 0) > 0 ? 'var(--danger)' : '') +
            stat('handoff.stat.quietIncidents', data.quiet_incidents || 0) +
            `</div>` +
            (sources ? `<div class="handoff-sources">${t('handoff.topSources')}: ${sources}</div>` : '') +
            // The brief is markdown on purpose: it is meant to be pasted into
            // the chat where the next shift actually reads things.
            `<div class="handoff-brief-bar">
                <button class="btn btn-sm" data-act="copyHandoff" data-args="">${wwIcon('copy')} ${escapeHtml(t('handoff.copy'))}</button>
             </div>
             <pre class="ww-pre handoff-brief" id="handoffText">${escapeHtml(String(data.summary_text || ''))}</pre>`;
    } catch (error) {
        container.innerHTML =
            `<div class="empty-state"><p>${wwIcon('x')} ${escapeHtml(error.message || String(error))}</p></div>`;
    }
}

async function _loadAuditLog(container) {
    try {
        const result = await API.getAuditLog(60);
        const rows = (result && result.data) || [];
        if (!rows.length) {
            container.innerHTML = `<div class="empty-state"><p>${escapeHtml(t('handoff.audit.empty'))}</p></div>`;
            return;
        }
        container.innerHTML = '<div class="audit-list">' + rows.map((row) => {
            const when = row.created_at ? formatTime(row.created_at) : '';
            return `<div class="audit-row">
                <span class="badge badge-outline">${escapeHtml(String(row.action || ''))}</span>
                <span class="audit-what">${escapeHtml(String(row.summary || row.resource_name || ''))}</span>
                <span class="audit-who">${escapeHtml(String(row.actor || ''))} · ${escapeHtml(when)}</span>
            </div>`;
        }).join('') + '</div>';
    } catch (error) {
        container.innerHTML =
            `<div class="empty-state"><p>${wwIcon('x')} ${escapeHtml(error.message || String(error))}</p></div>`;
    }
}

function setHandoffWindow(hours) {
    handoffHours = Number(hours) || 8;
    loadHandoff();
}

function copyHandoff() {
    const text = document.getElementById('handoffText');
    // wwCopyText, not copyToClipboard: the latter expects the BUTTON element
    // of a code block and walked .closest() on whatever it was given — handing
    // it a string threw a silent TypeError, so the copy button did nothing.
    if (text) wwCopyText(text.textContent || '', t('handoff.copied'));
}

const HandoffModule = {
    load: loadHandoff
};
