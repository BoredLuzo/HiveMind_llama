// HiveMind UI module: Markdown renderer, link sanitizer & live markdown.
// ── Chat Markdown Renderer ─────────────────────────────────────────────────
// CHAT-MD-FIX (2026-08-27): normal (direct) responses are rendered as markdown
// like LM Studio / llama.cpp UIs: real block elements (p, ul/ol, h1-h3,
// pre>code, blockquote, hr, tables) instead of a span flood with <br>. Important:
// the result lands in a <div> — block elements inside a <span> get broken by
// the browser during innerHTML parsing (adoption agency) and the formatting breaks.
function _escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function _mdSafeHref(url) {
  // XSS (audit 2026-11-03): the URL sits inside a double-quoted href. The
  // text was escaped before this pass, but encode quote/angle chars anyway
  // so a crafted URL cannot break out of the attribute.
  return String(url).replace(/["'<>`\\]/g, function(ch) {
    return encodeURIComponent(ch);
  });
}

function _mdInline(s) {
  // Inline code, bold, italic, markdown links, bare http/https URLs (target=_blank)
  s = s.replace(/`([^`\n]+)`/g, '<code class="coder-inline-code">$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function(_m, label, url) {
    return '<a href="' + _mdSafeHref(url) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
  });
  s = s.replace(/(<code\b[^>]*>[\s\S]*?<\/code>|<a\b[^>]*>[\s\S]*?<\/a>)|(https?:\/\/[^\s<>"')]+)/g, function(_m, skip, url) {
    if (skip) return skip;
    var trail = '';
    var tm = url.match(/([.,;:!?]|&(?:quot|lt|gt|#39);)+$/);
    if (tm) {
      trail = tm[0];
      url = url.slice(0, url.length - trail.length);
    }
    if (!url) return trail;
    return '<a href="' + _mdSafeHref(url) + '" target="_blank" rel="noopener noreferrer">' + url + '</a>' + trail;
  });
  return s;
}

function _mdRender(text) {
  var s = String(text || '');
  // Fenced code blocks -> placeholders (never processed as markdown)
  var blocks = [];
  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, function(_, lang, code) {
    blocks.push('<pre class="coder-code-block' + (lang ? ' lang-' + lang : '') +
                '"><code>' + _escHtml(code.replace(/\n$/, '')) + '</code></pre>');
    return '\u0000B' + (blocks.length - 1) + '\u0000';
  });
  var lines = s.split('\n');
  var out = [];
  var list = null;  // 'ul' | 'ol'
  function closeList() { if (list) { out.push('</' + list + '>'); list = null; } }
  var tableOpen = false;
  function closeTable() { if (tableOpen) { out.push('</table>'); tableOpen = false; } }

  for (var i = 0; i < lines.length; i++) {
    var ln = lines[i];
    var m;

    // Restore code block placeholder (allow optional leading/trailing whitespace)
    m = ln.match(/^\s*\u0000B(\d+)\u0000\s*$/);
    if (m) { closeList(); closeTable(); out.push(blocks[+m[1]]); continue; }

    if (!ln.trim()) { closeList(); closeTable(); continue; }

    // Headings  # .. ###### (clamped to coder-h1..coder-h4)
    m = ln.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if (m) {
      closeList(); closeTable();
      out.push('<div class="coder-h coder-h' + Math.min(m[1].length, 4) + '">' + _mdInline(_escHtml(m[2])) + '</div>');
      continue;
    }

    // Horizontal rule  ---  ***  ___
    m = ln.match(/^\s{0,3}([-*_])\s*\1\s*\1\s*$/);
    if (m) { closeList(); closeTable(); out.push('<hr>'); continue; }

    // Blockquote
    m = ln.match(/^\s{0,3}>\s?(.*)$/);
    if (m) { closeList(); closeTable(); out.push('<blockquote class="coder-bq">' + _mdInline(_escHtml(m[1])) + '</blockquote>'); continue; }

    // Task list  "- [x] foo"
    m = ln.match(/^\s*[-*]\s+\[([ xX])\]\s+(.+)$/);
    if (m) {
      if (list !== 'ul') { closeList(); closeTable(); out.push('<ul class="coder-ul">'); list = 'ul'; }
      var done = (m[1] === 'x' || m[1] === 'X');
      out.push('<li class="coder-task' + (done ? ' done' : '') + '">' + (done ? '\u2611 ' : '\u2610 ') + _mdInline(_escHtml(m[2])) + '</li>');
      continue;
    }

    // Bullet list
    m = ln.match(/^\s*[-*]\s+(.+)$/);
    if (m) {
      if (list !== 'ul') { closeList(); closeTable(); out.push('<ul class="coder-ul">'); list = 'ul'; }
      out.push('<li>' + _mdInline(_escHtml(m[1])) + '</li>');
      continue;
    }

    // Numbered list (preserve explicit value so code blocks / splits don't reset counter)
    m = ln.match(/^\s*(\d+)[.)]\s+(.+)$/);
    if (m) {
      if (list !== 'ol') { closeList(); closeTable(); out.push('<ol class="coder-ol">'); list = 'ol'; }
      out.push('<li value="' + m[1] + '">' + _mdInline(_escHtml(m[2])) + '</li>');
      continue;
    }

    // Table  | a | b |
    if (/^\s*\|.*\|\s*$/.test(ln)) {
      closeList();
      var cells = ln.replace(/^\s*\|\s?|\s?\|\s*$/g, '').split(/\s*\|\s*/);
      var isSep = cells.every(function(c) { return /^:?-{2,}:?$/.test(c); });
      if (isSep) { continue; }  // separator row — header styling via CSS
      if (!tableOpen) {
        out.push('<table class="coder-tbl"><tr>' +
                 cells.map(function(c) { return '<th>' + _mdInline(_escHtml(c)) + '</th>'; }).join('') +
                 '</tr>');
        tableOpen = true;
      } else {
        out.push('<tr>' +
                 cells.map(function(c) { return '<td>' + _mdInline(_escHtml(c)) + '</td>'; }).join('') +
                 '</tr>');
      }
      continue;
    }

    // Indented continuation line inside an open list item
    if (list !== null && /^\s{2,}\S/.test(ln) && out.length && /<\/li>$/.test(out[out.length - 1])) {
      out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, '<br>' + _mdInline(_escHtml(ln.trim())) + '</li>');
      continue;
    }

    // Plain paragraph (per line — chat style, single \n = line break)
    closeList(); closeTable();
    out.push('<p>' + _mdInline(_escHtml(ln)) + '</p>');
  }
  closeList();
  closeTable();
  return out.join('').replace(/\u0000B(\d+)\u0000/g, function(_, idx) {
    return blocks[+idx] || '';
  });
}

function _mdInlinePlan(s) {
  s = _escHtml(s);
  s = s.replace(/`([^`\n]+)`/g, '<code class="coder-inline-code">$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');
  return s;
}

// PLAN-STEP-DISPLAY (2026-09-17): planner step lines are pipe-field contracts
// ("1. file: src/x.js | touch: Foo.bar | decision: add hook | risk: ...") —
// fine for the coder, unreadable as a task bullet. Show "<file>: <intent>"
// (first touch/decision/read-style field), keep the full contract as tooltip.
function _planStepDisplay(stepText) {
  var s = String(stepText || '');
  var m = s.match(/^\s*(?:\d+[.)]\s*)?file:\s*([^|]+?)\s*(?:\|(.*))?$/);
  if (!m) return _mdInlinePlan(s);
  var file = m[1].trim().replace(/[.,;]+$/, '');
  // INTENT-PRIORITY (2026-09-21): known keys first; then the FIRST unknown
  // non-risk segment ("modify: canvas resolution …" — MiniCPM/Qwen planners
  // invent their own verbs). risk is the last resort.
  var intent = '';
  var fallbackIntent = '';
  var keys = ['touch', 'decision', 'modify', 'read', 'fix', 'change', 'action', 'implement'];
  var parts = (m[2] || '').split('|');
  for (var i = 0; i < parts.length; i++) {
    var idx = parts[i].indexOf(':');
    if (idx < 0) continue;
    var k = parts[i].slice(0, idx).trim().toLowerCase();
    var v = parts[i].slice(idx + 1).trim();
    if (!v) continue;
    if (keys.indexOf(k) >= 0) { intent = v; break; }
    if (!fallbackIntent && k !== 'risk') fallbackIntent = v;
  }
  if (!intent) intent = fallbackIntent;
  if (!intent) {
    for (var i = 0; i < parts.length; i++) {
      var idx = parts[i].indexOf(':');
      if (idx < 0) continue;
      if (parts[i].slice(0, idx).trim().toLowerCase() === 'risk') { intent = parts[i].slice(idx + 1).trim(); break; }
    }
  }
  var title = (file && intent) ? (file + ': ' + intent)
            : file ? ('Implement changes to ' + file)
            : s;
  if (title.length > 110) title = title.slice(0, 110) + '\u2026';
  // PLAN-STEP-CLEAN: no inline marker on every row - the file is set as code
  // and the full planner contract becomes the row's tooltip instead.
  var _fmt = _mdInlinePlan(title);
  if (file) {
    var _fi = _fmt.indexOf(_escHtml(file));
    if (_fi === 0) {
      _fmt = '<code class="coder-inline-code">' + _escHtml(file) + '</code>' + _fmt.slice(_escHtml(file).length);
    }
  }
  return _fmt;
}

function _buildPlanMarkdown(text) {
  var out = [];
  var lines = String(text || '').split('\n');
  var buf = [];
  function flushP() {
    if (buf.length) {
      out.push('<div class="pm-p">' + _mdInlinePlan(buf.join(' ')) + '</div>');
      buf = [];
    }
  }
  for (var i = 0; i < lines.length; i++) {
    var ln = lines[i];
    var m;
    m = ln.match(/^\s*(#{1,3})\s+(.+)$/);
    if (m) { flushP(); out.push('<div class="pm-h pm-h' + m[1].length + '">' + _mdInlinePlan(m[2]) + '</div>'); continue; }
    m = ln.match(/^\s*[-*]\s+\[([ xX])\]\s+(.+)$/);
    if (m) {
      flushP();
      var _done = (m[1] === 'x' || m[1] === 'X');
      out.push('<div class="pm-li pm-task' + (_done ? ' pm-task-done' : '') + '"><span class="pm-chk">' + (_done ? '\u2611' : '\u2610') + '</span>' + _mdInlinePlan(m[2]) + '</div>');
      continue;
    }
    m = ln.match(/^\s*\d+[.)]\s+(.+)$/);
    if (m) {
      flushP();
      // the raw planner contract stays available as a tooltip, not as an icon
      var _fullRaw = _escHtml(String(m[1]).slice(0, 400)).replace(/"/g, '&quot;');
      out.push('<div class="pm-li" title="' + _fullRaw + '"><span class="pm-bul">\u2022</span>' + _planStepDisplay(m[1]) + '</div>');
      continue;
    }
    m = ln.match(/^\s*[-*]\s+(.+)$/);
    if (m) { flushP(); out.push('<div class="pm-li"><span class="pm-bul">\u2022</span>' + _mdInlinePlan(m[1]) + '</div>'); continue; }
    if (!ln.trim()) { flushP(); continue; }
    buf.push(ln);
  }
  flushP();
  return out.join('');
}

function _renderPlanMarkdown() {
  // READ ALOUD: the plan first (agentic). Both plan paths end here, and the
  // guard makes sure a re-render never speaks the same plan twice.
  (function () {
    var _runKey = String(S.currentRunId || 'run');
    if (S._spokenPlanKey === _runKey) return;
    setTimeout(function () {
      var _el = document.getElementById('planner-plan-content')
             || document.getElementById('planner-plan-block');
      if (!_el) return;
      var _txt = String(_el.innerText || '').trim();
      if (_txt.length > 40 && S._spokenPlanKey !== _runKey) {
        S._spokenPlanKey = _runKey;
        _readAloud('plan', _txt);
      }
    }, 80);
  })();
  var _el = document.getElementById('planner-plan-content');
  if (!_el || _el._mdRendered) return;
  var _txt = (_el.textContent || '').trim();
  if (!_txt) return;
  var _wrap = document.createElement('div');
  _wrap.className = 'plan-md';
  _wrap.innerHTML = _buildPlanMarkdown(_txt);
  _el.textContent = '';
  _el.appendChild(_wrap);
  _el._mdRendered = true;
}

// Render list_dir output as a colour-coded tree
function _renderDirTree(raw) {
  var _extIcon = function(name) {
    var ext = (name.split('.').pop() || '').toLowerCase();
    var map = {js:'🟨',ts:'🔷',py:'🐍',json:'📋',yml:'⚙️',yaml:'⚙️',
               md:'📄',html:'🌐',css:'🎨',sh:'⚡',bat:'⚡',
               txt:'📄',env:'🔑',lock:'🔒',sql:'🗃',prisma:'🔷',
               dockerfile:'🐳',toml:'⚙️',rs:'🦀'};
    return map[ext] || '📄';
  };
  var lines = raw.split('\n');
  var html = '';
  lines.forEach(function(line) {
    if (!line.trim()) { html += '<br>'; return; }
    var indent = line.length - line.trimStart().length;
    var name = line.trim();
    // CHAT-UI-FIX (2026-08-07): server sends list_dir lines with 📄/📁 prefix.
    // The emoji is the reliable type source — the name heuristic below
    // would otherwise confuse dotless files (Dockerfile, README) with folders.
    var isDir = null;
    if (name.indexOf('📄') === 0 || name.indexOf('📁') === 0) {
      isDir = name.indexOf('📁') === 0;
      name = name.replace(/^[^\s]+\s*/, '');
    }
    if (isDir === null) {
      // Folder detection: ends with / or has no extension
      isDir = name.endsWith('/') || name.endsWith('\\') ||
              (name.indexOf('.') === -1 && name.indexOf(' ') === -1 && name.length < 40);
    }
    var icon = isDir ? '📁' : _extIcon(name);
    var cls  = isDir ? 'tr-dir' : 'tr-file';
    html += '<span class="' + cls + '" style="padding-left:' + (indent * 5) + 'px">'
          + icon + '&thinsp;' + _escHtml(name) + '</span>\n';
  });
  return html;
}

// DIFF-CLEANUP (2026-09-18): drop the --- a/... +++ b/... file header lines
// (the file is already named in the tool chip) and humanize the hunk header:
// "@@ -7,6 +7,7 @@" is diff-internal line counting — "@@ line 7 @@" says
// what a reader actually wants (where in the new file this block lands).
function _cleanDiffBody(body) {
  return body
    .replace(/^---\s+\S+[^\n]*\n\+\+\+\s+\S+[^\n]*\n?/, '')
    .replace(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@[^\n]*$/gm, '@@ line $1 @@');
}

// DIFF-BODY (2026-09-17): colour a unified diff body line by line
// (+ green / - red / @@ hunk header / --- +++ meta). Input is already
// length-capped server-side (tool_result.full <= 4000 chars).
function _renderDiffBody(body) {
  var lines = String(body || '').split('\n');
  var out = [];
  for (var i = 0; i < lines.length; i++) {
    var ln = lines[i];
    var style = '';
    if (/^(\+\+\+|---)\s/.test(ln))      style = 'color:#686868';
    else if (/^@@/.test(ln))             style = 'color:#7aa2f7';
    else if (/^\+/.test(ln))             style = 'color:#22c55e';
    else if (/^-/.test(ln))              style = 'color:#e06060';
    if (ln.length > 400) ln = ln.slice(0, 400) + '\u2026';
    out.push('<span style="' + style + '">' + _escHtml(ln) + '</span>');
  }
  return out.join('\n');
}

// Apply markdown rendering to direct text nodes of a coder bubble body
// CHAT-MD-FIX (2026-08-27): <div> instead of <span> — block elements (p/ul/pre)
// inside a <span> are destroyed by the HTML parser (adoption agency), so the
// normal response was not formatted correctly.
function _applyMdToBody(body) {
  var nodes = Array.from(body.childNodes);
  nodes.forEach(function(node) {
    if (node.nodeType !== Node.TEXT_NODE) return;
    var txt = node.textContent;
    if (!txt.trim()) return;
    var html = _mdRender(txt);
    var div = document.createElement('div');
    div.className = 'coder-md-text';
    div.innerHTML = html;
    body.replaceChild(div, node);
  });
}

// ── Live Markdown Rendering ────────────────────────────────────────────────
// LIVE-MD (2026-08-31): streaming text is NO longer shown as a raw text node,
// but passed through _mdRender per frame during streaming.
// Tables, code blocks, lists etc. therefore appear live — not only at the
// end in doneAgent. The buffer is kept as a string in body._liveRaw, the
// rendered result lives in body._liveEl (<div class="coder-md-text">).
var _liveMdMaxLen   = 100000;   // above this: pause live rendering (plain text)
var _liveMdThrottle = 25000;    // above this: re-render only every 2nd frame
var _liveMdTick     = 0;
// PERF (2026-10-23): _mdRender re-parses the WHOLE accumulated answer and
// rebuilds the bubble DOM on every call — quadratic over one answer at up to
// 60 fps. Streaming paint at ~8 fps is indistinguishable; finalize always
// forces the full render.
var _liveMdMinMs    = 120;
var _liveMdLastAt   = 0;
var _liveMdTrailT   = null;

function _liveMdRawOf(body) {
  if (body._liveRaw) return body._liveRaw;
  if (body._textRun && body._textRun.nodeType === Node.TEXT_NODE) return body._textRun.textContent;
  return '';
}

function _liveMdReset(body) {
  if (_liveMdTrailT) { clearTimeout(_liveMdTrailT); _liveMdTrailT = null; }
  body._liveRaw = null;
  body._liveEl  = null;
  body._textRun = null;
  body._liveDirty = false;
}

function _liveMdAppend(body, text) {
  if (!text) return;
  body._liveRaw = (body._liveRaw || '') + text;
  body._liveDirty = true;
}

function _liveMdRender(body, force) {
  if (!body) return;
  if (force && _liveMdTrailT) { clearTimeout(_liveMdTrailT); _liveMdTrailT = null; }
  var raw = _liveMdRawOf(body);
  if (!raw && !body._liveEl) return;  // nothing to render, no element to create
  if (raw.length > _liveMdMaxLen && !force) {
    // performance: stream very long answers as plain text,
    // the final markdown is done by doneAgent (force).
    if (!body._liveEl) {
      body._liveEl = document.createElement('div');
      body._liveEl.className = 'coder-md-text';
      body.appendChild(body._liveEl);
    }
    body._liveEl.textContent = raw;
    return;
  }
  if (raw.length > _liveMdThrottle && !force && (_liveMdTick++ & 1)) {
    return;  // only render every 2nd frame
  }
  if (!force) {
    var _nowMs = Date.now();
    if (_nowMs - _liveMdLastAt < _liveMdMinMs) {
      // Trailing paint: the flush loop stops once the queue drains, so a
      // skipped tail must render itself when the gate opens (doneAgent's
      // force render only covers run end).
      if (!_liveMdTrailT) {
        _liveMdTrailT = setTimeout(function () {
          _liveMdTrailT = null;
          body._liveDirty = true;
          _liveMdRender(body, true);
        }, _liveMdMinMs);
      }
      return;
    }
    _liveMdLastAt = _nowMs;
  }
  if (!body._liveEl) {
    body._liveEl = document.createElement('div');
    body._liveEl.className = 'coder-md-text';
    body.appendChild(body._liveEl);
  }
  body._liveEl.innerHTML = _mdRender(raw);
}

var _tokenQueue = [];
var _tokenRafId = null;
var _TOKEN_PER_FRAME = 3;  // Tokens pro Frame (~180/s bei 60fps)

// Flush all pending text tokens synchronously — call before tool_call/tool_result
// rendering so mid-sentence text is committed before the tool chip appears.
