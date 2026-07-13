/** Curated integration catalog and guided forwarding-rule setup. */
const IntegrationsModule = (function () {
    let catalog = [];
    let selected = null;

    async function load() {
        const container = document.getElementById('integrationCatalog');
        if (!container) return;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' + t('common.loading') + '</p></div>';
        try {
            const response = await API.authenticatedFetch('/v1/integrations/catalog');
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
            catalog = payload.data || [];
            renderCatalog();
        } catch (error) {
            container.innerHTML = '<div class="empty-state" style="color:var(--danger);">' + escapeHtml(error.message || String(error)) + '</div>';
        }
    }

    function renderCatalog() {
        const container = document.getElementById('integrationCatalog');
        container.innerHTML = '<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px;">' +
            catalog.map(function (item) {
                return '<button type="button" class="integration-card" data-template="' + escapeHtml(item.id) +
                    '" style="text-align:left; padding:18px; border:1px solid var(--border); border-radius:var(--radius-lg); background:var(--bg-surface); color:inherit; cursor:pointer;">' +
                    '<div style="font-size:32px;">' + escapeHtml(item.icon || '🔌') + '</div>' +
                    '<div style="font-weight:700; margin:8px 0 5px;">' + escapeHtml(item.name) + '</div>' +
                    '<div style="font-size:0.82rem; color:var(--text-secondary); line-height:1.45;">' + escapeHtml(item.description) + '</div></button>';
            }).join('') + '</div>';
        container.querySelectorAll('[data-template]').forEach(function (button) {
            button.addEventListener('click', function () { selectTemplate(button.getAttribute('data-template')); });
        });
    }

    function selectTemplate(id) {
        selected = catalog.find(function (item) { return item.id === id; });
        if (!selected) return;
        const setup = document.getElementById('integrationSetup');
        setup.style.display = 'block';
        setup.innerHTML = '<div style="border:1px solid var(--border); border-radius:var(--radius-lg); padding:20px; background:var(--bg-surface);">' +
            '<div style="font-weight:700; margin-bottom:14px;">1. Configure ' + escapeHtml(selected.name) + '</div>' +
            '<div class="filter-grid">' +
            field('integrationName', 'Rule name', selected.name + ' alerts') +
            (selected.requires_url ? field('integrationUrl', 'Target URL', '', selected.url_hint) : '') +
            field('integrationSource', 'Source filter', '', 'Optional') +
            field('integrationProject', 'Project filter', '', 'Optional') +
            field('integrationEnvironment', 'Environment filter', '', 'Optional') +
            '<div class="filter-item"><label class="filter-label">Importance</label><select id="integrationImportance" class="filter-input"><option value="">All</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></div>' +
            '</div><div style="display:flex; gap:8px; margin-top:16px;">' +
            '<button class="btn" id="integrationTestBtn">2. Test</button>' +
            '<button class="btn btn-primary" id="integrationInstallBtn">3. Test and install</button>' +
            '<span id="integrationStatus" style="font-size:0.82rem; color:var(--text-secondary); align-self:center;"></span>' +
            '</div></div>';
        document.getElementById('integrationTestBtn').addEventListener('click', test);
        document.getElementById('integrationInstallBtn').addEventListener('click', install);
        setup.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function field(id, label, value, placeholder) {
        return '<div class="filter-item"><label class="filter-label">' + escapeHtml(label) + '</label><input id="' + id +
            '" class="filter-input" value="' + escapeHtml(value || '') + '" placeholder="' + escapeHtml(placeholder || '') + '"></div>';
    }

    function values() {
        return {
            template_id: selected.id,
            name: document.getElementById('integrationName').value.trim(),
            target_url: selected.requires_url ? document.getElementById('integrationUrl').value.trim() : '',
            source: document.getElementById('integrationSource').value.trim(),
            project: document.getElementById('integrationProject').value.trim(),
            environment: document.getElementById('integrationEnvironment').value.trim(),
            importance: document.getElementById('integrationImportance').value
        };
    }

    async function request(path, body) {
        const status = document.getElementById('integrationStatus');
        status.textContent = 'Working…';
        const response = await API.authenticatedFetch(path, { method: 'POST', body: JSON.stringify(body) });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || 'HTTP ' + response.status);
        status.textContent = payload.message || 'Done';
        return payload;
    }

    async function test() {
        try {
            const data = values();
            await request('/v1/integrations/test', { template_id: data.template_id, name: data.name, target_url: data.target_url });
        } catch (error) {
            document.getElementById('integrationStatus').textContent = 'Test failed: ' + (error.message || String(error));
        }
    }

    async function install() {
        try {
            await request('/v1/integrations', { ...values(), enabled: true, priority: 10, target_name: '' });
            if (typeof loadForwardRules === 'function') loadForwardRules();
        } catch (error) {
            document.getElementById('integrationStatus').textContent = 'Install failed: ' + (error.message || String(error));
        }
    }

    return { load: load };
})();
