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
        const stat = (labelKey, value, tone) =>
            `<div class="handoff-stat"><div class="handoff-stat-value"${tone ? ` style="color:${tone}"` : ''}>` +
            `${escapeHtml(String(value))}</div><div class="handoff-stat-label">${escapeHtml(t(labelKey))}</div></div>`;

        const sources = (data.top_sources || [])
            .map((row) => `${escapeHtml(String(row.source || ''))} <strong>${escapeHtml(String(row.count || 0))}</strong>`)
            .join(' · ');

        container.innerHTML =
            `<div class="handoff-window">` +
            [8, 12, 24].map((hours) =>
                `<button class="btn btn-sm${hours === handoffHours ? ' primary' : ''}" data-act="setHandoffWindow" ` +
                `data-args="${hours}">${escapeHtml(t('handoff.hours', { n: hours }))}</button>`).join(' ') +
            `</div>` +
            `<div class="handoff-stats">` +
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
                <button class="btn btn-sm" data-act="copyHandoff" data-args="">${escapeHtml(t('handoff.copy'))}</button>
             </div>
             <pre class="handoff-brief" id="handoffText">${escapeHtml(String(data.summary_text || ''))}</pre>`;
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
        container.innerHTML = rows.map((row) => {
            const when = row.created_at ? formatTime(row.created_at) : '';
            return `<div class="audit-row">
                <span class="a-tool">${escapeHtml(String(row.action || ''))}</span>
                <span class="audit-what">${escapeHtml(String(row.summary || row.resource_name || ''))}</span>
                <span class="audit-who">${escapeHtml(String(row.actor || ''))} · ${escapeHtml(when)}</span>
            </div>`;
        }).join('');
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
    if (text) copyToClipboard(text.textContent || '');
}

const HandoffModule = {
    load: loadHandoff
};
