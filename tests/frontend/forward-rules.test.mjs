/**
 * Forward rules as compact rows, against the REAL renderer and the REAL
 * Chinese dictionary.
 *
 * The card listed seven conditions per rule, most of them "全部". The row
 * must name only what a rule constrains ("重要性 高,严重 · 仅新告警"),
 * say "匹配全部" when nothing is, keep every operator hook (toggle, drill,
 * test/edit/delete), build the detail lazily, and keep an unfolded row open
 * across the re-render that every toggle causes.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (rel) => fs.readFileSync(path.resolve(here, '../../templates/static/js', rel), 'utf8');
const rulesSrc = read('forward-rules.js');
const utilsSrc = read('utils.js');
const zhSrc = read('i18n.zh.js');

// Real dictionary: a fake window collects the registration.
const fakeWindow = {};
new Function('window', zhSrc)(fakeWindow);
const zh = fakeWindow.__WW_I18N_DICT__.zh;
globalThis.t = (key, params) => {
  let value = zh[key] != null ? zh[key] : key;
  if (params) value = value.replace(/\{(\w+)\}/g, (m, name) => (params[name] != null ? params[name] : m));
  return value;
};

// Real helpers from utils.js.
const utilSlice = (from, to) => utilsSrc.slice(utilsSrc.indexOf(from), utilsSrc.indexOf(to));
const helpers = new Function(
  utilSlice('function escapeHtml(', 'function getAlertIcon(') +
  utilSlice('function wwIcon(', 'function htmlRaw(') +
  utilSlice('function wwFilterPage(', 'function wwResolveAction(') +
  'return { escapeHtml, wwIcon, wwFilterPage, wwPagerHtml };'
)();
Object.assign(globalThis, helpers);
globalThis.formatTime = (v) => 'T(' + v + ')';

// The list state + every render function, sliced out of the module.
const slice = rulesSrc.slice(rulesSrc.indexOf('let forwardRules = [];'), rulesSrc.indexOf('/**\n * Show the rule form'));

const container = { innerHTML: '' };
let detailEl = null;
let rowEl = null;
let expanderEl = null;
globalThis.document = {
  getElementById: (id) => (id === 'forwardRulesList' ? container : (id === 'rule-detail-1' ? detailEl : null)),
};

const M = new Function(slice + `
  return {
    render: (rules) => { forwardRules = rules; renderForwardRules(rules); },
    setStatus: (value) => { ruleStatus = value; rulePage = 1; renderForwardRules(forwardRules); },
    setQuery: (value) => { ruleQuery = value; rulePage = 1; renderForwardRules(forwardRules); },
    toggle: toggleRuleDetail,
    summary: ruleMatchSummary,
    mask: maskRuleUrl,
  };`)();

let fails = 0;
const check = (label, ok, extra) => {
  if (!ok) fails++;
  console.log((ok ? 'PASS' : 'FAIL') + '  ' + label + (ok || !extra ? '' : '   ' + extra));
};
const visibleText = (html) => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

const unconstrained = {
  id: 1, name: '所有告警通知', enabled: true, priority: 10, hit_count: 87,
  match_importance: '', match_duplicate: 'all', match_source: '', match_project: '', match_region: '',
  match_environment: '', match_payload: '', match_event_type: '',
  target_type: 'feishu', target_name: '运维飞书群',
  target_url: 'https://open.feishu.cn/open-apis/bot/v2/hook/0123456789abcdef',
  delivery_status: 'sent', delivery_failure_count_24h: 0, stop_on_match: false,
};
const constrained = {
  id: 2, name: 'P1 到值班', enabled: false, priority: 90, hit_count: 0,
  match_importance: 'high,critical', match_duplicate: 'new', match_source: '', match_project: '',
  match_region: '', match_environment: '', match_payload: '',
  target_type: 'webhook', target_name: '', target_url: 'https://hooks.example.com/services/T000/B000/secret',
  delivery_status: 'exhausted', delivery_failure_count_24h: 3, last_delivery_error: 'HTTP 502',
  last_delivery_at: '2026-09-01T00:00:00Z', stop_on_match: true,
};

M.render([unconstrained, constrained]);
const html = container.innerHTML;

// ── one compact row per rule ───────────────────────────────────────────
check('两条规则渲染为两行', (html.match(/class="rule-row[ "]/g) || []).length === 2);
// 已启用排在已停用之前，即使停用规则的优先级更高（优先级 90 但已停用）：
// 「当前真正在转发什么」不该散落在整张列表里。
check('已启用排在已停用之前', html.indexOf('所有告警通知') < html.indexOf('P1 到值班'));

// One row's markup, from its opening <div class="rule-row…"> to the next row.
const rowOf = (id, source = html) => {
  const anchor = source.indexOf('data-rule-id="' + id + '"');
  const start = source.lastIndexOf('<div class="rule-row', anchor);
  const nextAnchor = source.indexOf('data-rule-id="', anchor + 1);
  const end = nextAnchor < 0 ? undefined : source.lastIndexOf('<div class="rule-row', nextAnchor);
  return source.slice(start, end);
};
const row1 = rowOf(1);
const row2 = rowOf(2);

// ── match summary: only what the rule constrains ───────────────────────
check('无约束规则读作「匹配全部」', row1.includes('匹配全部'));
check('无约束规则不列七个「全部」', !visibleText(row1).includes('来源 全部') && !visibleText(row1).includes('项目'));
check('约束规则读作句子', M.summary(constrained) === '重要性 高,严重 · 仅新告警', M.summary(constrained));
check('行内摘要就是这句', row2.includes('重要性 高,严重 · 仅新告警'));

// ── target: icon + name, masked address in the title ──────────────────
check('目标显示名称', row1.includes('运维飞书群'));
check('目标 URL 只在 title 中且已遮蔽', row1.includes('https://open.feishu.cn/open-apis/…') && !row1.includes('0123456789abcdef'));
check('遮蔽保留 scheme+host+首段', M.mask('https://h.example.com/a/b/c') === 'https://h.example.com/a/…', M.mask('https://h.example.com/a/b/c'));
check('无路径的地址不加省略号', M.mask('https://h.example.com') === 'https://h.example.com');
check('飞书目标用 message 图标', row1.includes('#i-message'));
check('webhook 目标用 link 图标', row2.includes('#i-link'));

// ── priority: bare muted number, the label only on hover ──────────────
check('优先级为无标签数字', /class="rule-row-priority[^"]*"[^>]*>90</.test(row2));
check('「优先级：」不出现在可见文本中', !visibleText(row2).includes('优先级'));
check('优先级标签留在 title', row2.includes('title="优先级：90"'));

// ── badges and state ───────────────────────────────────────────────────
check('命中徽章可下钻', row1.includes('data-drill-rule="所有告警通知"') && row1.includes('近 90 天命中 87 次'));
check('投递正常徽章', row1.includes('投递正常'));
check('禁用规则带「已禁用」徽章与状态类', row2.includes('已禁用') && row2.includes('is-disabled'));
check('投递失败行带状态类与失败徽章', row2.includes('is-unhealthy') && row2.includes('24 小时失败 3 次'), row2.slice(0, 700));
check('禁用且零命中不报僵尸', !row2.includes('未命中任何告警') && row2.includes('近 90 天命中 0 次'));

// ── every hook the page always had ─────────────────────────────────────
check('启用开关保留 data-toggle-rule', row1.includes('data-toggle-rule="1"') && row1.includes(' checked'));
check('开关有可读名称', row1.includes('aria-label="启用规则 所有告警通知"'));
check('测试通道按钮', row1.includes('data-act="testRule" data-args="1"'));
check('编辑按钮', row1.includes('data-act="showRuleForm" data-args="1"'));
check('删除按钮仍为红色', row1.includes('data-act="deleteRule" data-args="1"') && row1.includes('rule-row-delete'));
check('开关与操作区不触发展开(data-stop)', row1.includes('class="switch rule-row-toggle" data-stop') && row1.includes('class="rule-row-actions" data-stop'));
check('无虚线边框', !html.includes('dashed'));
check('无内联十六进制颜色', !/#[0-9a-fA-F]{3,6}\b(?![^<]*<\/use>)/.test(html.replace(/#i-[a-z-]+/g, '')));

// ── detail: lazy, keyboard-reachable, survives a re-render ────────────
check('详情容器初始为空且隐藏', row1.includes('id="rule-detail-1" hidden></div>'));
check('展开按钮声明 aria-expanded=false', row1.includes('aria-expanded="false"') && row1.includes('aria-controls="rule-detail-1"'));

const attrs = { hidden: '' };
expanderEl = { attrs: {}, setAttribute(n, v) { this.attrs[n] = v; } };
rowEl = { classes: new Set(), classList: { toggle(c, on) { on ? rowEl.classes.add(c) : rowEl.classes.delete(c); } }, querySelector: () => expanderEl };
detailEl = {
  innerHTML: '',
  hasAttribute: (n) => n in attrs,
  removeAttribute: (n) => { delete attrs[n]; },
  setAttribute: (n, v) => { attrs[n] = v; },
  closest: () => rowEl,
};
M.toggle(1);
check('首次展开才生成详情', detailEl.innerHTML.includes('匹配条件') && detailEl.innerHTML.includes('推送至'));
check('详情列出全部条件(含「全部」)', detailEl.innerHTML.includes('来源') && detailEl.innerHTML.includes('全部'));
check('展开后可见', !('hidden' in attrs) && rowEl.classes.has('is-open'));
check('aria-expanded 同步为 true', expanderEl.attrs['aria-expanded'] === 'true');
M.toggle(1);
check('再次点击收起', 'hidden' in attrs && !rowEl.classes.has('is-open') && expanderEl.attrs['aria-expanded'] === 'false');
M.toggle(1);

// A toggle re-renders the list; the row the operator had open stays open.
M.render([unconstrained, constrained]);
const reRow1 = rowOf(1, container.innerHTML);
check('重渲染后已展开行保持展开', reRow1.includes('class="rule-row is-open"') && reRow1.includes('匹配条件'), reRow1.slice(0, 300));
check('未展开行仍为惰性', container.innerHTML.includes('id="rule-detail-2" hidden></div>'));

// Constrained-rule detail: stop-on-match and the failure note read.
const d2 = new Function(slice + 'return renderRuleDetail;')()(constrained);
check('详情含「命中即停」', d2.includes('停止匹配后续规则'));
check('详情含最近投递与错误', d2.includes('最近投递：T(2026-09-01T00:00:00Z)') && d2.includes('HTTP 502') && d2.includes('is-danger'));

// ── 排序与状态筛选 ──────────────────────────────────────────────────────
// 停用规则原先按优先级与启用规则交错，操作者要读完整张列表才知道哪些在线。
// 启用优先，组内仍按优先级；筛选器在共享的搜索+分页助手之前生效，两者可叠加。
const live = (id, name, priority) => ({
  ...unconstrained, id, name, priority, enabled: true, hit_count: 1,
});
const dead = (id, name, priority) => ({ ...live(id, name, priority), enabled: false });
const mixed = [dead(11, '停用-高', 99), live(12, '启用-低', 1), dead(13, '停用-低', 2), live(14, '启用-高', 50)];

M.render(mixed);
const order = (source) => ['启用-高', '启用-低', '停用-高', '停用-低']
  .map((name) => source.indexOf(name));
const positions = order(container.innerHTML);
check('启用全部排在停用之前', Math.max(positions[0], positions[1]) < Math.min(positions[2], positions[3]));
check('组内仍按优先级降序', positions[0] < positions[1] && positions[2] < positions[3]);

M.setStatus('enabled');
const onlyLive = container.innerHTML;
check('筛选「已启用」隐藏停用规则', onlyLive.includes('启用-高') && !onlyLive.includes('停用-高'));

M.setStatus('disabled');
const onlyDead = container.innerHTML;
check('筛选「已停用」隐藏启用规则', onlyDead.includes('停用-高') && !onlyDead.includes('启用-高'));

// 状态筛选与文本搜索叠加：两者都要生效，而不是后者覆盖前者。
// 「已停用」+ 搜索「启用」无交集，应落到无匹配态而不是列出启用规则。
M.setQuery('启用');
check('筛选与搜索叠加后无匹配', !container.innerHTML.includes('rule-row') && container.innerHTML.includes(zh['common.noMatches']), container.innerHTML.slice(0, 200));
M.setStatus('enabled');
check('叠加后仍能命中', container.innerHTML.includes('启用-高') && !container.innerHTML.includes('停用-'));
M.setQuery('');
M.setStatus('all');
check('恢复「全部」后四条都在', order(container.innerHTML).every((i) => i >= 0));


// ── 系统事件规则：计数来自投递，而不是决策链路 ──────────────────────────
// 只匹配 incident_created/incident_resolved 的规则从不写决策链路，命中数因此
// 永远是 0，规则页在 44 条健康投递旁边挂着「未命中任何告警」——两个面板对同
// 一条规则给出相反结论。这类规则读的是投递数，措辞也随之改变。
const systemRule = {
  ...unconstrained, id: 29, name: '事件通知 -> 值班群', priority: 500,
  match_event_type: 'incident_created,incident_resolved',
  hit_count: 44, hit_count_source: 'system_event_delivery',
};
M.render([systemRule]);
const sysRow = container.innerHTML;
check('系统事件规则显示投递数', sysRow.includes('近 90 天投递 44 次'));
check('系统事件规则不说「命中」', !sysRow.includes('近 90 天命中'));
check('投递数无下钻(无决策链路可看)', !sysRow.includes('data-drill-rule'));
check('健康投递不再显示僵尸徽章', !sysRow.includes('未命中任何告警'));

M.render([{ ...systemRule, hit_count: 0 }]);
const sysZero = container.innerHTML;
check('零投递的系统事件规则说「未投递」', sysZero.includes('未投递任何事件') && !sysZero.includes('未命中任何告警'));

// 普通告警规则不受影响：仍是可下钻的命中数。
M.render([{ ...unconstrained, hit_count: 87, hit_count_source: 'decision_trace' }]);
check('告警规则仍显示可下钻命中数', container.innerHTML.includes('近 90 天命中 87 次') && container.innerHTML.includes('data-drill-rule'));


process.exit(fails ? 1 : 0);
