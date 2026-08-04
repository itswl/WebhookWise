import fs from 'node:fs';
const src = fs.readFileSync('templates/static/js/dashboard.js', 'utf8');
const nav = src.slice(src.indexOf('function setSubView('), src.indexOf('/**\n * Initialize the Dashboard'));
let hash = '', warned = [];
globalThis.console = { warn: (...a) => warned.push(a.join(' ')), log: console.log };
globalThis.window = {
  get location() { return { get hash() { return hash; }, set hash(v) { hash = v; } }; },
  history: { replaceState: (_a, _b, u) => { hash = u; } },
  // RoutingModule deliberately absent: simulate one module failing to parse.
};
globalThis.switchMainTab = () => {};
globalThis.setInboxView = (v) => recordDestination(v);
globalThis.setOperationsView = (v) => recordDestination(v);
globalThis.document = { getElementById: () => null, querySelectorAll: () => [], querySelector: () => null };
const M = new Function(nav + 'return {navigateTo, recordDestination};')();
globalThis.recordDestination = M.recordDestination;

let ok = true;
try {
  M.navigateTo('rules');           // module missing
  console.log('PASS  模块缺失时不抛异常');
} catch (e) { ok = false; console.log('FAIL  抛了异常:', e.message); }
console.log(warned.length ? 'PASS  记录了告警: ' + warned[0] : 'FAIL  静默失败');
try { M.navigateTo('incidents'); console.log('PASS  其余目的地仍可用'); }
catch (e) { ok = false; console.log('FAIL  连带崩溃:', e.message); }
process.exit(ok && warned.length ? 0 : 1);
