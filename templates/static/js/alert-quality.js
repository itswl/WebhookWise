/**
 * Alert Quality Center — read-only diagnostics for inbound alert sources.
 *
 * There are deliberately no mutation calls or action buttons in this module.
 * Findings describe evidence and upstream changes an operator may make outside
 * WebhookWise after reviewing the source configuration.
 */
const AlertQualityModule = (function () {
    'use strict';

    let bound = false;
    let loading = false;

    function number(value) {
        const parsed = Number(value || 0);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function percent(value) {
        return number(value).toFixed(1).replace(/\.0$/, '') + '%';
    }

    function scoreClass(score) {
        if (score === null || score === undefined) return ' is-no-data';
        if (number(score) >= 90) return ' is-healthy';
        if (number(score) >= 75) return ' is-fair';
        if (number(score) >= 60) return ' is-warning';
        return ' is-poor';
    }

    function gradeLabel(grade) {
        return t('quality.grade.' + String(grade || 'no_data'));
    }

    function severityLabel(severity) {
        return t('quality.severity.' + String(severity || 'low'));
    }

    function coverageItem(key, value) {
        const safeValue = Math.max(0, Math.min(100, number(value)));
        return '<div class="alert-quality-coverage-item">' +
            '<div><span>' + escapeHtml(t('quality.coverage.' + key)) + '</span><strong>' +
            escapeHtml(percent(safeValue)) + '</strong></div>' +
            '<div class="alert-quality-meter"><i style="width:' + safeValue + '%"></i></div></div>';
    }

    function renderSummary(data) {
        const summary = data.summary || {};
        const recovery = summary.recovery || {};
        const score = summary.quality_score;
        const severity = summary.severity_counts || {};
        return '<section class="alert-quality-summary">' +
            '<article class="alert-quality-score-card' + scoreClass(score) + '">' +
                '<span>' + escapeHtml(t('quality.summary.score')) + '</span>' +
                '<strong>' + escapeHtml(score == null ? '—' : Math.round(number(score))) + '</strong>' +
                '<small>' + escapeHtml(t('quality.summary.scoredSources', {
                    value: number(summary.scored_source_count)
                })) + '</small></article>' +
            '<article><span>' + escapeHtml(t('quality.summary.sources')) + '</span><strong>' +
                escapeHtml(formatNumber(number(summary.source_count))) + '</strong><small>' +
                escapeHtml(t('quality.summary.noData', {
                    value: number(summary.no_data_source_count)
                })) + '</small></article>' +
            '<article><span>' + escapeHtml(t('quality.summary.events')) + '</span><strong>' +
                escapeHtml(formatNumber(number(summary.events_scanned))) + '</strong><small>' +
                escapeHtml(t('quality.summary.windowEvents', {
                    value: formatNumber(number(summary.events_in_window))
                })) + '</small></article>' +
            '<article><span>' + escapeHtml(t('quality.summary.findings')) + '</span><strong>' +
                escapeHtml(formatNumber(number(summary.finding_count))) + '</strong><small>' +
                escapeHtml(t('quality.summary.highFindings', {
                    value: number(severity.high)
                })) + '</small></article>' +
            '<article><span>' + escapeHtml(t('quality.summary.recovery')) + '</span><strong>' +
                escapeHtml(percent(recovery.match_rate)) + '</strong><small>' +
                escapeHtml(t('quality.summary.recoveryDetail', {
                    matched: number(recovery.matched),
                    total: number(recovery.events)
                })) + '</small></article></section>';
    }

    function renderGlobalCoverage(summary) {
        const coverage = summary.field_coverage || {};
        return '<section class="alert-quality-panel"><div class="alert-quality-panel-heading">' +
            '<div><h3>' + escapeHtml(t('quality.coverage.title')) + '</h3><p>' +
            escapeHtml(t('quality.coverage.note')) + '</p></div></div>' +
            '<div class="alert-quality-coverage-grid">' +
            coverageItem('stable_identity', coverage.stable_identity) +
            coverageItem('service', coverage.service) +
            coverageItem('environment', coverage.environment) +
            coverageItem('severity', coverage.severity) +
            '</div></section>';
    }

    function renderTopFindings(findings) {
        if (!findings.length) {
            return '<section class="alert-quality-panel"><div class="alert-quality-panel-heading"><div><h3>' +
                escapeHtml(t('quality.top.title')) + '</h3><p>' +
                escapeHtml(t('quality.top.healthy')) + '</p></div></div></section>';
        }
        return '<section class="alert-quality-panel"><div class="alert-quality-panel-heading"><div><h3>' +
            escapeHtml(t('quality.top.title')) + '</h3><p>' +
            escapeHtml(t('quality.top.note')) + '</p></div></div><div class="alert-quality-top-list">' +
            findings.map(function (finding) {
                return '<div class="alert-quality-top-item is-' + escapeHtml(finding.severity || 'low') + '">' +
                    '<span class="alert-quality-severity">' +
                    escapeHtml(severityLabel(finding.severity)) + '</span><strong>' +
                    escapeHtml(t('quality.issue.' + finding.code + '.title')) + '</strong><small>' +
                    escapeHtml(t('quality.top.sources', {
                        sources: number(finding.source_count),
                        count: number(finding.affected_count)
                    })) + '</small></div>';
            }).join('') + '</div></section>';
    }

    function renderFinding(finding) {
        const code = String(finding.code || 'unknown');
        const samples = Array.isArray(finding.sample_event_ids) ? finding.sample_event_ids : [];
        return '<article class="alert-quality-finding is-' + escapeHtml(finding.severity || 'low') + '">' +
            '<div class="alert-quality-finding-title"><span class="alert-quality-severity">' +
            escapeHtml(severityLabel(finding.severity)) + '</span><strong>' +
            escapeHtml(t('quality.issue.' + code + '.title')) + '</strong><span>' +
            escapeHtml(t('quality.issue.affected', {
                count: number(finding.count),
                rate: percent(finding.rate)
            })) + '</span></div><p>' +
            escapeHtml(t('quality.issue.' + code + '.description', {
                count: number(finding.count),
                rate: percent(finding.rate)
            })) + '</p><div class="alert-quality-fix"><span>' +
            escapeHtml(t('quality.issue.suggestion')) + '</span><p>' +
            escapeHtml(t('quality.fix.' + code)) + '</p></div>' +
            (samples.length ? '<small class="alert-quality-samples">' +
                escapeHtml(t('quality.issue.samples', {
                    value: samples.map(function (id) { return '#' + id; }).join(', ')
                })) + '</small>' : '') + '</article>';
    }

    function renderSource(source) {
        const findings = Array.isArray(source.findings) ? source.findings : [];
        const score = source.quality_score;
        const coverage = source.coverage || {};
        const recovery = source.recovery || {};
        const state = source.credential_state || 'unmanaged';
        return '<article class="alert-quality-source">' +
            '<header><div><div class="alert-quality-source-name"><strong>' +
                escapeHtml(source.display_name || source.source || t('quality.unknownSource')) +
                '</strong><span>' + escapeHtml(source.source || 'unknown') + '</span>' +
                (source.managed ? '<em>' + escapeHtml(t('quality.managed')) + '</em>' : '') +
                '</div><p>' + escapeHtml(t('quality.source.activity', {
                    events: formatNumber(number(source.event_count)),
                    time: source.last_event_at ? timeAgo(source.last_event_at) : t('quality.never')
                })) + ' · ' + escapeHtml(t('quality.credential.' + state)) + '</p></div>' +
                '<div class="alert-quality-source-score' + scoreClass(score) + '"><strong>' +
                escapeHtml(score == null ? '—' : Math.round(number(score))) + '</strong><span>' +
                escapeHtml(gradeLabel(source.grade)) + '</span></div></header>' +
            '<div class="alert-quality-source-metrics">' +
                coverageItem('stable_identity', coverage.stable_identity) +
                coverageItem('service', coverage.service) +
                coverageItem('environment', coverage.environment) +
                coverageItem('severity', coverage.severity) +
                '<div class="alert-quality-mini-metric"><span>' +
                    escapeHtml(t('quality.source.recoveryMatch')) + '</span><strong>' +
                    escapeHtml(percent(recovery.match_rate)) + '</strong></div>' +
            '</div>' +
            (findings.length
                ? '<div class="alert-quality-findings">' + findings.map(renderFinding).join('') + '</div>'
                : '<div class="alert-quality-source-healthy">' + wwIcon('check') + ' ' +
                    escapeHtml(t(source.event_count ? 'quality.source.healthy' : 'quality.source.noFindings')) +
                    '</div>') +
            '</article>';
    }

    function renderSources(sources) {
        if (!sources.length) {
            return '<section class="alert-quality-panel"><div class="empty-state"><div class="empty-icon">' + wwIcon('inbox') + '</div>' +
                '<div class="empty-title">' + escapeHtml(t('quality.empty.title')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(t('quality.empty.text')) + '</div></div></section>';
        }
        return '<section class="alert-quality-sources"><div class="alert-quality-panel-heading"><div><h3>' +
            escapeHtml(t('quality.sources.title')) + '</h3><p>' +
            escapeHtml(t('quality.sources.note')) + '</p></div></div>' +
            sources.map(renderSource).join('') + '</section>';
    }

    function renderScanNote(data) {
        const scan = data.scan || {};
        const messages = [];
        if (scan.event_truncated) messages.push(t('quality.scan.eventsTruncated', {
            value: formatNumber(number(scan.event_limit))
        }));
        if (scan.source_truncated) messages.push(t('quality.scan.sourcesTruncated', {
            shown: number(scan.source_limit),
            total: number(scan.source_total)
        }));
        return '<div class="alert-quality-disclaimer"><span>' + wwIcon('lock') + '</span><div><strong>' +
            escapeHtml(t('quality.readOnlyTitle')) + '</strong><p>' +
            escapeHtml(t('quality.readOnlyNote')) +
            (messages.length ? ' ' + escapeHtml(messages.join(' ')) : '') +
            '</p></div></div>';
    }

    function render(data) {
        const container = document.getElementById('alertQualityContent');
        if (!container) return;
        const summary = data.summary || {};
        const sources = Array.isArray(data.sources) ? data.sources : [];
        const topFindings = Array.isArray(data.top_findings) ? data.top_findings : [];
        container.innerHTML = renderScanNote(data) + renderSummary(data) +
            renderGlobalCoverage(summary) + renderTopFindings(topFindings) +
            renderSources(sources);
    }

    async function load() {
        const container = document.getElementById('alertQualityContent');
        if (!container || loading) return;
        loading = true;
        container.innerHTML = '<div class="loading"><div class="spinner"></div><p>' +
            escapeHtml(t('common.loading')) + '</p></div>';
        const selector = document.getElementById('alertQualityWindow');
        const windowDays = number(selector && selector.value) || 7;
        try {
            const payload = await API.getAlertQuality({
                window_days: windowDays,
                source_limit: 100
            });
            render(payload.data || {});
        } catch (error) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">' + wwIcon('alert-triangle') + '</div>' +
                '<div class="empty-title">' + escapeHtml(t('common.loadFailed')) + '</div>' +
                '<div class="empty-text">' + escapeHtml(error.message || String(error)) + '</div></div>';
        } finally {
            loading = false;
        }
    }

    function bind() {
        if (bound) return;
        const selector = document.getElementById('alertQualityWindow');
        if (selector) selector.addEventListener('change', load);
        bound = true;
    }

    return {
        load: function () {
            bind();
            return load();
        }
    };
})();
