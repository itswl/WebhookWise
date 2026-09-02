/**
 * The noise centre's per-kind copy, against the real source.
 *
 * Both functions under test used to end in a fallback that quietly absorbed an
 * unknown kind: suggestionCopy returned the THRESHOLD copy for anything it did
 * not recognise, and actionLabel was a ternary that called everything that was
 * not a duplicate filter a silence. A new suggestion kind therefore renders —
 * with the wrong words — and no assertion anywhere notices.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');
const src = fs.readFileSync(path.join(root, 'templates/static/js/noise-center.js'), 'utf8');

const slice = src.slice(src.indexOf('function suggestionCopy('), src.indexOf('function renderSummary('))
  + src.slice(src.indexOf('    // One label per applied action type.'), src.indexOf('function renderActions('));

// t() echoes its key so a missing branch is visible rather than plausible.
const run = new Function('t', slice + 'return { suggestionCopy, actionLabel };')((key) => key);

let failed = 0;
const check = (label, ok, extra) => {
  if (ok) { console.log('PASS ' + label); } else { failed++; console.log('FAIL ' + label + (extra ? ' — ' + extra : '')); }
};

const digest = run.suggestionCopy({ kind: 'digest', scope: { rule_name: 'Example deposit threshold' } });
check('a digest suggestion gets its own copy', digest.title === 'noise.suggestion.digest.title', digest.title);
check('and its own reason', digest.reason === 'noise.suggestion.digest.reason', digest.reason);

const duplicate = run.suggestionCopy({ kind: 'duplicate_filter', scope: {} });
check('the duplicate filter is unchanged', duplicate.title === 'noise.suggestion.duplicate.title', duplicate.title);
const silence = run.suggestionCopy({ kind: 'temporary_silence', scope: {} });
check('the trial silence is unchanged', silence.title === 'noise.suggestion.silence.title', silence.title);

check('the history panel names a digest', run.actionLabel({ action_type: 'digest' }) === 'noise.action.digest');
check('and a duplicate filter', run.actionLabel({ action_type: 'duplicate_filter' }) === 'noise.action.duplicate');
check('and a silence', run.actionLabel({ action_type: 'temporary_silence' }) === 'noise.action.silence');
check('an unknown type is generic, not mislabelled',
  run.actionLabel({ action_type: 'invented_later' }) === 'noise.action.unknown');

// Copy that only exists in one language is a page half of the operators cannot read.
const keys = ['noise.suggestion.digest.title', 'noise.suggestion.digest.reason',
  'noise.action.digest', 'noise.action.unknown'];
for (const language of ['en', 'zh']) {
  const dict = fs.readFileSync(path.join(root, `templates/static/js/i18n.${language}.js`), 'utf8');
  for (const key of keys) {
    check(`${key} exists in ${language}`, dict.includes(`'${key}':`));
  }
}

process.exit(failed ? 1 : 0);
