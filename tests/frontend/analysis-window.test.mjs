/**
 * Shared analysis window, against the REAL module.
 *
 * Five pages read one localStorage key; the day-count selects map through it
 * (1 → day, 7 → week, 30 → month) and unmapped values stay local. The rules
 * that matter — a foreign value normalises to unset, a local 90-day pick
 * survives its own refresh but yields to a newer shared choice, a select with
 * no option for the shared window is left alone — are logic the static
 * contracts cannot see, so they are exercised here.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.resolve(here, '../../templates/static/js/analysis-window.js'), 'utf8');

let fails = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok ? '' : `  got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`);
};

function build(storage) {
  return new Function('localStorage', src + '; return wwAnalysisWindow;')(storage);
}

function memoryStorage(initial = {}) {
  const store = { ...initial };
  return {
    store,
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  };
}

function fakeSelect(values, value) {
  const attrs = {};
  const listeners = {};
  return {
    options: values.map((v) => ({ value: String(v) })),
    value: String(value),
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    setAttribute: (n, v) => { attrs[n] = String(v); },
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    fire: (type) => (listeners[type] || []).forEach((fn) => fn()),
    listeners,
  };
}

// ── get / set / persist ────────────────────────────────────────────────
let storage = memoryStorage();
let W = build(storage);
check('未选择时落在今天(决策链原默认)', W.get(), { period: 'day', days: 1 });
check('未选择时 isSet 为 false', W.isSet(), false);
check('set(week) 返回窗口', W.set('week'), { period: 'week', days: 7 });
check('持久化到 ww-analysis-window', storage.store['ww-analysis-window'], 'week');
check('STORAGE_KEY 对外可见', W.STORAGE_KEY, 'ww-analysis-window');
check('get 读回 week', W.get(), { period: 'week', days: 7 });
check('非法 period 被忽略', W.set('fortnight'), null);
check('非法 set 不改变存储', storage.store['ww-analysis-window'], 'week');

// Another page load sees the same choice.
check('新实例读到同一窗口', build(storage).get().period, 'week');

// A foreign / corrupted value normalises to unset, never to a crash.
check('损坏的存储值视为未设置', build(memoryStorage({ 'ww-analysis-window': '{"p":1}' })).isSet(), false);
check('损坏的存储值回落默认', build(memoryStorage({ 'ww-analysis-window': 'zzz' })).get().period, 'day');

// localStorage refused (private mode): the in-page window still holds.
const refusing = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); } };
const P = build(refusing);
check('私密模式下 set 不抛异常', P.set('month'), { period: 'month', days: 30 });
check('私密模式下同页仍读回', P.get().period, 'month');

// ── mapping ────────────────────────────────────────────────────────────
check('1 → day', W.fromDays(1), 'day');
check('"7" → week', W.fromDays('7'), 'week');
check('30 → month', W.fromDays(30), 'month');
check('90 无映射', W.fromDays(90), null);
check('day → 1', W.toDays('day'), 1);
check('month → 30', W.toDays('month'), 30);
check('未知 period → null', W.toDays('year'), null);

// ── selects ────────────────────────────────────────────────────────────
// Nothing chosen yet: a page keeps its own default.
storage = memoryStorage();
W = build(storage);
let sel = fakeSelect([7, 30, 90, 180], 30);
check('未设置时 sync 不动 select', W.syncSelect(sel), null);
check('  → select 仍为 30', sel.value, '30');

// The trace page picks week; the audit select follows on its next load.
W.set('week');
check('sync 应用共享窗口', W.syncSelect(sel), 'week');
check('  → select 变为 7', sel.value, '7');
check('重复 sync 无动作', W.syncSelect(sel), null);

// A local unmapped pick (90) is not persisted, and survives its own refresh.
sel.value = '90';
check('adopt(90) 不持久化', W.adoptSelect(sel), null);
check('  → 存储仍是 week', storage.store['ww-analysis-window'], 'week');
check('  → 刷新后 90 保留', W.syncSelect(sel), null);
check('  → select 仍为 90', sel.value, '90');

// …but a newer shared choice made elsewhere wins.
W.set('month');
check('别处改为 month 后本页跟随', W.syncSelect(sel), 'month');
check('  → select 变为 30', sel.value, '30');

// A mapped pick writes the shared window.
sel.value = '7';
check('adopt(7) 写入共享窗口', W.adoptSelect(sel), 'week');
check('  → 存储为 week', storage.store['ww-analysis-window'], 'week');

// A select with no option for the shared window is left alone (noise/audit have no 1-day).
W.set('day');
check('无对应选项时不改 select', W.syncSelect(sel), null);
check('  → select 仍为 7', sel.value, '7');
check('  → 且不再重试', W.syncSelect(sel), null);

// bindSelect: one change listener, adopting on change.
const bound = fakeSelect([1, 7, 30, 90], 7);
W.bindSelect(bound);
W.bindSelect(bound);
check('bindSelect 只绑定一次', (bound.listeners.change || []).length, 1);
bound.value = '30';
bound.fire('change');
check('change 事件写入共享窗口', storage.store['ww-analysis-window'], 'month');
check('bindSelect(null) 不抛异常', W.bindSelect(null), undefined);
check('syncSelect(null) 不抛异常', W.syncSelect(null), null);

process.exit(fails ? 1 : 0);
