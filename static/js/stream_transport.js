// stream_transport.js — token queue flushing, approval cards/poll, worker pool & perf bar, sendMsg/SSE pump, run-journal reattach & stop/pause controls

function _flushTokenQueueSync() {
  if (!_tokenQueue.length) return;
  if (_tokenRafId) { cancelAnimationFrame(_tokenRafId); _tokenRafId = null; }
  if (!S.curAgent) { _tokenQueue = []; return; }
  var body = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
  if (!body) { _tokenQueue = []; return; }
  while (_tokenQueue.length > 0) {
    var _chunk = _tokenQueue.shift();
    if (S.curAgent._inThink) {
      S.curAgent._thinkBuf = (S.curAgent._thinkBuf || '') + _chunk;
    } else {
      _liveMdAppend(body, _chunk);
    }
  }
  if (body._liveDirty) { body._liveDirty = false; _liveMdRender(body, true); }
}

function _tokenFlushLoop() {
  _tokenRafId = null;
  if (!S.curAgent) { _tokenQueue = []; return; }
  // SPEAK-BUTTON-REVEAL (2026-10-18): the button became visible only via
  // loadSettings, i.e. never during a run. Throttled to 4/s here.
  if (typeof _speakSyncSoon === 'function') _speakSyncSoon();
  var body = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
  if (!body) { _tokenQueue = []; return; }

  // LIVE-MD: streaming text goes into the live-markdown buffer (body._liveRaw),
  // rendered at the end of the frame by _liveMdRender. Segments are reset by
  // _createThinkBlock, tool_call, tool_result (like the old _textRun).
  var count = 0;
  while (_tokenQueue.length > 0 && count < _TOKEN_PER_FRAME) {
    var _chunk = _tokenQueue.shift();
    // Think-block routing (same logic as before)
    var combined = (S.curAgent._thinkBuf || '') + _chunk;
    var thinkBlockEl = document.getElementById('tk-' + S.curAgent.tid);
    var thinkBodyEl  = document.getElementById('thb-' + S.curAgent.tid);
    if (!S.curAgent._inThink && combined.indexOf('<think>') >= 0) {
      S.curAgent._inThink = true;
      S.curAgent._thinkShown = 0;
      var before = combined.split('<think>')[0];
      if (before) _liveMdAppend(body, before);
      S.curAgent._thinkBuf = '<think>' + combined.split('<think>').slice(1).join('<think>');
      if (thinkBlockEl) {
        thinkBlockEl.style.display = '';
        thinkBlockEl.classList.add('live');
        if (thinkBlockEl.dataset.userToggled !== '1') thinkBlockEl.classList.add('open');
      }
      if (thinkBodyEl) thinkBodyEl._followBottom = true;
    } else if (S.curAgent._inThink) {
      S.curAgent._thinkBuf = combined;
      if (thinkBodyEl) {
        var visibleThink = combined.replace(/^<think>/i, '').split('</think>')[0];
        // PERF (2026-10-23): append the delta on a persistent text node — the
        // per-frame whole-block textContent rewrite copied the full think text
        // every frame (O(n²) over long reasoning streams).
        var _shown = S.curAgent._thinkShown || 0;
        if (visibleThink.length > _shown) {
          var _tn = thinkBodyEl.firstChild;
          if (!(_tn && _tn.nodeType === Node.TEXT_NODE)) {
            thinkBodyEl.textContent = '';
            _tn = document.createTextNode('');
            thinkBodyEl.appendChild(_tn);
          }
          _tn.appendData(visibleThink.slice(_shown));
          S.curAgent._thinkShown = visibleThink.length;
        }
        var tktEl = document.getElementById('tkt-' + S.curAgent.tid);
        if (tktEl) {
          var _nowTk = Date.now();
          if (!S.curAgent._tktAt || _nowTk - S.curAgent._tktAt >= 500) {
            S.curAgent._tktAt = _nowTk;
            tktEl.textContent = ' · ' + visibleThink.length + ' chars';
          }
        }
        if (_chatFollow) _scrollElToBottomSoon(thinkBodyEl);
      }
      if (combined.indexOf('</think>') >= 0) {
        S.curAgent._inThink = false;
        var after = combined.split('</think>').slice(1).join('</think>');
        S.curAgent._thinkBuf = '';
        if (thinkBlockEl) thinkBlockEl.classList.remove('live', 'open');
        // THINK-CODE-FORMAT (2026-09-17): on close, render ``` fences in the
        // reasoning text as real code blocks (plain textContent otherwise).
        if (thinkBodyEl) _renderThinkCodeBlocks(thinkBodyEl);
        if (after) _liveMdAppend(body, after);
      }
    } else {
      _liveMdAppend(body, _chunk);
    }
    count++;
  }

  if (count > 0) {
    scrollBtmIfNearBottom(120);
    if (body._liveDirty) { body._liveDirty = false; _liveMdRender(body); }
  }

  if (_tokenQueue.length > 0) {
    _tokenRafId = requestAnimationFrame(_tokenFlushLoop);
  }
}

function appendToken(t) {
  if (!S.curAgent) return;
  t = _stripToolCallXmlChunk(t);
  t = _sanitizeRuntimeToken(t);
  // WHITESPACE-GUARD (2026-09-16): chunks that are ONLY a space used to be
  // dropped here — llama.cpp splits streams at spaces before digits, so
  // "September" + " " + "2026" rendered as "September2026". A whitespace
  // chunk is meaningful; drop only null/empty.
  if (t === null || t === undefined || t === "") return;

  // Clear load indicator on first token
  if (S.curAgent.lt) {
    clearTimeout(S.curAgent.lt);
    S.curAgent.lt = null;
    var lh = document.getElementById('lh-' + S.curAgent.tid);
    if (lh && lh._loadTimer) { clearInterval(lh._loadTimer); lh._loadTimer = null; }
  }

  if (!S.curAgent._thinkBuf) S.curAgent._thinkBuf = '';
  if (!S.curAgent._inThink)  S.curAgent._inThink  = false;
  if (!S.curAgent._toolXmlCarry) S.curAgent._toolXmlCarry = '';

  _tokenQueue.push(t);
  if (!_tokenRafId) {
    _tokenRafId = requestAnimationFrame(_tokenFlushLoop);
  }
}

function doneAgent(elapsed) {
  if (!S.curAgent) return;
  if (S.curAgent.lt) { clearTimeout(S.curAgent.lt); S.curAgent.lt = null; }

  // Clear loading timer interval to prevent leak
  var _lhEl = document.getElementById('lh-' + S.curAgent.tid);
  if (_lhEl && _lhEl._loadTimer) { clearInterval(_lhEl._loadTimer); _lhEl._loadTimer = null; }

  // Flush remaining queued tokens instantly (don't animate leftovers after done)
  if (_tokenRafId) { cancelAnimationFrame(_tokenRafId); _tokenRafId = null; }
  if (_tokenQueue.length > 0) {
    var _body = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
    if (_body) {
      while (_tokenQueue.length > 0) {
        var _t = _tokenQueue.shift();
        if (S.curAgent._inThink) {
          S.curAgent._thinkBuf = (S.curAgent._thinkBuf || '') + _t;
        } else {
          _liveMdAppend(_body, _t);
        }
      }
    }
    _tokenQueue = [];
  }

  if (S.curAgent._thinkBuf) {
    const body = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
    var flushed = S.curAgent._thinkBuf.replace(/^<think>/i, '');
    if (flushed.indexOf('</think>') >= 0) {
      var afterClose = flushed.split('</think>').slice(1).join('</think>').trim();
      if (body && afterClose) {
        _liveMdAppend(body, afterClose);
      }
    } else if (!S.curAgent._inThink && body && flushed.trim()) {
      _liveMdAppend(body, flushed);
    }
    S.curAgent._thinkBuf = '';
    S.curAgent._inThink  = false;
  }
  // Reset text run so next agent starts with fresh TextNode
  var _bodyClean = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
  if (_bodyClean) { _liveMdRender(_bodyClean, true); _liveMdReset(_bodyClean); }
  S.curAgent._toolXmlCarry = '';
  // THINKING-STREAM FINALIZE: close the think block if filled via thinking_token events
  // (in contrast to <[think]>-tag-based streaming via _inThink/_thinkBuf).
  // Remove the "live" class → think block becomes collapsible, no longer auto-open.
  {
    var _tkBlock = document.getElementById('tk-' + S.curAgent.tid);
    if (_tkBlock) {
      _tkBlock.classList.remove('live');
      // if the think block has content: leave it open so the user can read it
      var _tkBody = document.getElementById('thb-' + S.curAgent.tid);
      if (_tkBody && _tkBody.textContent.trim()) {
        // THINK-CODE-FORMAT: fenced code in reasoning renders as code blocks
        _renderThinkCodeBlocks(_tkBody);
        // show "🧠 Thinking · X chars · ✓" to indicate thinking is complete
        var _tktEl = document.getElementById('tkt-' + S.curAgent.tid);
        if (_tktEl) _tktEl.textContent = ' \u00b7 ' + _tkBody.textContent.length + ' chars \u00b7 \u2713';
      }
    }
  }
  // Also finalize all inline think-blocks inside the body
  var _bodyDone = document.getElementById('ab-' + S.curAgent.tid);
  if (_bodyDone) {
    _bodyDone.querySelectorAll('.think-block').forEach(function(_tb) {
      _tb.classList.remove('live');
      // INLINE-THINK-COLLAPSE (2026-10-17): inline reasoning used to stay
      // OPEN forever, so a coder run with several tool rounds was a wall of
      // expanded reasoning. Collapse it like any other block - the header
      // keeps the char count and one click opens the (now formatted) text.
      _tb.classList.remove('open');
      var _tbt = _tb.querySelector('.th-tokens');
      if (_tbt) _tbt.textContent = (_tbt.textContent || '').replace(' \u2713', '') + ' \u2713';
      // INLINE-THINK-FORMAT (2026-10-17): the reasoning that arrives BETWEEN
      // tool calls goes through _createThinkBlock() and was the only path that
      // never got the fence formatting. The main streaming block did get it
      // (_renderThinkCodeBlocks above), so the very same reasoning looked raw
      // inside the coder's expanded tool-call flow (owner report: "fine
      // while streaming, broken once the tool call expands").
      var _tbb = _tb.querySelector('.think-body');
      if (_tbb && _tbb.textContent.trim()) {
        _renderThinkCodeBlocks(_tbb);
        if (_tbt) {
          _tbt.textContent = ' \u00b7 ' + _tbb.textContent.length + ' chars \u00b7 \u2713';
        }
      }
    });
  }
  // ── Markdown finalization for coder bubbles ──────────────────────
  // Converts **bold**, `code`, headers etc. in direct text nodes.
  // Skips tool-call-rows and tool-result-blocks (they are element nodes).
  // CHAT-UI-FIX (2026-08-07): no longer coupled to the agent name —
  // after the first tool call the header says "🛠️ Execution" and the
  // intermediate text between tool calls otherwise stayed unformatted forever.
  var _mdBody = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
  if (_mdBody) { try { _applyMdToBody(_mdBody); } catch(e) {} }
  // hide the loading indicator
  const lh = document.getElementById('lh-' + S.curAgent.tid);
  if (lh) lh.style.display = 'none';
  S.curAgent.body.classList.remove('live');
  if (typeof _syncSpeakButtons === 'function') _syncSpeakButtons(S.curAgent.body.closest('.ablock'));
  const t = document.getElementById(S.curAgent.tid);
  if (t) {
    // Prefer server-reported decode_tps (usage_meta arrives before agent_done);
    // S.perfTokRate is only a rough client token-arrival EMA until `done`.
    var _tokRate = (S.perfDecodeEma > 0) ? Number(S.perfDecodeEma) : Number(S.perfTokRate || 0);
    t.textContent = elapsed + 's' + (_tokRate > 0 ? ' \u00b7 ' + _tokRate.toFixed(1) + ' t/s' : '');
  }
  const name = (S.curAgent.name || '').toLowerCase();
  if (S.constraintMode && (name.indexOf('critic') >= 0 || name.indexOf('kritiker') >= 0)) {
    renderCriticConstraints(S.curAgent.body);
  }
  // THINK-BLOCK-FIX: agent done → reset the think-block reference,
  // so the next agent gets a fresh 🧠 block.
  S._thinkBlockId = null;
  S.curAgent = null;
}

function renderCriticConstraints(bodyEl) {
  // Tune format: ERR:/MISS:/FIX:/CONTRA: lines → constraint cards
  // Fallback: old JSON structure (critic_tune via SSE or free text)
  const PREFIX_MAP = {
    'ERR:'    : 'Logical error',
    'MISS:'   : 'Ignored',
    'FIX:'    : 'Mandatory fix',
    'CONTRA:' : 'Contradiction'
  };
  const sections = {};
  let hasTune = false;

  // Tune-Format: aus bodyEl._criticTune (SSE) oder textContent
  const raw = (bodyEl._criticTune || bodyEl.textContent || '').trim();
  raw.split('\n').forEach(function(line) {
    line = line.trim();
    for (const prefix in PREFIX_MAP) {
      if (line.startsWith(prefix)) {
        const text = line.slice(prefix.length).trim();
        if (text) {
          if (!sections[prefix]) sections[prefix] = [];
          sections[prefix].push(text);
          hasTune = true;
        }
        break;
      }
    }
  });

  if (!hasTune) return;

  bodyEl.textContent = '';
  const box = document.createElement('div'); box.className = 'critic-box';
  const hdr = document.createElement('div'); hdr.className = 'critic-box-hdr';
  hdr.textContent = '\u25B2 Tune-Constraints \u2014 Adversarial Feedback Loop';
  box.appendChild(hdr);

  Object.keys(PREFIX_MAP).forEach(function(prefix) {
    const items = sections[prefix];
    if (!items || !items.length) return;
    const s  = document.createElement('div'); s.className = 'critic-sec';
    const kd = document.createElement('div'); kd.className = 'critic-sec-key';
    kd.textContent = PREFIX_MAP[prefix];
    s.appendChild(kd);
    items.forEach(function(text) {
      const id = document.createElement('div'); id.className = 'critic-item';
      id.textContent = text;
      s.appendChild(id);
    });
    box.appendChild(s);
  });
  bodyEl.parentNode.insertBefore(box, bodyEl.nextSibling);
}


// ACTION-APPROVAL card builder + recovery poll (2026-09-18).
// The approval_request SSE event is fire-and-forget: a page reload during
// the pause loses the card while the server keeps waiting. The poll asks
// the server for pending approvals while a run streams and re-renders the
// card — server truth wins.
// ARG-FEED (2026-09-19): gated tool args stream into the staged approval
// card (shows WHICH file / WHICH command early) + instant-clear tombstones
// against ghost re-renders from the pending poll.
var _apprGenBuf = '';
var _apprGenTool = '';
var _apprClearedAt = 0;
var _askClearedAt = 0;

// APPROVAL-CARD AUTO TIMER (2026-10-01): checkbox + visible countdown on
// every approval card. With duo_action_approval_timeout_s > 0 the SERVER
// runs the countdown (DENIES once after N s, fail-closed — robust even
// when the browser is closed) and the card mirrors it; unchecking posts
// auto_timeout_off which cancels the server countdown ("wait for me").
// With the setting off the user can still check the box: the card then
// counts down itself and posts an explicit approve-once at 0.
function _apAutoBox(card, d) {
  var _n = parseInt(d.auto_timeout_s, 10) || 0;
  var _box = card.querySelector('[data-ap-auto]');
  if (_box) {  // card already has the box (staged->pause update-in-place)
    var _prev = card._apAutoServer || 0;
    card._apAutoServer = _n;
    if (_n > 0 && !_prev && card._apAutoStart) {
      var _cb2 = _box.querySelector('input');
      if (_cb2 && !_cb2.checked) { _cb2.checked = true; card._apAutoStart(_n); }
    }
    return;
  }
  var _row = document.createElement('label');
  _row.setAttribute('data-ap-auto', '1');
  _row.style.cssText = 'display:flex;gap:6px;align-items:center;font-size:10px;color:var(--tx2);margin-bottom:8px;cursor:pointer;user-select:none';
  var _cb = document.createElement('input');
  _cb.type = 'checkbox';
  _cb.checked = _n > 0;
  var _lbl = document.createElement('span');
  _row.appendChild(_cb);
  _row.appendChild(_lbl);
  card._apAutoServer = _n;
  var _setLbl = function(txt) { _lbl.textContent = txt; };
  var _stop = function() {
    if (card._apAutoInt) { clearInterval(card._apAutoInt); card._apAutoInt = null; }
  };
  var _tickLbl = function(left) {
    _setLbl((card._apAutoServer || 0) > 0
      ? 'denying in ' + left + 's (nobody answered)'
      : 'auto-approving once in ' + left + 's (you enabled this)');
  };
  card._apAutoStart = function(n2) {
    var _left = n2 || 30;
    _stop();
    _tickLbl(_left);
    card._apAutoInt = setInterval(function() {
      if (!card.isConnected) { _stop(); return; }
      _left--;
      if (_left <= 0) {
        _stop();
        _setLbl((card._apAutoServer || 0) > 0 ? 'denied (no answer)' : 'auto-approved');
        if ((card._apAutoServer || 0) > 0) return;  // the server denies itself (fail closed)
        var _rid = card.dataset.runId || S.currentRunId || '';
        card.dataset.apLocked = '1';
        fetch('/approval/decide/' + encodeURIComponent(_rid), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            answer: '1|auto-approved (card timer)',
            decision_id: (card.dataset.decisionId || ''),
            tool: (card.dataset.tool || ''),
            approval_key: (card.dataset.approvalKey || '')
          })
        }).then(function(resp) {
          return resp && typeof resp.json === 'function' ? resp.json().catch(function() { return {}; }) : {};
        }).then(function(rj) {
          if (rj && rj.routed === 'awaiting_args') {
            card.dataset.apLocked = '';
            return;
          }
          var _gone = document.getElementById('approval-request-card');
          if (_gone) { _apprClearedAt = Date.now(); _gone.remove(); }
        }).catch(function(e) { if (window._showErrorToast) window._showErrorToast('Approval failed: ' + e); });
        return;
      }
      _tickLbl(_left);
    }, 1000);
  };
  _cb.onchange = function() {
    if (_cb.checked) { card._apAutoStart(card._apAutoServer || 30); return; }
    _stop();
    _setLbl('waiting for your decision');
    var _rid = card.dataset.runId || S.currentRunId || '';
    fetch('/approval/decide/' + encodeURIComponent(_rid), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_timeout_off: true })
    }).catch(function(e) { if (window._showErrorToast) window._showErrorToast('Auto-timer off failed: ' + e); });
  };
  if (_cb.checked) card._apAutoStart(_n); else _setLbl('waiting for your decision');
  var _firstInput = card.querySelector('input');
  if (_firstInput) card.insertBefore(_row, _firstInput); else card.appendChild(_row);
}

function _renderApprovalCard(d) {
  // READ ALOUD: the defensive gate - what wants to run and why it is gated.
  setTimeout(function () {
    var _el = document.getElementById('approval-request-card');
    if (_el) _readAloud('gate', _el.innerText);
  }, 60);
  var _runId = d.run_id || S.currentRunId || '';
  var _isArgsPending = !d.approval_key && !d.decision_id && /^\(generating\b/i.test(String(d.preview || '').trim());
  var _apOld = document.getElementById('approval-request-card');
  // UPDATE-IN-PLACE (2026-09-18): the card is emitted twice per gated call
  // (staged the moment the tool name streams in + again when the execution
  // gate pauses). Rebuilding wiped typed notes and re-enabled dimmed
  // buttons mid-click ("weird reload"). Now: same run -> patch label and
  // preview only; a locked card (decision already being sent) is never
  // rebuilt.
  if (_apOld) {
    if (_apOld.dataset.apLocked === '1') return;
    if (_apOld.dataset.runId === _runId) {
      var _lblUp = _apOld.querySelector('[data-ap-label]');
      var _preUp = _apOld.querySelector('[data-ap-preview]');
      if (_lblUp) _lblUp.textContent = '\uD83D\uDEE1 ' + (d.tool || 'tool') + ' wants to run. Your approval is needed';
      if (_preUp && _preUp.textContent !== (d.preview || '')) _preUp.textContent = d.preview || '';
      if (d.tool) _apOld.dataset.tool = d.tool;
      if (d.approval_key) _apOld.dataset.approvalKey = d.approval_key;
      if (d.decision_id) _apOld.dataset.decisionId = d.decision_id;
      if (!_isArgsPending) {
        _apOld.dataset.apStaged = '';  // real preview known — the arg feed stops
        _apOld.querySelectorAll('button[data-ap-approve="1"]').forEach(function(btn) {
          btn.disabled = false;
          btn.style.opacity = '';
          btn.title = '';
        });
      }
      _apAutoBox(_apOld, d);  // pause emit carries auto_timeout_s — start countdown
      return;
    }
    _apOld.remove();
  }
  _apprGenBuf = '';
  _apprGenTool = d.tool || '';
  var _ap = document.createElement('div');
  _ap.id = 'approval-request-card';
  _ap.className = 'msg divider';
  _ap.dataset.runId = _runId;
  _ap.dataset.tool = d.tool || '';
  _ap.dataset.approvalKey = d.approval_key || '';
  _ap.dataset.decisionId = d.decision_id || '';
  _ap.dataset.apStaged = _isArgsPending ? '1' : '';
  _ap.style.cssText = 'border-color:rgba(240,173,78,.4);background:rgba(240,173,78,.06);padding:9px 13px';
  var _apLbl = document.createElement('div');
  _apLbl.setAttribute('data-ap-label', '1');
  _apLbl.style.cssText = 'font-size:12px;color:#f0ad4e;font-weight:600;margin-bottom:6px';
  _apLbl.textContent = '\uD83D\uDEE1 ' + (d.tool || 'tool') + ' wants to run. Your approval is needed';
  var _apPre = document.createElement('pre');
  _apPre.setAttribute('data-ap-preview', '1');
  _apPre.style.cssText = 'font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#cfe3ff;background:rgba(0,0,0,.3);border-radius:4px;padding:6px 8px;margin:0 0 8px;max-height:130px;overflow:auto;white-space:pre-wrap;word-break:break-all';
  _apPre.textContent = d.preview || '';
  var _apNote = document.createElement('input');
  _apNote.type = 'text';
  _apNote.placeholder = 'Optional message to the agent (guidance / reason for deny)\u2026';
  _apNote.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--b2);border-radius:4px;padding:5px 8px;color:var(--tx);font-size:11px;margin-bottom:8px';
  // EXACT-MATCH memory: writes remember the FILE, commands the exact call
  var _isWrite = /^(write_file|edit_file|write_file_append)$/.test(d.tool || '');
  var _apRow = document.createElement('div');
  _apRow.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap';
  [['1', '\u2713 Approve once', '#22c55e'],
   ['2', _isWrite ? '\u2713 Approve this file (remember)' : '\u2713 Approve this exact call (remember)', '#60a0e0'],
   ['3', '\u2715 Deny', '#e06060'],
   ['0', '\u2709 Input only', '#c8a04a']].forEach(function(b) {
    var btn = document.createElement('button');
    btn.className = 'ghost';
    btn.style.cssText = 'font-size:10px;padding:5px 10px;border:1px solid ' + b[2] + '55;color:' + b[2];
    btn.textContent = b[1];
    if (b[0] === '1' || b[0] === '2') {
      btn.dataset.apApprove = '1';
      if (_isArgsPending) {
        btn.disabled = true;
        btn.style.opacity = .45;
        btn.title = 'Waiting for full arguments before approval\u2026';
      }
    }
    btn.onclick = function() {
      var _n = (_apNote.value || '').trim();
      if (b[0] === '0' && !_n) { _apNote.focus(); _apNote.style.borderColor = '#e06060'; return; }
      _apRow.querySelectorAll('button').forEach(function(x) { x.disabled = true; x.style.opacity = .45; });
      btn.style.opacity = 1;
      _ap.dataset.apLocked = '1';  // poll/pause re-emits must not rebuild now
      var _ans = b[0];
      if (_n) _ans += '|' + _n;
      // decide endpoint routes by state: resolves an active pause OR stores
      // the pre-decision while the content is still generating
      fetch('/approval/decide/' + encodeURIComponent(d.run_id || S.currentRunId || ''), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          answer: _ans,
          decision_id: (_ap.dataset.decisionId || d.decision_id || ''),
          tool: (_ap.dataset.tool || d.tool || ''),
          approval_key: (_ap.dataset.approvalKey || d.approval_key || '')
        })
      }).then(function(resp) {
        return resp && typeof resp.json === 'function' ? resp.json().catch(function() { return {}; }) : {};
      }).then(function(rj) {
        if (rj && rj.routed === 'awaiting_args') {
          _ap.dataset.apLocked = '';
          _apRow.querySelectorAll('button').forEach(function(x) {
            if (x.dataset.apApprove === '1' && _ap.dataset.apStaged === '1') {
              x.disabled = true;
              x.style.opacity = .45;
            } else {
              x.disabled = false;
              x.style.opacity = '';
            }
          });
          return;
        }
        // INSTANT-CLEAR (2026-09-19): die Entscheidung ist registriert — die
        // Card fliegt sofort, nicht erst wenn agent_resumed/poll dazu kommen
        // (der naechste Prefill kann auf langsamer Hardware Minuten dauern).
        var _gone = document.getElementById('approval-request-card');
        if (_gone && _gone.dataset.apLocked === '1') { _apprClearedAt = Date.now(); _gone.remove(); }
      }).catch(function(e) {
        _ap.dataset.apLocked = '';
        _apRow.querySelectorAll('button').forEach(function(x) {
          if (x.dataset.apApprove === '1' && _ap.dataset.apStaged === '1') {
            x.disabled = true;
            x.style.opacity = .45;
          } else {
            x.disabled = false;
            x.style.opacity = '';
          }
        });
        if (window._showErrorToast) window._showErrorToast('Approval failed: ' + e);
      });
    };
    _apRow.appendChild(btn);
  });
  _ap.appendChild(_apLbl);
  _ap.appendChild(_apPre);
  _apAutoBox(_ap, d);
  _ap.appendChild(_apNote);
  _ap.appendChild(_apRow);
  document.getElementById('chat').appendChild(_ap);
  scrollBtmIfNearBottom(120);
  _apEnsureBottomPin();
}

// ASK-CARD (2026-09-19): the ask_user twin of the approval card — same
// lifecycle (pinned at the content end, update in place, lock while the
// answer is in flight, cleared on resume/done), different buttons: free
// text answer back to the model, or "proceed autonomously".
function _renderAskCard(d) {
  // READ ALOUD: speak the question once the card is in the DOM (the payload
  // field differs between the ask-user and the tool-approval events, the card
  // text never does).
  setTimeout(function () {
    var _el = document.getElementById('ask-user-card');
    if (_el) _readAloud('ask', _el.innerText);
  }, 60);
  var _runId = d.run_id || S.currentRunId || '';
  var _old = document.getElementById('ask-user-card');
  if (_old) {
    if (_old.dataset.apLocked === '1') return;
    if (_old.dataset.runId === _runId) {
      var _qUp = _old.querySelector('[data-ask-q]');
      if (_qUp && _qUp.textContent !== (d.question || d.preview || '')) {
        _qUp.textContent = d.question || d.preview || '';
      }
      return;
    }
    _old.remove();
  }
  var _c = document.createElement('div');
  _c.id = 'ask-user-card';
  _c.className = 'msg divider';
  _c.dataset.runId = _runId;
  _c.dataset.decisionId = d.decision_id || '';
  _c.style.cssText = 'border-color:rgba(96,160,224,.45);background:rgba(96,160,224,.07);padding:9px 13px';
  var _lbl = document.createElement('div');
  _lbl.style.cssText = 'font-size:12px;color:#60a0e0;font-weight:600;margin-bottom:6px';
  _lbl.textContent = '\u2753 The agent asks. Your answer goes back to the model';
  var _q = document.createElement('div');
  _q.setAttribute('data-ask-q', '1');
  _q.style.cssText = "font-family:'IBM Plex Mono',monospace;font-size:11px;color:#cfe3ff;background:rgba(0,0,0,.3);border-radius:4px;padding:6px 8px;margin:0 0 8px;white-space:pre-wrap;word-break:break-word";
  _q.textContent = d.question || d.preview || 'Your input is needed.';
  var _inp = document.createElement('input');
  _inp.type = 'text';
  _inp.placeholder = 'Your answer\u2026';
  _inp.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--b2);border-radius:4px;padding:5px 8px;color:var(--tx);font-size:11px;margin-bottom:8px';
  var _row = document.createElement('div');
  _row.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap';
  function _sendAnswer(_txt) {
    _row.querySelectorAll('button').forEach(function(x) { x.disabled = true; x.style.opacity = .45; });
    _inp.disabled = true;
    _c.dataset.apLocked = '1';
    fetch('/approval/decide/' + encodeURIComponent(d.run_id || S.currentRunId || ''), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: _txt, decision_id: (d.decision_id || '') })
    }).then(function() {
      stopAskUserCountdown();
      // INSTANT-CLEAR (2026-09-19): wie bei der Approval-Card — weg sofort
      // nach registrierter Antwort, nicht erst nach agent_resumed/poll.
      var _gone = document.getElementById('ask-user-card');
      if (_gone && _gone.dataset.apLocked === '1') { _askClearedAt = Date.now(); _gone.remove(); }
    }).catch(function(e) {
      _c.dataset.apLocked = '';
      _row.querySelectorAll('button').forEach(function(x) { x.disabled = false; x.style.opacity = ''; });
      _inp.disabled = false;
      if (window._showErrorToast) window._showErrorToast('Answer failed: ' + e);
    });
  }
  var _send = document.createElement('button');
  _send.className = 'ghost';
  _send.style.cssText = 'font-size:10px;padding:5px 10px;border:1px solid #60a0e088;color:#60a0e0';
  _send.textContent = '\u2713 Send answer';
  _send.onclick = function() {
    var _t = (_inp.value || '').trim();
    if (!_t) { _inp.focus(); _inp.style.borderColor = '#e06060'; return; }
    _sendAnswer(_t);
  };
  // YES/NO BUTTONS (2026-10-07, user feedback: git fields must not reset on save; revised
  // same day: own row with Yes / No / Wait-later, the text field stays
  // pure text). "Wait later" removes only the buttons - the run keeps
  // waiting and the free-text answer below stays available anytime.
  var _askText = (d.question || d.preview || '').toLowerCase();
  var _yrow = null;
  if (_askText.indexOf('yes') !== -1 && _askText.indexOf('no') !== -1 &&
      (_askText.indexOf('confirm') !== -1 || _askText.indexOf('reply with') !== -1)) {
    _yrow = document.createElement('div');
    _yrow.style.cssText = 'display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap';
    var _yBtn = document.createElement('button');
    _yBtn.className = 'ghost';
    _yBtn.style.cssText = 'font-size:11px;padding:5px 16px;border:1px solid #4dcf7f88;color:#4dcf7f';
    _yBtn.textContent = '\u2705 Yes';
    _yBtn.onclick = function() { _sendAnswer('yes'); };
    var _nBtn = document.createElement('button');
    _nBtn.className = 'ghost';
    _nBtn.style.cssText = 'font-size:11px;padding:5px 16px;border:1px solid #e0606088;color:#e06060';
    _nBtn.textContent = '\u26D4 No';
    _nBtn.onclick = function() { _sendAnswer('no'); };
    var _wBtn = document.createElement('button');
    _wBtn.className = 'ghost';
    _wBtn.style.cssText = 'font-size:11px;padding:5px 16px;border:1px solid var(--b2);color:var(--tx2)';
    _wBtn.textContent = '\u23F3 Wait later';
    _wBtn.title = 'Keep the run waiting - answer with text below anytime';
    _wBtn.onclick = function() {
      if (_yrow && _yrow.parentNode) _yrow.remove();
    };
    _yrow.appendChild(_yBtn); _yrow.appendChild(_nBtn); _yrow.appendChild(_wBtn);
  }
  var _skip = document.createElement('button');
  _skip.className = 'ghost';
  _skip.style.cssText = 'font-size:10px;padding:5px 10px;border:1px solid #f0ad4e55;color:#f0ad4e';
  _skip.textContent = '\u23E9 Proceed autonomously';
  _skip.onclick = function() { _sendAnswer('(no user input, proceed autonomously)'); };
  // AUTO-PROCEED (2026-09-19; A5 2026-11-08): the toggle persists the global
  // setting. The ENGINE governor performs the 30 s auto-answer; the card no
  // longer runs its own timer (two sources raced on the same question).
  var _tg = document.createElement('label');
  _tg.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:10px;color:var(--tx2);border:1px solid var(--b2);border-radius:4px;padding:5px 10px;cursor:pointer;user-select:none';
  // W2-3: this toggle is the SAME global setting as the settings row next
  // to Action Approval (ask_user_auto_proceed); the engine auto-answers
  // after 30 s when it is on.
  _tg.title = 'Global setting (Settings \u2192 Action Approval gate): the engine auto-answers ask_user after 30 s. until-finished runs keep their configured timeout.';
  var _tgCb = document.createElement('input');
  _tgCb.type = 'checkbox';
  _tgCb.className = 'ask-aup-cb';
  _tgCb.checked = !!S.askUserAutoProceed;
  var _tgTxt = document.createElement('span');
  _tgTxt.textContent = 'Auto 30s';
  var _tgTm = document.createElement('span');
  _tgTm.className = 'ask-aup-tm';
  _tgTm.style.cssText = "color:#f0ad4e;font-family:'IBM Plex Mono',monospace";
  _tg.appendChild(_tgCb); _tg.appendChild(_tgTxt); _tg.appendChild(_tgTm);
  // A5 (2026-11-08): the ENGINE governor is the single auto-answer source.
  // The card used to run its own 30 s interval and could race the engine's
  // 30 s timeout (two answers for one question). The checkbox now only
  // toggles the global setting; when on, the engine answers after 30 s.
  function _syncTimerLabel() {
    _tgTm.textContent = _tgCb.checked ? 'engine 30s' : '';
  }
  _tgCb.onchange = function() {
    _syncAskUserAutoProceedUI(_tgCb.checked);
    // Persisted as a setting: survives reloads and is part of presets
    // (the preset save snapshots settings.json).
    postSettings({ ask_user_auto_proceed: _tgCb.checked });
    _syncTimerLabel();
  };
  _syncTimerLabel();
  _inp.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); _send.click(); }
  });
  _row.appendChild(_send);
  _row.appendChild(_skip);
  _row.appendChild(_tg);
  _c.appendChild(_lbl);
  _c.appendChild(_q);
  if (_yrow) _c.appendChild(_yrow);
  _c.appendChild(_inp);
  _c.appendChild(_row);
  document.getElementById('chat').appendChild(_c);
  scrollBtmIfNearBottom(120);
  _apEnsureBottomPin();
  _inp.focus();
}

// BOTTOM-PIN (2026-09-19): a pending card (approval or ask_user) lives at
// the CONTENT END of #chat — like the last tool-call row. A MutationObserver
// re-appends it whenever new chat rows land after it, so it stays the
// bottom-most entry while pending; it scrolls normally with the chat
// instead of overlaying content (the sticky attempt did that — rejected).
var _apPinObserver = null;
function _apEnsureBottomPin() {
  var _chat = document.getElementById('chat');
  if (!_chat) return;
  var _pinAll = function() {
    // BACK-TO-BACK (2026-09-19): when both cards are pending (ask_user
    // paused plus a later write already staged), they sit adjacently at
    // the content end instead of stacking somewhere in the flow.
    var _pend = ['approval-request-card', 'ask-user-card']
      .map(function(id) { return document.getElementById(id); })
      .filter(Boolean);
    if (!_pend.length) {
      if (_apPinObserver) { _apPinObserver.disconnect(); _apPinObserver = null; }
      return;
    }
    var _tail = [].slice.call(_chat.children, -_pend.length);
    var _inPlace = _tail.length === _pend.length &&
      _pend.every(function(_c, i) { return _tail[i] === _c; });
    if (_inPlace) return;
    _pend.forEach(function(_c) { _chat.appendChild(_c); });
    scrollBtmIfNearBottom(120);
  };
  if (!_apPinObserver) {
    _apPinObserver = new MutationObserver(_pinAll);
    _apPinObserver.observe(_chat, { childList: true });
  }
  _pinAll();
}

// POLL-SCOPE (2026-09-19): nicht mehr an S.streaming gekoppelt — ein Reload
// waehrend einer Pause verliert zwar den SSE-Stream, aber der Run wartet
// serverseitig weiter. Die Run-ID ueberlebt im sessionStorage, die Card
// re-rendert und die Entscheidung erreicht das wartende Gate.
if (typeof sessionStorage !== 'undefined') {
  try { S.currentRunId = S.currentRunId || sessionStorage.getItem('hm_run_id') || ''; } catch (e) {}
}
if (typeof document !== 'undefined') {
  setInterval(function() {
  if (document.hidden || !S.currentRunId) return;
  var _pollRunId = S.currentRunId;
  try { sessionStorage.setItem('hm_run_id', _pollRunId); } catch (e) {}
  fetch('/approval/pending/' + encodeURIComponent(_pollRunId))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (_pollRunId !== S.currentRunId) return;
      var existing = document.getElementById('approval-request-card');
      var existingAsk = document.getElementById('ask-user-card');
      if (d && d.active) {
        // TOMBSTONE (2026-09-19): direkt nach einem Instant-Clear kann das
        // Pending noch kurz aktiv sein (das Gate raeumt erst beim Aufwachen
        // auf). 4s Grace verhindert Geister-Cards, bleibt aber self-healing:
        // ist das Pending echte weiterhin aktiv, kommt die Card zurueck.
        if (d.kind === 'ask') {
          if (!existingAsk && Date.now() - _askClearedAt > 4000) {
            _renderAskCard({ run_id: _pollRunId, question: d.question || d.preview, decision_id: d.decision_id });
          }
        } else if ((!existing && Date.now() - _apprClearedAt > 4000) ||
                   (existing && existing.dataset.runId === _pollRunId &&
                    existing.dataset.apStaged === '1' &&
                    (d.decision_id || d.approval_key))) {
          _renderApprovalCard({
            run_id: _pollRunId,
            tool: d.tool,
            preview: d.preview,
            decision_id: d.decision_id,
            approval_key: d.approval_key,
            auto_timeout_s: d.auto_timeout_s
          });
        }
      } else {
        if (existing && (!existing.dataset.runId || existing.dataset.runId === _pollRunId)) {
          if (existing._apAutoInt) { clearInterval(existing._apAutoInt); existing._apAutoInt = null; }
          existing.remove();
        }
        if (existingAsk && (!existingAsk.dataset.runId || existingAsk.dataset.runId === _pollRunId)) {
          existingAsk.remove();
        }
      }
    }).catch(function() {});
  }, 2000);
}

// PANEL-RESIZE (2026-09-18): drag the code panel's left edge to resize
// (--cp-w on body). Width persists in localStorage.
(function() {
  if (typeof document === 'undefined') return;
  function initCpResize() {
    var h = document.getElementById('cp-resize');
    if (!h || h._wired) return;
    h._wired = true;
    // --cp-w-user is INERT while the panel is closed (only
    // body.code-panel-open consumes it) — a stale width can't leave a gap.
    var saved = parseInt(_hmStorageGet('cp_w', '0') || '0', 10);
    if (saved >= 280 && saved <= 1000) document.body.style.setProperty('--cp-w-user', saved + 'px');
    h.addEventListener('mousedown', function(e) {
      e.preventDefault();
      var startX = e.clientX;
      var startW = parseInt(_hmStorageGet('cp_w', '440') || '440', 10) || 440;
      document.body.classList.add('resizing-col');
      function onMove(ev) {
        var w = Math.max(280, Math.min(1000, startW + (startX - ev.clientX)));
        document.body.style.setProperty('--cp-w-user', w + 'px');
      }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.classList.remove('resizing-col');
        var w = parseInt(getComputedStyle(document.body).getPropertyValue('--cp-w-user'), 10);
        if (w) { _hmStorageSet('cp_w', w); }
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initCpResize);
  else initCpResize();
})();


function _workerBaseModelName(model) {
  return String(model || '').replace(/\s*#\d+\s*$/, '').trim().toLowerCase();
}

function _workerAliasVariant(model) {
  var m = String(model || '').trim().match(/#(\d+)\s*$/);
  if (!m) return null;
  var n = parseInt(m[1], 10);
  if (isNaN(n) || n < 1) return null;
  return n - 1;
}

function _workerVariantIndex(model, seenMap) {
  var aliasVariant = _workerAliasVariant(model);
  if (aliasVariant !== null) return aliasVariant;
  var base = _workerBaseModelName(model) || String(model || '').trim().toLowerCase() || '?';
  if (!seenMap[base]) seenMap[base] = 0;
  var idx = seenMap[base];
  seenMap[base] = idx + 1;
  return idx;
}

function _workerColorForVariant(variantIdx) {
  var palette = [
    { chipBd:'rgba(72,120,192,.35)', chipBg:'rgba(72,120,192,.16)', chipFg:'#8fb0d2', model:'#9fb8d1', port:'#657f98', laneBg:'rgba(6,12,20,.7)' },
    { chipBd:'rgba(58,153,96,.38)', chipBg:'rgba(58,153,96,.16)', chipFg:'#8fc8a9', model:'#9ccfb5', port:'#5f8f74', laneBg:'rgba(5,14,11,.76)' },
    { chipBd:'rgba(214,140,56,.40)', chipBg:'rgba(214,140,56,.14)', chipFg:'#d9b07a', model:'#e1bb87', port:'#997549', laneBg:'rgba(17,12,6,.76)' },
    { chipBd:'rgba(176,96,144,.42)', chipBg:'rgba(176,96,144,.15)', chipFg:'#d2a2c1', model:'#dcb0c9', port:'#946983', laneBg:'rgba(15,9,14,.76)' },
    { chipBd:'rgba(96,164,176,.40)', chipBg:'rgba(96,164,176,.14)', chipFg:'#9cc6d0', model:'#a9d2dc', port:'#6d9199', laneBg:'rgba(7,12,14,.76)' },
    { chipBd:'rgba(132,112,196,.40)', chipBg:'rgba(132,112,196,.14)', chipFg:'#baaddc', model:'#c6bbe5', port:'#8476a7', laneBg:'rgba(9,10,17,.76)' }
  ];
  var idx = parseInt(variantIdx, 10);
  if (isNaN(idx)) idx = 0;
  idx = Math.abs(idx) % palette.length;
  return palette[idx];
}

function _applyWorkerPoolStateToUi() {
  var _st = S.prexPoolState;
  if (!_st) return;
  var _pf = document.getElementById('prex-fail');
  if (!_pf) return;

  var _target = parseInt(_st.target_workers || '0', 10);
  var _active = parseInt(_st.active_workers || '0', 10);
  var _missing = Array.isArray(_st.missing) ? _st.missing : [];
  if (!isFinite(_target) || _target <= 0) return;

  if (_missing.length > 0) {
    _pf.style.display = 'inline-flex';
    _pf.dataset.n = String(_missing.length);
    _pf.textContent = '⚠ active ' + _active + '/' + _target;
    _pf.title = _missing.map(function(m) {
      return String(m.model || '?') + ': ' + String(m.reason || 'unknown');
    }).join('\n');
  } else {
    _pf.style.display = 'none';
    _pf.dataset.n = '0';
    _pf.title = '';
  }
}

function _perfResetRuntimeState() {
  var _now = Date.now();
  S.perfRunStartedAt = _now;
  S.perfFirstTokenAt = 0;
  S.perfActiveMs = 0;
  S.perfLastTokAt = 0;
  S.perfDecodeEma = 0;
  S.perfToolCalls = 0; S.perfToolMs = 0;
  S.perfToolLabel = ''; S.perfToolStart = 0; S.perfLastToolTxt = '--';
  S.perfChars = 0;
  S.perfEstTokens = 0;
  S.perfRealTokens = 0;
  // TOKEN-TRACKER UI (2026-08-25): Run-scope Akkumulatoren zuruecksetzen.
  S.runPromptTokens = 0;
  S.runCachedTokens = 0;
  S.runRequestCount = 0;
  S.perfCtxLimit = 0;
  S.perfCtxPct = 0;
  S.perfCtxReal = 0;
  S.perfCtxCached = 0;
  S.perfTokRate = 0;
  S.perfTokRateEma = 0;
  S.perfCompressing = false;
}

// ── GROSSE PERF-BAR UNTEN (AUDIT-R2+ 2026-08-25) ─────────────────────────────
function _pbbEnsure() {
  var _bar = document.getElementById('perf-bar-bottom');
  if (!_bar || _bar.dataset.built) return;
  _bar.dataset.built = '1';
  _bar.innerHTML =
    '<div id="pbb-head" onclick="_pbbToggle()" title="Collapse/expand">'
      + '<span class="pbb-chev">\u25be</span><span id="pbb-summary">--</span></div>'
    + '<div id="pbb-body">'
      + '<div class="ctx-perf">'
        + '<span class="ctx-perf-item live" id="pbb-ctx" data-tip-title="Context window" data-tip="How much of the model&#39;s context this run has used, and its share of the window. &#39;est&#39; means the number was estimated from characters before the server reported real token counts." role="note" tabindex="0">ctx: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-ctxreuse" data-tip-title="Prefix cache reuse" data-tip="Share of the prompt the server could keep from its cache instead of reading it again. High values mean cheap rounds." role="note" tabindex="0">reuse: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-tok" data-tip-title="Output tokens" data-tip="Tokens generated in this run so far, across all rounds." role="note" tabindex="0">out: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-rate" data-tip-title="Generation rate" data-tip="Tokens per second including prompt processing - the honest end-to-end figure." role="note" tabindex="0">tok/s: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-decode" data-tip-title="Decode rate" data-tip="Tokens per second while the model is writing, prompt processing excluded. This is the number the model itself reports." role="note" tabindex="0">decode: --</span>'

      + '</div>'
      + '<div class="ctx-perf">'
        + '<span class="ctx-perf-item ok" id="pbb-in" data-tip-title="Prompt tokens (last request)" data-tip="Tokens sent with the most recent request, whole conversation included." role="note" tabindex="0">in: --</span>'
        + '<span class="ctx-perf-item ok" id="pbb-cached" data-tip-title="Cached tokens" data-tip="Tokens the server served from its prompt cache instead of computing them again." role="note" tabindex="0">cached: --</span>'
        + '<span class="ctx-perf-item ok" id="pbb-new" data-tip-title="Recomputed tokens" data-tip="Prompt tokens the server had to process again. Large values after a compression or an edited earlier message: that is what makes a round slow." role="note" tabindex="0">recomputed: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-reqs" data-tip-title="Requests" data-tip="Model requests made in this run." role="note" tabindex="0">reqs: --</span>'
      + '</div>'
      + '<div class="ctx-perf">'
        + '<span class="ctx-perf-item live" id="pbb-tools" data-tip-title="Tool calls" data-tip="Tool calls in this run: count, total time, and the tool that is running right now (or the last one)." role="note" tabindex="0">tools: --</span>'
      + '</div></div>';
  try {
    if ((_hmStorageGet('hivemind-pbb-collapsed', '') || '') === '1')
      _bar.classList.add('collapsed');
  } catch (e) {}
}

function _pbbToggle() {
  var _bar = document.getElementById('perf-bar-bottom');
  if (!_bar) return;
  var _collapsed = _bar.classList.toggle('collapsed');
  _hmStorageSet('hivemind-pbb-collapsed', _collapsed ? '1' : '0');
}
if (typeof window !== 'undefined') window._pbbToggle = _pbbToggle;

function _perfEnsureUiRow() {
  // PERF-CONSOLIDATION (2026-08-26): the top sticky bar (#perf-bar with
  // ctx-perf row + thin ctx bar) is gone — ALL performance values
  // now live in the lower, collapsible bar (#perf-bar-bottom).
  _pbbEnsure();
  return document.getElementById('perf-bar-bottom');
}

function _perfRender(finalized) {
  if (!_perfEnsureUiRow()) return;
  var _pct = parseInt(S.perfCtxPct || 0, 10);
  _pct = isNaN(_pct) ? 0 : Math.max(0, Math.min(100, _pct));
  var _tokRate = Number(S.perfTokRate || 0);
  var _realRate = 0;
  if (S.perfRealTokens > 0 && S.perfFirstTokenAt) {
    _realRate = _perfWallRate();
  }
  // SUMMARY (2026-10-07, user feedback: previous display was better): the collapsed
  // line shows the plain wall-clock rate again; the CLEANED rate (active
  // decode time only) lives as its own pill in the expanded details.
  var _rateVal = (finalized && _tokRate > 0) ? _tokRate : (_realRate > 0 ? _realRate : _tokRate);
  var _est = Math.max(0, parseInt(S.perfEstTokens || 0, 10) || 0);
  var _ctx = Math.max(0, parseInt(S.perfCtxLimit || 0, 10) || 0);

  // BIG PERF BAR AT THE BOTTOM (AUDIT-R2+ 2026-08-25): full live dimensions.
  var _pbb = document.getElementById('perf-bar-bottom');
  if (_pbb && _pbb.dataset && _pbb.dataset.built) {
    _pbb.classList.add('visible');
    _pbb.style.display = '';
    _pbb.style.opacity = '';
    var _qId = function (id) { return document.getElementById(id); };
    var _outT = S.perfRealTokens > 0 ? S.perfRealTokens : S.perfEstTokens;
    var _e;
    if ((_e = _qId('pbb-ctx'))) {
      var _ctxTxt = 'ctx: ' + (_ctx > 0 ? (_pct > 0 ? _pct + '%' : '--') : '--');
      if (_ctx > 0 && _pct > 0) {
        var _absC = _est > 0 ? _fmtTokens(_est) + '/' + _fmtTokens(_ctx) : _pct + '%';
        _ctxTxt = 'ctx: ' + _absC + ' (' + _pct + '%)' + (S.perfCtxReal > 0 ? ' \u00b7 real' : ' \u00b7 est');
      }
      _e.textContent = _ctxTxt;
    }
    if ((_e = _qId('pbb-ctxreuse'))) {
      _e.textContent = (S.perfCtxReal > 0 && S.perfCtxReal >= S.perfCtxCached)
        ? 'reuse: ' + Math.round((S.perfCtxCached / S.perfCtxReal) * 100) + '%'
        : 'reuse: --';
    }
    if ((_e = _qId('pbb-tok'))) _e.textContent = 'out: ' + (_outT > 0 ? _fmtTokens(_outT) : '--');
    if ((_e = _qId('pbb-rate'))) _e.textContent = (_rateVal > 0 ? _rateVal.toFixed(1) : '--') + ' t/s';
    // DECODE-RATE (2026-10-07): the cleaned figure comes from the engine
    // (llama.cpp predicted_per_second, EMA over coder calls) - reliable,
    // unlike the earlier event-pulse estimate.
    if ((_e = _qId('pbb-decode'))) {
      var _dv = Number(S.perfDecodeEma || 0);
      if (_dv > 0) {
        _e.textContent = 'decode: ' + _dv.toFixed(1) + ' t/s';
      } else {
        var _liveDec = _perfRealRate() || Number(S.perfTokRate || 0);
        _e.textContent = 'decode: ' + (_liveDec > 0 ? '~' + _liveDec.toFixed(1) + ' t/s' : '--');
      }
    }
    // PERF-CONSOLIDATION: consistent '--' placeholders until the first usage_meta arrives
    if ((_e = _qId('pbb-in'))) _e.textContent = S.runPromptTokens > 0 ? ('in: ' + _fmtTokens(S.runPromptTokens)) : 'in: --';
    if ((_e = _qId('pbb-cached')))
      _e.textContent = S.runPromptTokens > 0
        ? ('cached: ' + _fmtTokens(S.runCachedTokens) + ' (' + Math.round((S.runCachedTokens / S.runPromptTokens) * 100) + '%)')
        : 'cached: --';
    if ((_e = _qId('pbb-new')))
      _e.textContent = S.runPromptTokens > 0
        ? ('recomputed: ' + _fmtTokens(Math.max(0, S.runPromptTokens - S.runCachedTokens)))
        : 'recomputed: --';
    if ((_e = _qId('pbb-reqs'))) _e.textContent = 'reqs: ' + ((S.runRequestCount || 0) > 0 ? S.runRequestCount : '--');
    // TOOL METRICS (2026-10-07, owner): count, total time and the last
    // tool with its duration - live, including a running indicator.
    if ((_e = _qId('pbb-tools'))) {
      var _tTxt = 'tools: --';
      if ((S.perfToolCalls || 0) > 0 || S.perfToolStart > 0) {
        _tTxt = 'tools: ' + (S.perfToolCalls || 0)
          + ' · ' + ((S.perfToolMs || 0) / 1000).toFixed(1) + 's total';
        if (S.perfToolStart > 0) {
          _tTxt += ' · ▶ ' + S.perfToolLabel + ' '
            + ((Date.now() - S.perfToolStart) / 1000).toFixed(1) + 's';
        } else if (S.perfLastToolTxt && S.perfLastToolTxt !== '--') {
          _tTxt += ' · last: ' + S.perfLastToolTxt;
        }
      }
      _e.textContent = _tTxt;
    }
    var _sumEl = _qId('pbb-summary');
    if (_sumEl) {
      // SUMMARY-REDESIGN (2026-08-26): collapsed = ctx ABS/LIMIT (P%) · N t/s · M% cached.
      // reqs deliberately ONLY expanded. Color coding on ctx-% (green <70, amber <90, red ≥90).
      var _parts = [];
      if (_ctx > 0 && _pct > 0) {
        var _absTxt = _est > 0 ? (_fmtTokens(_est) + '/' + _fmtTokens(_ctx)) : String(_pct);
        _parts.push('ctx ' + _absTxt + ' (' + _pct + '%)' + (S.perfCtxReal > 0 ? ' real' : ''));
      } else if (_ctx > 0) {
        _parts.push('ctx --/' + _fmtTokens(_ctx));
      }
      if (_rateVal > 0) _parts.push(_rateVal.toFixed(1) + ' t/s');
      if (S.runPromptTokens > 0)
        _parts.push(Math.round((S.runCachedTokens / S.runPromptTokens) * 100) + '% cached');
      _sumEl.textContent = _parts.length ? _parts.join(' \u00b7 ') : '--';
      _sumEl.className = (_ctx > 0 && _pct > 89) ? 'ctx-bad' : (_ctx > 0 && _pct > 69) ? 'ctx-warn' : (_ctx > 0 ? 'ctx-ok' : '');
    }
  }
}

function _perfOnToken(content) {
  if (!content) return;
  if (!S.perfRunStartedAt) _perfResetRuntimeState();
  var _now = Date.now();
  if (!S.perfFirstTokenAt) { S.perfFirstTokenAt = _now; S.perfLastTokAt = _now; }
  // active-time accumulation now lives in handleEvent (every event type)
  var _chars = String(content).length;
  if (_chars <= 0) return;
  S.perfChars += _chars;
  S.perfEstTokens += Math.max(1, Math.floor(_chars / 3));
  var _elapsed = Math.max(0.25, (_now - S.perfFirstTokenAt) / 1000);
  var _inst = S.perfEstTokens / _elapsed;
  if (!isFinite(S.perfTokRateEma) || S.perfTokRateEma <= 0) S.perfTokRateEma = _inst;
  else S.perfTokRateEma = (S.perfTokRateEma * 0.8) + (_inst * 0.2);
  S.perfTokRate = S.perfTokRateEma;
  // PERF (2026-10-23): the per-agent counter + full bar render ran on EVERY
  // token event (600-1200 DOM writes/s at local decode rates). 4 Hz is plenty
  // for a display; the accumulators above stay per-token.
  var _nowPr = Date.now();
  if (_nowPr - (_perfPaintLastAt || 0) >= 250) {
    _perfPaintLastAt = _nowPr;
    if (S.curAgent) {
      var at = document.getElementById(S.curAgent.tid);
      if (at) {
        var _t = S.perfEstTokens || 0;
        var _r = S.perfTokRate > 0 ? S.perfTokRate.toFixed(1) + ' t/s' : '';
        at.textContent = _t + ' tok' + (_r ? ' \u00b7 ' + _r : '');
      }
    }
    _perfRender(false);
  }
}

var _perfPaintLastAt = 0;

function _perfWallRate() {
  // the "normal" figure: tokens over wall clock since the first token -
  // includes tool runs and processing gaps (honest over the whole run)
  if (!S.perfRealTokens || !S.perfFirstTokenAt) return 0;
  return S.perfRealTokens / Math.max(0.25, (Date.now() - S.perfFirstTokenAt) / 1000);
}

function _perfRealRate() {
  // FIX 2026-10-07: tokens / ACTIVE decode time (wall-clock gaps >= 2 s
  // between token events excluded). Falls back to 0 while no active
  // time has accumulated yet.
  if (!S.perfRealTokens || !(S.perfActiveMs > 0)) return 0;
  return S.perfRealTokens / Math.max(0.25, S.perfActiveMs / 1000);
}

function _perfOnCtxMeter(estTokens, ctxLimit, compressing, realTokens, cachedTokens) {
  var _est = parseInt(estTokens || 0, 10);
  var _ctx = parseInt(ctxLimit || 0, 10);
  if (!isNaN(_est) && _est >= 0) S.perfEstTokens = _est;
  // METER-REAL (2026-09-09): real_tokens > 0 = measured usage (usage_meta),
  // 0 = heuristic estimate (e.g. right after compression).
  S.perfCtxReal = parseInt(realTokens || 0, 10) || 0;
  S.perfCtxCached = parseInt(cachedTokens || 0, 10) || 0;
  if (!isNaN(_ctx) && _ctx > 0) {
    S.perfCtxLimit = _ctx;
    S.perfCtxPct = Math.round((S.perfEstTokens / _ctx) * 100);
  }
  S.perfCompressing = !!compressing;
  _perfRender(false);
}

function _perfOnDone(d) {
  if (!d || typeof d !== 'object') return;
  var _peak = parseInt(d.metrics_ctx_peak_tokens || 0, 10);
  var _ctx = parseInt(d.metrics_ctx_limit || 0, 10);
  var _pressure = parseFloat(d.metrics_ctx_pressure_peak || 0);
  if (!isNaN(_peak) && _peak > 0) S.perfEstTokens = _peak;
  if (!isNaN(_ctx) && _ctx > 0) S.perfCtxLimit = _ctx;
  if (isFinite(_pressure) && _pressure > 0) S.perfCtxPct = Math.round(_pressure * 100);
  var _elapsed = parseFloat(d.elapsed || 0);
  // GEN-TIME-FIX: globale tok/s aus echten Tokens / Generierungszeit (nicht ctx/Heuristik / Wandzeit)
  var _ptTot = (d.phase_timings && d.phase_timings.total) || {};
  var _genRate = (_ptTot.real_tokens > 0 && _ptTot.gen_s > 0) ? (_ptTot.real_tokens / _ptTot.gen_s) : 0;
  if (_genRate > 0) {
    S.perfTokRate = _genRate;
  } else if (isFinite(_elapsed) && _elapsed > 0 && S.perfEstTokens > 0) {
    S.perfTokRate = S.perfEstTokens / _elapsed;
  }
  S.perfCompressing = false;
  _perfRender(true);

  // ── Phase Timing Pills ────────────────────────────────────────────────────
  var _phEl = document.getElementById('h-phases');
  if (_phEl && d.phase_timings && typeof d.phase_timings === 'object') {
    var _timings = d.phase_timings;
    var _labels = { pre_explore: 'Pre-Explore', soft_planner: 'Planner', coder_loop: 'Coder' };
    var _phaseOrder = ['pre_explore', 'soft_planner', 'coder_loop'];
    var _html = '';
    var _hasAny = false;

    _phaseOrder.forEach(function(key) {
      var p = _timings[key];
      if (!p) return;
      var st = p.status || '';
      var dur = p.duration_s;
      var lbl = _labels[key] || key;

      if (st === 'skipped') {
        _html += '<div class="hphase hp-skip"><span class="hp-lbl">' + lbl + '</span>–</div>';
        _hasAny = true;
        return;
      }
      if (dur === null || dur === undefined) return;

      var m = Math.floor(dur / 60), s = Math.round(dur % 60);
      var ts = m > 0 ? (m + 'm' + String(s).padStart(2,'0') + 's') : (s + 's');
      var cls = (st === 'ok' || st === 'completed') ? 'hp-ok'
              : (st === 'timeout')                  ? 'hp-timeout'
              : (st === 'error' || st === 'aborted') ? 'hp-error'
              : 'hp-ok';
      var flag = (st !== 'ok' && st !== 'completed') ? ' <span style="font-size:10px;opacity:.75">(' + st + ')</span>' : '';
      // GEN-TIME-FIX: tok/s = echte Completion-Tokens / Generierungszeit (ohne Tool-Latenz)
      var _prt = p.real_tokens || 0, _pgs = p.gen_s || 0;
      var _ptok = (_prt > 0 && _pgs > 0) ? ' <span style="font-size:10px;opacity:.75">' + Math.round(_prt / _pgs) + 't/s</span>' : '';
      _html += '<div class="hphase ' + cls + '" title="' + lbl + ': ' + dur + 's (' + st + ')">'
             + '<span class="hp-lbl">' + lbl + '</span>' + ts + flag + _ptok + '</div>';
      _hasAny = true;
    });

    // Total pill — only when ≥2 phases ran
    var _totalPhases = _phaseOrder.filter(function(k){ return _timings[k] && _timings[k].status !== 'skipped' && _timings[k].duration_s !== null; });
    if (_totalPhases.length >= 2 && _timings.total && _timings.total.duration_s > 0) {
      var td = _timings.total.duration_s;
      var tm = Math.floor(td / 60), ts2 = Math.round(td % 60);
      var tstr = tm > 0 ? (tm + 'm' + String(ts2).padStart(2,'0') + 's') : (ts2 + 's');
      _html += '<div class="hphase hp-total" title="Total: ' + td + 's"><span class="hp-lbl">∑</span>' + tstr + '</div>';
    }

    var _tot = _timings.total || {};
    var _globalTokRate = (_tot.real_tokens > 0 && _tot.gen_s > 0) ? (_tot.real_tokens / _tot.gen_s) : 0;
    if (_hasAny && _globalTokRate > 0) {
      _html += '<div class="hphase" style="background:var(--amber);color:#000;font-weight:700" title="' + _globalTokRate.toFixed(1) + ' tokens pro Sekunde (Generation, ohne Tool-Latenz)">'
             + '<span class="hp-lbl">tok/s</span>' + _globalTokRate.toFixed(1) + '</div>';
    }

    if (_hasAny) {
      _phEl.innerHTML = _html;
      _phEl.style.display = 'flex';
    } else {
      _phEl.style.display = 'none';
    }
  }
}


// -- Send -------------------------------------------------------
// RETRY (2026-08-31): resend the last prompt — used by the retry button
// on error/abort runs. Rebuilds the user bubble and calls sendMsg().
function _retryLastPrompt() {
  var _lp = S.lastPrompt;
  if (!_lp || S.streaming) return;
  var _inp = document.getElementById('input');
  if (_inp) { _inp.value = _lp; }
  sendMsg();
}

// RETRY-IN-NEW-CHAT (2026-09-01): same prompt in a fresh chat —
// clears session/DOM via newChat() (S.lastPrompt stays preserved)
// and then sends again. Help after errors with broken context.
async function _retryLastPromptNewChat() {
  var _lp = S.lastPrompt;
  if (!_lp || S.streaming) return;
  // A first-turn failure has no context to reset. Retrying in a new chat
  // only created duplicate one-message history entries; reuse the chat.
  var _hasAssistant = (S.currentChatMessages || []).some(function(m) {
    return m && m.role === 'assistant' && String(m.content || '').trim();
  });
  if (!_hasAssistant) { _retryLastPrompt(); return; }
  S._skipPlaceholderClear = true;
  await newChat();
  var _inp = document.getElementById('input');
  if (_inp) { _inp.value = _lp; }
  sendMsg();
}

async function sendMsg() {
  if (S.agentPaused) { resumeWithAnswer(); return; }
  const inp = document.getElementById('input');
  const txt = inp.value.trim();
  if (!txt && !S.pendingImgs.length) return;
  // MID-RUN STEERING / FOLLOW-UP QUEUE: allow queueing or steering text + images.
  if (S.streaming) {
    queueFollowUp(txt, S.pendingImgs.slice());
    S.pendingImgs = []; renderImgPreview();
    inp.value = ''; inp.style.height = 'auto';
    _updateSendState();
    return;
  }

  const imgs = S.pendingImgs.slice();
  S.pendingImgs = []; renderImgPreview();
  inp.value = ''; inp.style.height = 'auto';
  _updateSendState();
  var _helReset = document.getElementById('h-elapsed'); if (_helReset) _helReset.style.display = 'none';
  var _phReset = document.getElementById('h-phases'); if (_phReset) { _phReset.style.display = 'none'; _phReset.innerHTML = ''; }
  // reset the agent-phase badge
  var _papReset = document.getElementById('perf-agent-phase'); if (_papReset) { _papReset.className = ''; _papReset.innerHTML = ''; }
  // historicize the planner bubble: old run stays visible, new run gets a new bubble id.
  document.querySelectorAll('#planner-bubble').forEach(function(el) {
    if (typeof _stripPlannerLiveIds === 'function') _stripPlannerLiveIds(el);
    el.classList.remove('live');
    el.id = '';
  });
  S.lastPrompt = txt;
  addUserMsg(txt, imgs);

  S.streaming = true;
  S.currentRunId = null;
  S._runIsDuo = false;
  // the switches may have changed in another tab or via a preset - the answer
  // at the end of this run must use the CURRENT server truth
  refreshVoiceFlags();
  // ROUTE-FOR-SPEECH: which route this run actually took. The final answer is
  // read with the "agentic" switch only when the run really was multi-agent;
  // the mode alone cannot tell (auto routes to either, and the decider decides
  // per message).
  S._routeMultiAgent = false;
  S._lastRunStats = null;   // RUN-STATS: a new run must never inherit the old line
  if (S._runAbortCtrl) { S._runAbortCtrl.abort(); }
  S._runAbortCtrl = new AbortController();
  _perfResetRuntimeState();
  _perfRender(false);
  // send button stays clickable while streaming — clicking it queues the
  // typed message (steering bar), same as pressing Enter
  const inpStart = document.getElementById('input');
  if (inpStart) inpStart.placeholder = 'Keep typing to queue follow-up changes';
  document.getElementById('h-dot').className = 'busy';
  const stopBtn = document.getElementById('stop-btn');
  if (stopBtn) stopBtn.classList.add('visible');
  const pauseBtn = document.getElementById('pause-btn');
  if (pauseBtn) pauseBtn.classList.add('visible');
  setPauseBtnState('running');
  setStopBtnState('idle');
  const skipBtn = document.getElementById('skip-btn');
  if (skipBtn) skipBtn.style.display = 'inline-flex';
  document.getElementById('h-complexity').style.display = 'none';
  const taskBadge = document.getElementById('h-tasktype');
  if (taskBadge) taskBadge.style.display = 'none';
  // reset the elapsed badge
  const _elBadge = document.getElementById('h-elapsed');
  if (_elBadge) _elBadge.style.display = 'none';

  const skippedKeys = Object.keys(S.skippedAgents).filter(function(k){return S.skippedAgents[k];});
  if (S.forcedComplexity !== 'auto' || skippedKeys.length || S.judgeBias !== 50) {
    const parts = [];
    if (S.forcedComplexity !== 'auto') parts.push('MODE FORCE: ' + S.forcedComplexity.toUpperCase());
    if (S.judgeBias !== 50) parts.push('JUDGE-BIAS: ' + S.judgeBias + (S.judgeBias > 50 ? ' (prefers complex)' : ' (prefers simple)'));
    if (skippedKeys.length) parts.push('SKIP: ' + skippedKeys.join(', '));
    const info = document.createElement('div');
    info.className = 'msg divider';
    info.style.cssText = 'color:#e09030;border-color:rgba(224,144,48,.25);font-size:9px;letter-spacing:.07em';
    info.textContent = parts.join(' | ');
    document.getElementById('chat').appendChild(info);
  }

  // SAFETY-FIX: outer try{}finally{} ensures S.streaming is always reset,
  // even if runAutomap(), _checkIntentBeforeStream() or other code throws before the fetch-try.
  try {

  if (S.mode === 'automap') {
    const map = await runAutomap(txt, imgs.map(function(i){return i.b64;}));
    if (map && map.assignments) {
      await applyAutomap(map.assignments);
      showAutomapEvent(map.task_type, map.assignments);
    }
  }

  // Intent Agent: check before stream (evolve handled locally, rest goes to server)
  const _intentHandled = await _checkIntentBeforeStream(txt);
  if (_intentHandled) {
    return; // the finally block takes care of S.streaming=false + UI cleanup
  }

  if (imgs.length && S.mode !== 'automap') {
    var _canProcessImg = false;
    if (S.mode === 'code_duo') {
      var _dmPre = (S.duoImageMode === 'preprocess');
      var _duoMd = _duoImageRoleModels();
      _canProcessImg = (_dmPre && !!S.visionModel)
        || (!_dmPre && ((S.duoImageToPlanner && _isVisionCapableModel(_duoMd.planner))
                        || (S.duoImageToCoder && S.coderImagesOff !== true && _isVisionCapableModel(_duoMd.coder))));
    } else if (S.mode === 'pipeline') {
      if (S.pipelineVisionDirect === true) {
        var _anyCfgRole = ['analyst','refiner','critic','synthesizer'].some(function(r) {
          return S.pipelineVisionRoles && S.pipelineVisionRoles[r];
        });
        var _pHasRawRole = ['analyst','refiner','critic','synthesizer'].some(function(r) {
          var am = (S.currentAssignments[r] || {}).model || '';
          var active = (r === 'analyst' && !_anyCfgRole) || (S.pipelineVisionRoles && S.pipelineVisionRoles[r]);
          return active && _isVisionCapableModel(am);
        });
        _canProcessImg = _pHasRawRole || !!S.visionAgentModel;
      } else {
        _canProcessImg = !!S.visionAgentModel;
      }
    } else {
      var _directM = (S.currentAssignments['direct'] || {}).model || '';
      var _canDirectImg = (S.visionEnabled && !!S.visionModel)
        || _isVisionCapableModel(_directM)
        || _anyInstalledVisionModel();
      if (S.mode === 'auto') {
        var _aDmPre = (S.duoImageMode === 'preprocess');
        var _aDuoMd = _duoImageRoleModels();
        var _canDuoAuto = (_aDmPre && !!S.visionModel)
          || (!_aDmPre && ((S.duoImageToPlanner && _isVisionCapableModel(_aDuoMd.planner))
                            || (S.duoImageToCoder && S.coderImagesOff !== true && _isVisionCapableModel(_aDuoMd.coder))));
        _canProcessImg = _canDirectImg || _canDuoAuto;
      } else {
        _canProcessImg = _canDirectImg;
      }
    }
    if (!_canProcessImg) {
      const hint = document.createElement('div');
      hint.className = 'msg divider';
      hint.style.cssText = 'color:#e09030;border-color:rgba(224,144,48,.3)';
      hint.textContent = '\u26A0 No active vision handler for this mode \u2014 check Image Processing settings';
      document.getElementById('chat').appendChild(hint);
    }
  }

  try {
    const _automapCodeDuoReq = (S.mode === 'automap' && S.automapCodeDuoEnabled);
    const _reqDuoPreExplore = _automapCodeDuoReq ? (S.automapDuoPreExplore || false) : (S.duoPreExplore || false);

    // ── Settings flush: pending postSettings vor /stream synchronisieren ──
    if (_settingsPostTimer) {
      clearTimeout(_settingsPostTimer);
      _settingsPostTimer = null;
      await _flushQueuedSettings();
    }

    // HISTORY-SOURCE FLUSH (2026-10-03): the run seeds its history from
    // the saved chat json - persist BEFORE the stream starts so the json
    // holds this turn (and any post-edit state) exactly as the UI does.
    var _flushOk = true;
    if (S.chatAutosave) {
      try {
        _flushOk = await persistCurrentChat(true);
        if (!_flushOk) showStatus('⚠ Chat could not be saved before this run - sending the current state with the request.');
      } catch (e) { _flushOk = false; showStatus('⚠ Chat could not be saved before this run - sending the current state with the request.'); }
    }

    const res = await fetch('/stream', {
      method:'POST', headers:{'Content-Type':'application/json'},
      signal: S._runAbortCtrl ? S._runAbortCtrl.signal : undefined,
      body: JSON.stringify({
        q: txt,
        flush_ok: _flushOk,
        dom_messages: _flushOk ? undefined : S.currentChatMessages,
        images: imgs.map(function(i) { return i.b64; }),
        mode: S.mode,
        iterations: S.iters,
        constraint_mode: S.constraintMode,
        force_complexity: S.forcedComplexity !== 'auto' ? S.forcedComplexity : undefined,
        skip_agents:      Object.keys(S.skippedAgents).filter(function(k){return S.skippedAgents[k];}),
        judge_bias:       S.judgeBias,
        duo_tool_rounds:   S.duoToolRounds  || 0,
        // TIER-WITH-RUN (2026-11-09): pin the tool tiers with the request so
        // a just-changed dropdown cannot race the async settings.json write.
        direct_tools_enabled: S.directToolsEnabled !== false,
        direct_tools_tier: S.directToolsTier || 'readonly',
        duo_coder_tool_tier: S.duoCoderToolTier || 'core',
        duo_websearch_enabled: !!S.duoWebsearch,
        subagent_lite_enabled: S.subagentLite !== false,
        git_tools_enabled: S.gitToolsEnabled || 'auto',
        lsp_auto_diagnostics: typeof S.lspAutoDiag === 'boolean' ? S.lspAutoDiag : undefined,
        duo_use_pipeline:  !!S.duoUsePipeline,
        duo_critic_tools:  !!S.duoCriticTools,
        duo_chunking:      !!S.duoChunking,
        duo_test_feedback_chunk: !!S.duoTestFeedbackChunk,
        duo_test_feedback_final: !!S.duoTestFeedbackFinal,
                duo_planner:       !!S.duoPlannerEnabled,
        duo_coding_mode:   S.duoCodingMode  !== false,
        duo_pre_explore:   _reqDuoPreExplore,
        duo_parallel_preexplore: _automapCodeDuoReq
          ? !!S.automapDuoParallelPreexplore
          : !!S.duoParallelPreexplore,
        duo_pass_explore_files: S.duoPassFiles || 'touched',
                duo_agentic_mode:  !!S.duoAgenticMode,
                duo_agentic_thinking:    !!S.duoAgenticThinking,
                duo_thinking_per_chunk:  !!S.duoThinkingPerChunk,
                // Tool-Thinking: Toggle=always → auto_mode=always, sonst segmented control
                duo_coder_tool_thinking: S.duoToolThinkingAlways || S.duoToolThinkingEnabled,
                duo_coder_tool_thinking_auto_mode: S.duoToolThinkingAlways ? 'always' : (S.duoToolThinkingMode || 'off'),
                reasoning_effort: S.reasoningEffort || 'auto',
                planner_reasoning_effort: S.plannerReasoningEffort || 'auto',
                chat_id: S.currentChatId || undefined,
        until_finished: !!S.duoUntilFinished,
        // CTX-WITH-RUN: send the currently chosen context so the run uses it
        // even if the settings.json write lagged/failed.
        duo_coder_ctx_agentic: (function(){var e=document.getElementById('duo-ctx-agentic'); if(!e)return undefined; var v=parseInt(e.value,10); return (isFinite(v)&&v>0)?v:undefined;})(),
        duo_coder_ctx_normal: (function(){var e=document.getElementById('duo-ctx-normal'); if(!e)return undefined; var v=parseInt(e.value,10); return (isFinite(v)&&v>0)?v:undefined;})(),
        duo_planner_ctx_target: (function(){var e=document.getElementById('duo-ctx-planner'); if(!e||e.disabled)return undefined; var v=parseInt(e.value,10); return (isFinite(v)&&v>0)?v:undefined;})(),
        duo_critic_ctx: (function(){var e=document.getElementById('duo-ctx-critic'); if(!e)return undefined; var v=parseInt(e.value,10); return (isFinite(v)&&v>0)?v:undefined;})(),
      })
    });
    if (!res.ok) {
      var _httpErr = '';
      try { _httpErr = (await res.text()).slice(0, 200); } catch (_re) {}
      throw new Error('HTTP ' + res.status + (_httpErr ? ': ' + _httpErr : ''));
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    S._sseBuf = '';
    S._sseFrameSeq = 0;
    S._sawDoneEvent = false;
    S._sseReaderActive = true;
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) {
          var _tail = dec.decode();
          if (_tail) _sseFeedLines(_tail);
          if (S._sseBuf && S._sseBuf.indexOf('data:') === 0) _sseFeedLines('\n');
          break;
        }
        _sseFeedLines(dec.decode(chunk.value, {stream:true}));
      }
    } finally {
      S._sseReaderActive = false;
    }
    // RUN-JOURNAL (2026-09-20): a clean stream end WITHOUT a done event used
    // to leave the bubble on "⟳ Loading" forever. Reattach via the journal.
    if (!S._sawDoneEvent && S.currentRunId && !S._journalTailing) {
      S._sseBuf = '';
      S._attachedRunId = S.currentRunId;
      _journalNote('Connection lost - reattaching via the run journal ...', { reattachBanner: true });
      _journalStartTail(S.currentRunId);
    }
  } catch(e) {
    // STREAM-ERROR-UX (2026-09-04): "Error in input stream" is a
    // transport drop (stop/connection), not a model error - show as a
    // neutral/amber notice instead of a red error bubble.
    const _errMsg = String((e && e.message) || e || '');
    const _isAbort = !!(e && e.name === 'AbortError');
    const _isConnDrop = !_isAbort && /input stream|network|fetch failed|failed to fetch|body stream|load failed|chunked/i.test(_errMsg);
    if (_isConnDrop && !S._sawDoneEvent && S.currentRunId && !S._journalTailing) {
      // RUN-JOURNAL-REATTACH-ON-DROP (2026-11-22): transport drop while
      // run_id is live leaves the run running detached on the server. Keep
      // S.curAgent/live state intact and tail from S._sseFrameSeq instead of
      // tearing down and letting _journalInitAttach() replay from frame 0.
      S._sseBuf = '';
      S._attachedRunId = S.currentRunId;
      _journalNote('Connection lost - reattaching via the run journal ...', { reattachBanner: true });
      _journalStartTail(S.currentRunId);
    } else {
      if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
      _cleanupLoadTimers();
      _toolGenDone();
      const _hadBody = S.curAgent && S.curAgent.body && String(S.curAgent.body.textContent || '').trim();
      if (S.curAgent) {
        var _setNotice = function (el, msg, col) {
          var sp = document.createElement('span');
          sp.className = 'status-txt';
          sp.style.color = col;
          sp.textContent = msg;
          el.textContent = '';
          el.appendChild(sp);
        };
        if (!_hadBody && _isAbort) {
          _setNotice(S.curAgent.body, '[Run aborted]', '#7a8fa8');
        } else if (!_hadBody && _isConnDrop) {
          _setNotice(S.curAgent.body, '[Connection lost - run may be parked]', '#7a8fa8');
        } else if (!_hadBody && !_isAbort) {
          _setNotice(S.curAgent.body, '[Error: ' + _errMsg + ']', '#b04040');
        }
        S.curAgent.body.classList.remove('live');
        S.curAgent = null;
      }
      // SSE-DEATH-VISIBILITY (2026-09-15): the chat error line used to be
      // suppressed when the agent card already had partial text (_hadBody) —
      // a mid-run stream death then looked like a normal end. The line now
      // ALWAYS appears.
      if (!_isAbort) {
        const errDiv = document.createElement('div');
        errDiv.className = 'msg status-txt';
        if (_isConnDrop) {
          errDiv.style.cssText = 'color:#b08a40;font-size:10px;border:1px solid rgba(200,160,64,.25);padding:6px 8px;border-radius:4px';
          errDiv.textContent = '\u26A0 Connection lost - the run may still be running or parked. Send a message to resume, or check the log.';
        } else {
          errDiv.style.cssText = 'color:#b04040;font-size:10px;border:1px solid rgba(200,64,64,.25);padding:6px 8px;border-radius:4px';
          errDiv.textContent = '\u26A0 Stream error: ' + (e.message || e);
        }
        const chatEl = document.getElementById('chat');
        if (chatEl) chatEl.appendChild(errDiv);
        scrollBtmIfNearBottom(120);
      }
      document.querySelectorAll('.abody.live').forEach(function(b) {
        if (!b.textContent.trim()) {
          var sp = document.createElement('span');
          sp.className = 'status-txt';
          sp.style.color = '#7a8fa8';
          sp.textContent = '[No output - stream aborted]';
          b.appendChild(sp);
        }
        b.classList.remove('live');
      });
      showStatus(_isAbort ? 'Run stopped.' : (_isConnDrop ? 'Connection lost - check the log for status.' : '\u26A0 Stream error: ' + e.message));
    }
  } // end inner try/catch (fetch)

  } finally { // end outer try — covers automap + intent + fetch
    // If journal reattach took over after a stream EOF without done, leave the
    // live UI state intact; _journalStopTail() will run _finalizeStreamUi().
    if (!S._journalTailing) {
      _finalizeStreamUi();
    }
  }
}

function _finalizeStreamUi() {
  if (typeof _toolGenDone === 'function') _toolGenDone();
  try {
    document.querySelectorAll('#chat .abody.live').forEach(function (b) { b.classList.remove('live'); });
  } catch (e) {}
  if (typeof _syncSpeakButtons === 'function') _syncSpeakButtons();
  // READ ALOUD at run end: direct reads the whole answer, an agentic run reads
  // the coder's final answer (the plan was already spoken when it appeared).
  try {
    var _bodies = document.querySelectorAll('#chat .abody');
    var _lastBody = _bodies.length ? _bodies[_bodies.length - 1] : null;
    var _answerTxt = _lastBody ? _answerTextOnly(_lastBody) : '';
    if (_answerTxt && _answerTxt.indexOf('[Run aborted]') !== 0) {
      var _isDuo = S._routeMultiAgent === true
        || S._runIsDuo === true
        || ['code_duo', 'duo', 'autoagent'].indexOf(String(S.mode || '')) >= 0;
      _readAloud(_isDuo ? 'fazit' : 'direct', _answerTxt);
    }
  } catch (e) { /* never break the run for speech */ }
  S.streaming = false;
  if (typeof _cleanupLoadTimers === 'function') _cleanupLoadTimers();
  S.currentRunId = null;
  try { sessionStorage.removeItem('hm_run_id'); } catch (e) {}
  var _sendBtn = document.getElementById('send');
  if (_sendBtn) _sendBtn.disabled = false;
  if (typeof _updateSendState === 'function') _updateSendState();
  var _hdot = document.getElementById('h-dot');
  if (_hdot) _hdot.className = 'on';
  const stopBtnEnd = document.getElementById('stop-btn');
  if (stopBtnEnd) stopBtnEnd.classList.remove('visible');
  if (typeof setPauseBtnState === 'function') setPauseBtnState('idle');
  if (typeof setStopBtnState === 'function') setStopBtnState('idle');
  if (typeof stopAskUserCountdown === 'function') stopAskUserCountdown();
  S.agentPaused = false;
  S.agentQuestion = null;
  const inpEnd = document.getElementById('input');
  if (inpEnd) inpEnd.placeholder = 'Message...';
  if (typeof _drainFollowUpQueue === 'function') _drainFollowUpQueue();
  const skipBtnEnd = document.getElementById('skip-btn');
  if (skipBtnEnd) skipBtnEnd.style.display = 'none';
  // refresh VRAM after pipeline/stream — only if the panel is open
  if (!_vramPending && _vramOpen && typeof refreshVram === 'function') refreshVram();
  // update prefetch avgs (backend has recalculated the lead)
  if (S.smartPreload && typeof loadPrefetchAvgs === 'function') setTimeout(loadPrefetchAvgs, 800);
  // always hide the task-type badge after the stream
  const taskBadgeEnd = document.getElementById('h-tasktype');
  if (taskBadgeEnd) taskBadgeEnd.style.display = 'none';
  if (typeof rmEl === 'function') rmEl('status-el');
  if (typeof loadMemory === 'function') loadMemory();
  // Auto-save chat message (on by default; covers stream end, errors, stop and abort)
  if (S.chatAutosave && !S._chatPersistBusy && typeof persistCurrentChat === 'function') {
    S._chatPersistBusy = true;
    persistCurrentChat(true).then(function() { S._chatPersistBusy = false; })
      .catch(function() { S._chatPersistBusy = false; });
  }
}

var _TOOL_ICONS = {
  edit_file:'\uD83D\uDCDD', read_file:'\uD83D\uDCD6', write_file:'\uD83D\uDCDD',
  write_file_append:'\uD83D\uDCDD', patch_file:'\uD83D\uDCDD', replace_lines:'\uD83D\uDCDD',
  edit_ast:'\uD83D\uDCDD', run_bash:'\u26A1', run_tests:'\uD83E\uDDEA',
  run_python:'\uD83D\uDC0D', install_package:'\uD83D\uDCE6',
  web_search:'\uD83D\uDD0D', web_fetch:'\uD83C\uDF10', find_files:'\uD83D\uDD0E',
  find_references:'\uD83D\uDD17', search_code:'\uD83D\uDD0E',
  get_signatures:'\uD83D\uDDFA', list_dir:'\uD83D\uDCC1',
  git_commit:'\uD83D\uDD27', git_status:'\uD83D\uDD27', task_complete:'\u2705',
  start_background:'\u25B6', get_background_output:'\uD83D\uDCE4', stop_background:'\u23F9',
  ask_user:'\u2753', browser:'\uD83C\uDF10', undo_last:'\u21A9'
};

// Tool-cluster anchor (2026-09-02): chips / WRITE badges / result details stay
// contiguous; streaming think blocks are appended AFTER the whole tool cluster,
// so a think block can never sit between a tool chip and its result.
function _toolClusterAnchor(body) {
  if (!body) return null;
  var els = body.querySelectorAll(
    '.tool-call-row, .tool-act, .tool-result-block, .fc-preview, .write-sep, .tool-sep'
  );
  return els.length ? els[els.length - 1] : null;
}
function _appendToolEl(body, el) {
  if (!body) { if (el && el.parentNode) el.parentNode.removeChild(el); return; }
  var anchor = _toolClusterAnchor(body);
  if (anchor) { anchor.insertAdjacentElement('afterend', el); }
  else { body.appendChild(el); }
}

// ── RUN-JOURNAL REATTACH (2026-09-20) ─────────────────────────────
// The /stream response is single-use: a page reload or a silently dropped
// connection used to detach the UI from a running run forever (bubble stuck
// on "⟳ Loading" while the backend kept working). The server journals every
// SSE frame, so frames are replayable by index and the UI can reattach.

function _sseFeedLines(text) {
  S._sseBuf = (S._sseBuf || '') + text;
  var _ls = S._sseBuf.split('\n'); S._sseBuf = _ls.pop();
  for (var li = 0; li < _ls.length; li++) {
    var _ln = _ls[li];
    if (_ln.indexOf('data:') !== 0) continue;
    S._sseFrameSeq = (S._sseFrameSeq || 0) + 1;
    try { handleEvent(JSON.parse(_ln.slice(5))); } catch(e) { console.warn('[SSE] Bad JSON:', _ln.slice(5,80), e); }
  }
}

function _journalNote(txt, opts) {
  try {
    var chatEl = document.getElementById('chat');
    if (!chatEl) return null;
    var d = document.createElement('div');
    d.className = 'msg status-txt';
    d.style.cssText = 'color:#b08a40;font-size:10px;border:1px solid rgba(200,160,64,.25);padding:6px 8px;border-radius:4px';
    d.textContent = '\u26A0 ' + txt;
    chatEl.appendChild(d);
    if (opts && opts.reattachBanner) S._reattachBannerEl = d;
    scrollBtmIfNearBottom(120);
    return d;
  } catch (e) { return null; }
}

function _journalClearReattachBanner(reattached) {
  var el = S._reattachBannerEl;
  if (!el) return;
  if (reattached) {
    el.style.cssText = 'color:#3a9960;font-size:10px;border:1px solid rgba(58,153,96,.25);padding:4px 8px;border-radius:4px;opacity:.85';
    el.textContent = '\u2713 Reattached via run journal';
  } else if (el.parentNode) {
    el.parentNode.removeChild(el);
  }
  S._reattachBannerEl = null;
}

function _journalStopTail() {
  // STREAMING-RELEASE (audit 2026-11-03): attaching to a live run set
  // S.streaming=true; nothing cleared it on done/abort, so the composer
  // queued forever. Only clear it when this tail owned the state.
  var _wasTailing = Boolean(S._journalTailing);
  S._journalTailing = null;
  if (window._journalTailIv) { clearInterval(window._journalTailIv); window._journalTailIv = null; }
  _journalClearReattachBanner(false);
  if (_wasTailing) {
    _finalizeStreamUi();
  }
}

function _journalStartTail(runId) {
  if (!runId || S._journalTailing) return;
  S._journalTailing = runId;
  var _tailInFlight = false;
  var _pollOnce = function() {
    if (S._journalTailing !== runId) {
      if (window._journalTailIv) { clearInterval(window._journalTailIv); window._journalTailIv = null; }
      return;
    }
    if (document.hidden || _tailInFlight) return;
    _tailInFlight = true;
    var _reqAfter = S._sseFrameSeq || 0;
    fetch('/run/journal?run_id=' + encodeURIComponent(runId) + '&after=' + _reqAfter)
      .then(function(r) { return r.json(); })
      .then(function(j) {
        _tailInFlight = false;
        if (S._journalTailing !== runId) return;
        if (!j || j.active === false) { _journalStopTail(); return; }
        if (j.frames && j.frames.length) {
          _journalClearReattachBanner(true);
          var _alreadySkip = Math.max(0, (S._sseFrameSeq || 0) - _reqAfter);
          for (var i = _alreadySkip; i < j.frames.length; i++) _sseFeedLines(j.frames[i]);
          if (typeof j.n === 'number') S._sseFrameSeq = Math.max(S._sseFrameSeq || 0, j.n);
        }
        if (S._sawDoneEvent || j.done || j.aborted) {
          _journalStopTail();
          if (j.aborted && !S._sawDoneEvent) _journalNote('Run was interrupted (connection/tab). Send a message to resume.');
        }
      })
      .catch(function() { _tailInFlight = false; });
  };
  var _iv = setInterval(_pollOnce, 2000);
  window._journalTailIv = _iv;
  _pollOnce();
}

function _journalApplyAttachSnapshot(j, _initEpoch) {
  if (S.streaming || S._journalTailing || (S._chatSwitchEpoch || 0) !== _initEpoch) return;
  if (!j || !j.active || !j.frames || !j.frames.length) return;
  if (j.done && ((Date.now() / 1000) - (j.ts || 0)) > 300) return;
  // JOURNAL-CHAT-SCOPE (audit 2026-11-03): only reattach a run that
  // belongs to the chat currently open. Without this, the newest run's
  // frames were replayed into whatever chat happened to be on screen.
  if (j.chat_id && (!S.currentChatId || String(j.chat_id) !== String(S.currentChatId))) return;
  if (S._attachedRunId === j.run_id) return;
  S._attachedRunId = j.run_id;
  if (j.done) return;
  S._sseFrameSeq = 0; S._sseBuf = ''; S._sawDoneEvent = false;
  // A mirrored live run counts as streaming: the composer must not start
  // a second concurrent run on the same chat.
  S.streaming = true;
  _journalNote('Attached to the running job (live).');
  for (var i = 0; i < j.frames.length; i++) _sseFeedLines(j.frames[i]);
  // TG-AUDIT (2026-10-07): `n` is the LOGICAL frame count while the
  // window only holds the newest slice (20k cap) - tail polls must
  // ask from the logical position, not the window length.
  S._sseFrameSeq = (typeof j.n === 'number' && j.frames.length)
    ? j.n : (S._sseFrameSeq || 0);
  if (!S._sawDoneEvent && !j.done && !j.aborted) {
    _journalStartTail(j.run_id);
  } else {
    _finalizeStreamUi();
    if (j.aborted && !S._sawDoneEvent) _journalNote('Run was interrupted (connection/tab). Send a message to resume.');
  }
}

function _journalInitAttach() {
  if (S.streaming || S._journalTailing) return;
  var _initEpoch = S._chatSwitchEpoch || 0;
  // Meta-only idle peek avoids re-serializing up to 8 MB of frames every 10 s.
  fetch('/run/journal?after=1000000000')
    .then(function(r) { return r.json(); })
    .then(function(j) {
      if (S.streaming || S._journalTailing || (S._chatSwitchEpoch || 0) !== _initEpoch) return;
      if (!j || !j.active) return;
      if (!(Number(j.n || 0) > 0 || (j.frames && j.frames.length))) return;
      if (j.done && ((Date.now() / 1000) - (j.ts || 0)) > 300) return;
      if (j.chat_id && (!S.currentChatId || String(j.chat_id) !== String(S.currentChatId))) return;
      if (S._attachedRunId === j.run_id) return;
      // DONE-JOURNAL-SILENT (2026-10-11): a finished run's journal is fully
      // covered by the chat-transcript replay - feed no frames AND print no note.
      if (j.done) { S._attachedRunId = j.run_id; return; }
      if (j.frames && j.frames.length) {
        _journalApplyAttachSnapshot(j, _initEpoch);
        return;
      }
      return fetch('/run/journal?run_id=' + encodeURIComponent(j.run_id) + '&after=0')
        .then(function(r2) { return r2.json(); })
        .then(function(fullJ) { _journalApplyAttachSnapshot(fullJ, _initEpoch); });
    })
    .catch(function() {});
}


// -- Stop Stream -----------------------------------------------
async function stopStream() {
  if (typeof _readAloudStop === 'function') _readAloudStop();
  if (!S.currentRunId) return;
  // reset the pause state if the agent was blocked
  S.agentPaused = false;
  S.agentQuestion = null;
  setPauseBtnState('idle');
  const btn = document.getElementById('stop-btn');
  if (btn) btn.style.opacity = '0.5';
  try {
    // primary: chat_id-based abort → resume block is written (stop button)
    if (S.currentChatId) {
      await fetch('/abort?chat_id=' + S.currentChatId + '&silent=false', {method: 'POST'});
    } else {
      // fallback: run_id-based abort (no resume, no chat_id known)
      await fetch('/abort/' + S.currentRunId, {method: 'POST'});
    }
  } catch(e) {}
  // the UI cleans itself up when the stream ends
}

function setStopBtnState(state) {
  S._stopBtnState = state;
  var sb = document.getElementById('stop-btn');
  if (!sb) return;
  sb.classList.remove('stop-graceful-pending', 'stop-force');
  switch (state) {
    case 'idle':
      sb.style.opacity = '';
      sb.title = 'Abort run';
      break;
    case 'graceful_pending':
      sb.style.opacity = '';
      sb.title = 'Stop after current chunk \u2014 click again for immediate abort';
      sb.classList.add('stop-graceful-pending');
      break;
    case 'force':
      sb.classList.add('stop-force');
      sb.title = 'Abort sent\u2026';
      break;
  }
}

async function handleStopClick() {
  if (!S.currentRunId) return;
  switch (S._stopBtnState) {
    case 'idle':
      // DIRECT/CHAT RUN — no chunks, so the graceful after-chunk stop does not
      // apply. The first click aborts the generation immediately.
      if (!S._runIsDuo) {
        setStopBtnState('force');
        stopStream();
        break;
      }
      setStopBtnState('graceful_pending');
      try {
        var resp = await fetch('/abort/graceful/' + S.currentRunId, {method: 'POST'});
        if (!resp.ok) {
          setStopBtnState('idle');
          showInfo('\u26A0 Graceful stop failed');
          return;
        }
        showInfo('\u23F9 Stop requested after chunk end \u2014 runs until the current chunk completes');
      } catch(e) {
        setStopBtnState('idle');
        showInfo('\u26A0 Graceful stop failed: ' + e);
      }
      break;
    case 'graceful_pending':
      setStopBtnState('force');
      stopStream();
      break;
    case 'force':
      break;
  }
}


async function skipCurrentStep() {
  if (!S.currentRunId) return;
  const btn = document.getElementById('skip-btn');
  if (btn) { btn.style.opacity = '0.5'; setTimeout(() => { if (btn) btn.style.opacity = '1'; }, 800); }
  try {
    await fetch('/abort/step/' + S.currentRunId, {method: 'POST'});
  } catch(e) {}
}
// -- Pause / Ask-User -------------------------------------------
function setPauseBtnState(state) {
  S._pauseBtnState = state;
  var pb = document.getElementById('pause-btn');
  if (!pb) return;
  pb.classList.remove('paused', 'pause-manual', 'pause-pending', 'pause-by-throttle');
  switch (state) {
    case 'idle':
      pb.disabled = true;
      pb.textContent = '\u23F8';
      pb.title = 'Run not active';
      pb.classList.remove('visible');
      break;
    case 'running':
      pb.disabled = false;
      pb.textContent = '\u23F8';
      pb.title = 'Pause after current chunk';
      pb.classList.add('visible');
      break;
    case 'pending_pause':
      pb.disabled = false;
      pb.textContent = '\u23F8';
      pb.title = 'Pause requested \u2014 waiting for chunk end';
      pb.classList.add('visible', 'pause-pending');
      break;
    case 'paused_manual':
      pb.disabled = false;
      pb.textContent = '\u25B6';
      pb.title = 'Paused \u2014 click to resume';
      pb.classList.add('visible', 'pause-manual');
      break;
    case 'paused_by_ask_user':
      pb.disabled = false;
      pb.textContent = '\u25B6';
      pb.title = 'Agent is asking \u2014 enter answer and resume';
      pb.classList.add('visible', 'paused');
      break;
    case 'paused_by_throttle':
      pb.disabled = false;
      pb.textContent = '\u26A0';
      pb.title = 'Agent throttled \u2014 enter clarification';
      pb.classList.add('visible', 'pause-by-throttle');
      break;
  }
}

async function handlePauseClick() {
  switch (S._pauseBtnState) {
    case 'running':
      if (!S.currentRunId) return;
      setPauseBtnState('pending_pause');
      try {
        var resp = await fetch('/pause/' + S.currentRunId, {method: 'POST'});
        if (!resp.ok) { setPauseBtnState('running'); showInfo('\u26A0 Pause failed'); return; }
        showInfo('\u23F8 Pause requested after chunk end');
      } catch(e) { setPauseBtnState('running'); showInfo('\u26A0 Network error: ' + e.message); }
      break;
    case 'pending_pause':
      break;
    case 'paused_manual':
      if (!S.currentRunId) return;
      try {
        var resp2 = await fetch('/resume/' + S.currentRunId, {method: 'POST'});
        if (resp2.status === 409) { showInfo('\u26A0 [Error] Run no longer active'); S.streaming = false; S.currentRunId = null; document.getElementById('send').disabled = false; setPauseBtnState('idle'); return; }
        if (!resp2.ok) { showInfo('\u26A0 Resume failed'); return; }
        setPauseBtnState('running');
        // RESUME-REATTACH (audit 2026-11-03): /resume returns JSON, no
        // stream. When the tab still holds an open /stream SSE reader, events
        // continue arriving directly (starting a journal tail would duplicate them).
        if (S.currentRunId && !S._sseReaderActive) _journalStartTail(S.currentRunId);
        showInfo('\u25B6 Run resumed');
      } catch(e) { showInfo('\u26A0 Network error: ' + e.message); }
      break;
    case 'paused_by_ask_user':
      resumeWithAnswer();
      break;
    case 'paused_by_throttle':
      resumeWithAnswer();
      break;
    case 'idle':
      break;
  }
}

