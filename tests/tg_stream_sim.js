// Isolated exercise of the tool-gen stream card helpers from static/app.js.
// Run: node tests/tg_stream_sim.js   (exit 1 on failure)
var fs = require('fs');
var src = fs.readFileSync(__dirname + '/../static/app.js', 'utf8');
var m1 = src.match(/function _tgUnescape[\s\S]*?\n}/)[0];
var m2 = src.match(/function _tgExtractField[\s\S]*?\n}/)[0];
eval(m1 + '\n' + m2);

var failed = 0;
function check(name, cond, extra) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (cond ? '' : '  ' + String(extra)));
  if (!cond) failed++;
}

// 1. write_file: fragments of 7 chars, final extraction must equal the real value
var args = JSON.stringify({ path: 'src/app.js', content: 'line1\nline"2" \\ end\nc\xc3\xbcnicode: \u00e9' });
var buf = '';
for (var i = 0; i < args.length; i += 7) buf += args.slice(i, i + 7);
var want = JSON.parse(args).content;
var got = _tgExtractField(args, 'content');
check('write content roundtrip', got === want, JSON.stringify(got));
var pm = args.match(/"path"\s*:\s*"((?:[^"\\]|\\.)*)"/);
check('path extraction', pm && _tgUnescape(pm[1]) === 'src/app.js', pm && pm[1]);

// 2. stream ends mid \u-sequence -> tail dropped, no crash
var t1 = _tgExtractField('"content": "abc\\u00e', 'content');
check('partial \\u tail tolerated', t1 === 'abc', JSON.stringify(t1));

// 3. complete \n at tail must survive
var t2 = _tgExtractField('"content": "end\\n', 'content');
check('complete \\n at tail survives', t2 === 'end\n', JSON.stringify(t2));

// 4. edit_file: old_text closed, new_text still streaming
var ef = '{"path": "a.js", "old_text": "OLD", "new_text": "NEW1\\nNEW"';
var o = _tgExtractField(ef, 'old_text');
var n = _tgExtractField(ef, 'new_text');
check('edit old_text closed', o === 'OLD', JSON.stringify(o));
check('edit new_text streaming', n === 'NEW1\nNEW', JSON.stringify(n));

// 5. escaped quotes inside a still-open string
var t3 = _tgExtractField('{"content": "say \\"hi\\" the', 'content');
check('escaped quotes mid-stream', t3 === 'say "hi" the', JSON.stringify(t3));

// 6. field not yet started -> null (render shows placeholder)
var t4 = _tgExtractField('{"path": "x.js", ', 'content');
check('missing field -> null', t4 === null, JSON.stringify(t4));

// 7. windows path with escaped backslashes
var wp = JSON.stringify({ path: 'C:\\temp\\x.py', content: 'x = 1\n' });
var pwm = wp.match(/"path"\s*:\s*"((?:[^"\\]|\\.)*)"/);
check('windows path unescape', _tgUnescape(pwm[1]) === 'C:\\temp\\x.py', pwm[1]);

process.exit(failed ? 1 : 0);
