// Extract just the navigation layer from dashboard.js and exercise it against
// stub DOM/history so the routing contract is proven, not assumed.
import fs from 'node:fs';
const src = fs.readFileSync('templates/static/js/dashboard.js', 'utf8');
const start = src.indexOf('function setSubView(');
const end = src.indexOf('/**\n * Initialize the Dashboard');
const nav = src.slice(start, end);

const calls = [];
globalThis.console = console;
let hash = '';
globalThis.window = {
  get location() { return { get hash() { return hash; }, set hash(v) { hash = v; } }; },
  history: { replaceState: (_a, _b, url) => { hash = url; } },
};
globalThis.switchMainTab = (t) => calls.push('tab:' + t);
globalThis.DecisionTraceModule = { setView: (v) => { calls.push('dt:' + v); recordDestination(v); } };
globalThis.RoutingModule = { setView: (v) => { calls.push('rt:' + v); recordDestination(v); } };
globalThis.setInboxView = (v) => { calls.push('ib:' + v); recordDestination(v); };
globalThis.setOperationsView = (v) => { calls.push('op:' + v); recordDestination(v); };

const fn = new Function(nav + `
  return { navigateTo, applyHashRoute, recordDestination, takePendingFocus,
           DESTINATIONS, get currentDestination(){return currentDestination;} };`);
globalThis.document = { getElementById: () => null, querySelectorAll: () => [], querySelector: () => null };
const M = fn();
globalThis.recordDestination = M.recordDestination;

let fails = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok ? '' : `\n        got ${JSON.stringify(got)}\n       want ${JSON.stringify(want)}`}`);
};

check('目的地总数', Object.keys(M.DESTINATIONS).length, 22);

calls.length = 0; M.navigateTo('incidents');
check('navigateTo(incidents) 切标签+子视图', calls, ['tab:alerts', 'ib:incidents']);
check('  → URL', hash, '#/incidents');

calls.length = 0; M.navigateTo('settings');
check('navigateTo(settings)', calls, ['tab:operations', 'op:settings']);
check('  → URL', hash, '#/settings');

calls.length = 0; M.navigateTo('sandbox');
check('sandbox 可达(此前导航条空白)', calls, ['tab:routing', 'rt:sandbox']);
check('  → URL', hash, '#/sandbox');

calls.length = 0; M.navigateTo('overview');
check('overview(action-center 曾无此分支)', calls, ['tab:decision-trace', 'dt:overview']);

// paste a link
hash = '#/quality'; calls.length = 0; M.applyHashRoute();
check('粘贴链接直达', calls, ['tab:routing', 'rt:quality']);

// focus id survives the round trip
calls.length = 0; M.navigateTo('alerts', { focus: '42' });
check('带 focus 的 URL', hash, '#/alerts/42');

hash = '#/incidents/7'; calls.length = 0; M.applyHashRoute();
check('从 URL 解析 focus', M.takePendingFocus(), '7');

hash = '#/nope'; calls.length = 0; M.applyHashRoute();
check('未知路由回落默认', calls, ['tab:decision-trace', 'dt:overview']);

hash = '#/kb'; M.applyHashRoute(); calls.length = 0; M.applyHashRoute();
check('重复应用同一路由不重复拉取', calls, []);

// Cold load with #/overview on a virgin instance — the pre-claimed-destination
// regression: seeding currentDestination with DEFAULT_DESTINATION made
// applyHashRoute's "already here" guard swallow the boot navigation, so the
// landing URL everyone actually opens rendered a permanent spinner.
hash = '#/overview'; calls.length = 0;
const M2 = fn();
globalThis.recordDestination = M2.recordDestination;
M2.applyHashRoute();
check('冷启动 #/overview 真正进入(此前永久转圈)', calls, ['tab:decision-trace', 'dt:overview']);
check('  → 目的地由进入挣得,而非预置', M2.currentDestination, 'overview');

process.exit(fails ? 1 : 0);
