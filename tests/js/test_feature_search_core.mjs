// Node test for the pure feature-search helpers (no browser needed).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, '..', '..', 'static', 'feature_search_core.js'), 'utf8');
const mod = { exports: {} };
(new Function('module', 'exports', src))(mod, mod.exports);
const FSCore = mod.exports;

let fails = 0;
const ok = (name, cond) => { if (!cond) { fails++; console.log('FAIL ' + name); } };

// alias expansion
let t = FSCore.expandTerms('ctx');
ok('ctx expands to context', t.includes('ctx') && t.includes('context'));
t = FSCore.expandTerms('temp');
ok('temp expands to temperature', t.includes('temperature'));
t = FSCore.expandTerms('');
ok('empty query -> no terms', t.length === 0);
t = FSCore.expandTerms('planner');
ok('non-alias query stays itself', t.length === 1 && t[0] === 'planner');

// dedupe: first occurrence wins, others survive
const idx = [
  { panel: 'presets', label: 'Overwrite this preset with the current config', el: 'a' },
  { panel: 'presets', label: 'Error-Rollup', el: 'b' },
  { panel: 'presets', label: 'Overwrite this preset with the current config', el: 'c' },
];
const dd = FSCore.dedupe(idx);
ok('dedupe keeps first, drops dup', dd.length === 2 && dd[0].el === 'a' && dd[1].el === 'b');
ok('dedupe does not mutate input', idx.length === 3);

// combined: expand + dedupe on a label haystack
const hay = (item, terms) => terms.some(x => (item.kw || item.label).toLowerCase().includes(x));
const entry = { panel: 'agents', label: 'Analyst — Context', kw: 'agent analyst context ctx' };
const terms = FSCore.expandTerms('ctx');
ok('ctx matches a Context setting via expansion', hay(entry, terms));

process.exit(fails ? 1 : 0);
