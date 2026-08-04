import fs from 'node:fs';
const src = fs.readFileSync('templates/static/js/command-palette.js', 'utf8');
const store = {};
globalThis.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
globalThis.escapeHtml = s => String(s);
globalThis.t = k => k;                       // untranslated: exercises the slug fallback
globalThis.document = { addEventListener(){}, getElementById: () => null, querySelectorAll: () => [] };
globalThis.navigateTo = () => true;

const M = new Function(src + '; return CommandPalette;')();
let fails = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok ? '' : `  got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`);
};
const slugs = r => r.map(i => i.slug);

check('目的地总数 = 19', M._all.length, 19);
check('空查询列出全部(功能地图)', M._compute('').length, 19);
check('精确名: silence', slugs(M._compute('silence'))[0], 'silences');
check('中文: 静音', slugs(M._compute('静音'))[0], 'silences');
check('中文: 降噪', slugs(M._compute('降噪'))[0], 'noise');
check('子序列: wq → work-queue', slugs(M._compute('wq')).includes('work-queue'), true);
check('同义词: mute → silences', slugs(M._compute('mute'))[0], 'silences');
check('中文: 成本 → cost', slugs(M._compute('成本'))[0], 'cost');
check('无匹配返回空', M._compute('zzzzqqq').length, 0);

// recents float to the top of the empty state
store['ww-palette-recent'] = JSON.stringify(['settings', 'noise']);
check('最近使用置顶', slugs(M._compute('')).slice(0, 2), ['settings', 'noise']);
check('最近使用不重复出现', M._compute('').length, 19);
// a stale slug from an older build must not break the list
store['ww-palette-recent'] = JSON.stringify(['no-such-view']);
check('陈旧的最近记录被忽略', M._compute('').length, 19);
store['ww-palette-recent'] = 'not json';
check('损坏的 localStorage 不崩溃', M._compute('').length, 19);

// Direct record jumps: typing an id is answered by the palette itself
// (before this, reaching alert 245 meant navigate-then-scroll).
const jumps = (q) => M._compute(q).filter((i) => i.jump).map((i) => i.jump.kind + ':' + i.jump.id);
check('纯数字 → 告警+事件跳转', jumps('245'), ['alert:245', 'incident:245']);
check('#号前缀同样识别', jumps('#245'), ['alert:245', 'incident:245']);
check('带词的 id', jumps('alert 12'), ['alert:12', 'incident:12']);
check('说“事件”时只给事件跳转', jumps('事件 7'), ['incident:7']);
check('纯文字查询无跳转项', jumps('cost'), []);
check('跳转项排在目的地之前', M._compute('245')[0].jump.kind, 'alert');

process.exit(fails ? 1 : 0);
