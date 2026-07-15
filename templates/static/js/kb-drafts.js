/**
 * KB drafts review module (Operations → Knowledge drafts sub-view).
 *
 * Resolved-incident summaries are queued as drafts before they feed the AI
 * knowledge base (RAG). An operator reviews each and either publishes it into
 * the KB or discards it. Read via the API key; publish/discard are admin-write.
 * Mirrors the action-center / noise-center Operations sub-view pattern.
 */
const KbDraftsModule = (function () {
    function render(drafts) {
        const listEl = document.getElementById('kbDraftsList');
        if (!listEl) return;

        const items = Array.isArray(drafts) ? drafts : [];
        if (!items.length) {
            listEl.innerHTML = '<div class="empty-state" style="text-align:center; padding:60px;">' +
                '<div style="font-size:48px; margin-bottom:16px;">📚</div>' +
                '<div class="empty-title">' + escapeHtml(t('kb.empty.title')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('kb.empty.text')) + '</div></div>';
            return;
        }

        listEl.innerHTML = '<div style="display:flex; flex-direction:column; gap:12px;">' + items.map(function (draft) {
            const ref = draft.source_ref || '';
            const when = draft.updated_at && typeof formatTime === 'function' ? formatTime(draft.updated_at) : '';
            const chunks = escapeHtml(String(draft.chunks != null ? draft.chunks : 0));
            return '<div style="background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:16px;">' +
                '<div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start;">' +
                '<div style="min-width:0;">' +
                '<div style="font-weight:700; margin-bottom:6px; overflow-wrap:anywhere;">📄 ' + escapeHtml(draft.title || ref) + '</div>' +
                '<div style="font-size:0.8rem; color:var(--text-muted);">' +
                '<span class="badge badge-outline" style="font-size:0.65rem;">' + escapeHtml(ref) + '</span> · ' +
                escapeHtml(t('kb.chunks', { n: chunks })) + '</div></div>' +
                '<span style="font-size:0.75rem; color:var(--text-muted); white-space:nowrap;">' + escapeHtml(when) + '</span></div>' +
                '<div style="display:flex; gap:8px; margin-top:12px;">' +
                '<button type="button" class="btn btn-sm btn-primary" data-kb-publish="' + escapeHtml(ref) + '">✅ ' + escapeHtml(t('kb.publish')) + '</button>' +
                '<button type="button" class="btn btn-sm" data-kb-discard="' + escapeHtml(ref) + '">🗑️ ' + escapeHtml(t('kb.discard')) + '</button>' +
                '</div></div>';
        }).join('') + '</div>';

        listEl.querySelectorAll('[data-kb-publish]').forEach(function (button) {
            button.addEventListener('click', function () { publish(button.getAttribute('data-kb-publish'), button); });
        });
        listEl.querySelectorAll('[data-kb-discard]').forEach(function (button) {
            button.addEventListener('click', function () { discard(button.getAttribute('data-kb-discard'), button); });
        });
    }

    async function publish(sourceRef, button) {
        if (!sourceRef || !window.confirm(t('kb.confirmPublish'))) return;
        if (button) button.disabled = true;
        try {
            const result = await API.publishKbDraft(sourceRef);
            const n = (result && result.data && result.data.published_chunks) || 0;
            if (typeof showToast === 'function') showToast(t('kb.publishOk', { n: n }), 'success');
            await load();  // optimistic refresh: the published draft drops off the list
        } catch (error) {
            if (button) button.disabled = false;
            alert(t('kb.publishFail') + ': ' + (error.message || String(error)));
        }
    }

    async function discard(sourceRef, button) {
        if (!sourceRef || !window.confirm(t('kb.confirmDiscard'))) return;
        if (button) button.disabled = true;
        try {
            const result = await API.discardKbDraft(sourceRef);
            const n = (result && result.data && result.data.discarded_chunks) || 0;
            if (typeof showToast === 'function') showToast(t('kb.discardOk', { n: n }), 'success');
            await load();
        } catch (error) {
            if (button) button.disabled = false;
            alert(t('kb.discardFail') + ': ' + (error.message || String(error)));
        }
    }

    async function load() {
        const listEl = document.getElementById('kbDraftsList');
        if (listEl) listEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        try {
            const result = await API.getKbDrafts();
            render((result && result.data) || []);
        } catch (error) {
            if (listEl) {
                listEl.innerHTML = '<div class="empty-state" style="color:var(--danger); padding:40px;">' +
                    escapeHtml(t('common.loadFailed')) + ': ' + escapeHtml(error.message || String(error)) + '</div>';
            }
        }
    }

    return { load: load };
})();
