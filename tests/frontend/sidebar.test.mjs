import fs from 'node:fs';
const paletteSrc = fs.readFileSync('templates/static/js/command-palette.js', 'utf8');
const dashSrc = fs.readFileSync('templates/static/js/dashboard.js', 'utf8');

// slice: sidebar functions live between SIDEBAR_COLLAPSED_KEY and renderBreadcrumb
const nav = dashSrc.slice(dashSrc.indexOf("const SIDEBAR_COLLAPSED_KEY"), dashSrc.indexOf('/** The tab highlight is gone'));

const store = {};
globalThis.localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
globalThis.escapeHtml = s => String(s);
globalThis.wwIcon = (n) => `<svg data-icon="${n}"></svg>`;
globalThis.htmlRaw = (m) => ({ __wwHtml: String(m ?? '') });
globalThis.html = (strings, ...vals) => strings.reduce((acc, s, i) => {
  if (i === 0) return s;
  const v = vals[i - 1];
  const piece = (v && v.__wwHtml !== undefined) ? v.__wwHtml
    : (v === null || v === undefined) ? '' : globalThis.escapeHtml(String(v));
  return acc + piece + s;
}, '');
globalThis.t = k => ({'nav.group.overview':'概览','nav.group.analysis':'分析','nav.group.inbox':'收件箱','nav.group.routing':'路由','nav.group.operations':'运维'}[k] || k);
globalThis.navigateTo = () => true;

const elements = {};
function el(id) {
  if (!elements[id]) elements[id] = { id, innerHTML: '', style: {}, classList: { add(){}, remove(){}, toggle(){} }, addEventListener(){}, };
  return elements[id];
}
const items = [];
globalThis.document = {
  getElementById: (id) => (id === 'sidebar' || id === 'sidebarCollapseBtn') ? el(id) : null,
  querySelectorAll: (sel) => sel === '[data-sidebar-slug]' ? items : [],
  querySelector: () => null,
  body: { classList: { toggle: () => true, add(){}, remove(){} }, getAttribute: (n) => n === 'data-app-version' ? '3.6.1' : null },
};
globalThis.CommandPalette = new Function(paletteSrc + '; return CommandPalette;')();
globalThis.currentDestination = 'overview';

const M = new Function(nav + '; return { renderSidebar, updateSidebarActive };')();
M.renderSidebar();
const html = el('sidebar').innerHTML;
let fails = 0;
const check = (label, ok) => { if (!ok) fails++; console.log((ok ? 'PASS' : 'FAIL') + '  ' + label); };

check('渲染了全部 23 项', (html.match(/data-sidebar-slug=/g) || []).length === 23);
check('23 项全部走 sprite 图标', (html.match(/data-icon=/g) || []).length >= 23);
check('5 个分组头', (html.match(/sidebar-group/g) || []).length === 5);
check('分组已翻译', html.includes('收件箱') && html.includes('运维') && html.includes('分析'));
check('incidents 带徽章占位', html.includes('sidebarIncidentsBadge'));
check('折叠按钮存在', html.includes('sidebarCollapseBtn'));

// The five analysis pages share one group (and one time window): all five
// render between the 分析 header and the next header, none in the low-freq fold.
const analysisStart = html.indexOf('>分析<');
const analysisEnd = html.indexOf('sidebar-group', analysisStart + 1);
const analysisHtml = html.slice(analysisStart, analysisEnd);
check('分析分组存在', analysisStart > 0 && analysisEnd > analysisStart);
for (const slug of ['trace', 'cost', 'quality', 'audit', 'noise']) {
  check('分析分组包含 ' + slug, analysisHtml.includes('data-sidebar-slug="' + slug + '"'));
}

// Rarely-used tier: every lowFreq slug still renders (parity is about
// reachability), inside one collapsed <details> after the groups.
check('低频层存在', html.includes('sidebarLowfreq'));
const detailsHtml = html.slice(html.indexOf('<details'));
for (const slug of ['sandbox', 'integrations', 'kb', 'gaps']) {
  check('低频层包含 ' + slug, detailsHtml.includes('data-sidebar-slug="' + slug + '"'));
}
check('audit 随分析组出列，不再在低频层', !detailsHtml.includes('data-sidebar-slug="audit"'));
check('主列表不再平铺低频项', !html.slice(0, html.indexOf('<details')).includes('data-sidebar-slug="sandbox"'));
check('低频计数正确', html.includes('(6)'));
check('版本脚注渲染', html.includes('sidebar-version') && html.includes('v3.6.1'));
// Navigation + drawer dismissal now travel as ONE dispatched action name
// (markup carries no call chain since the CSP burn-down).
check('点击项走派发器', html.includes('data-act="navigateFromSidebar"'));

// active-state sync
const mk = (slug) => ({ _a: false, getAttribute: () => slug, classList: { toggle(cls, on) { if (cls === 'is-active') this._on = on; } } });
items.push(mk('alerts'), mk('silences'), mk('settings'));
M.updateSidebarActive('silences');
check('激活态指向 silences', items[1].classList._on === true && items[0].classList._on === false && items[2].classList._on === false);

// ── Dictionary-not-ready phase: labels degrade to slugs, never raw keys ──
globalThis.t = k => k;                       // identity = nothing translated yet
M.renderSidebar();
const rawHtml = el('sidebar').innerHTML;
check('未就绪时无生键泄漏', !rawHtml.includes('nav.dest.'));
check('未就绪时用 slug 顶替', rawHtml.includes('>investigations<') && rawHtml.includes('>silences<'));
check('分组头去前缀', rawHtml.includes('>inbox<') || rawHtml.includes('>overview<'));

process.exit(fails ? 1 : 0);
