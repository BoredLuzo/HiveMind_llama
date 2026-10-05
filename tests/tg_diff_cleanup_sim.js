// Diff-body cleanup for the DIFFSTAT result block (2026-09-18):
// file-header lines dropped, hunk headers humanized. Extracts the REAL
// _cleanDiffBody from static/app.js — no regex copy drift.
// Run: node tests/tg_diff_cleanup_sim.js   (exit 1 on failure)
var fs = require('fs');
var src = fs.readFileSync(__dirname + '/../static/app.js', 'utf8');
var m = src.match(/function _cleanDiffBody[\s\S]*?\n}/);
if (!m) { console.log('  FAIL  _cleanDiffBody not found in static/app.js'); process.exit(1); }
eval(m[0]);

var failed = 0;
function check(name, cond, extra) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (cond ? '' : '  ' + String(extra)));
  if (!cond) failed++;
}

function cleanDiff(body) {
  return _cleanDiffBody(body.trim());
}

// exact sample from the user's screenshot
var sample = [
  '--- a/app.js',
  '+++ b/app.js',
  '@@ -7,6 +7,7 @@',
  '     }',
  ' ',
  '     async init() {',
  '+        await this.fetchAndRenderReadme();',
  '         await this.fetchRepoData();',
  '         this.setupEventListeners();',
  '         this.renderReadme();',
].join('\n');

var out = cleanDiff(sample);
check('file header lines removed', out.indexOf('--- a/app.js') === -1 && out.indexOf('+++ b/app.js') === -1, out);
check('hunk header humanized', out.indexOf('@@ line 7 @@') !== -1, out);
check('context lines untouched', out.indexOf('     async init() {') !== -1, out);
check('added line untouched', out.indexOf('+        await this.fetchAndRenderReadme();') !== -1, out);
check('no diff-count residue', !/@@ -\d/.test(out), out);

// multi-hunk diff: every @@ gets its target line
var multi = [
  '--- a/x.py',
  '+++ b/x.py',
  '@@ -12,4 +12,5 @@ def a():',
  '+one()',
  ' two()',
  '@@ -40,3 +41,4 @@',
  '+last()',
].join('\n');
var out2 = cleanDiff(multi);
check('multi-hunk: first header', out2.indexOf('@@ line 12 @@') !== -1, out2);
check('multi-hunk: second header', out2.indexOf('@@ line 41 @@') !== -1, out2);

// hunk header with trailing context text (git puts the enclosing function)
var ctx = '--- a/f.js\n+++ b/f.js\n@@ -3,4 +3,5 @@ async init() {\n+x();\n';
var out3 = cleanDiff(ctx);
check('trailing context in @@ stripped', out3.indexOf('@@ line 3 @@\n+x();') === 0, JSON.stringify(out3));

process.exit(failed ? 1 : 0);
