/**
 * The pending-proposal decision surface in the Action Center.
 *
 * The envelopes below are the ones production actually returned on 2026-08-18
 * (GET /v1/action-center/proposals -> {data: {items: [...]}}, and the reject
 * response -> {data: {status, result}}), copied verbatim rather than guessed.
 *
 * What must hold:
 *  - an empty queue renders NOTHING, not an empty-state box: this block sits
 *    above the board and must cost no space when there is nothing to decide;
 *  - the reason is escaped (it is prose the proposer wrote);
 *  - approve asks first, reject does not;
 *  - a 502 (approved, execution failed) is reported as neither a success nor a
 *    rejection, because it is a third thing.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.resolve(here, '../../templates/static/js/action-center.js'), 'utf8');

class StubElement {
  constructor() { this.innerHTML = ''; this.style = {}; this.disabled = false; this.attrs = {}; }
  querySelectorAll(selector) {
    // Only the two selectors the module uses are answered; each returns one
    // stub button so the listener wiring is exercised.
    if (selector === '[data-proposal-decision]' || selector === 'button') return this.buttons || [];
    return [];
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
}

const containers = {
  actionCenterSummary: new StubElement(),
  actionCenterList: new StubElement(),
  actionCenterProposals: new StubElement(),
};

const boardEnvelope = { success: true, data: { summary: {}, items: [] } };

const pendingEnvelope = {
  success: true,
  data: {
    items: [
      {
        id: 1,
        action: 'retry_outbox',
        resource_type: null,
        resource_id: 4182,
        batch_size: 50,
        reason: 'outbox 4182 retried 6 times <img src=x onerror=alert(1)>',
        proposed_by: 'hookprobe',
        status: 'pending',
        expires_at: '2026-08-18T11:35:26.779198Z',
        decided_by: null,
        decided_at: null,
        result: {},
        created_at: '2026-08-18T10:35:26.779213Z',
      },
      {
        id: 2,
        action: 'retry_dead_letters',
        resource_type: null,
        resource_id: null,
        reason: 'expired one, must not render',
        proposed_by: 'hookprobe',
        status: 'expired',
        expires_at: '2026-08-18T09:00:00Z',
        result: {},
        created_at: '2026-08-18T08:00:00Z',
      },
    ],
  },
};

let confirmAsked = 0;
let decisionCalls = [];
let toasts = [];
let nextDecisionResponse = null;

globalThis.document = { getElementById: (id) => containers[id] || null };
globalThis.API = {
  authenticatedFetch: async (url, init) => {
    if (url === '/v1/action-center') {
      return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(boardEnvelope)) };
    }
    if (url === '/v1/action-center/proposals?status=pending') {
      return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(pendingEnvelope)) };
    }
    decisionCalls.push({ url, method: (init || {}).method });
    return nextDecisionResponse;
  },
};
globalThis.t = (key) => key;
globalThis.escapeHtml = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
globalThis.wwIcon = () => '';
globalThis.formatTime = (v) => String(v || '-');
globalThis.formatNumber = (n) => String(n);
globalThis.showToast = (msg, kind) => { toasts.push({ msg, kind }); };
globalThis.wwConfirm = async () => { confirmAsked++; return true; };
globalThis.wwRegisterActionRoot = () => {};
globalThis.navigateTo = () => {};

const M = new Function(src + '\nreturn ActionCenterModule;')();

let failed = 0;
const check = (label, ok, extra) => {
  if (ok) { console.log('PASS ' + label); } else { failed++; console.error('FAIL ' + label + (extra ? ' — ' + extra : '')); }
};

// ── A queue with one pending proposal ────────────────────────────────────────
await M.load();
const html = containers.actionCenterProposals.innerHTML;

check('the pending proposal renders', html.includes('retry_outbox'));
check('its action arguments render', html.includes('4182'));
check('the proposer is named', html.includes('hookprobe'));
check('the count badge is the pending count, not the row count', html.includes('>1<'));
check('an expired row is not offered for decision', !html.includes('retry_dead_letters'));
check('the reason is escaped', !html.includes('<img src=x') && html.includes('&lt;img'));
check('approve and reject are both offered', html.includes('approve') && html.includes('reject'));
check('the expiry is shown', html.includes('2026-08-18T11:35:26'));
check('the row carries its id for the decision call', html.includes('data-proposal-id="1"'));

// ── An empty queue costs no space ────────────────────────────────────────────
pendingEnvelope.data.items = [];
await M.load();
check('an empty queue renders nothing at all', containers.actionCenterProposals.innerHTML === '',
  JSON.stringify(containers.actionCenterProposals.innerHTML));

// ── Deciding ────────────────────────────────────────────────────────────────
function proposalRowStub(id) {
  const row = new StubElement();
  row.attrs['data-proposal-id'] = String(id);
  row.buttons = [{ disabled: false }];
  return row;
}
function decisionButton(decision, row) {
  return { getAttribute: (n) => (n === 'data-proposal-decision' ? decision : null), closest: () => row };
}

// The module reaches the row through closest(); drive decideProposal by
// re-rendering with a stub row in place.
const rowStub = proposalRowStub(7);
const approveButton = decisionButton('approve', rowStub);
const rejectButton = decisionButton('reject', rowStub);

// Reject: no confirm, success toast.
confirmAsked = 0; decisionCalls = []; toasts = [];
nextDecisionResponse = {
  ok: true, status: 200,
  json: async () => ({ success: true, data: { id: 7, status: 'rejected', result: { changed: false, reason: 'Rejected by operator' } } }),
};
await M._decideProposal(rejectButton);
check('rejecting asks for no confirmation', confirmAsked === 0);
check('rejecting posts to the reject endpoint', decisionCalls.some((c) => c.url === '/v1/action-center/proposals/7/reject' && c.method === 'POST'));
check('rejecting reports itself as a rejection', toasts.some((x) => x.msg.includes('rejected') && x.kind === 'success'));

// Approve: confirms first.
confirmAsked = 0; decisionCalls = []; toasts = [];
nextDecisionResponse = {
  ok: true, status: 200,
  json: async () => ({ success: true, data: { id: 7, status: 'approved', result: { changed: true } } }),
};
await M._decideProposal(approveButton);
check('approving asks first', confirmAsked === 1);
check('approving posts to the approve endpoint', decisionCalls.some((c) => c.url === '/v1/action-center/proposals/7/approve'));
check('approving reports success', toasts.some((x) => x.msg.includes('approved') && x.kind === 'success'));

// Approved but changed nothing -> a warning, not a success.
confirmAsked = 0; toasts = [];
nextDecisionResponse = {
  ok: true, status: 200,
  json: async () => ({ success: true, data: { id: 7, status: 'approved', result: { changed: false } } }),
};
await M._decideProposal(approveButton);
check('an approval that changed nothing warns', toasts.some((x) => x.msg.includes('ranNothing') && x.kind === 'warning'));

// 502 -> approved, execution failed. Neither success nor rejection.
confirmAsked = 0; toasts = [];
nextDecisionResponse = {
  ok: false, status: 502,
  json: async () => ({ success: false, error: 'RuntimeError: outbox is gone' }),
};
await M._decideProposal(approveButton);
check('a failed execution is reported as its own outcome',
  toasts.some((x) => x.msg.includes('executionFailed') && x.msg.includes('outbox is gone') && x.kind === 'error'));
check('a failed execution is never toasted as success',
  !toasts.some((x) => x.kind === 'success'));

process.exit(failed ? 1 : 0);
