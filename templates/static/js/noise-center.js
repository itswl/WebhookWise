/** Noise analytics, recommendations, and reversible optimization actions. */
const NoiseCenterModule = (function () {
    function statCard(label, value, trend, color) {
        return '<div class="stat-card"><div class="stat-label">' + escapeHtml(label) +
            '</div><div class="stat-value" style="color:' + color + ';">' + escapeHtml(String(value)) +
            '</div><div class="stat-trend">' + escapeHtml(trend || '') + '</div></div>';
    }

    function windowDays() {
        const select = document.getElementById('noiseWindowDays');
        return Number(select && select.value) || 7;
    }

    function savedTime(minutes) {
        const value = Number(minutes || 0);
        if (value < 60) return t('noise.time.minutes', { value: value });
        return t('noise.time.hours', { value: (value / 60).toFixed(1) });
    }

    function suggestionCopy(item) {
        const scope = item.scope || {};
        if (item.kind === 'duplicate_filter') {
            return {
                title: t('noise.suggestion.duplicate.title', { rule: scope.rule_name || '–' }),
                reason: t('noise.suggestion.duplicate.reason', {
                    rate: Number(scope.duplicate_rate || 0).toFixed(1),
                    duplicates: scope.duplicates || 0,
                    total: scope.total || 0
                })
            };
        }
        if (item.kind === 'temporary_silence') {
            return {
                title: t('noise.suggestion.silence.title', { rule: scope.rule_name || '–' }),
                reason: t('noise.suggestion.silence.reason', {
                    duplicates: scope.duplicates || 0,
                    total: scope.total || 0,
                    hours: scope.duration_hours || 24
                })
            };
        }
        return {
            title: t('noise.suggestion.threshold.title', { rule: scope.rule_name || '–' }),
            reason: t('noise.suggestion.threshold.reason', { total: scope.total || 0 })
        };
    }

    function renderSummary(data) {
        const summary = data.summary || {};
        const previous = data.previous || {};
        const target = document.getElementById('noiseCenterSummary');
        if (!target) return;
        const delta = Number(previous.noise_rate_delta || 0);
        const trend = delta === 0
            ? t('noise.trend.flat')
            : t(delta < 0 ? 'noise.trend.down' : 'noise.trend.up', { value: Math.abs(delta).toFixed(1) });
        const recoveryTrend = summary.recovery_sampled
            ? t('noise.summary.recoveriesSampled', { value: summary.recoveries || 0, sample: summary.recovery_sample_size || 0 })
            : t('noise.summary.recoveries', { value: summary.recoveries || 0 });
        target.innerHTML =
            statCard(t('noise.summary.total'), summary.total || 0, t('noise.window.label', { days: data.window_days }), 'var(--text-main)') +
            statCard(t('noise.summary.noiseRate'), (summary.noise_rate || 0) + '%', trend, Number(summary.noise_rate || 0) >= 50 ? 'var(--danger)' : 'var(--warning)') +
            statCard(t('noise.summary.duplicateRate'), (summary.duplicate_rate || 0) + '%', t('noise.summary.duplicates', { value: summary.duplicates || 0 }), 'var(--primary)') +
            statCard(t('noise.summary.recoveryRate'), (summary.recovery_rate || 0) + '%', recoveryTrend, 'var(--success)') +
            statCard(t('noise.summary.avoided'), summary.notifications_avoided || 0, t('noise.summary.filtered'), 'var(--success)') +
            statCard(t('noise.summary.timeSaved'), savedTime(summary.estimated_minutes_saved), t('noise.summary.timeAssumption', { minutes: (data.assumptions || {}).minutes_per_avoided_notification || 3 }), 'var(--success)');
    }

    function renderSuggestions(items) {
        const target = document.getElementById('noiseSuggestions');
        if (!target) return;
        if (!items.length) {
            target.innerHTML = '<div class="empty-state" style="padding:36px; text-align:center;">' +
                '<div style="font-size:40px; margin-bottom:10px;">✨</div><div class="empty-title">' +
                escapeHtml(t('noise.empty.title')) + '</div><div class="empty-text">' +
                escapeHtml(t('noise.empty.text')) + '</div></div>';
            return;
        }
        target.innerHTML = '<div style="display:flex; flex-direction:column; gap:12px;">' + items.map(function (item) {
            const copy = suggestionCopy(item);
            const risk = item.risk === 'low' ? t('noise.risk.low') : (item.risk === 'medium' ? t('noise.risk.medium') : t('noise.risk.external'));
            const action = item.action_available
                ? '<button type="button" class="btn btn-sm btn-primary" data-noise-apply="' + escapeHtml(item.id) +
                    '" data-noise-risk="' + escapeHtml(item.risk || '') + '">' + escapeHtml(t('noise.apply')) + '</button>'
                : '<span style="font-size:0.78rem; color:var(--text-muted);">' + escapeHtml(t('noise.manual')) + '</span>';
            return '<div style="border:1px solid var(--border); border-radius:var(--radius-lg); padding:16px; background:var(--bg-surface);">' +
                '<div style="display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap;">' +
                '<div style="min-width:0; flex:1;"><div style="font-weight:700; margin-bottom:6px;">' + escapeHtml(copy.title) + '</div>' +
                '<div style="font-size:0.85rem; color:var(--text-secondary);">' + escapeHtml(copy.reason) + '</div>' +
                '<div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; font-size:0.75rem; color:var(--text-muted);">' +
                '<span class="badge">' + escapeHtml(t('noise.impact', { value: item.estimated_notifications || 0 })) + '</span>' +
                '<span class="badge">' + escapeHtml(savedTime(item.estimated_minutes_saved)) + '</span>' +
                '<span class="badge">' + escapeHtml(t('noise.confidence', { value: Math.round(Number(item.confidence || 0) * 100) })) + '</span>' +
                '<span class="badge">' + escapeHtml(risk) + '</span></div></div><div>' + action + '</div></div></div>';
        }).join('') + '</div>';
        target.querySelectorAll('[data-noise-apply]').forEach(function (button) {
            button.addEventListener('click', function () { applySuggestion(button); });
        });
    }

    function renderSources(sources) {
        const target = document.getElementById('noiseSources');
        if (!target) return;
        if (!sources.length) {
            target.innerHTML = '<div class="empty-text" style="padding:24px;">' + escapeHtml(t('noise.sources.empty')) + '</div>';
            return;
        }
        target.innerHTML = '<div style="overflow-x:auto;"><table class="data-table" style="width:100%;">' +
            '<thead><tr><th>' + escapeHtml(t('noise.sources.source')) + '</th><th>' + escapeHtml(t('noise.summary.total')) +
            '</th><th>' + escapeHtml(t('noise.summary.duplicateRate')) + '</th><th>' + escapeHtml(t('noise.summary.noiseRate')) +
            '</th><th>' + escapeHtml(t('noise.summary.avoided')) + '</th><th>' + escapeHtml(t('noise.sources.recoveries')) + '</th></tr></thead><tbody>' +
            sources.map(function (source) {
                return '<tr><td><strong>' + escapeHtml(source.source || 'unknown') + '</strong></td><td>' + escapeHtml(String(source.total || 0)) +
                    '</td><td>' + escapeHtml(String(source.duplicate_rate || 0)) + '%</td><td>' + escapeHtml(String(source.noise_rate || 0)) +
                    '%</td><td>' + escapeHtml(String(source.notifications_avoided || 0)) + '</td><td>' + escapeHtml(String(source.recoveries || 0)) + '</td></tr>';
            }).join('') + '</tbody></table></div>';
    }

    function actionLabel(action) {
        return action.action_type === 'duplicate_filter'
            ? t('noise.action.duplicate')
            : t('noise.action.silence');
    }

    function renderActions(actions) {
        const target = document.getElementById('noiseRecentActions');
        if (!target) return;
        if (!actions.length) {
            target.innerHTML = '<div class="empty-text" style="padding:20px 0;">' + escapeHtml(t('noise.actions.empty')) + '</div>';
            return;
        }
        target.innerHTML = '<div style="display:flex; flex-direction:column; gap:8px;">' + actions.map(function (action) {
            const when = action.created_at && typeof formatTime === 'function' ? formatTime(action.created_at) : '';
            const undo = action.undo_available
                ? '<button type="button" class="btn btn-sm" data-noise-undo="' + escapeHtml(String(action.id)) + '">' + escapeHtml(t('noise.undo')) + '</button>'
                : '<span class="badge">' + escapeHtml(t('noise.undone')) + '</span>';
            return '<div style="display:flex; justify-content:space-between; gap:12px; align-items:center; padding:10px 0; border-bottom:1px solid var(--border);">' +
                '<div><div style="font-weight:600;">' + escapeHtml(actionLabel(action)) + '</div><div style="font-size:0.75rem; color:var(--text-muted);">' +
                escapeHtml(action.actor || 'operator') + ' · ' + escapeHtml(when) + '</div></div>' + undo + '</div>';
        }).join('') + '</div>';
        target.querySelectorAll('[data-noise-undo]').forEach(function (button) {
            button.addEventListener('click', function () { undoAction(button); });
        });
    }

    async function applySuggestion(button) {
        if (button.getAttribute('data-noise-risk') === 'medium' && !window.confirm(t('noise.confirm.medium'))) return;
        button.disabled = true;
        try {
            const response = await API.authenticatedFetch('/v1/noise-center/actions', {
                method: 'POST',
                body: JSON.stringify({ suggestion_id: button.getAttribute('data-noise-apply'), window_days: windowDays() })
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            await load();
        } catch (error) {
            window.alert(t('noise.applyFailed', { error: error.message || String(error) }));
            button.disabled = false;
        }
    }

    async function undoAction(button) {
        if (!window.confirm(t('noise.confirm.undo'))) return;
        button.disabled = true;
        try {
            const response = await API.authenticatedFetch('/v1/noise-center/actions/' + button.getAttribute('data-noise-undo') + '/undo', {
                method: 'POST', body: JSON.stringify({ actor: 'operator' })
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            await load();
        } catch (error) {
            window.alert(t('noise.undoFailed', { error: error.message || String(error) }));
            button.disabled = false;
        }
    }

    function render(data) {
        renderSummary(data);
        renderSuggestions(Array.isArray(data.suggestions) ? data.suggestions : []);
        renderSources(Array.isArray(data.sources) ? data.sources : []);
        renderActions(Array.isArray(data.recent_actions) ? data.recent_actions : []);
    }

    async function load() {
        const suggestions = document.getElementById('noiseSuggestions');
        if (suggestions) suggestions.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + escapeHtml(t('common.loading')) + '</p></div>';
        try {
            const response = await API.authenticatedFetch('/v1/noise-center?days=' + windowDays());
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            render(payload.data || {});
        } catch (error) {
            if (suggestions) suggestions.innerHTML = '<div class="empty-state" style="color:var(--danger); padding:40px;">' +
                escapeHtml(t('common.loadFailed')) + ': ' + escapeHtml(error.message || String(error)) + '</div>';
        }
    }

    return { load: load };
})();
