/**
 * Delivery-queue response parsing, against the REAL endpoint envelopes.
 *
 * Shipped bug: /v1/outbox answers {data: {items, total, next_cursor,
 * has_more}} — data is an OBJECT — and the module concat'd it as a row,
 * rendering one NaN row in production. Dead letters answer a {page,
 * page_size, total} pagination with NO has_more, so the next-page button
 * never appeared. Both envelopes are pinned here verbatim.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.resolve(here, '../../templates/static/js/delivery-queue.js'), 'utf8');

class StubElement {
  constructor() { this.innerHTML = ''; this.style = {}; this.value = ''; }
  querySelectorAll() { return []; }
}
const containers = { deliveryOutboxList: new StubElement(), deliveryDeadLetters: new StubElement() };

const outboxEnvelope = {
  success: true,
  data: {
    items: [
      {
        id: 4181, webhook_event_id: 902, rule_name: '所有告警通知', target_type: 'feishu',
        status: 'sent', attempts: 1, max_attempts: 5, last_error: '',
        last_attempt_at: '2026-08-17T04:55:00+00:00', created_at: '2026-08-17T04:54:58+00:00',
      },
      {
        id: 4180, webhook_event_id: 901, rule_name: '所有告警通知', target_type: 'feishu',
        status: 'exhausted', attempts: 5, max_attempts: 5, last_error: 'relay http 502',
        last_attempt_at: '2026-08-17T04:40:00+00:00', created_at: '2026-08-17T04:20:00+00:00',
      },
    ],
    page: 1, page_size: 50, total: 128, total_pages: 3, next_cursor: 4180, has_more: true,
  },
};
const deadLetterEnvelope = {
  success: true,
  data: [
    { id: 77, source: 'grafana', failure_reason: 'parse failed', retry_count: 3, created_at: '2026-08-17T02:00:00+00:00' },
  ],
  pagination: { page: 1, page_size: 1, total: 3 },
};

globalThis.document = { getElementById: (id) => containers[id] || null };
globalThis.API = {
  getOutbox: async () => JSON.parse(JSON.stringify(outboxEnvelope)),
  getDeadLetters: async () => JSON.parse(JSON.stringify(deadLetterEnvelope)),
};
globalThis.t = (key, params, missing) => {
  // Keys resolve to themselves here; parameter VALUES are appended so
  // assertions can see interpolated numbers without loading the dictionaries.
  let value = String(missing != null ? missing : key);
  if (params) value += ' ' + Object.values(params).join(' ');
  return value;
};
globalThis.escapeHtml = (s) => String(s ?? '');
globalThis.wwIcon = () => '';
globalThis.formatNumber = (n) => String(n);
globalThis.formatTime = (v) => String(v || '-');
globalThis.showToast = () => {};
globalThis.wwConfirm = async () => true;
globalThis.wwRegisterActionRoot = () => {};

const M = new Function(src + '\nreturn DeliveryQueueModule;')();

let failed = 0;
const check = (label, ok, extra) => {
  if (ok) { console.log('PASS ' + label); } else { failed++; console.error('FAIL ' + label + (extra ? ' — ' + extra : '')); }
};

await M.reloadOutbox();
const outboxHtml = containers.deliveryOutboxList.innerHTML;
check('outbox rows come from data.items', outboxHtml.includes('4181') && outboxHtml.includes('4180'));
check('no NaN row is ever rendered', !outboxHtml.includes('NaN'));
check('the first-page total is shown', outboxHtml.includes('128'));
check('has_more inside data arms load-more', outboxHtml.includes('loadMoreOutbox'));
check('an exhausted row offers retry', outboxHtml.includes('retryOutboxRow'));

await M.deadLetterPage(1);
const dlHtml = containers.deliveryDeadLetters.innerHTML;
check('dead letters render from the data array', dlHtml.includes('grafana') && dlHtml.includes('parse failed'));
check('next-page derives from total (no has_more field exists)', dlHtml.includes('deadLetterPage'));

process.exit(failed ? 1 : 0);
