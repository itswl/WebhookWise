/**
 * Payload Sandbox — dry-run a pasted webhook payload.
 *
 * Sends {source, payload} to /v1/sandbox/test and renders what WebhookWise
 * would extract and decide (adapter, identity, alert hash, rule-based
 * importance, matched rules / silence, forward verdict). Read-only: the backend
 * does no enqueue, no AI call, no persistence.
 */

const SANDBOX_SAMPLE = {
    Type: 'alarm',
    RuleName: 'GPU memory utilization high',
    Level: 'critical',
    ProjectName: 'eve-cn-prod',
    Region: 'cn-beijing',
    Resources: [{ ResourceName: 'gpu-node-7', Metrics: [{ MetricName: 'gpu_mem_used', Value: 98 }] }]
};

function loadSandboxSample() {
    const src = document.getElementById('sandboxSource');
    const body = document.getElementById('sandboxPayload');
    if (src) src.value = 'volcengine';
    if (body) body.value = JSON.stringify(SANDBOX_SAMPLE, null, 2);
}

async function runSandboxTest() {
    const container = document.getElementById('sandboxResult');
    const source = (document.getElementById('sandboxSource')?.value || '').trim();
    const raw = (document.getElementById('sandboxPayload')?.value || '').trim();
    if (!container) return;

    if (!raw) {
        container.innerHTML = sandboxError(t('sandbox.error.noPayload'));
        return;
    }
    let payload;
    try {
        payload = JSON.parse(raw);
    } catch (e) {
        container.innerHTML = sandboxError(t('sandbox.error.invalidJson', { msg: e.message }));
        return;
    }
    if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
        container.innerHTML = sandboxError(t('sandbox.error.notObject'));
        return;
    }

    container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
    try {
        const res = await API.testWebhookPayload(source, payload);
        if (!res || !res.success || !res.data) {
            container.innerHTML = sandboxError((res && res.error) || t('common.unknownError'));
            return;
        }
        container.innerHTML = renderSandboxResult(res.data);
    } catch (err) {
        console.error('Sandbox test failed:', err);
        container.innerHTML = sandboxError(err.message || String(err));
    }
}

function sandboxError(msg) {
    return '<div class="empty-state" style="text-align:center; padding:30px; color:var(--danger);">' +
        '<div style="font-size:34px; margin-bottom:10px;">⚠️</div><p>' + escapeHtml(msg) + '</p></div>';
}

function renderSandboxResult(d) {
    const src = d.source || {};
    const fwd = d.forwarding || {};
    const rb = d.rule_based_analysis || {};
    const fields = d.match_fields || {};

    // Forward verdict banner.
    const willForward = !!fwd.should_forward;
    const verdictColor = willForward ? 'var(--success)' : (fwd.skip_code === 'silenced' ? 'var(--warning)' : 'var(--text-secondary)');
    const verdictIcon = willForward ? '✅' : (fwd.skip_code === 'silenced' ? '🔕' : '⏭️');
    const verdictText = willForward ? t('sandbox.verdict.forward') : t('sandbox.verdict.skip', { code: fwd.skip_code || '' });

    let html = '';
    html += '<div style="background:var(--bg-surface); border:1px solid var(--border); border-left:4px solid ' + verdictColor + '; border-radius:var(--radius); padding:1rem 1.25rem; margin-bottom:1rem;">' +
        '<div style="font-size:1.05rem; font-weight:600; color:' + verdictColor + ';">' + verdictIcon + ' ' + escapeHtml(verdictText) + '</div>' +
        (fwd.skip_reason ? '<div style="font-size:0.85rem; color:var(--text-secondary); margin-top:0.25rem;">' + escapeHtml(fwd.skip_reason) + '</div>' : '') +
        '</div>';

    // Source / adapter.
    const adapterBadge = src.matched
        ? '<span class="badge badge-success">' + escapeHtml(src.adapter) + '</span>'
        : '<span class="badge badge-outline" title="' + escapeHtml(t('sandbox.passthroughHint')) + '">passthrough</span>';
    html += sandboxSection(t('sandbox.section.source'),
        sandboxRow(t('sandbox.field.input'), escapeHtml(src.input || '—')) +
        sandboxRow(t('sandbox.field.resolved'), escapeHtml(src.resolved || '—')) +
        sandboxRow(t('sandbox.field.adapter'), adapterBadge));

    // Matched rules.
    let rulesHtml;
    if ((fwd.matched_rules || []).length) {
        rulesHtml = fwd.matched_rules.map(function (r) {
            return '<div style="display:flex; justify-content:space-between; padding:0.35rem 0; border-bottom:1px dashed var(--border);">' +
                '<span>⚙️ ' + escapeHtml(r.name) + (r.stop_on_match ? ' <span class="badge badge-outline" style="font-size:0.7rem;">stop</span>' : '') + '</span>' +
                '<span style="color:var(--text-muted);">' + escapeHtml(r.target_type || '') + (r.target_name ? ' · ' + escapeHtml(r.target_name) : '') + '</span></div>';
        }).join('');
    } else if (fwd.skip_code === 'silenced' && fwd.silenced_by) {
        rulesHtml = '<div style="color:var(--text-secondary);">' + escapeHtml(t('sandbox.silencedBy', { id: fwd.silenced_by.silence_id })) + '</div>';
    } else {
        rulesHtml = '<div style="color:var(--text-secondary);">' + escapeHtml(t('sandbox.noRules')) + '</div>';
    }
    html += sandboxSection(t('sandbox.section.rules'), rulesHtml);

    // Rule-based analysis (clearly labelled as non-AI).
    html += sandboxSection(t('sandbox.section.analysis'),
        sandboxRow(t('sandbox.field.importance'), '<span class="badge badge-' + impClass(rb.importance) + '">' + escapeHtml(rb.importance || 'unknown') + '</span>') +
        sandboxRow(t('sandbox.field.eventType'), escapeHtml(rb.event_type || '—')) +
        '<div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.5rem; font-style:italic;">ℹ️ ' + escapeHtml(rb.note || '') + '</div>');

    // Identity / fingerprints.
    html += sandboxSection(t('sandbox.section.identity'),
        sandboxRow(t('sandbox.field.project'), escapeHtml(fields.project || '—')) +
        sandboxRow(t('sandbox.field.region'), escapeHtml(fields.region || '—')) +
        sandboxRow(t('sandbox.field.environment'), escapeHtml(fields.environment || '—')) +
        sandboxRow('alert_hash', '<code style="font-size:0.75rem;">' + escapeHtml((d.alert_hash || '').slice(0, 16)) + '…</code>') +
        sandboxRow('dedup_key', '<code style="font-size:0.75rem;">' + escapeHtml((d.dedup_key || '').slice(0, 16)) + '…</code>'));

    // Extracted identity detail (collapsible raw view).
    const idJson = JSON.stringify(d.identity || {}, null, 2);
    html += '<details style="margin-top:0.75rem;"><summary style="cursor:pointer; font-size:0.85rem; color:var(--text-secondary);">' +
        escapeHtml(t('sandbox.section.extracted')) + '</summary>' +
        '<pre style="background:var(--bg-subtle,#f1f5f9); padding:0.75rem; border-radius:6px; overflow:auto; font-size:0.75rem; margin-top:0.5rem;">' +
        escapeHtml(idJson) + '</pre></details>';

    html += '<div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.75rem;">ℹ️ ' + escapeHtml(d.dedup_note || '') + '</div>';
    return html;
}

function sandboxSection(title, inner) {
    return '<div style="margin-bottom:1rem;">' +
        '<div style="font-size:0.8rem; text-transform:uppercase; color:var(--text-muted); font-weight:600; letter-spacing:0.04em; margin-bottom:0.4rem;">' + escapeHtml(title) + '</div>' +
        '<div style="background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius); padding:0.75rem 1rem;">' + inner + '</div></div>';
}

function sandboxRow(label, valueHtml) {
    return '<div style="display:flex; justify-content:space-between; gap:1rem; padding:0.2rem 0;">' +
        '<span style="color:var(--text-secondary);">' + escapeHtml(label) + '</span><span style="text-align:right;">' + valueHtml + '</span></div>';
}

function impClass(importance) {
    if (importance === 'high') return 'danger';
    if (importance === 'medium') return 'medium';
    if (importance === 'low') return 'success';
    return 'outline';
}

// Module shell (mirrors SilencesModule; init is a no-op, load is on tab switch).
const SandboxModule = {
    init: function () { console.log('🧪 Sandbox module initialized'); },
    load: function () { /* lazy: the form is static, nothing to fetch on open */ }
};
