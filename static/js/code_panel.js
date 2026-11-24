// HiveMind UI module: Tool-Gen live streaming, Right Code Panel, Diff viewer & Git integration.
// ── TOOL-GEN-STREAM (2026-09-17, panel-stream rework 2026-09-18) ────────────
// Live-streaming of write-family tool call arguments. The chat shows only a
// COMPACT chip (tool + target path + char counter, gone once the real
// tool_call chip arrives) — the generated code itself streams into the
// RIGHT code panel (tab per file, plain render while streaming, throttled).
// edit_file streams its new_text; writes stream the full content.
var _tgCards = {};
var _tgPanelLastRender = 0;
var _TG_PANEL_RENDER_MS = 120;
var _TG_PENDING_KEY = '\u0000pending-write';   // panel tab key until the path parses
var _tgPanelRafId = null;
var _tgPanelRafIsTimer = false;
var _tgPanelDirty = false;
var _tgPendingCards = new Set();
// Stream-follow state (2026-09-21): which file the panel auto-follows, the
// last tick time per streaming file (sticky switch, no two-file yoyo), and
// the file a live stream currently feeds (for the panel-open jump).
var _tgLivePath = null;   // file a live stream currently feeds (panel-open jump)
// Sticky follow (2026-09-18): while the user is at the panel bottom, the
// stream scrolls along; scrolling up pauses it, returning to the bottom
// resumes it. Tracked by a scroll listener, not per-render heuristics —
// content growth alone never fires scroll, so the flag stays honest.
var _cpFollowStream = true;

function _cpNearBottom() {
  var b = document.getElementById('code-panel-body');
  if (!b) return true;
  return (b.scrollTop + b.clientHeight) >= (b.scrollHeight - 60);
}

function _cpEnsureFollowListener() {
  var b = document.getElementById('code-panel-body');
  if (!b || b._followWired) return;
  b._followWired = true;
  b.addEventListener('scroll', function() {
    _cpFollowStream = _cpNearBottom();
  });
}

function _tgUnescape(s) {
  // JSON string body → text. Single left-to-right pass so multi-char
  // sequences decode atomically ("\\\\t" must stay "\\t", never become TAB).
  // Tolerates a trailing partial "\\uXX" (stream cut mid-escape).
  try {
    var done = s.replace(/\\u[0-9a-fA-F]{0,3}$/, '');
    done = done.replace(/\\(u[0-9a-fA-F]{4}|[\s\S])/g, function(_, g) {
      if (g[0] === 'u') return String.fromCharCode(parseInt(g.slice(1), 16));
      switch (g) {
        case 'n': return '\n';
        case 't': return '\t';
        case 'r': return '\r';
        case '"': return '"';
        case '\\': return '\\';
        case '/': return '/';
        default: return g;
      }
    });
    return done;
  } catch(e) { return s; }
}

function _tgFieldSpan(buf, field, fromIdx) {
  // Position span of a JSON string field in a possibly-incomplete args
  // buffer: {start, end, value}. end === null while the string is still
  // streaming (no closing quote yet). fromIdx: search after this index —
  // lets new_text skip everything the old_text value occupied.
  var at = fromIdx || 0;
  var m = buf.slice(at).match(new RegExp('"' + field + '"\\s*:\\s*"'));
  if (!m) return null;
  var start = at + m.index + m[0].length;
  var rest = buf.slice(start);
  for (var i = 0; i < rest.length; i++) {
    if (rest[i] === '\\') { i++; continue; }
    if (rest[i] === '"') {
      return { start: start, end: start + i + 1, value: _tgUnescape(rest.slice(0, i)) };
    }
  }
  return { start: start, end: null, value: _tgUnescape(rest) };
}

function _tgExtractField(buf, field, fromIdx) {
  // Value of "field": "..." from a possibly-incomplete args JSON. The value
  // runs to the buffer end (stream open) or to the closing quote.
  var _sp = _tgFieldSpan(buf, field, fromIdx);
  return _sp ? _sp.value : null;
}

function _tgPanelStream(st) {
  // Push the streamed value into the right code panel. The tab appears as
  // soon as anything streams — under a PENDING key when the path hasn't
  // parsed yet (some models emit content before path) and is re-keyed to
  // the real file the moment the path parses out of the buffer. Content
  // renders plain + throttled while streaming; the file_change event after
  // execution replaces it with the real file.
  var field = (st.tool === 'edit_file') ? 'new_text' : 'content';
  // NEW_TEXT AFTER OLD_TEXT (2026-09-20): edit_file streams path, old_text,
  // new_text in order. An old_text body that itself contains the literal
  // `"new_text": "` (code that edits code) made the naive first-match
  // extractor lock onto the wrong position forever. Search for new_text
  // only AFTER the old_text value ends.
  var txt = null;
  var _otInfo = null;
  if (st.tool === 'edit_file') {
    _otInfo = _tgFieldSpan(st.buf, 'old_text');
    var _ntFrom = (_otInfo && _otInfo.end != null) ? _otInfo.end : 0;
    var _nt = _tgFieldSpan(st.buf, 'new_text', _ntFrom);
    if (_nt) {
      // started = closed, or streaming with actual content. An open span
      // with an empty value means new_text has not really begun (pure
      // deletions close the span with an empty value and render as such).
      if (_nt.end != null || _nt.value !== '') txt = _nt.value;
    }
  } else {
    txt = _tgExtractField(st.buf, field);
  }
  // OLD_TEXT DEAD-AIR (2026-09-20): while new_text has not started, the
  // panel sat frozen on its previous content for the whole old_text stream
  // (often minutes at local speeds) — the "inconsistent streaming" feel.
  // Show the matching progress instead.
  if (txt == null && st.tool === 'edit_file' && _otInfo && _otInfo.value != null) {
    var _otTail = _otInfo.value;
    if (_otTail.length > 600) _otTail = '\u2026' + _otTail.slice(-600);
    txt = '\u23F3 edit_file: old_text found (' + _otInfo.value.length
        + ' chars) \u2014 new text follows\u2026\n\n' + _otTail;
  }
  if (txt == null) return;
  var op = (st.tool === 'edit_file') ? 'edit' : (st.tool === 'write_file_append' ? 'append' : 'write');
  _cpEnsureFollowListener();
  var body = document.getElementById('code-panel-body');
  var now = Date.now();
  var doRender = (now - _tgPanelLastRender) >= _TG_PANEL_RENDER_MS;
  // PER-CARD PENDING KEY (2026-10-21, live: two parallel write_file calls —
  // both streams wrote into ONE shared pending tab, call #1's tab stuck in
  // 'generation' while call #2 took the channel over): every card gets its
  // own pending key derived from the tool-call index.
  var _pkey = _TG_PENDING_KEY + '#' + (st.idx != null ? st.idx : 0);
  // FOLLOW KEY: the file this card currently streams into (real path, or
  // the pending tab until the path parses out of the buffer).
  var _fkey = st.path || ((_cpFiles[_pkey] || st.buf.length > 0) ? _pkey : '');
  if (st.path && _cpFiles[_pkey]) _tgPendingRekey(st.path, _pkey);
  // ENTRY FIRST (2026-09-21): register the file before the follow switch —
  // a first tick must be able to create AND show the tab in one pass.
  if (st.path) {
    _cpAddOrUpdateFile(st.path, txt, op, doRender ? 'plain' : false);
  } else {
    // Diagnose (2026-09-18): path extraction failed on real fragment data
    // once (chip stayed "…" at 4.7k chars) — leave a trace in the console.
    if (st.buf.length > 300 && !st.pathWarned) {
      st.pathWarned = true;
      console.warn('[tool-gen] path not parsed, streaming under pending tab. buf head:', st.buf.slice(0, 160));
    }
    _cpAddOrUpdateFile(_pkey, txt, op, doRender ? 'plain' : false);
    if (_cpFiles[_pkey]) {
      _cpFiles[_pkey].tab.title = 'streaming (file name pending)';
      var pn = _cpFiles[_pkey].tab.querySelector('.cp-name');
      if (pn) pn.textContent = 'streaming\u2026';
    }
  }
  // NO AUTO-SWITCH (2026-09-21): ticks only ever update the entry — the
  // view follows a stream exclusively via explicit clicks (streaming card,
  // chip, tab). _tgLivePath marks the live stream for the panel-open jump.
  if (_fkey) _tgLivePath = _fkey;
  if (doRender) _tgPanelLastRender = now;
  // follow the stream while the user sits at the bottom (after the paint
  // callback queued by _cpRequestShow, so scrollHeight is current)
  if (doRender && _cpFollowStream && body && document.body.classList.contains('code-panel-open')) {
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(function() {
        if (_cpFollowStream) body.scrollTop = body.scrollHeight;
      });
    } else {
      body.scrollTop = body.scrollHeight;
    }
  }
}

function _tgPendingRekey(realPath, pkey) {
  // Move a card's pending stream tab to the real file path once known.
  var _k = pkey || _TG_PENDING_KEY;
  var pend = _cpFiles[_k];
  if (!pend) return;
  var _wasActive = (_cpActive === _k);
  var content = pend.content, op = pend.op;
  delete _cpFiles[_k];
  pend.tab.remove();
  if (_wasActive) _cpActive = null;
  _cpAddOrUpdateFile(realPath, content, op, _wasActive ? false : 'plain');
  if (_wasActive) {
    _cpShowFile(realPath, true);
    var _bRekey = document.getElementById('code-panel-body');
    if (_bRekey && _cpFollowStream && document.body.classList.contains('code-panel-open')) {
      _bRekey.scrollTop = _bRekey.scrollHeight;
    }
  }
}

function _tgEachPendingKey(fn) {
  // Every per-card pending tab (keys '_TG_PENDING_KEY#<idx>').
  for (var k in _cpFiles) {
    if (k.indexOf(_TG_PENDING_KEY) === 0) fn(k);
  }
}

function _tgPanelDropPending() {
  _tgEachPendingKey(function (_k) {
    var pend = _cpFiles[_k];
    if (pend) {
      delete _cpFiles[_k];
      pend.tab.remove();
      if (_cpActive === _k) _cpActive = null;
    }
  });
}

function _tgCancelPanelRaf() {
  if (!_tgPanelRafId) return;
  if (_tgPanelRafIsTimer) clearTimeout(_tgPanelRafId);
  else if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(_tgPanelRafId);
  _tgPanelRafId = null;
  _tgPanelRafIsTimer = false;
}

function _tgSyncCardMeta(st) {
  if (!st) return;
  if (!st.path) {
    var pm = st.buf.match(/"path"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (pm) {
      var p = _tgUnescape(pm[1]);
      if (p !== st.path) {
        st.path = p;
        if (st.chip) {
          var pathEl = st.chip.querySelector('.tg-path');
          if (pathEl) pathEl.textContent = p;
          st.chip.title = 'click: watch the code stream into the panel';
        }
      }
    }
  }
  if (st.chip) {
    var charsEl = st.chip.querySelector('.tg-chars');
    if (charsEl) charsEl.textContent = st.buf.length + ' chars';
  }
}

function _tgSchedulePanelStream(st) {
  if (st) _tgPendingCards.add(st);
  _tgPanelDirty = true;
  if (_tgPanelRafId) return;
  var _run = function() {
    _tgPanelRafId = null;
    _tgPanelRafIsTimer = false;
    if (!_tgPanelDirty && !_tgPendingCards.size) return;
    _tgPanelDirty = false;
    var cards = Array.from(_tgPendingCards);
    _tgPendingCards.clear();
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      if (c && _tgCards[c.idx] === c) {
        _tgSyncCardMeta(c);
        _tgPanelStream(c);
      }
    }
  };
  if (typeof requestAnimationFrame === 'function') {
    _tgPanelRafIsTimer = false;
    _tgPanelRafId = requestAnimationFrame(_run);
  } else {
    _tgPanelRafIsTimer = true;
    _tgPanelRafId = setTimeout(_run, 16);
  }
}

function _tgPanelFlush() {
  // Final unthrottled render of every streaming tab (throttle may have
  // skipped the tail of the stream). Pending entries move to their real
  // path when known; a path-less pending tab is dropped by _toolGenDone
  // (the file_change event brings the real file after execution).
  _tgCancelPanelRaf();
  _tgPanelDirty = false;
  _tgPendingCards.clear();
  for (var k in _tgCards) {
    var st = _tgCards[k];
    if (!st) continue;
    _tgSyncCardMeta(st);
    var txt;
    if (st.tool === 'edit_file') {
      var _otE = _tgFieldSpan(st.buf, 'old_text');
      txt = _tgExtractField(st.buf, 'new_text', (_otE && _otE.end != null) ? _otE.end : 0);
    } else {
      txt = _tgExtractField(st.buf, 'content');
    }
    if (txt == null) continue;
    var op = (st.tool === 'edit_file') ? 'edit' : (st.tool === 'write_file_append' ? 'append' : 'write');
    if (st.path) {
      var _pk = _TG_PENDING_KEY + '#' + (st.idx != null ? st.idx : 0);
      if (_cpFiles[_pk]) _tgPendingRekey(st.path, _pk);
      _cpAddOrUpdateFile(st.path, txt, op, 'plain');
    } else {
      _cpAddOrUpdateFile(_TG_PENDING_KEY + '#' + (st.idx != null ? st.idx : 0), txt, op, 'plain');
    }
  }
}

function _apprCardArgFeed(d) {
  var _card = document.getElementById('approval-request-card');
  if (!_card || _card.dataset.apLocked === '1' || _card.dataset.apStaged !== '1') {
    _apprGenBuf = '';
    return;
  }
  var _pre = _card.querySelector('[data-ap-preview]');
  if (!_pre) return;
  if (d.name && d.name !== _apprGenTool) { _apprGenTool = d.name; _apprGenBuf = ''; }
  _apprGenBuf += (d.content || '');
  var _what = '';
  var _pm = _apprGenBuf.match(/"path"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  var _cm = _apprGenBuf.match(/"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  var _pk = _apprGenBuf.match(/"package"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  var _mm = _apprGenBuf.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  var _km = _apprGenBuf.match(/"code"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  try {
    if (_pm) _what = _tgUnescape(_pm[1]);
    else if (_cm) { _what = _tgUnescape(_cm[1]); if (_what.length > 120) _what = _what.slice(0, 120) + '\u2026'; }
    else if (_pk) _what = _tgUnescape(_pk[1]);
    else if (_mm) { _what = _tgUnescape(_mm[1]); if (_what.length > 120) _what = _what.slice(0, 120) + '\u2026'; }
    else if (_km) { _what = _tgUnescape(_km[1]); if (_what.length > 100) _what = _what.slice(0, 100) + '\u2026'; }
  } catch (e) { _what = ''; }
  if (_what) _pre.textContent = '\u2192 ' + _what + '  \u00b7  generating\u2026';
}

function _toolGenStream(d) {
  try {
    _apprCardArgFeed(d);
    // CARD-ARG-FEED (2026-09-19): non write tools (run_bash etc.) feed only
    // the approval card — no chip and no code-panel streaming for them.
    if (!/^(write_file|write_file_append|edit_file)$/.test(d.name || '')) return;
    if (!S.curAgent) return;
    var body = document.getElementById('ab-' + S.curAgent.tid);
    if (!body) return;
    if (typeof rmEl === 'function') rmEl('status-el');
    if (S._thinkBlockId) {
      var _tkPre = document.getElementById(S._thinkBlockId);
      if (_tkPre) _tkPre.classList.remove('live');
      S._thinkBlockId = null;
    }
    var idx = (d.index != null) ? d.index : 0;
    var _cid = d.call_id || '';
    var st = _tgCards[idx];
    if (st && _cid && st.callId && _cid !== st.callId) {
      _toolGenDone();
      st = null;
    }
    if (!st || !st.row || !st.row.isConnected) {
      var row = document.createElement('div');
      row.className = 'tool-call-row';
      row.dataset.tid = S.curAgent.tid;
      var chip = document.createElement('div');
      chip.className = 'tool-call-chip tc-write tg-card';
      chip.innerHTML =
        '<span class="tc-icon">' + (_TOOL_ICONS[d.name] || '\u270d\ufe0f') + '</span>' +
        '<span class="tc-name">' + esc(d.name || 'write') + '</span>' +
        '<span class="tg-sep">\u00b7</span>' +
        '<span class="tc-label tg-path">\u2026</span>' +
        '<span class="tg-chars"></span>';
      // click: open the right code panel AND follow THIS card's stream.
      // CARD-CLICK-FOLLOW (2026-09-21): the handler only toggled the panel
      // open — with the panel already open nothing switched, and the click
      // on a streaming card left the previous file's view up (live: new
      // edit streamed under the pending tab while the panel showed the old
      // EDITED view).
      chip.addEventListener('click', function() {
        var _own = _TG_PENDING_KEY + '#' + idx;
        var _k = (st && st.path) ? st.path
               : ((_cpFiles[_own]) ? _own : '');
        _cpFollowStream = true;
        toggleCodePanel(true);
        if (_k && _cpFiles[_k]) {
          _cpFiles[_k].view = 'file';
          _cpShowFile(_k, true);
          var _cbClick = document.getElementById('code-panel-body');
          if (_cbClick) _cbClick.scrollTop = _cbClick.scrollHeight;
        }
      });
      row.appendChild(chip);
      body.appendChild(row);
      scrollBtmIfNearBottom(60);
      st = _tgCards[idx] = { row: row, chip: chip, buf: '', path: '', tool: d.name || '', idx: idx, callId: _cid };
    } else if (_cid && !st.callId) {
      st.callId = _cid;
    }
    st.buf += (d.content || '');
    if (d.name && st.tool !== d.name) {
      st.tool = d.name;
      st.chip.querySelector('.tc-name').textContent = d.name;
      st.chip.querySelector('.tc-icon').textContent = _TOOL_ICONS[d.name] || '\u270d\ufe0f';
    }
    _tgSchedulePanelStream(st);
  } catch(e) { /* never break the stream */ }
}

function _toolGenDone() {
  // Flush the last stream state into the panel (throttle may have skipped
  // the tail), then drop the compact chips — the real tool_call chips and
  // the result metrics take over from here. A still-pending panel tab is
  // dropped too (the file_change event brings the real file after exec).
  _tgPanelFlush();
  _tgPanelDropPending();
  for (var k in _tgCards) {
    var st = _tgCards[k];
    if (st && st.row && st.row.parentNode) st.row.parentNode.removeChild(st.row);
    delete _tgCards[k];
  }
  _tgLivePath = null;
}

// END_TOOLGEN_STREAM

// ── Live Code Panel ──────────────────────────────────────────────────────────
var _cpFiles = {};          // path → {content, op, tab, pre}
var _cpActive = null;       // currently shown path
var _cpLastShowAt = 0;      // ms timestamp of the last explicit _cpShowFile
// PERF (2026-10-31): the panel used to rebuild every line (number span +
// syntax-highlight regexes) on every 120ms stream tick and on every tab
// switch. Three cheap guards keep large sessions responsive:
//   - streamed plain text appends to one text node instead of rebuilding,
//   - finished file/diff HTML is cached per tab (tab switches are free),
//   - very large files fall back to plain text instead of thousands of spans.
var _CP_LARGE_CHARS = 200000;
var _CP_LARGE_LINES = 6000;
var _CP_DIFF_MAX_LINES = 3000;
var _cpPaintRaf = 0;
var _cpPaintJob = null;
var _cpPaintSeq = 0;

function _cpTextIsLarge(text) {
  if (text.length > _CP_LARGE_CHARS) return true;
  if (text.length < (_CP_LARGE_CHARS / 2)) return false;
  var n = 0, at = -1;
  while ((at = text.indexOf('\n', at + 1)) !== -1) {
    if (++n > _CP_LARGE_LINES) return true;
  }
  return false;
}

function _cpRequestShow(path, plain, streamOnly) {
  // Coalesce stream ticks into one paint per frame. A full render (final
  // file_change, tab click) bumps _cpPaintSeq and invalidates the queued
  // plain paint, so the panel never falls back to streaming text afterwards.
  _cpPaintJob = {path: path, plain: plain, streamOnly: !!streamOnly, seq: ++_cpPaintSeq};
  if (_cpPaintRaf) return;
  var run = function() {
    _cpPaintRaf = 0;
    var job = _cpPaintJob;
    _cpPaintJob = null;
    if (job && job.seq === _cpPaintSeq && _cpActive === job.path) {
      _cpShowFile(job.path, job.plain, job.streamOnly);
      if (job.plain && _cpFollowStream && document.body.classList.contains('code-panel-open')) {
        var _cpB = document.getElementById('code-panel-body');
        if (_cpB) _cpB.scrollTop = _cpB.scrollHeight;
      }
    }
  };
  if (window.requestAnimationFrame) _cpPaintRaf = window.requestAnimationFrame(run);
  else _cpPaintRaf = setTimeout(run, 16);
}

function _cpRenderStreamText(body, text) {
  // Plain stream paint: one <pre> + one text node. Prefix growth appends the
  // delta; a changed fragment resets the node. No line numbers or syntax
  // spans while streaming - the final render adds them once.
  text = String(text || '');
  var pre = body._cpPre;
  if (!pre || pre.parentNode !== body) {
    body.innerHTML = '';
    pre = document.createElement('pre');
    body.appendChild(pre);
    body._cpPre = pre;
    body._cpText = '';
    body._cpTextNode = null;
  }
  if (!text) {
    pre.textContent = '';
    body._cpTextNode = null;
  } else if (text.indexOf(body._cpText || '') === 0) {
    var delta = text.slice((body._cpText || '').length);
    if (delta) {
      if (!body._cpTextNode || body._cpTextNode.parentNode !== pre) {
        pre.textContent = text;
        body._cpTextNode = pre.firstChild;
      } else {
        body._cpTextNode.appendData(delta);
      }
    }
  } else {
    pre.textContent = text;
    body._cpTextNode = pre.firstChild;
  }
  body._cpText = text;
}

function toggleCodePanel(forceOpen) {
  var open = forceOpen !== undefined ? forceOpen : !document.body.classList.contains('code-panel-open');
  var _wasOpen = document.body.classList.contains('code-panel-open');
  document.body.classList.toggle('code-panel-open', open);
  var btn = document.getElementById('h-code-btn');
  if (btn) btn.classList.toggle('active', open);
  // LIVE-JUMP (2026-09-21): opening the panel while a write/edit streams
  // jumps straight to the streaming file. Chip clicks are exempt — they
  // call _cpShowFile right before opening (fresh timestamp).
  if (open && !_wasOpen && _tgLivePath && _cpFiles[_tgLivePath]
      && Date.now() - (_cpLastShowAt || 0) > 300) {
    var _live = _cpFiles[_tgLivePath];
    _live.view = 'file';
    _cpFollowStream = true;
    // An edit/append stream is a FRAGMENT in streamText while content still
    // holds the old disk state; opening the panel mid-stream must show the
    // fragment, not the stale file.
    _cpShowFile(_tgLivePath, true, !!_live.streamText);
    var _cpJumpB = document.getElementById('code-panel-body');
    if (_cpJumpB) _cpJumpB.scrollTop = _cpJumpB.scrollHeight;
  }
}

function toggleSidebar() {
  var collapsed = document.body.classList.toggle('sidebar-collapsed');
  var btn = document.getElementById('h-sidebar-btn');
  if (btn) btn.classList.toggle('active', !collapsed);
}

function _cpSyntaxHighlight(code) {
  // Single-pass tokenizer so generated <span style="color:#..."> attributes
  // are never re-matched by later comment/string regex passes.
  var src = String(code || '');
  var re = /("""[\s\S]*?"""|'''[\s\S]*?''')|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|(#[^\n]*|\/\/[^\n]*)|\b(def|class|return|import|from|if|else|elif|for|while|try|except|finally|with|as|pass|break|continue|yield|async|await|const|let|var|function|export|default|new|this|null|true|false|None|True|False|and|or|not|in|is)\b|\b(\d+\.?\d*)\b|\b([a-zA-Z_]\w*)(?=\s*\()/g;
  var out = [];
  var last = 0;
  var m;
  while ((m = re.exec(src)) !== null) {
    if (m.index > last) out.push(_escHtml(src.slice(last, m.index)));
    var col = m[1] ? '#7ea' : m[2] ? '#9a8' : m[3] ? '#5a7a5a' : m[4] ? '#7ab' : m[5] ? '#c8a' : '#acb';
    out.push('<span style="color:' + col + '">' + _escHtml(m[0]) + '</span>');
    last = re.lastIndex;
  }
  if (last < src.length) out.push(_escHtml(src.slice(last)));
  return out.join('');
}

// Findet den _cpFiles-Key zu einem (evtl. kurzen/relativen) Pfad vom Tool-Chip.
function _cpFindEntry(p) {
  p = (p || '').trim();
  if (!p) return null;
  if (_cpFiles[p]) return p;
  var norm = p.replace(/\\/g, '/');
  var keys = Object.keys(_cpFiles);
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i].replace(/\\/g, '/');
    if (k === norm || k.endsWith('/' + norm) || norm.endsWith('/' + k)) return keys[i];
  }
  return null;
}

function _cpShowFile(path, plain, streamOnly) {
  var entry = _cpFiles[path];
  if (!entry) return;
  _cpActive = path;
  _cpLastShowAt = Date.now();
  _cpPaintSeq++;  // an immediate paint supersedes any queued stream paint
  // Update tab highlights
  Object.keys(_cpFiles).forEach(function(p) {
    var t = _cpFiles[p].tab;
    if (t) t.classList.toggle('active', p === path);
  });
  // Render content
  var body = document.getElementById('code-panel-body');
  if (!body) return;
  // DIFF/FILE VIEW (2026-09-19): per-tab switch — 'diff' renders the last
  // recorded unified diff, 'file' the full disk state.
  if (entry.view === 'diff' && entry.diffText) {
    // pinned call wins over latest; the pin is set via the ⇱ icon in the
    // chat result metrics
    var _d = entry.diffs && entry.diffs.length
      ? (entry.pinnedCallId
          ? (entry.diffs.filter(function(d) { return d.id === entry.pinnedCallId; })[0]
             || entry.diffs[entry.diffs.length - 1])
          : entry.diffs[entry.diffs.length - 1])
      : null;
    var _dt = String((_d ? _d.text : entry.diffText) || '');
    if (entry._diffCache && entry._diffCache.text === _dt) {
      body.innerHTML = entry._diffCache.html;
    } else {
      _cpRenderDiff(body, _dt);
      entry._diffCache = {text: _dt, html: body.innerHTML};
    }
    _cpUpdateViewToggle();
    return;
  }
  // LIVESTREAM FILE VIEW (2026-09-20): while an edit/append is streaming,
  // disk truth is not in yet (file_change ships after execution) — render
  // the streamed fragment instead of an empty panel.
  var text = streamOnly
    ? (entry.streamText || '')
    : (entry.content || entry.streamText || '');
  if (plain) {
    _cpRenderPre(body, text, true);
  } else if (entry._fileCache && entry._fileCache.text === text) {
    body.innerHTML = entry._fileCache.html;
    body._cpPre = null;
    body._cpText = '';
  } else {
    _cpRenderPre(body, text, false);
    entry._fileCache = {text: text, html: body.innerHTML};
  }
  _cpUpdateViewToggle();
}

function _cpRenderPre(body, text, plain) {
  text = String(text || '');
  if (plain) {
    _cpRenderStreamText(body, text);
    return;
  }
  var _large = _cpTextIsLarge(text);
  if (_large) {
    // Large files: line-number spans + regex highlighting cost seconds and
    // allocate a huge HTML string. Plain text stays scrollable; the final
    // line count is not worth a frozen panel.
    body.innerHTML = '<div style="font-family:IBM Plex Mono,monospace;font-size:9px;'
      + 'color:var(--tx2);opacity:.55;margin-bottom:6px">'
      + 'large file &middot; plain view (line numbers/syntax highlighting off)</div>'
      + '<pre>' + _escHtml(text) + '</pre>';
    body._cpPre = null;
    body._cpText = '';
    return;
  }
  var hl = _cpSyntaxHighlight;
  var lines = text.split('\n');
  var lineNums = lines.map(function(l, i) {
    return '<span style="color:var(--tx2);user-select:none;margin-right:14px;display:inline-block;min-width:28px;text-align:right;opacity:.45">'+(i+1)+'</span>'+hl(l);
  }).join('\n');
  body.innerHTML = '<pre>' + lineNums + '</pre>';
  body._cpPre = null;
  body._cpText = '';
}

// View toggle lives in the panel header, pinned right next to the close
// button (tabs never push it around). Applies to the ACTIVE tab.
var _cpViewToggle = null;
var _cpCallSeq = 0;
// LAST-VIEW MODE (2026-09-30): File vs ±Diff is a remembered MODE, not a
// per-file property — whichever view was used last is how the next tool
// call / tab click opens (chip click still pins its own call's diff).
var _cpLastView = 'file';
function _cpEnsureViewToggle() {
  if (_cpViewToggle && _cpViewToggle.isConnected) return;
  var hdr = document.getElementById('code-panel-hdr');
  var closeBtn = document.getElementById('code-panel-close');
  if (!hdr || !closeBtn) return;
  var wrap = document.createElement('span');
  wrap.style.cssText = 'display:inline-flex;gap:4px;margin-left:auto;margin-right:8px;flex-shrink:0;align-items:center';
  var mkBtn = function(label, view, title) {
    var b = document.createElement('button');
    b.textContent = label; b.title = title;
    b.setAttribute('data-cp-view', view);
    b.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:9px;padding:3px 8px;background:none;border:1px solid var(--b2);border-radius:3px;color:var(--tx2);cursor:pointer';
    b.onclick = function() {
      var entry = _cpFiles[_cpActive];
      if (!entry) return;
      if (view === 'diff' && !(entry.diffText || (entry.diffs && entry.diffs.length))) return;
      // VIEW/PIN DECOUPLED (2026-09-19): File vs ±Diff only swaps the
      // rendering; the pinned call survives so ±Diff comes back to exactly
      // that call. Pin changes only via ⇱ / chip click.
      entry.view = view;
      _cpLastView = view;  // remembered mode for the next open
      _cpShowFile(_cpActive);
    };
    return b;
  };
  wrap.appendChild(mkBtn('File', 'file', 'Full file state (as on disk)'));
  wrap.appendChild(mkBtn('\u00b1Diff', 'diff', 'Diff of the selected tool call'));
  hdr.insertBefore(wrap, closeBtn);
  closeBtn.style.marginLeft = '0';
  _cpViewToggle = wrap;
}

function _cpUpdateViewToggle() {
  if (!_cpViewToggle || !_cpViewToggle.isConnected) return;
  var entry = _cpFiles[_cpActive];
  var view = entry ? (entry.view || 'file') : 'file';
  var hasDiff = !!(entry && (entry.diffText || (entry.diffs && entry.diffs.length)));
  _cpViewToggle.querySelectorAll('button').forEach(function(b) {
    var v = b.getAttribute('data-cp-view');
    var isView = v === view;
    var dim = (v === 'diff' && !hasDiff);
    if (v === 'diff') {
      // CALL-LABEL (2026-09-30): show WHICH call the diff view is bound to —
      // the pinned call (chip click / ⇱) or the latest recorded one
      var _lbl = '\u00b1Diff';
      if (isView && entry && entry.diffs && entry.diffs.length) {
        var _dc = entry.pinnedCallId
          ? entry.diffs.filter(function(d){ return d.id === entry.pinnedCallId; })[0]
          : null;
        if (!_dc) _dc = entry.diffs[entry.diffs.length - 1];
        if (_dc && _dc.name) _lbl += ' \u00b7 ' + _dc.name;
      }
      b.textContent = _lbl;
    }
    b.style.borderColor = isView ? '#20b0a0' : 'var(--b2)';
    b.style.color = isView ? '#20b0a0' : 'var(--tx2)';
    b.style.opacity = dim ? .35 : 1;
    b.style.pointerEvents = dim ? 'none' : 'auto';
  });
}

function _cpRenderDiff(body, diffText) {
  var lines = String(diffText || '').split('\n');
  var _dropped = 0;
  if (lines.length > _CP_DIFF_MAX_LINES) {
    _dropped = lines.length - _CP_DIFF_MAX_LINES;
    lines = lines.slice(0, _CP_DIFF_MAX_LINES);
  }
  var html = lines.map(function(l) {
    var _cut = false;
    if (l.length > 400) { l = l.slice(0, 400) + '\u2026'; _cut = true; }
    var e = _escHtml(l);
    var st = '';
    if (/^(\+\+\+|---)/.test(l)) st = 'color:var(--tx2);opacity:.55';
    else if (/^@@/.test(l)) st = 'color:#f0ad4e;opacity:.8';
    else if (/^\+/.test(l)) st = 'color:#22c55e;background:rgba(34,197,94,.06);display:inline-block;width:100%';
    else if (/^-/.test(l)) st = 'color:#e06060;background:rgba(224,96,96,.06);display:inline-block;width:100%';
    if (_cut) st += (st ? ';' : '') + 'opacity:.7';
    return st ? '<span style="' + st + '">' + e + '</span>' : e;
  }).join('\n');
  if (_dropped) {
    html += '\n<span style="color:var(--tx2);opacity:.55">'
      + '\u2026 ' + _dropped + ' more diff line(s) not rendered</span>';
  }
  body.innerHTML = '<pre>' + html + '</pre>';
  body._cpPre = null;
  body._cpText = '';
}

function _cpAddOrUpdateFile(path, content, op, render) {
  var hdr = document.getElementById('code-panel-hdr');
  if (!hdr) return;
  // TAB-KEY MERGE (2026-09-30): the streaming tab is created from the path
  // AS THE MODEL WROTE IT ("tetris.html", "/C:/Users/...", backslashes),
  // while file_change / tool results carry the backend's normalized
  // absolute path. Direct map access created TWO tabs for one file (live:
  // an "EDIT" fragment tab next to an "EDITED" full-file tab, diff view
  // grayed out on both because diffText attached to the other key). Resolve
  // through the suffix matcher and re-key to the incoming (canonical) path.
  var _mk = _cpFindEntry(path);
  if (_mk && _mk !== path && _cpFiles[_mk]) {
    var _moved = _cpFiles[_mk];
    delete _cpFiles[_mk];
    _moved.tab.title = path;
    _moved.tab.onclick = (function(p){return function(){
      var _te = _cpFiles[p];
      if (_te) _te.view = _cpLastView;  // tabs open in the last used view mode
      _cpShowFile(p);
    }})(path);
    _cpFiles[path] = _moved;
    if (_cpActive === _mk) _cpActive = path;
  }
  // Clear placeholder text if first file
  var empty = hdr.querySelector('#code-panel-empty');
  if (empty) empty.remove();
  var segs = path.replace(/\\/g,'/').split('/').filter(Boolean);
  var shortName = segs.slice(-1)[0] || path;
  var opLabel = (op||'write').toLowerCase();
  if (!_cpFiles[path]) {
    // New tab
    var tab = document.createElement('button');
    tab.className = 'cp-tab';
    // TAB-LABEL (2026-09-30): just the file name — the operation lives in
    // the timeline chips and the view toggle, not in the tab row
    tab.innerHTML = '<span class="cp-name">'+esc(shortName)+'</span>';
    tab.title = path;
    tab.onclick = (function(p){return function(){
      var _te = _cpFiles[p];
      if (_te) _te.view = _cpLastView;  // tabs open in the last used view mode
      _cpShowFile(p);
    }})(path);
    // Insert before the view toggle (which sits right before the close btn)
    var closeBtn = document.getElementById('code-panel-close');
    var _cpAnchor = (_cpViewToggle && _cpViewToggle.isConnected) ? _cpViewToggle : closeBtn;
    hdr.insertBefore(tab, _cpAnchor);
    _cpFiles[path] = {content: '', op: opLabel, tab: tab, view: 'file', diffText: '', diffs: [], pinnedCallId: null, streamText: ''};
  } else {
    _cpFiles[path].op = opLabel;
    // op badge was removed from tabs (2026-09-30) — nothing to restamp
  }
  // STREAM/FILE SPLIT (2026-09-19): edit/append streams carry only a
  // FRAGMENT (the search/replace block, the appended tail) — never treat
  // it as the file state. A write stream IS the file being born.
  // Fragment lives in streamText; content holds only disk truth
  // (final file_change). File view therefore never shows fragment garbage.
  var _e = _cpFiles[path];
  var _isFrag = (opLabel === 'edit' || opLabel === 'append');
  if (_isFrag && render !== undefined) {
    _e.streamText = content;
    _e._fileCache = null;
  } else if (_isFrag) {
    _e.content = content;   // final event: disk truth
    _e.streamText = '';
    _e._fileCache = null;
  } else {
    _e.content = content;   // write: the file is being born (stream or final)
    _e._fileCache = null;
  }
  _cpEnsureViewToggle();
  // Auto-show this file (panel contents update invisibly along).
  // render: 'plain' → no syntax highlight (streaming), false → skip render
  // entirely (throttled streaming tick), undefined → normal highlighted.
  // An active diff view is NOT clobbered by stream ticks or file_change —
  // it stays stable until the next tool_result updates the diff.
  var _viewDiff = _e.view === 'diff' && _e.diffText;
  if (render !== false && !_viewDiff) {
    if (_isFrag && render === 'plain') {
      // stream tick: render the fragment live without touching file state
      if (_cpActive === path) _cpRequestShow(path, true, true);
    } else {
      // NO AUTO-SWITCH (2026-09-21): the write stream updates its tab
      // silently; only an explicit click shows it. While this file IS the
      // active tab, render the stream in place. Stream ticks coalesce into
      // one paint per frame; the final file_change paints immediately.
      if (_cpActive === path) {
        if (render === 'plain') _cpRequestShow(path, true);
        else _cpShowFile(path);
      }
    }
  }
  else if (_viewDiff && _cpActive === path) _cpUpdateViewToggle();
  // 2026-08-25: the panel no longer opens by itself — manually via
  // the "⌨ Code" button or a click on a write/edit tool chip.
  var btn = document.getElementById('h-code-btn');
  if (btn) btn.style.display = '';
}

// Reset code panel on new session / new chat
function _cpReset() {
  _cpFiles = {};
  _cpActive = null;
  _cpViewToggle = null;
  _cpCallSeq = 0;
  _tgLivePath = null;
  var hdr = document.getElementById('code-panel-hdr');
  if (hdr) hdr.innerHTML = '<div id="code-panel-empty" style="padding:10px 16px;flex-direction:row;height:auto;justify-content:flex-start;display:flex;gap:8px;color:var(--tx2);font-family:IBM Plex Mono,monospace;font-size:11px;opacity:.5"><span>⌨</span><span>Waiting for coder output…</span></div>'
    +'<button id="code-panel-close" onclick="toggleCodePanel()" title="Close panel" style="margin-left:auto;flex-shrink:0;background:none;border:none;color:var(--tx2);cursor:pointer;padding:7px 12px;font-size:14px">×</button>';
  var body = document.getElementById('code-panel-body');
  if (body) body.innerHTML = '';
  toggleCodePanel(false);
  var btn = document.getElementById('h-code-btn');
  if (btn) btn.style.display = 'none';
}
// ─────────────────────────────────────────────────────────────────────────────

// Action approval gate sub-options visibility (2026-09-18)
function updateApprovalUI() {
  var _apEl = document.getElementById('approval-options');
  if (_apEl) _apEl.style.display = S.duoActionApproval ? 'block' : 'none';
}

// -- Git Integration ------------------------------------------------
function updateGitIntegrationUI() {
  var cfgToggle = document.getElementById('git-autocommit-cfg-toggle');
  if (cfgToggle) cfgToggle.checked = !!S.duoGitAutocommit;
  updateGitAuthStatus();
}

function hasGitCreds() {
  return !!((S.gitRepoUrl || '').trim()
    || (S.gitUsername || '').trim()
    || (S.gitEmail || '').trim()
    || S.gitTokenSaved
    || (S.gitToken || '').trim());
}

function isGitToolsToggleOn() {
  var raw = String(S.gitToolsEnabled || 'auto').trim().toLowerCase();
  if (raw === '0' || raw === 'false' || raw === 'off' || raw === 'no' || raw === 'never') {
    return false;
  }
  if (raw === '1' || raw === 'true' || raw === 'on' || raw === 'yes' || raw === 'always') {
    return true;
  }
  return hasGitCreds();
}

function isGitToolsEffective() {
  return hasGitCreds() && isGitToolsToggleOn();
}

function updateGitToolsUI() {
  var tog = document.getElementById('git-tools-toggle');
  var pill = document.getElementById('git-tools-cred-light');
  var led = document.getElementById('git-tools-led');
  var txt = document.getElementById('git-tools-cred-text');
  var link = document.getElementById('git-tools-cfg-link');
  var creds = hasGitCreds();
  var on = isGitToolsToggleOn();
  if (tog) tog.checked = on;

  if (creds) {
    if (pill) {
      pill.classList.remove('is-warn');
      pill.classList.toggle('is-ok', on);
    }
    if (led) led.className = 'tools-led ' + (on ? 'is-on' : 'is-off');
    if (txt) txt.textContent = on ? 'Credentials OK \u00B7 Git tools ON' : 'Credentials OK \u00B7 Git tools OFF';
    if (link) link.style.display = 'none';
  } else if (on) {
    if (pill) {
      pill.classList.remove('is-ok');
      pill.classList.add('is-warn');
    }
    if (led) led.className = 'tools-led is-warn';
    if (txt) txt.textContent = 'No credentials \u2014 effective: OFF';
    if (link) link.style.display = '';
  } else {
    if (pill) pill.classList.remove('is-ok', 'is-warn');
    if (led) led.className = 'tools-led is-off';
    if (txt) txt.textContent = 'No credentials \u00B7 OFF';
    if (link) link.style.display = '';
  }
}

function onGitToolsToggle(on) {
  S.gitToolsEnabled = on ? 'true' : 'false';
  postSettings({git_tools_enabled: S.gitToolsEnabled});
  updateGitToolsUI();
  updateDuoCoderTierHint();
  updateDirectToolsTierHint();
  updateChatToolsBadge();
}

function updateGitAuthStatus() {
  updateAgentGitLine();
  updateGitToolsUI();
  if (typeof updateDuoCoderTierHint === 'function') updateDuoCoderTierHint();
  if (typeof updateDirectToolsTierHint === 'function') updateDirectToolsTierHint();
}

// SUBAGENT-LITE (2026-08-24): Body ausblenden wenn deaktiviert —
// das Tool ist dann auch serverseitig unsichtbar (definitions-Filter).
function syncSubagentUI() {
  var tgl = document.getElementById('subagent-lite-toggle');
  var body = document.getElementById('subagent-lite-body');
  if (tgl && body) body.style.display = tgl.checked ? '' : 'none';
  if (typeof updateDuoCoderTierHint === 'function') updateDuoCoderTierHint();
  if (typeof updateDirectToolsTierHint === 'function') updateDirectToolsTierHint();
}

function syncGitToggles() {
  updateGitIntegrationUI();
}

// GIT-WS-SOURCE (2026-10-25 audit): the git panel must act on the project
// the user is LOOKING AT. The workspace field holds the loaded chat's
// workspace (loadChat fills it display-only); the git calls used to send the
// global settings.workspace instead — with a per-chat workspace the panel
// showed project X while validate/branches/stash/reset operated on the
// default repo (hard-reset-on-the-wrong-repo risk). WYSIWYG: read the field.
function _gitWs() {
  var el = document.getElementById('workspace-input');
  var v = el ? String(el.value || '').trim() : '';
  return v || S.workspace || '';
}

async function testGitConfig() {
  var btn = document.getElementById('git-test-btn');
  var result = document.getElementById('git-test-result');
  if (btn) btn.textContent = 'Teste...';
  // GIT-TEST ROUTE FIX (2026-09-22): /git/test never existed (404 x3)
    // - validation lives at /git/validate.
    try {
    var res = await fetch('/git/validate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ workspace: _gitWs() })
    });
    var data = await res.json();
    // GIT-TRANSPARENCY (2026-10-25): remote_state is machine-readable -
    // render the exact remedy and cache the state for the Agents-tab line.
    S._gitRemote = { state: data.remote_state || '', msg: data.remote_msg || '',
                     hint: data.remote_hint || '', valid: !!data.valid,
                     branch: data.branch || '' };
    var _remoteBad = (data.remote_ok === false);
    var _remote = '';
    if (data.remote_state === 'missing_or_invisible') {
      _remote = ' | ⚠ ' + (data.remote_hint || data.remote_msg || '');
    } else if (data.remote_msg) {
      _remote = ' | ' + data.remote_msg;
    }
    updateAgentGitLine();
    if (data.valid) {
      if (result) { result.textContent = '\u2713 ' + (data.branch ? ('OK - ' + data.branch) : 'Connection OK') + _remote; result.style.color = _remoteBad ? '#e0a030' : 'var(--green)'; }
    } else {
      if (result) { result.textContent = '\u2717 ' + (data.reason || 'Error') + _remote; result.style.color = 'var(--red)'; }
    }
  } catch(e) {
    if (result) { result.textContent = '\u2717 Server error'; result.style.color = 'var(--red)'; }
  }
  if (btn) btn.textContent = 'Test connection';
}
function updateAgentGitLine() {
  // Status element lives only in removed legacy block; keep stub for callers.
}

function jumpToGitConfig() {
  // TAB-SELECTOR FIX (2026-10-25 audit): the tab is data-p="configs"
  // (index.html) — the old '.tab[data-p="config"]' matched nothing, so the
  // Agents-tab "Configure →" link was a silent no-op (no tab switch, and
  // scrollIntoView on a hidden card).
  var tab = document.querySelector('.tab[data-p="configs"]');
  if (tab) tab.click();
  setTimeout(function() {
    var card = document.getElementById('git-cfg-card');
    if (!card) return;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.style.outline = '2px solid var(--amber)';
    card.style.outlineOffset = '3px';
    setTimeout(function() { card.style.outline = 'none'; }, 2200);
  }, 60);
}

async function initGitRepo() {
  var btn = document.getElementById('git-init-btn');
  var statusEl = document.getElementById('git-init-status');
  if (btn) btn.textContent = 'Creating...';
  try {
    var res = await fetch('/git/init', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        workspace: _gitWs(),
        clone_url: S.gitRepoUrl || '',
        username: S.gitUsername || '',
        email: '',
        default_branch: S.gitBranch || 'main'
      })
    });
    var data = await res.json();
    if (data.ok) {
      // GIT-LINE-REFRESH (2026-10-25 audit): a successful Create repo must
      // update the Agents-tab git line — it kept showing the stale
      // "repo missing or token cannot see it" from the last Test until a
      // manual re-test.
      S._gitRemote = {
        state: data.github_created ? 'ok' : (S._gitRemote ? S._gitRemote.state : ''),
        msg: data.github_msg || (S._gitRemote ? S._gitRemote.msg : ''),
        hint: S._gitRemote ? S._gitRemote.hint : '',
        valid: !!data.repo_valid,
        branch: data.branch || (S._gitRemote ? S._gitRemote.branch : '')
      };
      updateAgentGitLine();
      if (data.already_existed) {
        if (statusEl) { statusEl.textContent = '\u2713 Repo already exists (branch: ' + (data.branch || '?') + ')'; statusEl.style.color = 'var(--green)'; }
      } else if (data.cloned) {
        if (statusEl) { statusEl.textContent = '\u2713 Repo cloned: ' + data.repo; statusEl.style.color = 'var(--green)'; }
      } else if (data.initialized) {
        // GITHUB-PANEL FIX (2026-10-07): say what actually happened -
        // local init plus (optional) real GitHub repo creation with the
        // token, honest message when the token lacks permissions.
        var _gm = data.github_msg ? (' | ' + data.github_msg) : '';
        var _col = (data.github_msg && data.github_msg.indexOf('created') !== -1) ? 'var(--green)'
          : (data.github_msg ? '#e0a030' : 'var(--green)');
        if (statusEl) { statusEl.textContent = '\u2713 Initialized locally (branch: ' + (data.branch || 'main') + ')' + _gm; statusEl.style.color = _col; }
      }
      updateGitIntegrationUI();
    } else {
      if (statusEl) { statusEl.textContent = '\u2717 ' + (data.error || 'Error'); statusEl.style.color = 'var(--red)'; }
    }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '\u2717 Server error: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
  if (btn) btn.textContent = 'Create repo';
}

async function gitStash(action) {
  var statusEl = document.getElementById('git-ops-status');
  if (action === 'push' || action === 'pop' || action === 'drop') {
    if (!confirm(action === 'drop' ? 'Delete latest stash?' : 'Stash ' + action + '?')) return;
  }
  try {
    var res = await fetch('/git/stash', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({workspace: _gitWs(), action: action})
    });
    var data = await res.json();
    if (statusEl) { statusEl.textContent = data.message || 'OK'; statusEl.style.color = data.ok ? 'var(--green)' : 'var(--red)'; }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '\u2717 Error: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
}

async function gitShowDiff() {
  var view = document.getElementById('git-diff-view');
  var statusEl = document.getElementById('git-ops-status');
  if (!view) return;
  if (view.style.display !== 'none') { view.style.display = 'none'; return; }
  try {
    var res = await fetch('/git/diff?workspace=' + encodeURIComponent(_gitWs()));
    var data = await res.json();
    if (!data.ok) {
      if (statusEl) { statusEl.textContent = '\u2717 ' + (data.error || 'diff failed'); statusEl.style.color = 'var(--red)'; }
      return;
    }
    view.textContent = (data.stat || '') + (data.stat && data.diff ? '\n\n' : '') +
      (data.diff || '(no uncommitted changes)') +
      (data.truncated ? '\n\n[... diff truncated at 200 KB]' : '');
    view.style.display = 'block';
    if (statusEl) { statusEl.textContent = data.truncated ? 'Diff truncated' : ''; statusEl.style.color = 'var(--tx2)'; }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '\u2717 Error: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
}

async function gitLoadCheckpoints() {
  var sel = document.getElementById('git-checkpoint-sel');
  var btn = document.getElementById('git-checkpoint-restore');
  var statusEl = document.getElementById('git-ops-status');
  if (!sel) return;
  try {
    var res = await fetch('/git/checkpoints?limit=20&workspace=' + encodeURIComponent(_gitWs()));
    var data = await res.json();
    if (!data.ok) {
      if (statusEl) { statusEl.textContent = '\u2717 ' + (data.error || 'checkpoints failed'); statusEl.style.color = 'var(--red)'; }
      return;
    }
    sel.innerHTML = '';
    (data.checkpoints || []).forEach(function(cp) {
      var opt = document.createElement('option');
      opt.value = cp.hash;
      var when = cp.ts ? new Date(cp.ts * 1000).toLocaleString() : '';
      opt.textContent = cp.short + '  ' + (cp.label || 'checkpoint') + (when ? '  ' + when : '');
      sel.appendChild(opt);
    });
    var has = sel.options.length > 0;
    sel.style.display = has ? 'inline-block' : 'none';
    if (btn) btn.style.display = has ? 'inline-block' : 'none';
    if (statusEl) { statusEl.textContent = has ? '' : 'No checkpoints found'; statusEl.style.color = 'var(--tx2)'; }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '\u2717 Error: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
}

async function gitRestoreCheckpoint() {
  var sel = document.getElementById('git-checkpoint-sel');
  var statusEl = document.getElementById('git-ops-status');
  if (!sel || !sel.value) return;
  if (!confirm('Restore tracked files to this checkpoint? A safety stash snapshot is created first.')) return;
  try {
    var res = await fetch('/git/checkpoint/restore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({workspace: _gitWs(), target: sel.value})
    });
    var data = await res.json();
    if (statusEl) { statusEl.textContent = data.message || (data.ok ? 'Restored' : 'Restore failed'); statusEl.style.color = data.ok ? 'var(--green)' : 'var(--red)'; }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '\u2717 Error: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
}

async function gitReset(hard) {
  var label = hard ? 'HARD RESET (all changes will be lost!)' : 'Soft Reset (changes stay staged)';
  if (!confirm(label + ' Really continue?')) return;
  var statusEl = document.getElementById('git-ops-status');
  try {
    var res = await fetch('/git/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({workspace: _gitWs(), target: 'HEAD', hard: hard})
    });
    var data = await res.json();
    if (statusEl) { statusEl.textContent = data.message || 'OK'; statusEl.style.color = data.ok ? 'var(--green)' : 'var(--red)'; }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '\u2717 Error: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
}

async function loadGitConfig(s) {
  S.duoGitAutocommit = s.duo_git_autocommit || false;
  S.gitRepoUrl = s.git_repo_url || '';
  S.gitUsername = s.git_username || '';
  S.gitEmail = s.git_email || '';
  S.gitToken = '';
  S.gitTokenSaved = !!s.git_token_saved;
  S.gitBranch = s.git_default_branch || s.git_branch || 'main';
  // GIT-TOOLS-GATE (2026-10-17 / 2026-11-12): toggle + credential check-light.
  // JSON booleans and strings both occur, so normalise before syncing the toggle.
  if (s.git_tools_enabled === undefined || s.git_tools_enabled === null) {
    S.gitToolsEnabled = 'auto';
  } else if (typeof s.git_tools_enabled === 'boolean') {
    S.gitToolsEnabled = s.git_tools_enabled ? 'true' : 'false';
  } else {
    S.gitToolsEnabled = String(s.git_tools_enabled);
  }
  // Sync toggles
  updateGitToolsUI();
  var cfgToggle = document.getElementById('git-autocommit-cfg-toggle');
  if (cfgToggle) cfgToggle.checked = S.duoGitAutocommit;
  var cpToggle = document.getElementById('git-checkpoints-cfg-toggle');
  if (cpToggle) cpToggle.checked = s.duo_git_checkpoints === true;
  // Fill inputs
  var repoInp = document.getElementById('git-repo-url');
  if (repoInp) repoInp.value = S.gitRepoUrl;
  var userInp = document.getElementById('git-username');
  if (userInp) userInp.value = S.gitUsername;
  var tokInp = document.getElementById('git-token');
  if (tokInp) {
    tokInp.value = '';
    tokInp.placeholder = S.gitTokenSaved
      ? '••••••••  (token saved — enter a new one to replace)'
      : 'Personal Access Token';
  }
  var brInp = document.getElementById('git-branch-inp');
  if (brInp) brInp.value = S.gitBranch;
  // v0.96.5: Additional git config fields
  var autoPushTog = document.getElementById('git-auto-push-toggle');
  if (autoPushTog) autoPushTog.checked = s.git_auto_push || false;
  // Telegram gateway master switch (hivemind_gateway veto, 2026-10-05)
  var tgTog = document.getElementById('tg-enabled-toggle');
  if (tgTog) tgTog.checked = s.telegram_gateway_enabled || false;
  // P10: mirror engine runs to the paired phone
  var tgMirrorTog = document.getElementById('tg-mirror-toggle');
  if (tgMirrorTog) tgMirrorTog.checked = s.telegram_mirror_enabled || false;
  // phone runs restricted to web + text (2026-10-05 safety toggle)
  var tgRestrictTog = document.getElementById('tg-restrict-toggle');
  if (tgRestrictTog) tgRestrictTog.checked = s.telegram_phone_restricted !== false;
  // phone approval policy (ask|deny|off) - /gate and this select write
  // the same setting
  var tgApprSel = document.getElementById('tg-approval-mode-sel');
  if (tgApprSel) tgApprSel.value = s.telegram_approval_mode || 'deny';
  // P8-lite supervisor: heartbeat status + start/stop buttons (2026-10-05)
  function supSet(text, color) {
    var el = document.getElementById('tg-sup-status');
    if (el) { el.textContent = text; el.style.color = color || ''; }
  }
  async function gatewaySupervisorTick() {
    try {
      var st = await (await fetch('/gateway/status')).json();
      var s = document.getElementById('tg-sup-start');
      var x = document.getElementById('tg-sup-stop');
      if (st.running) {
        supSet('● running (pid ' + st.pid +
               (st.paired ? ', paired' : ', unpaired') + ')', '#2e7d32');
        if (s) s.disabled = true;
        if (x) x.disabled = false;
      } else {
        supSet('○ stopped', '#888');
        if (s) s.disabled = false;
        if (x) x.disabled = true;
      }
    } catch (e) {
      supSet('● engine unreachable', '#b3541e');
    }
  }
  var supStart = document.getElementById('tg-sup-start');
  var supStop = document.getElementById('tg-sup-stop');
  var supBusy = false;
  if (supStart) supStart.disabled = true;  // unknown until first tick
  if (supStop) supStop.disabled = true;
  if (supStart) supStart.onclick = async function() {
    if (supBusy) return;
    supBusy = true; supStart.disabled = true; supStop.disabled = true;
    supSet('starting …', '#b3541e');
    try { await fetch('/gateway/start', {method: 'POST'}); } catch (e) {}
    var n = 0;
    var iv = setInterval(async function() {
      n += 1;
      try { await gatewaySupervisorTick(); } catch (e) {}
      if (n >= 12) { clearInterval(iv); supBusy = false; supStart.disabled = false; }
    }, 1000);
  };
  if (supStop) supStop.onclick = async function() {
    if (supBusy) return;
    supBusy = true; supStart.disabled = true; supStop.disabled = true;
    supSet('stopping …', '#b3541e');
    try { await fetch('/gateway/stop', {method: 'POST'}); } catch (e) {}
    var n = 0;
    var iv = setInterval(async function() {
      n += 1;
      try { await gatewaySupervisorTick(); } catch (e) {}
      if (n >= 3) { clearInterval(iv); supBusy = false; supStop.disabled = false; }
    }, 500);
  };
  window.refreshTgSupStatus = gatewaySupervisorTick;
  if (supStart || supStop) {
    gatewaySupervisorTick();
    if (window._tgSupIv) { clearInterval(window._tgSupIv); window._tgSupIv = null; }
    window._tgSupIv = setInterval(function() {
      if (document.hidden) return;
      var _stEl = document.getElementById('tg-sup-status');
      if (_stEl && _stEl.offsetParent !== null) gatewaySupervisorTick();
    }, 5000);
  }
  var emailInp = document.getElementById('git-email');
  if (emailInp) emailInp.value = s.git_email || '';
  var prefixInp = document.getElementById('git-commit-prefix');
  if (prefixInp) prefixInp.value = s.git_commit_prefix || 'hivemind:';
  var defBranchInp = document.getElementById('git-default-branch-inp');
  if (defBranchInp) defBranchInp.value = s.git_default_branch || 'main';
  var searxngInp = document.getElementById('searxng-host-inp');
  if (searxngInp) searxngInp.value = s.searxng_host || 'http://localhost:8888';
  S.searxngHost = s.searxng_host || 'http://localhost:8888';
  var _wsHostEl = document.getElementById('ws-searxng-host');
  if (_wsHostEl) _wsHostEl.textContent = S.searxngHost.replace(/^https?:\/\//i, '');
  updateGitIntegrationUI();
  updateWebsearchHint();
}



