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

    const willForward = !!fwd.should_forward;
    const verdictColor = willForward ? 'var(--success)' : (fwd.skip_code === 'silenced' ? 'var(--warning)' : 'var(--text-secondary)');
    const verdictIcon = willForward ? '✅' : (fwd.skip_code === 'silenced' ? '🔕' : '⏭️');
    const verdictText = willForward ? t('sandbox.verdict.forward') : t('sandbox.verdict.skip', { code: fwd.skip_code || '' });

    let html = '';

    // Summary banner
    html += '<div style="background:var(--bg-surface); border:1px solid var(--border); border-left:4px solid ' + verdictColor + '; border-radius:var(--radius); padding:1rem 1.25rem; margin-bottom:1.5rem;">' +
        '<div style="font-size:1.1rem; font-weight:700; color:' + verdictColor + '; display:flex; align-items:center; gap:8px;">' +
        '<span>' + verdictIcon + '</span> <span>' + escapeHtml(verdictText) + '</span>' +
        '</div>' +
        (fwd.skip_reason ? '<div style="font-size:0.85rem; color:var(--text-secondary); margin-top:0.4rem; line-height:1.4;">' + escapeHtml(fwd.skip_reason) + '</div>' : '') +
        '</div>';

    // Timeline wrapper
    html += '<div class="pipeline-flow" style="display: flex; flex-direction: column; gap: 1.25rem; position: relative; padding-left: 1.5rem; border-left: 2px dashed var(--border);">';

    // Step 1: Ingress
    html += renderPipelineStep(
        1,
        '✓',
        'var(--success)',
        '1. Ingress 接收',
        'Raw webhook payload is ingested and validated.',
        '<div><strong>Input source:</strong> <span class="badge badge-outline">' + escapeHtml(src.input || '—') + '</span></div>'
    );

    // Step 2: Normalization
    const adapterBadge = src.matched
        ? '<span class="badge badge-success">' + escapeHtml(src.adapter) + '</span>'
        : '<span class="badge badge-outline" title="' + escapeHtml(t('sandbox.passthroughHint')) + '">passthrough</span>';
    html += renderPipelineStep(
        2,
        '✓',
        'var(--success)',
        '2. Normalization 归一化',
        'Adapter parses external format into WW standard shape.',
        '<div style="display:flex; flex-direction:column; gap:4px;">' +
        '<div><strong>Resolved source:</strong> <code>' + escapeHtml(src.resolved || '—') + '</code></div>' +
        '<div><strong>Adapter matched:</strong> ' + adapterBadge + '</div>' +
        '</div>'
    );

    // Step 3: Deduplication
    html += renderPipelineStep(
        3,
        'ℹ️',
        'var(--primary)',
        '3. Deduplication 去重指纹',
        'Generates deterministic hash signatures for rate-limiting.',
        '<div style="display:flex; flex-direction:column; gap:4px; font-family:monospace; font-size:0.75rem;">' +
        '<div>alert_hash: <span style="color:var(--text-secondary);">' + escapeHtml(d.alert_hash || '') + '</span></div>' +
        '<div>dedup_key : <span style="color:var(--text-secondary);">' + escapeHtml(d.dedup_key || '') + '</span></div>' +
        '</div>' +
        '<div style="font-size:0.75rem; color:var(--text-muted); margin-top:0.4rem; font-style:italic;">ℹ️ ' + escapeHtml(d.dedup_note || '') + '</div>'
    );

    // Step 4: Silence
    const isSilenced = fwd.skip_code === 'silenced';
    const silenceColor = isSilenced ? 'var(--warning)' : 'var(--success)';
    const silenceIcon = isSilenced ? '🔕' : '✓';
    const silenceDesc = isSilenced ? t('sandbox.silencedBy', { id: fwd.silenced_by.silence_id }) : 'No active silence rules matched.';
    html += renderPipelineStep(
        4,
        silenceIcon,
        silenceColor,
        '4. Silence 静默过滤',
        'Checks if the alert is manually muted.',
        '<div style="color:' + silenceColor + '; font-weight:600;">' + escapeHtml(silenceDesc) + '</div>'
    );

    // Step 5: AI & Fallback Analysis
    html += renderPipelineStep(
        5,
        'ℹ️',
        'var(--primary)',
        '5. AI & Fallback Analysis 智能分析',
        'Enriches with KB and runs rule-based simulation.',
        '<div style="display:flex; flex-direction:column; gap:6px;">' +
        '<div><strong>Importance:</strong> <span class="badge badge-' + impClass(rb.importance) + '">' + escapeHtml(rb.importance || 'unknown') + '</span></div>' +
        '<div><strong>Event Type:</strong> <code style="font-size:0.8rem;">' + escapeHtml(rb.event_type || '—') + '</code></div>' +
        (rb.summary ? '<div><strong>Summary:</strong> <span style="color:var(--text-main); font-weight:500;">' + escapeHtml(rb.summary) + '</span></div>' : '') +
        '</div>' +
        '<div style="font-size:0.75rem; color:var(--text-muted); margin-top:0.4rem; font-style:italic;">ℹ️ ' + escapeHtml(rb.note || '') + '</div>'
    );

    // Step 6: Noise Reduction
    html += renderPipelineStep(
        6,
        'ℹ️',
        'var(--primary)',
        '6. Noise Reduction 降噪评分',
        'Extracts structural geo metadata for routing.',
        '<div style="display:grid; grid-template-columns: 1fr 1fr; gap:6px;">' +
        '<div><strong>Project:</strong> <code>' + escapeHtml(fields.project || '—') + '</code></div>' +
        '<div><strong>Region:</strong> <code>' + escapeHtml(fields.region || '—') + '</code></div>' +
        '<div><strong>Environment:</strong> <code>' + escapeHtml(fields.environment || '—') + '</code></div>' +
        '</div>'
    );

    // Step 7: Forwarding Outbox
    let rulesHtml = '';
    if ((fwd.matched_rules || []).length > 0) {
        rulesHtml = fwd.matched_rules.map(function (r) {
            return '<div style="display:flex; justify-content:space-between; padding:0.3rem 0; border-bottom:1px dashed var(--border);">' +
                '<span style="font-weight:600;">⚙️ ' + escapeHtml(r.name) + (r.stop_on_match ? ' <span class="badge badge-outline" style="font-size:0.65rem; padding:1px 4px;">stop</span>' : '') + '</span>' +
                '<span style="color:var(--text-muted); font-size:0.8rem;">' + escapeHtml(r.target_type) + (r.target_name ? ' · ' + escapeHtml(r.target_name) : '') + '</span>' +
                '</div>';
        }).join('');
    } else {
        rulesHtml = '<div style="color:var(--text-secondary); font-style:italic;">' + escapeHtml(t('sandbox.noRules')) + '</div>';
    }

    const step7Color = willForward ? 'var(--success)' : 'var(--text-muted)';
    const step7Icon = willForward ? '✓' : '⏭️';
    html += renderPipelineStep(
        7,
        step7Icon,
        step7Color,
        '7. Forward Outbox 规则转发',
        'Matches forwarding rules and schedules deliveries.',
        '<div style="display:flex; flex-direction:column; gap:6px;">' +
        '<div><strong>Matched rules:</strong></div>' +
        '<div style="background:var(--bg-subtle, #f8fafc); border:1px solid var(--border); border-radius:4px; padding:8px 12px;">' + rulesHtml + '</div>' +
        '</div>'
    );

    html += '</div>'; // close pipeline-flow wrapper

    // Extracted identity detail (collapsible raw view).
    const idJson = JSON.stringify(d.identity || {}, null, 2);
    html += '<details style="margin-top:1.5rem; border-top: 1px dashed var(--border); padding-top: 1rem;"><summary style="cursor:pointer; font-size:0.85rem; color:var(--text-secondary); font-weight:600;">' +
        escapeHtml(t('sandbox.section.extracted')) + '</summary>' +
        '<pre style="background:var(--bg-subtle,#f1f5f9); padding:0.75rem; border-radius:6px; overflow:auto; font-size:0.75rem; margin-top:0.5rem; max-height:200px; border:1px solid var(--border);">' +
        escapeHtml(idJson) + '</pre></details>';

    return html;
}

function renderPipelineStep(num, icon, color, title, desc, bodyHtml) {
    return '<div class="pipeline-step" style="position: relative; margin-bottom: 0.5rem;">' +
        '<!-- Circle indicator -->' +
        '<div class="step-indicator" style="position: absolute; left: -2.15rem; top: 0; width: 1.35rem; height: 1.35rem; border-radius: 50%; background: ' + color + '; color: white; display: flex; align-items: center; justify-content: center; font-size: 0.75rem; font-weight: bold; box-shadow: 0 0 0 4px var(--bg-surface);">' +
        icon +
        '</div>' +
        '<!-- Step contents -->' +
        '<div style="background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 0.75rem 1rem;">' +
        '<div style="font-weight: 700; font-size: 0.9rem; color: var(--text-main); margin-bottom: 2px;">' + escapeHtml(title) + '</div>' +
        '<div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 8px;">' + escapeHtml(desc) + '</div>' +
        '<div style="font-size: 0.82rem; color: var(--text-secondary); line-height: 1.5; border-top: 1px dashed var(--border); padding-top: 6px; margin-top: 6px;">' +
        bodyHtml +
        '</div>' +
        '</div>' +
        '</div>';
}

function impClass(importance) {
    if (importance === 'high') return 'danger';
    if (importance === 'medium') return 'medium';
    if (importance === 'low') return 'success';
    return 'outline';
}

// Module shell (mirrors SilencesModule; init is a no-op, load is on tab switch).
const SandboxModule = {
    init: function () { /* bind-only; the sandbox form is static */ },
    load: function () { /* lazy: the form is static, nothing to fetch on open */ }
};
