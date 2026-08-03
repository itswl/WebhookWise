// Model the REAL three-way interplay that caused the two-click bug:
// navigateTo → switchMainTab (tab default loader) → enter() → setView(report).
import fs from 'node:fs';
const src = fs.readFileSync('templates/static/js/dashboard.js', 'utf8');
const navSlice = src.slice(src.indexOf('// Module lookups stay late-bound'),
                           src.indexOf('/**\n * Initialize the Dashboard'));
const smStart = src.indexOf('function switchMainTab(');
const smEnd = src.indexOf('\nfunction ', smStart + 10);
const switchSlice = src.slice(smStart, smEnd);

let hash = '';
const events = [];
const winStub = {
  get location() { return { get hash() { return hash; }, set hash(v) { hash = v; } }; },
  history: { replaceState: (_a, _b, u) => { hash = u; } },
  scrollTo: () => {},
};
const harness = new Function('window', 'document', 'console', 'events',
  'setInboxView', 'setOperationsView', `
  let currentTab = 'alerts';
  // A faithful DecisionTraceModule stub: load() re-renders the CURRENT view
  // (no reporting), setView() switches and reports — mirroring the real file.
  var currentView = 'overview';
  var DecisionTraceModule = {
    load() { events.push('dt.load:' + currentView); },
    setView(v) {
      currentView = v;
      events.push('dt.setView:' + v);
      recordDestination(v);
    },
  };
  var RoutingModule = { setView(v) { events.push('rt:' + v); recordDestination(v); } };
  ${navSlice}
  ${switchSlice}
  return { navigateTo, get hash() { return window.location.hash; } };
`);
const M = harness(winStub,
  { hidden: false, getElementById: () => null, querySelectorAll: () => [], body: { classList: { toggle(){}, add(){}, remove(){} } } },
  { warn: () => {}, log: () => {} }, events,
  (v) => { events.push('ib:' + v); }, (v) => { events.push('op:' + v); });

let fails = 0;
const check = (label, ok, extra) => { if (!ok) fails++; console.log((ok ? 'PASS' : 'FAIL') + '  ' + label + (ok ? '' : '   ' + extra)); };

// THE bug: from another tab, ONE click on a dt destination must move the URL.
events.length = 0; M.navigateTo('trace');
check('一次点击后 URL 即为 #/trace', hash === '#/trace', 'hash=' + hash);
check('旧子视图未被预加载(无 dt.load)', !events.some(e => e.startsWith('dt.load')), events.join(','));
check('setView 恰好一次', events.filter(e => e.startsWith('dt.setView')).length === 1, events.join(','));

events.length = 0; M.navigateTo('cost');
check('同标签内切换一次到位', hash === '#/cost', 'hash=' + hash);
check('无多余加载', events.join(',') === 'dt.setView:cost', events.join(','));

events.length = 0; M.navigateTo('overview');
check('overview 一次到位', hash === '#/overview', 'hash=' + hash);
process.exit(fails ? 1 : 0);
