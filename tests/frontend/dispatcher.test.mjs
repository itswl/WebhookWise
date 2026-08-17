/**
 * Delegated-action resolver harness.
 *
 * Runs the REAL allowlist + resolver source. Exists because const-declared
 * modules create global lexical bindings with no window property, so the
 * resolver's window[name] lookup returned null for every "Module.method"
 * data-act — measured in a real browser, all of them were silently dead.
 * The registry closed that hole; this pins it shut.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.resolve(here, '../../templates/static/js/utils.js'), 'utf8');
const start = src.indexOf('var WW_ACTION_ROOTS');
const end = src.indexOf('function wwParseArgs');
if (start < 0 || end < 0) {
  console.error('FAIL could not slice the resolver out of utils.js');
  process.exit(1);
}

const harness = new Function('window', src.slice(start, end) + `
  // A const module, exactly as the real files declare them: reachable as a
  // bare identifier, absent from window.
  const OverviewModule = { drillToSkip() { return 'drilled'; } };
  wwRegisterActionRoot('OverviewModule', OverviewModule);
  wwRegisterActionRoot('EvilModule', { steal() { return 'stolen'; } });
  return { resolve: wwResolveAction };
`);
const M = harness({});

let failed = 0;
function check(label, ok, extra) {
  if (ok) { console.log('PASS ' + label); } else { failed++; console.error('FAIL ' + label + (extra ? ' — ' + extra : '')); }
}

const drill = M.resolve('OverviewModule.drillToSkip');
check('a registered const module resolves', typeof drill === 'function');
check('and its method actually runs', drill && drill() === 'drilled');
check('an allowlisted but unregistered root resolves to null (not a crash)', M.resolve('AlertsModule.loadAlerts') === null);
check('a name outside the allowlist never registers', M.resolve('EvilModule.steal') === null);
check('a one-part unknown global refuses', M.resolve('stealEverything') === null);
check('garbage input refuses', M.resolve('a.b.c') === null && M.resolve('') === null);

process.exit(failed ? 1 : 0);
