/**
 * KB drafts review module (Operations → Knowledge drafts sub-view).
 *
 * Resolved-incident summaries are queued as drafts before they feed the AI
 * knowledge base (RAG). An operator reviews each and either publishes it into
 * the KB or discards it. Read via the API key; publish/discard are admin-write.
 * Mirrors the action-center / noise-center Operations sub-view pattern.
 */
const KbDraftsModule = (function () {
    let kbQuery = '';
    let kbPage = 1;
    let kbDrafts = [];
    function render(drafts) {
        const listEl = document.getElementById('kbDraftsList');
        if (!listEl) return;

        kbDrafts = Array.isArray(drafts) ? drafts : [];
        if (!kbDrafts.length) {
            listEl.innerHTML = '<div class="empty-state">' +
                '<div class="empty-icon">' + wwIcon('book-open') + '</div>' +
                '<div class="empty-title">' + escapeHtml(t('kb.empty.title')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('kb.empty.text')) + '</div></div>';
            return;
        }

        const paged = wwFilterPage(kbDrafts, kbQuery, kbPage, 20, function (draft) {
            return [draft.title, draft.source_ref].filter(Boolean).join(' ');
        });
        kbPage = paged.page;
        if (!paged.rows.length) {
            listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('filter') + '</div>' +
                '<div class="empty-title">' + t('common.noMatches') + '</div></div>';
            return;
        }
        const bulkBar = '<div style="display:flex; gap:8px; align-items:center; margin-bottom:12px;">' +
            '<button type="button" class="btn btn-sm" data-kb-pick-all>' + escapeHtml(t('kb.bulk.selectPage')) + '</button>' +
            '<button type="button" class="btn btn-sm btn-quiet-primary" data-kb-bulk-publish>' + wwIcon('check') + ' ' + escapeHtml(t('kb.bulk.publish')) + '</button>' +
            '<button type="button" class="btn btn-sm" data-kb-bulk-discard>' + wwIcon('trash') + ' ' + escapeHtml(t('kb.bulk.discard')) + '</button>' +
            '<span data-kb-pick-count style="color:var(--text-muted); font-size:var(--fs-sm);"></span></div>';
        listEl.innerHTML = bulkBar + '<div style="display:flex; flex-direction:column; gap:12px;">' + paged.rows.map(function (draft, i) {
            const ref = draft.source_ref || '';
            const when = draft.updated_at && typeof formatTime === 'function' ? formatTime(draft.updated_at) : '';
            const chunks = escapeHtml(String(draft.chunks != null ? draft.chunks : 0));
            return '<div style="background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:16px;">' +
                '<div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start;">' +
                '<label style="display:flex; align-items:flex-start; gap:10px; min-width:0; cursor:pointer;">' +
                '<input type="checkbox" data-kb-pick="' + escapeHtml(ref) + '" style="margin-top:4px;">' +
                '<span style="min-width:0; display:block;">' +
                '<div style="font-weight:700; margin-bottom:6px; overflow-wrap:anywhere;">' + wwIcon('file-text') + ' ' + escapeHtml(draft.title || ref) + '</div>' +
                '<div style="font-size:0.8rem; color:var(--text-muted);">' +
                '<span class="badge badge-outline" style="font-size:0.65rem;">' + escapeHtml(ref) + '</span> · ' +
                escapeHtml(t('kb.chunks', { n: chunks })) + '</div></span></label>' +
                '<span style="font-size:0.75rem; color:var(--text-muted); white-space:nowrap;">' + escapeHtml(when) + '</span></div>' +
                '<div style="display:flex; gap:8px; margin-top:12px;">' +
                '<button type="button" class="btn btn-sm" data-kb-detail="' + escapeHtml(ref) + '" data-kb-panel="kbDraftDetail' + i + '">' + wwIcon('eye') + ' ' + escapeHtml(t('kb.detail')) + '</button>' +
                '<button type="button" class="btn btn-sm btn-quiet-primary" data-kb-publish="' + escapeHtml(ref) + '">' + wwIcon('check') + ' ' + escapeHtml(t('kb.publish')) + '</button>' +
                '<button type="button" class="btn btn-sm" data-kb-discard="' + escapeHtml(ref) + '">' + wwIcon('trash') + ' ' + escapeHtml(t('kb.discard')) + '</button>' +
                '</div>' +
                '<div id="kbDraftDetail' + i + '" style="display:none; margin-top:12px; padding-top:12px; border-top:1px solid var(--border-light);"></div>' +
                '</div>';
        }).join('') + '</div>' + wwPagerHtml(paged, 'KbDraftsModule.page');

        listEl.querySelectorAll('[data-kb-detail]').forEach(function (button) {
            button.addEventListener('click', function () {
                toggleDetail(button.getAttribute('data-kb-detail'), button.getAttribute('data-kb-panel'), button);
            });
        });
        listEl.querySelectorAll('[data-kb-publish]').forEach(function (button) {
            button.addEventListener('click', function () { publish(button.getAttribute('data-kb-publish'), button); });
        });
        listEl.querySelectorAll('[data-kb-discard]').forEach(function (button) {
            button.addEventListener('click', function () { discard(button.getAttribute('data-kb-discard'), button); });
        });
        bindBulkActions(listEl);
    }

    // Full text per open panel, for the edit textarea (chunks joined; the
    // server re-chunks on save, so the document edits as one text).
    const detailText = {};

    // Inline detail: fetched on first open, kept in the DOM after that. The
    // review queue must show the material under review — publish/discard
    // without it is approval of the invisible.
    async function toggleDetail(sourceRef, panelId, button) {
        const panel = document.getElementById(panelId);
        if (!panel) return;
        if (panel.style.display !== 'none') {
            panel.style.display = 'none';
            return;
        }
        panel.style.display = 'block';
        if (!panel.getAttribute('data-loaded')) {
            if (button) button.disabled = true;
            try {
                await renderDetail(sourceRef, panel);
                panel.setAttribute('data-loaded', '1');
            } finally {
                if (button) button.disabled = false;
            }
        }
    }

    async function renderDetail(sourceRef, panel) {
        panel.innerHTML = '<div style="color:var(--text-muted); font-size:0.85rem;">' + escapeHtml(t('common.loading')) + '</div>';
        try {
            const result = await API.getKbDraft(sourceRef);
            const chunks = (result && result.data && result.data.chunks) || [];
            detailText[panel.id] = chunks.map(function (c) { return c.content || ''; }).join('\n\n');
            panel.innerHTML = '<div style="display:flex; justify-content:flex-end; margin-bottom:8px;">' +
                '<button type="button" class="btn btn-sm" data-kb-edit="1">' + wwIcon('pencil') + ' ' + escapeHtml(t('kb.edit')) + '</button></div>' +
                (chunks.map(function (chunk, idx) {
                    return '<div style="margin-bottom:12px;">' +
                        (chunks.length > 1
                            ? '<div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px;">' +
                              escapeHtml(t('kb.chunkLabel', { n: idx + 1 })) + '</div>'
                            : '') +
                        '<div style="white-space:pre-wrap; overflow-wrap:anywhere; font-size:0.85rem; line-height:1.65; color:var(--text-secondary); max-width:70rem;">' +
                        escapeHtml(chunk.content || '') + '</div></div>';
                }).join('') || '<div style="color:var(--text-muted);">—</div>');
            const editBtn = panel.querySelector('[data-kb-edit]');
            if (editBtn) editBtn.addEventListener('click', function () { enterEdit(sourceRef, panel); });
        } catch (error) {
            panel.innerHTML = '<div style="color:var(--danger); font-size:0.85rem;">' +
                escapeHtml(t('common.loadFailed') + ': ' + (error.message || String(error))) + '</div>';
        }
    }

    // Amend-before-approve: a wrong AI summary should be corrected, not
    // published wrong or thrown away whole. Saving re-chunks and re-embeds
    // server-side, so what you read here is exactly what RAG will retrieve.
    function enterEdit(sourceRef, panel) {
        const original = detailText[panel.id] || '';
        panel.innerHTML = '<textarea class="filter-input" aria-label="' + escapeHtml(t('kb.contentLabel')) + '" style="width:100%; min-height:260px; font-size:0.85rem; line-height:1.6; resize:vertical; font-family:inherit;"></textarea>' +
            '<div style="display:flex; gap:8px; margin-top:8px;">' +
            '<button type="button" class="btn btn-sm btn-quiet-primary" data-kb-save="1">' + wwIcon('check') + ' ' + escapeHtml(t('kb.save')) + '</button>' +
            '<button type="button" class="btn btn-sm" data-kb-cancel="1">' + escapeHtml(t('kb.cancel')) + '</button></div>';
        const area = panel.querySelector('textarea');
        area.value = original;
        panel.querySelector('[data-kb-cancel]').addEventListener('click', function () { renderDetail(sourceRef, panel); });
        panel.querySelector('[data-kb-save]').addEventListener('click', async function () {
            const text = area.value.trim();
            if (!text) return;
            const saveBtn = panel.querySelector('[data-kb-save]');
            saveBtn.disabled = true;
            try {
                await API.updateKbDraft(sourceRef, text);
                if (typeof showToast === 'function') showToast(t('kb.editOk'), 'success');
                // Refresh in place; a full list reload would collapse this
                // panel right after the operator saved into it.
                await renderDetail(sourceRef, panel);
            } catch (error) {
                saveBtn.disabled = false;
                showToast(t('kb.editFail') + ': ' + (error.message || String(error)), 'error');
            }
        });
    }

    async function publish(sourceRef, button) {
        if (!sourceRef || !(await wwConfirm(t('kb.confirmPublish')))) return;
        if (button) button.disabled = true;
        try {
            const result = await API.publishKbDraft(sourceRef);
            const n = (result && result.data && result.data.published_chunks) || 0;
            if (typeof showToast === 'function') showToast(t('kb.publishOk', { n: n }), 'success');
            await load();  // optimistic refresh: the published draft drops off the list
        } catch (error) {
            if (button) button.disabled = false;
            showToast(t('kb.publishFail') + ': ' + (error.message || String(error)), 'error');
        }
    }

    async function discard(sourceRef, button) {
        if (!sourceRef || !(await wwConfirm(t('kb.confirmDiscard'), { danger: true }))) return;
        if (button) button.disabled = true;
        try {
            const result = await API.discardKbDraft(sourceRef);
            const n = (result && result.data && result.data.discarded_chunks) || 0;
            if (typeof showToast === 'function') showToast(t('kb.discardOk', { n: n }), 'success');
            await load();
        } catch (error) {
            if (button) button.disabled = false;
            showToast(t('kb.discardFail') + ': ' + (error.message || String(error)), 'error');
        }
    }

    function pickedRefs() {
        const listEl = document.getElementById('kbDraftsList');
        if (!listEl) return [];
        return Array.prototype.map.call(
            listEl.querySelectorAll('[data-kb-pick]:checked'),
            function (box) { return box.getAttribute('data-kb-pick'); }
        ).filter(Boolean);
    }

    function updatePickCount() {
        const el = document.querySelector('[data-kb-pick-count]');
        if (el) {
            const n = pickedRefs().length;
            el.textContent = n ? t('kb.bulk.picked', { n: n }) : '';
        }
    }

    /**
     * Sequential on purpose: publish re-embeds server-side, and 60 parallel
     * embedding calls is a rate-limit incident, not a feature.
     */
    async function bulkRun(refs, actionFn, doneKey) {
        let ok = 0;
        let failed = 0;
        for (const ref of refs) {
            try {
                await actionFn(ref);
                ok += 1;
            } catch (error) {
                failed += 1;
            }
        }
        showToast(t(doneKey, { ok: ok, failed: failed }), failed ? 'warning' : 'success');
        await load();
    }

    function bindBulkActions(listEl) {
        listEl.querySelectorAll('[data-kb-pick]').forEach(function (box) {
            box.addEventListener('change', updatePickCount);
        });
        const pickAll = listEl.querySelector('[data-kb-pick-all]');
        if (pickAll) {
            pickAll.addEventListener('click', function () {
                const boxes = listEl.querySelectorAll('[data-kb-pick]');
                const allOn = Array.prototype.every.call(boxes, function (b) { return b.checked; });
                boxes.forEach(function (b) { b.checked = !allOn; });
                updatePickCount();
            });
        }
        const bulkPublish = listEl.querySelector('[data-kb-bulk-publish]');
        if (bulkPublish) {
            bulkPublish.addEventListener('click', async function () {
                const refs = pickedRefs();
                if (!refs.length) { showToast(t('kb.bulk.none'), 'info'); return; }
                if (!(await wwConfirm(t('kb.bulk.confirmPublish', { n: refs.length })))) return;
                bulkPublish.disabled = true;
                await bulkRun(refs, function (ref) { return API.publishKbDraft(ref); }, 'kb.bulk.publishDone');
            });
        }
        const bulkDiscard = listEl.querySelector('[data-kb-bulk-discard]');
        if (bulkDiscard) {
            bulkDiscard.addEventListener('click', async function () {
                const refs = pickedRefs();
                if (!refs.length) { showToast(t('kb.bulk.none'), 'info'); return; }
                if (!(await wwConfirm(t('kb.bulk.confirmDiscard', { n: refs.length })))) return;
                bulkDiscard.disabled = true;
                await bulkRun(refs, function (ref) { return API.discardKbDraft(ref); }, 'kb.bulk.discardDone');
            });
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

    return {
        load: load,
        search: function (value) {
            kbQuery = String(value || '');
            kbPage = 1;
            render(kbDrafts);
        },
        page: function (page) {
            kbPage = Number(page) || 1;
            render(kbDrafts);
        }
    };
})();

// Join the delegated-action registry: const bindings never reach window,
// so without this every data-act="KbDraftsModule.*" resolves to null.
if (typeof wwRegisterActionRoot === 'function') wwRegisterActionRoot('KbDraftsModule', KbDraftsModule);
