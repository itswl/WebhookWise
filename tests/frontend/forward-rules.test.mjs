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
check('高优先级排前', html.indexOf('P1 到值班') < html.indexOf('所有告警通知'));

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

process.exit(fails ? 1 : 0);
