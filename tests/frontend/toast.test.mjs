/**
 * showToast classification harness.
 *
 * Runs the REAL showToast source against a stub DOM. Exists because an emoji
 * purge once rewrote the keyword guards to includes('') — always true — so
 * every toast rendered as a green success and lost its first two characters,
 * and no test noticed: the static contracts cannot see classification logic.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.resolve(here, '../../templates/static/js/utils.js'), 'utf8');
const start = src.indexOf('function showToast(');
const end = src.indexOf('function _wwDialog(');
if (start < 0 || end < 0) {
  console.error('FAIL could not slice showToast out of utils.js');
  process.exit(1);
}
const slice = src.slice(start, end);

class StubElement {
  constructor() {
    this.children = [];
    this.className = '';
    this.textContent = '';
    this.attrs = {};
    this.classes = new Set();
    this.classList = {
      add: (c) => this.classes.add(c),
      remove: (c) => this.classes.delete(c),
      contains: (c) => this.classes.has(c),
    };
  }
  setAttribute(name, value) { this.attrs[name] = value; }
  appendChild(child) { this.children.push(child); }
  remove() {}
}

let container = null;
globalThis.document = {
  getElementById: () => container,
  createElement: () => new StubElement(),
  body: { appendChild: (el) => { container = el; } },
};
// Toasts must classify without waiting on timers; neutralize auto-dismiss.
const showToast = new Function('document', 'setTimeout', slice + '\nreturn showToast;')(
  globalThis.document,
  () => 0
);

let failed = 0;
function check(label, actual, expected) {
  if (actual === expected) {
    console.log('PASS ' + label);
  } else {
    failed++;
    console.error('FAIL ' + label + ' — expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual));
  }
}
function lastToast() { return container.children[container.children.length - 1]; }

showToast('boom', 'error');
check('explicit error type is honoured even without keywords', lastToast().className, 'toast toast--error');
check('error toasts interrupt for assistive tech', lastToast().attrs.role, 'alert');

showToast('Rule saved', 'success');
check('explicit success type is honoured', lastToast().className, 'toast toast--success');
check('the message is never truncated', lastToast().textContent, 'Rule saved');

showToast('操作已成功保存');
check('untyped success keyword upgrades from info', lastToast().className, 'toast toast--success');
check('Chinese text survives intact', lastToast().textContent, '操作已成功保存');

showToast('delivery failed: 502');
check('untyped failure keyword upgrades from info', lastToast().className, 'toast toast--error');

showToast('conflict detected');
check('untyped warning keyword upgrades from info', lastToast().className, 'toast toast--warning');

showToast('plain note');
check('plain untyped messages stay info', lastToast().className, 'toast toast--info');
check('info toasts are polite status', lastToast().attrs.role, 'status');

showToast('operation failed midway', 'warning');
check('an explicit type is never overridden by keywords', lastToast().className, 'toast toast--warning');

showToast('whatever', 'bogus-type');
check('unknown types coerce to info', lastToast().className, 'toast toast--info');

process.exit(failed ? 1 : 0);
