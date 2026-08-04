/**
 * Static-markup event bindings, generated out of inline on*= attributes
 * (the CSP end-state is script-src-attr 'none'; markup carries no code).
 * Selector -> event -> the exact original handler body. Handlers resolve
 * their globals lazily at click time, so script order stays irrelevant.
 */
document.addEventListener('DOMContentLoaded', function () {
    var bind = function (selector, event, handler) {
        document.querySelectorAll(selector).forEach(function (el) {
            el.addEventListener(event, handler);
        });
    };
    bind('#incidentsBadge', 'click', function (event) { navigateTo('incidents'); });
    bind('#authBtn', 'click', function (event) { openAuthModal(); });
    bind('#langToggleBtn', 'click', function (event) { I18N.toggleLang(); });
    bind('#themeToggleBtn', 'click', function (event) { toggleTheme(); });
    bind('#importanceFilter', 'change', function (event) { filterAlerts(); });
    bind('#sourceFilter', 'change', function (event) { filterAlerts(); });
    bind('#duplicateFilter', 'change', function (event) { filterAlerts(); });
    bind('#processingStatusFilter', 'change', function (event) { filterAlerts(); });
    bind('#firstPage', 'click', function (event) { goToPage(1); });
    bind('#prevPage', 'click', function (event) { goToPage('prev'); });
    bind('#nextPage', 'click', function (event) { goToPage('next'); });
    bind('#lastPage', 'click', function (event) { goToPage('last'); });
    bind('#loadMoreBtn', 'click', function (event) { loadMoreWebhooks(); });
    bind('#pageSize', 'change', function (event) { changePageSize(); });
    bind('#incidentSearchInput', 'input', function (event) { IncidentsModule.search(); });
    bind('#incidentStatusFilter', 'change', function (event) { IncidentsModule.toggleStatus(); });
    bind('[data-sb="sb1"]', 'click', function (event) { IncidentsModule.load(); });
    bind('[data-sb="sb2"]', 'click', function (event) { DeepAnalysesModule.load(); });
    bind('[data-sb="sb3"]', 'click', function (event) { DecisionTraceModule.load(); });
    bind('[data-sb="sb4"]', 'click', function (event) { RoutingModule.setView('sandbox'); });
    bind('[data-sb="sb5"]', 'click', function (event) { RoutingModule.setView('audit'); });
    bind('[data-sb="sb6"]', 'click', function (event) { showRuleForm(); });
    bind('[data-sb="sb7"]', 'click', function (event) { showSilenceForm(); });
    bind('[data-sb="sb8"]', 'click', function (event) { showMaintenanceWindowForm(); });
    bind('[data-sb="sb9"]', 'click', function (event) { IngressSetupModule.reset(); });
    bind('[data-sb="sb10"]', 'click', function (event) { RoutingModule.setView('rules'); });
    bind('[data-sb="sb11"]', 'click', function (event) { runSandboxTest(); });
    bind('[data-sb="sb12"]', 'click', function (event) { loadSandboxSample(); });
    bind('#auditWindowDays', 'change', function (event) { RuleAuditModule.load(); });
    bind('[data-sb="sb13"]', 'click', function (event) { RuleAuditModule.load(); });
    bind('[data-sb="sb14"]', 'click', function (event) { RoutingModule.setView('rules'); });
    bind('#noiseWindowDays', 'change', function (event) { NoiseCenterModule.load(); });
    bind('[data-sb="sb15"]', 'click', function (event) { NoiseCenterModule.load(); });
    bind('[data-sb="sb16"]', 'click', function (event) { ActionCenterModule.load(); });
    bind('[data-sb="sb17"]', 'click', function (event) { KbDraftsModule.load(); });
    bind('#knowledgeGapWindow', 'change', function (event) { ResponseCenterModule.loadKnowledgeGaps(); });
    bind('[data-sb="sb18"]', 'click', function (event) { ResponseCenterModule.loadKnowledgeGaps(); });
    bind('[data-sb="sb19"]', 'click', function (event) { RuntimeSettingsModule.load(); });
    bind('[data-sb="sb20"]', 'click', function (event) { IncidentsModule.closeResolutionModal(); });
    bind('[data-sb="sb21"]', 'click', function (event) { IncidentsModule.closeResolutionModal(); });
    bind('#incidentResolutionDraftBtn', 'click', function (event) { IncidentsModule.saveResolutionDraft(); });
    bind('#incidentResolutionCloseBtn', 'click', function (event) { IncidentsModule.submitResolution(); });
    bind('[data-sb="sb22"]', 'click', function (event) { IncidentsModule.closeRunbookCompletionModal(); });
    bind('#runbookCompletionSubmitBtn', 'click', function (event) { IncidentsModule.submitRunbookCompletion(); });
    bind('[data-sb="sb23"]', 'click', function (event) { clearAuthKeys(); });
    bind('[data-sb="sb24"]', 'click', function (event) { closeAuthModal(); });
    bind('[data-sb="sb25"]', 'click', function (event) { saveAuthKeys(); });
    bind('[data-sb="sb26"]', 'click', function (event) { closeForwardModal(); });
    bind('#confirmForwardBtn', 'click', function (event) { confirmForward(); });
    bind('#ruleFormTargetType', 'change', function (event) { onTargetTypeChange(); });
    bind('[data-sb="sb27"]', 'click', function (event) { closeRuleForm(); });
    bind('[data-sb="sb28"]', 'click', function (event) { saveRule(); });
    bind('#silenceBacktestBtn', 'click', function (event) { backtestSilenceRule(); });
    bind('[data-sb="sb29"]', 'click', function (event) { closeSilenceForm(); });
    bind('[data-sb="sb30"]', 'click', function (event) { saveSilence(); });
    bind('[data-sb="sb31"]', 'click', function (event) { closeMaintenanceWindowForm(); });
    bind('[data-sb="sb32"]', 'click', function (event) { saveMaintenanceWindow(); });
});
