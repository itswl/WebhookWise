/**
 * The handoff copy button, against the real sources.
 *
 * Shipped bug: copyHandoff handed a STRING to copyToClipboard, which expects
 * the code-block BUTTON element and walks .closest() on it — a silent
 * TypeError, so the button did nothing. This runs the real copyHandoff and
 * the real wwCopyText with a stubbed clipboard.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const utils = fs.readFileSync(path.resolve(here, '../../templates/static/js/utils.js'), 'utf8');
const handoff = fs.readFileSync(path.resolve(here, '../../templates/static/js/handoff.js'), 'utf8');

const copySlice = utils.slice(utils.indexOf('function wwCopyText('), utils.indexOf('function copyToClipboard('));
const handoffSlice = handoff.slice(handoff.indexOf('function copyHandoff('), handoff.indexOf('const HandoffModule'));

const written = [];
const toasts = [];
// Node ships a read-only globalThis.navigator; inject ours as a parameter.
let clipboardStub = { clipboard: { writeText: (text) => { written.push(text); return Promise.resolve(); } } };
globalThis.showToast = (message, type) => toasts.push(type + ':' + message);
globalThis.t = (key) => key;
globalThis.document = { getElementById: () => ({ textContent: '## On-call Handoff — Last 8h\n...' }) };

const build = (nav) => new Function('navigator', 'console',
  copySlice + handoffSlice + 'return { copyHandoff, wwCopyText };')(nav, { error: () => {}, log: () => {} });
let run = build(clipboardStub);

let failed = 0;
const check = (label, ok, extra) => {
  if (ok) { console.log('PASS ' + label); } else { failed++; console.log('FAIL ' + label + (extra ? ' — ' + extra : '')); }
};

run.copyHandoff();
await new Promise((resolve) => setTimeout(resolve, 0));
check('the brief text reaches the clipboard', written.length === 1 && written[0].startsWith('## On-call Handoff'));
check('success is announced, not silent', toasts.some((entry) => entry.startsWith('success:')));

// Clipboard unavailable (http, old browser): a readable error, not a throw.
run = build({});
run.wwCopyText('x');
check('missing clipboard API degrades to an error toast', toasts.some((entry) => entry.startsWith('error:')));

process.exit(failed ? 1 : 0);
