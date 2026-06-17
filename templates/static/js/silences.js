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
    console.log('🔕 Loading silences...');
    const container = document.getElementById('silencesList');

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
            console.log('✅ Loaded', silences.length, 'silences');
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

    return `
        <div class="silence-card" style="
            background: #ffffff;
            border: 1px solid #cbd5e1;
            ${cardBorder}
            border-radius: var(--radius-lg);
            padding: 1.25rem 1.5rem;
            margin-bottom: 0.5rem;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -1px rgba(0,0,0,0.03);
            ${cardOpacity}
        ">
            <div class="silence-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem;">
                <div style="display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;">
                    ${statusBadge}
                    <span style="font-weight: 600; font-size: 1.05rem; ${titleColor}">${escapeHtml(silence.comment || t('silences.card.noComment'))}</span>
                </div>
                <span style="
                    background: #fffbeb;
                    padding: 4px 12px;
                    border-radius: 9999px;
                    font-size: 0.85rem;
                    font-weight: 600;
                    color: #b45309;
                    border: 1px solid #fde68a;
                ">⏳ ${escapeHtml(formatSilenceExpiry(silence))}</span>
            </div>

            <div class="silence-conditions" style="font-size: 0.95rem; color: #334155; background: #f8fafc; padding: 1.25rem; border-radius: 8px; border: 1px dashed #cbd5e1; margin-bottom: 1.25rem;">
                <div style="font-size: 0.8rem; text-transform: uppercase; color: #64748b; margin-bottom: 0.75rem; font-weight: 600; letter-spacing: 0.05em;">🎯 ${t('silences.card.matchConditions')}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.source')}:</strong> ${sourceText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.importance')}:</strong> ${importanceText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.eventType')}:</strong> ${eventTypeText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.project')}:</strong> ${projectText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.region')}:</strong> ${regionText}</div>
                <div style="margin-bottom: 0.5rem;"><strong>${t('silences.card.environment')}:</strong> ${environmentText}</div>
                ${silence.match_payload ? '<div><strong>' + t('silences.card.payload') + ':</strong> <code style="font-size:0.8rem;">' + escapeHtml(silence.match_payload) + '</code></div>' : ''}
                ${silence.created_by ? '<div style="margin-top:0.5rem; color:#64748b; font-size:0.85rem;">' + t('silences.card.createdBy', { name: escapeHtml(silence.created_by) }) + '</div>' : ''}
            </div>

            <div class="silence-actions" style="display: flex; gap: 0.75rem; justify-content: flex-end; padding-top: 1rem; border-top: 1px solid #e2e8f0;">
                ${isActive ? '<button class="btn" onclick="liftSilence(' + silence.id + ')" style="color: #b45309; border-color: #fde68a; background: #fffbeb; font-weight: 600;">🔔 ' + t('silences.action.lift') + '</button>' : ''}
                <button class="btn" onclick="showSilenceForm(${silence.id})" style="font-weight: 600;">✏️ ${t('silences.action.edit')}</button>
                <button class="btn" onclick="deleteSilence(${silence.id})" style="color: #dc2626; border-color: #fecaca; background: #fef2f2; font-weight: 600;">🗑️ ${t('silences.action.delete')}</button>
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

    const expiry = computeSilenceExpiry(document.getElementById('silenceFormDuration').value);
    if (expiry !== undefined) {
        silenceData.expires_at = expiry; // null = permanent, or ISO string
    }

    try {
        let result;
        if (silenceId) {
            console.log('📝 Updating silence:', silenceId, silenceData);
            result = await API.updateSilence(silenceId, silenceData);
        } else {
            console.log('➕ Creating silence:', silenceData);
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
        console.log('🔔 Lifting silence:', id);
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
        console.log('🗑️ Deleting silence:', id);
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

// Export the module (used by dashboard.js for initialization detection)
const SilencesModule = {
    init: function() {
        console.log('🔕 Silence module initialized');
    },
    loadSilences: loadSilences
};
