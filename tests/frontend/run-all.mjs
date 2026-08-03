/**
 * Headless dashboard-behaviour suite.
 *
 * These harnesses run the REAL dashboard source (sliced modules, faithful
 * quote/binding semantics) against stub DOMs. They exist because three
 * shipped regressions — const modules invisible to window[], the two-click
 * navigation bug, raw i18n keys on cold load — were each invisible to
 * python-side static contracts and to node --check. Runs from any cwd.
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');
process.chdir(root);

let failed = 0;

// 1. Every dashboard module must parse.
for (const name of fs.readdirSync('templates/static/js').filter((n) => n.endsWith('.js'))) {
  try {
    execFileSync('node', ['--check', path.join('templates/static/js', name)], { stdio: 'pipe' });
  } catch (error) {
    failed++;
    console.error(`SYNTAX ${name}\n${error.stderr}`);
  }
}

// 2. Behaviour harnesses; each exits non-zero on any failing assertion.
for (const test of fs.readdirSync(here).filter((n) => n.endsWith('.test.mjs')).sort()) {
  try {
    const out = execFileSync('node', [path.join(here, test)], { stdio: 'pipe' });
    const lines = String(out).trim().split('\n');
    console.log(`ok ${test} (${lines.length} checks)`);
  } catch (error) {
    failed++;
    console.error(`FAIL ${test}\n${error.stdout}${error.stderr}`);
  }
}

if (failed) {
  console.error(`frontend suite: ${failed} failure(s)`);
  process.exit(1);
}
console.log('frontend suite: green');
