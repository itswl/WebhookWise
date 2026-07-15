/**
 * Silence (manual mute / snooze) Management Module
 * Create, list, edit, lift, and delete silences. A silence suppresses
 * forwarding for matching alerts while active. Mirrors forward-rules.js.
 */

// Stores the current list of silences
let silences = [];

/**
 * Load the list of silences
 */
async function loadSilences() {
    const container = document.getElementById('silencesList');

    // Load the silence-debt panel alongside the list (best-effort: its own error
    // handling keeps a failure from affecting the list below).
    loadSilenceDebt();

    try {
        container.innerHTML = `
            <div class="loading">
                <div class="spinner"></div>
                <p>${t('common.loading')}</p>
            </div>
        `;

        const result = await API.getSilences();

        if (result.success) {
            silences = result.data || [];
            renderSilences(silences);
        } else {
            container.innerHTML = `
                <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <p>❌ ${t('common.loadFailed')}: ${escapeHtml(result.error || t('common.unknownError'))}</p>
                    <button class="btn" onclick="loadSilences()" style="margin-top: 10px;">${t('common.retry')}</button>
                </div>
            `;
        }
    } catch (error) {
        console.error('❌ Failed to load silences:', error);
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                <p>❌ ${t('common.loadFailed')}: ${escapeHtml(error.message || String(error))}</p>
                <button class="btn" onclick="loadSilences()" style="margin-top: 10px;">${t('common.retry')}</button>
            </div>
        `;
    }
}

/**
 * Silence debt: how much each active silence is suppressing over a trailing
 * window, with chronic (no-expiry, high-volume) mutes flagged as the actionable
 * ones — a still-firing source is being swallowed. Best-effort panel above the
 * silence list.
 */
async function loadSilenceDebt() {
    const container = document.getElementById('silenceDebtPanel');
    if (!container) return;
    try {
        const result = await API.getSilenceDebt(30);
        if (result && result.success && result.data) {
            renderSilenceDebt(result.data);
        } else {
            container.innerHTML = '';
        }
    } catch (error) {
        console.error('Failed to load silence debt:', error);
        container.innerHTML = '';
    }
}

function formatSilenceDebtTime(minutes) {
    const m = Math.max(0, Number(minutes) || 0);
    if (m >= 60) return t('silences.debt.timeHours', { n: (m / 60).toFixed(1) });
    return t('silences.debt.timeMinutes', { n: m });
}

function renderSilenceDebt(data) {
    const container = document.getElementById('silenceDebtPanel');
    if (!container) return;

    // Nothing to surface when there are no active silences at all.
    if (!data.active_silences) {
        container.innerHTML = '';
        return;
    }

    const days = data.window_days || 30;
    const chronic = Number(data.chronic_count || 0);

    let html = '<div style="font-size: 1rem; font-weight: 600; margin: 0 0 0.5rem;">' + t('silences.debt.title') + '</div>';
    html += '<p style="margin: 0 0 0.75rem; color: var(--text-muted); font-size: 0.8rem;">' + t('silences.debt.note', { days: days }) + '</p>';

    // Headline: chronic count (the actionable signal) + total suppressed.
    html += '<div class="stats-grid" style="margin-bottom: 1rem;">';
    html += '<div class="stat-card"' + (chronic > 0 ? ' style="border-left: 3px solid var(--warning);"' : '') + '>' +
        '<div class="stat-label">🔇 ' + t('silences.debt.chronicCount') + '</div>' +
        '<div class="stat-value" style="font-size: 1.75rem;' + (chronic > 0 ? ' color: var(--warning);' : '') + '">' + formatNumber(chronic) + '</div>' +
        '<div class="stat-trend">' + t('silences.debt.chronicTrend', { active: formatNumber(data.active_silences) }) + '</div></div>';
    html += '<div class="stat-card"><div class="stat-label">🔕 ' + t('silences.debt.suppressed') + '</div>' +
        '<div class="stat-value" style="font-size: 1.75rem;">' + formatNumber(data.total_suppressed || 0) + '</div>' +
        '<div class="stat-trend">' + t('silences.debt.suppressedTrend', { time: formatSilenceDebtTime(data.estimated_minutes_saved) }) + '</div></div>';
    html += '</div>';

    // Per-silence table, sorted by suppressed desc (backend order preserved);
    // rows with no suppression in the window are omitted as uninteresting.
    const withVolume = (Array.isArray(data.silences) ? data.silences : []).filter(function (r) {
        return Number(r.suppressed || 0) > 0;
    });
    if (!withVolume.length) {
        html += '<div style="color: var(--text-muted); font-size: 0.85rem;">' + t('silences.debt.empty') + '</div>';
        container.innerHTML = html;
        return;
    }

    const th = 'padding: 0.55rem 0.75rem; font-weight: 600;';
    const td = 'padding: 0.55rem 0.75rem; border-top: 1px solid var(--border);';
    html += '<div style="overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface);">';
    html += '<table style="width: 100%; border-collapse: collapse; font-size: 0.85rem;">';
    html += '<thead><tr style="color: var(--text-muted);">' +
        '<th style="' + th + ' text-align: left;">' + t('silences.debt.colSilence') + '</th>' +
        '<th style="' + th + ' text-align: right;">' + t('silences.debt.colSuppressed') + '</th>' +
        '<th style="' + th + ' text-align: right;">' + t('silences.debt.colPerDay') + '</th>' +
        '<th style="' + th + ' text-align: left;">' + t('silences.debt.colLast') + '</th>' +
        '</tr></thead><tbody>';
    withVolume.forEach(function (row) {
        const isChronic = !!row.chronic;
        const rowStyle = isChronic ? ' style="background: rgba(245, 158, 11, 0.08);"' : '';
        let badge = '';
        if (isChronic) {
            badge = ' <span class="badge badge-danger" title="' + escapeHtml(t('silences.debt.chronicTitle')) + '" style="font-size: 0.65rem;">⚠️ ' + t('silences.debt.chronicBadge') + '</span>';
        } else if (row.no_expiry) {
            badge = ' <span class="badge badge-outline" style="font-size: 0.65rem;">' + t('silences.debt.noExpiry') + '</span>';
        }
        const last = row.last_suppressed_at ? escapeHtml(formatSilenceTime(row.last_suppressed_at)) : '—';
        html += '<tr' + rowStyle + '>' +
            '<td style="' + td + '">' + escapeHtml(row.label || ('#' + row.silence_id)) + badge + '</td>' +
            '<td style="' + td + ' text-align: right; font-weight: 600;">' + formatNumber(row.suppressed || 0) + '</td>' +
            '<td style="' + td + ' text-align: right; color: var(--text-muted);">' + escapeHtml(String(row.daily_rate != null ? row.daily_rate : '—')) + '</td>' +
            '<td style="' + td + ' color: var(--text-muted);">' + last + '</td>' +
            '</tr>';
    });
    html += '</tbody></table></div>';
    container.innerHTML = html;
}

/**
 * Render the silence list (active first, then lifted/expired)
 * @param {Array} list - array of silences
 */
function renderSilences(list) {
    const container = document.getElementById('silencesList');

    if (!list || list.length === 0) {
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 60px; color: var(--text-secondary);">
                <div style="font-size: 48px; margin-bottom: 20px;">🔕</div>
                <p style="font-size: 16px; margin-bottom: 10px;">${t('silences.empty.title')}</p>
                <p style="font-size: 14px;">${t('silences.empty.text')}</p>
            </div>
        `;
        return;
    }

    // Active silences first, so the currently-muting ones are at the top.
    const sorted = [...list].sort((a, b) => (b.active === a.active ? 0 : (b.active ? 1 : -1)));

    let html = '<div class="silences-list" style="display: flex; flex-direction: column; gap: 15px;">';
    sorted.forEach(silence => {
        html += renderSilenceCard(silence);
    });
    html += '</div>';
    container.innerHTML = html;
}

/**
 * Format a silence's expiry for display
 */
function formatSilenceExpiry(silence) {
    if (silence.lifted_at) {
        return t('silences.card.lifted');
    }
    if (!silence.expires_at) {
        return t('silences.card.permanent');
    }
    return t('silences.card.until', { time: formatSilenceTime(silence.expires_at) });
}

/**
 * Format an ISO timestamp; falls back to the project-wide formatTime if present.
 */
function formatSilenceTime(iso) {
    if (!iso) return '-';
    if (typeof formatTime === 'function') {
        return formatTime(iso);
    }
    try {
        return new Date(iso).toLocaleString();
    } catch (e) {
        return iso;
    }
}

/**
 * Render a single silence card
 * @param {Object} silence - silence object
 */
function renderSilenceCard(silence) {
    const importanceText = escapeHtml(formatImportance(silence.match_importance));
    const sourceText = escapeHtml(silence.match_source || t('common.all'));
    const eventTypeText = escapeHtml(silence.match_event_type || t('common.all'));
    const projectText = escapeHtml(silence.match_project || t('common.all'));
    const regionText = escapeHtml(silence.match_region || t('common.all'));
    const environmentText = escapeHtml(silence.match_environment || t('common.all'));

    const isActive = !!silence.active;
    const cardBorder = isActive ? 'border-left: 4px solid var(--warning);' : 'border-left: 4px solid #cbd5e1;';
    const cardOpacity = isActive ? 'opacity: 1;' : 'opacity: 0.65; background: #f8fafc;';
    const titleColor = isActive ? 'color: var(--text-main);' : 'color: var(--text-muted);';

    const statusBadge = isActive
        ? '<span class="badge badge-medium">🔕 ' + t('silences.card.active') + '</span>'
        : '<span class="badge badge-new">' + t('silences.card.inactive') + '</span>';

    // ROI: how many alerts this silence has suppressed. A high count earns its
    // keep; an active rule that has suppressed nothing is a "zombie" worth a look.
    const suppressed = Number(silence.suppressed_count || 0);
    const suppressedBadge = suppressed > 0
        ? '<span class="badge badge-success" title="' + escapeHtml(t('silences.roi.tooltip')) + '">🛡️ ' +
            t('silences.roi.suppressed', { count: suppressed }) + '</span>'
        : (isActive
            ? '<span class="badge badge-danger" title="' + escapeHtml(t('silences.roi.zombieTooltip')) + '">⚠️ ' +
                t('silences.roi.zombie') + '</span>'
            : '<span class="badge badge-outline">' + t('silences.roi.suppressed', { count: 0 }) + '</span>');
    const lastSuppressed = silence.last_suppressed_at
        ? '<div style="margin-top:0.5rem; color:var(--text-muted); font-size:0.85rem;">' +
            t('silences.roi.lastSuppressed', { time: formatSilenceTime(silence.last_suppressed_at) }) + '</div>'
        : '';

    return `
        <div class="silence-card" style="
            background: var(--bg-surface);
            border: 1px solid var(--border);
            ${cardBorder}
            border-radius: var(--radius-lg);
            padding: 1.25rem 1.5rem;
            margin-bottom: 0.5rem;
            box-shadow: var(--shadow-sm);
            ${cardOpacity}
        ">
            <div class="silence-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem;">
                <div style="display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;">
                    ${statusBadge}
                    ${suppressedBadge}
                    <span style="font-weight: 600; font-size: 1.05rem; ${titleColor}">${escapeHtml(silence.comment || t('silences.card.noComment'))}</span>
                </div>
                <span style="
                    background: var(--warning-bg);
                    padding: 4px 12px;
                    border-radius: 9999px;
                    font-size: 0.85rem;
                    font-weight: 600;
                    color: var(--warning);
                    border: 1px solid rgba(217,119,6,0.18);
                ">⏳ ${escapeHtml(formatSilenceExpiry(silence))}</span>
            </div>

            <div class="silence-conditions" style="font-size: 0.95rem; color: var(--text-secondary); background: var(--bg-subtle); padding: 1.25rem; border-radius: 8px; border: 1px dashed var(--border); margin-bottom: 1.25rem;">
                <div style="font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🎯 ${t('silences.card.matchConditions')}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.source')}:</strong> ${sourceText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.importance')}:</strong> ${importanceText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.eventType')}:</strong> ${eventTypeText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.project')}:</strong> ${projectText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.region')}:</strong> ${regionText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.environment')}:</strong> ${environmentText}</div>
                ${silence.match_payload ? '<div><strong>' + t('silences.card.payload') + ':</strong> <code style="font-size:0.8rem;">' + escapeHtml(silence.match_payload) + '</code></div>' : ''}
                ${silence.created_by ? '<div style="margin-top:0.5rem; color:var(--text-muted); font-size:0.85rem;">' + t('silences.card.createdBy', { name: escapeHtml(silence.created_by) }) + '</div>' : ''}
                ${lastSuppressed}
            </div>

            <div class="silence-actions" style="display: flex; gap: 0.75rem; justify-content: flex-end; padding-top: 1rem; border-top: 1px solid var(--border);">
                ${isActive ? '<button class="btn" onclick="liftSilence(' + silence.id + ')" style="color: var(--warning); border-color: rgba(217,119,6,0.25); background: var(--warning-bg); font-weight: 600;">🔔 ' + t('silences.action.lift') + '</button>' : ''}
                <button class="btn" onclick="showSilenceForm(${silence.id})" style="font-weight: 600;">✏️ ${t('silences.action.edit')}</button>
                <button class="btn" onclick="deleteSilence(${silence.id})" style="color: var(--danger); border-color: rgba(225,29,72,0.25); background: var(--danger-bg); font-weight: 600;">🗑️ ${t('silences.action.delete')}</button>
            </div>
        </div>
    `;
}

/**
 * Show the silence form (create or edit)
 * @param {number} silenceId - silence ID; omit for create
 */
function showSilenceForm(silenceId) {
    const modal = document.getElementById('silenceFormModal');
    const title = document.getElementById('silenceFormTitle');

    // Reset the form
    document.getElementById('silenceFormId').value = '';
    document.getElementById('silenceFormComment').value = '';
    document.getElementById('silenceFormSource').value = '';
    ['silenceFormImportanceHigh', 'silenceFormImportanceMedium', 'silenceFormImportanceLow'].forEach(function(id) {
        document.getElementById(id).checked = false;
    });
    document.getElementById('silenceFormEventType').value = '';
    document.getElementById('silenceFormProject').value = '';
    document.getElementById('silenceFormRegion').value = '';
    document.getElementById('silenceFormEnvironment').value = '';
    document.getElementById('silenceFormPayload').value = '';
    document.getElementById('silenceFormDuration').value = '0';

    const backtestResult = document.getElementById('silenceFormBacktestResult');
    if (backtestResult) {
        backtestResult.style.display = 'none';
        backtestResult.innerHTML = '';
    }

    const expiresRow = document.getElementById('silenceFormExpiresRow');
    if (expiresRow) expiresRow.style.display = 'none';

    if (silenceId) {
        title.textContent = t('silence.editTitle');
        const silence = silences.find(s => s.id === silenceId);
        if (silence) {
            document.getElementById('silenceFormId').value = silence.id;
            document.getElementById('silenceFormComment').value = silence.comment || '';
            document.getElementById('silenceFormSource').value = silence.match_source || '';
            if (silence.match_importance) {
                const importances = silence.match_importance.split(',').map(s => s.trim());
                document.getElementById('silenceFormImportanceHigh').checked = importances.includes('high');
                document.getElementById('silenceFormImportanceMedium').checked = importances.includes('medium');
                document.getElementById('silenceFormImportanceLow').checked = importances.includes('low');
            }
            document.getElementById('silenceFormEventType').value = silence.match_event_type || '';
            document.getElementById('silenceFormProject').value = silence.match_project || '';
            document.getElementById('silenceFormRegion').value = silence.match_region || '';
            document.getElementById('silenceFormEnvironment').value = silence.match_environment || '';
            document.getElementById('silenceFormPayload').value = silence.match_payload || '';
            // On edit, expiry is managed via the duration selector below; keep
            // "keep current" as the default so editing fields doesn't reset it.
            document.getElementById('silenceFormDuration').value = 'keep';
        }
    } else {
        title.textContent = t('silence.addTitle');
    }

    modal.classList.add('active');
}

/**
 * Open the silence form pre-filled with alert context (quick-silence).
 * Duration defaults to 1 hour so the operator doesn't forget to lift it.
 */
function showQuickSilenceForm(source, project, region, environment, payload) {
    // Reset and open the form, then fill in what we know about this alert.
    showSilenceForm();
    if (source) document.getElementById('silenceFormSource').value = source;
    if (project) document.getElementById('silenceFormProject').value = project;
    if (region) document.getElementById('silenceFormRegion').value = region;
    if (environment) document.getElementById('silenceFormEnvironment').value = environment;
    if (payload) document.getElementById('silenceFormPayload').value = payload;
    // Default to 1 hour — long enough to investigate, short enough not to be
    // forgotten and left permanently silencing alerts.
    document.getElementById('silenceFormDuration').value = '1h';
}

/**
 * Close the silence form
 */
function closeSilenceForm() {
    document.getElementById('silenceFormModal').classList.remove('active');
}

/**
 * Compute an ISO expires_at from the duration selector value.
 * Returns: undefined = "keep current" (edit), null = permanent, or an ISO string.
 */
function computeSilenceExpiry(durationValue) {
    if (durationValue === 'keep') return undefined;
    const hours = parseFloat(durationValue);
    if (!hours || hours <= 0) return null; // 0 / permanent
    return new Date(Date.now() + hours * 3600 * 1000).toISOString();
}

/**
 * Save the silence (create or update)
 */
async function saveSilence() {
    const silenceId = document.getElementById('silenceFormId').value;
    const comment = document.getElementById('silenceFormComment').value.trim();

    const importances = [];
    if (document.getElementById('silenceFormImportanceHigh').checked) importances.push('high');
    if (document.getElementById('silenceFormImportanceMedium').checked) importances.push('medium');
    if (document.getElementById('silenceFormImportanceLow').checked) importances.push('low');

    const silenceData = {
        match_source: document.getElementById('silenceFormSource').value.trim(),
        match_importance: importances.join(','),
        match_event_type: document.getElementById('silenceFormEventType').value.trim(),
        match_project: document.getElementById('silenceFormProject').value.trim(),
        match_region: document.getElementById('silenceFormRegion').value.trim(),
        match_environment: document.getElementById('silenceFormEnvironment').value.trim(),
        match_payload: document.getElementById('silenceFormPayload').value.trim(),
        comment: comment
    };

    // Require at least one match criterion (mirrors the backend validation).
    const hasCriterion = silenceData.match_source || silenceData.match_importance ||
        silenceData.match_event_type || silenceData.match_project ||
        silenceData.match_region || silenceData.match_environment || silenceData.match_payload;
    if (!hasCriterion) {
        alert(t('silences.alert.criterionRequired'));
        return;
    }

    // Check for rule conflicts: if this silence specifies a source that is the
    // ONLY match_source for certain forward rules, those rules will be effectively
    // blocked — warn the operator before saving.
    if (!silenceId && silenceData.match_source && typeof forwardRules !== 'undefined' && forwardRules.length) {
        var blockedRules = forwardRules.filter(function (r) {
            return r.enabled && (r.match_source || '').trim() === silenceData.match_source;
        });
        if (blockedRules.length > 0) {
            var names = blockedRules.map(function (r) { return r.name; }).join(', ');
            if (!confirm(
                t('silences.confirm.conflict', { n: blockedRules.length }) + '\n\n' + names +
                '\n\n' + t('silences.confirm.conflictDetail')
            )) { return; }
        }
    }

    const expiry = computeSilenceExpiry(document.getElementById('silenceFormDuration').value);
    if (expiry !== undefined) {
        silenceData.expires_at = expiry; // null = permanent, or ISO string
    }

    try {
        let result;
        if (silenceId) {
            result = await API.updateSilence(silenceId, silenceData);
        } else {
            result = await API.createSilence(silenceData);
        }

        if (result.success) {
            alert(silenceId ? '✅ ' + t('silences.alert.updateSuccess') : '✅ ' + t('silences.alert.createSuccess'));
            closeSilenceForm();
            loadSilences();
        } else {
            alert('❌ ' + t('silences.alert.saveFailed') + ': ' + (result.error || t('common.unknownError')));
        }
    } catch (error) {
        console.error('❌ Failed to save silence:', error);
        alert('❌ ' + t('silences.alert.saveFailed') + ': ' + error.message);
    }
}

/**
 * Lift (deactivate) a silence
 * @param {number} id - silence ID
 */
async function liftSilence(id) {
    if (!confirm(t('silences.confirm.lift'))) {
        return;
    }
    try {
        const result = await API.liftSilence(id);
        if (result.success) {
            loadSilences();
        } else {
            alert('❌ ' + t('silences.alert.operationFailed') + ': ' + (result.error || t('common.unknownError')));
        }
    } catch (error) {
        console.error('❌ Failed to lift silence:', error);
        alert('❌ ' + t('silences.alert.operationFailed') + ': ' + error.message);
    }
}

/**
 * Delete a silence
 * @param {number} id - silence ID
 */
async function deleteSilence(id) {
    if (!confirm(t('silences.confirm.delete'))) {
        return;
    }
    try {
        const result = await API.deleteSilence(id);
        if (result.success) {
            alert('✅ ' + t('silences.alert.deleteSuccess'));
            loadSilences();
        } else {
            alert('❌ ' + t('silences.alert.deleteFailed') + ': ' + (result.error || t('common.unknownError')));
        }
    } catch (error) {
        console.error('❌ Failed to delete silence:', error);
        alert('❌ ' + t('silences.alert.deleteFailed') + ': ' + error.message);
    }
}

/**
 * Backtest the proposed silence rule match criteria against historical events.
 */
async function backtestSilenceRule() {
    const container = document.getElementById('silenceFormBacktestResult');
    if (!container) return;

    const importances = [];
    if (document.getElementById('silenceFormImportanceHigh').checked) importances.push('high');
    if (document.getElementById('silenceFormImportanceMedium').checked) importances.push('medium');
    if (document.getElementById('silenceFormImportanceLow').checked) importances.push('low');

    const silenceData = {
        match_source: document.getElementById('silenceFormSource').value.trim(),
        match_importance: importances.join(','),
        match_event_type: document.getElementById('silenceFormEventType').value.trim(),
        match_project: document.getElementById('silenceFormProject').value.trim(),
        match_region: document.getElementById('silenceFormRegion').value.trim(),
        match_environment: document.getElementById('silenceFormEnvironment').value.trim(),
        match_payload: document.getElementById('silenceFormPayload').value.trim(),
        lookback_days: 30
    };

    // Require at least one match criterion
    const hasCriterion = silenceData.match_source || silenceData.match_importance ||
        silenceData.match_event_type || silenceData.match_project ||
        silenceData.match_region || silenceData.match_environment || silenceData.match_payload;
    if (!hasCriterion) {
        alert(t('silences.alert.criterionRequired'));
        return;
    }

    container.style.display = 'block';
    container.innerHTML = `
        <div class="loading" style="padding: 15px 0;">
            <div class="spinner" style="width: 20px; height: 20px;"></div>
            <p style="font-size: 0.85rem; margin-top: 5px;">${t('common.loading')}</p>
        </div>
    `;

    try {
        const result = await API.backtestSilence(silenceData);
        if (!result.success || !result.data) {
            container.innerHTML = `<div style="color: var(--danger); font-size: 0.85rem;">⚠️ Error: ${escapeHtml(result.error || 'Unknown error')}</div>`;
            return;
        }

        const d = result.data;
        const matchedRate = d.total_scanned > 0 ? ((d.total_matched / d.total_scanned) * 100).toFixed(1) : '0.0';

        let impBadges = '';
        Object.keys(d.importance_counts || {}).forEach(function(k) {
            const val = d.importance_counts[k];
            if (val > 0) {
                impBadges += `<span class="badge badge-${impClass(k)}" style="margin-right: 5px; font-size: 0.75rem;">${escapeHtml(k)}: ${val}</span>`;
            }
        });

        let sampleHtml = '';
        if ((d.sample_matched_events || []).length > 0) {
            sampleHtml += `
                <div style="margin-top: 0.75rem;">
                    <div style="font-size: 0.8rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 0.4rem;">🎯 Recent Matched Samples (${d.sample_matched_events.length}):</div>
                    <div style="display: flex; flex-direction: column; gap: 5px;">
            `;
            d.sample_matched_events.forEach(function(ev) {
                const isDupBadge = ev.is_duplicate ? `<span class="badge badge-outline" style="font-size: 0.65rem; padding: 1px 4px;">dup</span>` : '';
                sampleHtml += `
                    <div style="font-size: 0.8rem; background: var(--bg-subtle, #f8fafc); border: 1px solid var(--border); border-radius: 4px; padding: 6px 10px; display: flex; justify-content: space-between; align-items: center; gap: 10px;">
                        <span style="flex-shrink: 0; color: var(--text-muted);">${formatSilenceTime(ev.timestamp).split(' ')[1] || formatSilenceTime(ev.timestamp)}</span>
                        <span class="badge badge-outline" style="font-size: 0.7rem; flex-shrink: 0;">${escapeHtml(ev.source)}</span>
                        <span class="badge badge-${impClass(ev.importance)}" style="font-size: 0.7rem; flex-shrink: 0;">${escapeHtml(ev.importance)}</span>
                        ${isDupBadge}
                        <span style="flex-grow: 1; text-overflow: ellipsis; overflow: hidden; white-space: nowrap; color: var(--text-main); font-weight: 500;" title="${escapeHtml(ev.summary)}">${escapeHtml(ev.summary || '—')}</span>
                    </div>
                `;
            });
            sampleHtml += '</div></div>';
        } else {
            sampleHtml = `<div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.5rem; font-style: italic;">No historical alerts would have been silenced. This rule is 100% safe.</div>`;
        }

        container.innerHTML = `
            <div style="background: var(--bg-surface); border: 1px solid var(--border); border-radius: 6px; padding: 1rem;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
                    <span style="font-weight: 600; font-size: 0.9rem; color: var(--primary);">🧪 Backtest Result (Past 30 Days)</span>
                    <span style="font-size: 0.8rem; color: var(--text-muted);">Scanned: <strong>${d.total_scanned}</strong> events</span>
                </div>
                <div style="display: flex; gap: 1.5rem; align-items: center; background: var(--bg-subtle, #f8fafc); border-radius: 4px; padding: 0.75rem; margin-bottom: 0.75rem;">
                    <div style="text-align: center; border-right: 1px solid var(--border); padding-right: 1.5rem;">
                        <div style="font-size: 1.5rem; font-weight: 700; color: ${d.total_matched > 0 ? 'var(--warning)' : 'var(--success)'};">${d.total_matched}</div>
                        <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 2px;">Would Mute</div>
                    </div>
                    <div>
                        <div style="font-size: 1rem; font-weight: 600;">${matchedRate}% <span style="font-size: 0.8rem; font-weight: normal; color: var(--text-muted);">noise reduction rate</span></div>
                        <div style="margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px;">${impBadges || '<span style="font-size:0.75rem; color:var(--text-muted);">No categories</span>'}</div>
                    </div>
                </div>
                ${sampleHtml}
            </div>
        `;

    } catch (error) {
        console.error('❌ Silence backtest failed:', error);
        container.innerHTML = `<div style="color: var(--danger); font-size: 0.85rem;">⚠️ Failure: ${escapeHtml(error.message || String(error))}</div>`;
    }
}

// Export the module (used by dashboard.js for initialization detection)
const SilencesModule = {
    init: function() {
    },
    loadSilences: loadSilences
};
