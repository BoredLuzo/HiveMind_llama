// HiveMind UI module: Mid-run steering queue, Planner bubble/checklist & Agent block builders.
// ── MID-RUN STEERING / FOLLOW-UP QUEUE (2026-09-29, ZCode-style) ───────────
// While a run streams, sendMsg() queues typed messages instead of dropping
// them. Queued items appear as a compact bar above the input (like ZCode):
// ↑ Steer injects at the next tool-call boundary (POST /api/run/{id}/steer),
// ✎ pushes the text back into the input for editing, ✕ discards. Steered
// messages stay visible in the chat via the server's "🧭 steered:" status
// frames. Unsteered items auto-send one-by-one when the run finishes.
if (typeof S !== 'undefined') S.followUpQueue = S.followUpQueue || [];

function queueFollowUp(text, images) {
  S.followUpQueue.push({
    text: text || '',
    images: Array.isArray(images) ? images.slice() : [],
    steered: false
  });
  renderFollowUpQueue();
}

function renderFollowUpQueue() {
  const bar = document.getElementById('followup-bar');
  if (!bar) return;
  bar.innerHTML = '';
  bar.style.display = S.followUpQueue.length ? 'block' : 'none';
  S.followUpQueue.forEach(function(item, idx) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;padding:5px 8px;margin-bottom:4px;border:1px solid var(--b2);border-radius:8px;background:var(--bg);font-size:11px';
    const badge = document.createElement('span');
    badge.textContent = 'queued';
    badge.style.cssText = 'flex:0 0 auto;font-size:8px;letter-spacing:.08em;text-transform:uppercase;color:#d0a020;border:1px solid currentColor;border-radius:4px;padding:2px 6px';
    row.appendChild(badge);
    const nImgs = Array.isArray(item.images) ? item.images.length : 0;
    const body = document.createElement('span');
    body.style.cssText = 'flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--tx)';
    body.textContent = (nImgs ? ('[+' + nImgs + ' img] ') : '') + (item.text || '(image steer)');
    row.appendChild(body);
    const mkBtn = function(label, title, fn, color) {
      const b = document.createElement('button');
      b.textContent = label; b.title = title;
      b.style.cssText = 'flex:0 0 auto;font-size:9px;padding:2px 8px;border-radius:5px;border:1px solid ' + (color || 'var(--b2)') + ';background:none;color:' + (color || 'var(--tx2)') + ';cursor:pointer';
      b.onclick = fn;
      return b;
    };
    row.appendChild(mkBtn('\u2191 Steer', 'Inject at the next tool-call boundary', function(){ steerFollowUp(idx); }, '#4a9eff'));
    row.appendChild(mkBtn('\u270E', 'Edit — back into the prompt input', function(){
      const inp = document.getElementById('input');
      if (inp) { inp.value = item.text || ''; inp.focus(); }
      if (nImgs) {
        S.pendingImgs = item.images.slice();
        if (typeof renderImgPreview === 'function') renderImgPreview();
      }
      S.followUpQueue.splice(idx, 1);
      renderFollowUpQueue();
    }));
    row.appendChild(mkBtn('\u2715', 'Discard queued message', function(){
      S.followUpQueue.splice(idx, 1);
      renderFollowUpQueue();
    }, '#c05050'));
    bar.appendChild(row);
  });
  _updateQueuePlaceholder();
}

function _steerChatNote(text) {
  const c = document.getElementById('chat');
  if (!c) return;
  const d = document.createElement('div');
  d.className = 'msg divider';
  d.style.cssText = 'color:#4a9eff;border-color:rgba(74,158,255,.25);font-size:9px';
  d.textContent = '\u2191 steering: ' + text;
  // STEER PLACEMENT (2026-10-08): inside the run block like the steer card
  const _host = (S.curAgent && document.getElementById('ab-' + S.curAgent.tid)) || c;
  _host.appendChild(d);
  scrollBtmIfNearBottom(120);
}

function steerFollowUp(idx) {
  const item = S.followUpQueue[idx];
  if (!item || item.steered) return;
  const rid = S.currentRunId;
  if (!rid || !S.streaming) { showStatus('\u26A0 Run already finished \u2014 message stays queued.'); return; }
  const imgsPayload = Array.isArray(item.images) ? item.images.map(function(i){ return i.b64; }).filter(Boolean) : [];
  fetch('/api/run/' + encodeURIComponent(rid) + '/steer', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ text: item.text || '', images: imgsPayload })
  }).then(function(r){
    if (!r.ok) throw new Error('HTTP ' + r.status);
    S.followUpQueue.splice(idx, 1);
    renderFollowUpQueue();
    const noteTxt = (item.text || '') + (imgsPayload.length ? (' [+' + imgsPayload.length + ' img]') : '');
    _steerChatNote(noteTxt.trim());
  }).catch(function(e){ showStatus('\u26A0 Steer failed: ' + e.message + ' \u2014 stays queued.'); });
}

function _drainFollowUpQueue() {
  // deliver one unsteered message per finished run — chained via sendMsg()
  S.followUpQueue = S.followUpQueue.filter(function(i){ return !i.steered; });
  if (!S.followUpQueue.length) { renderFollowUpQueue(); return; }
  const next = S.followUpQueue.shift();
  renderFollowUpQueue();
  const inp = document.getElementById('input');
  if (inp) inp.value = next.text || '';
  if (Array.isArray(next.images) && next.images.length) {
    S.pendingImgs = next.images.slice();
    if (typeof renderImgPreview === 'function') renderImgPreview();
  }
  sendMsg();
}

function _updateQueuePlaceholder() {
  const inp = document.getElementById('input');
  if (!inp) return;
  inp.placeholder = (S.streaming && S.currentRunId)
    ? 'Keep typing to queue follow-up changes' : 'Message...';
}

function _updateSendState() {
  // grey the send arrow while there is nothing to send or steer; amber as
  // soon as the field has content (while streaming, content = queueable)
  const inp = document.getElementById('input');
  const btn = document.getElementById('send');
  if (!inp || !btn) return;
  btn.disabled = !(inp.value.trim() || S.pendingImgs.length);
}

function _addFlowConnector(color, bgAlpha, agent, label, round) {
  var c = document.getElementById('chat');
  if (!c) return;
  var el = document.createElement('div');
  el.className = 'flow-connector';
  var _bg = color.startsWith('#')
    ? ('rgba(' + parseInt(color.slice(1,3),16) + ',' + parseInt(color.slice(3,5),16) + ',' + parseInt(color.slice(5,7),16) + ',' + (bgAlpha||0.07) + ')')
    : color;
  el.style.cssText = '--fc-color:' + color + ';--fc-bg:' + _bg;
  el.innerHTML = '<div class="fc-line"></div>'
    + '<div class="fc-body">'
    + '<div class="fc-dot"></div>'
    + '<span class="fc-agent">' + esc(agent) + '</span>'
    + (label ? '<span class="fc-arrow">›</span><span class="fc-label">' + esc(label) + '</span>' : '')
    + (round ? '<span class="fc-round">' + esc(round) + '</span>' : '')
    + '</div>'
    + '<div class="fc-line"></div>';
  c.appendChild(el);
  scrollBtmIfNearBottom(120);
  // ── Sticky agent-phase badge in the perf bar (stays visible when the flow connector scrolls away) ──
  var _perfBar = document.getElementById('perf-bar');
  if (_perfBar) {
    var _pap = document.getElementById('perf-agent-phase');
    if (!_pap) {
      _pap = document.createElement('div');
      _pap.id = 'perf-agent-phase';
      _perfBar.insertBefore(_pap, _perfBar.firstChild);
    }
    _pap.style.color = color;
    _pap.innerHTML = '<div class="pap-dot" style="background:' + color + '"></div>'
      + '<span class="pap-agent" style="color:' + color + '">' + esc(agent) + '</span>'
      + (label ? '<span class="pap-arrow">&#x203A;</span><span class="pap-label">' + esc(label) + '</span>' : '')
      + (round ? '<span class="pap-round" style="border-color:' + color + ';color:' + color + '">' + esc(round) + '</span>' : '');
    _pap.className = 'visible';
    _perfBar.style.display = 'flex';
  }
}

function _prexMarkDone(isTimeout) {
  var bar = document.getElementById('prex-info');
  if (!bar || bar.classList.contains('done') || bar.classList.contains('timeout')) return;
  bar.classList.add(isTimeout ? 'timeout' : 'done');
}

function _streamBriefingToBubble(briefing, research, onDone) {
  // Ensure bubble exists
  var wrap = document.getElementById('planner-bubble');
  if (!wrap) {
    upsertPlannerBubble('Briefing empfangen\u2026');
    wrap = document.getElementById('planner-bubble');
  }
  if (!wrap) { if (onDone) onDone(); return; }

  // Update header to "Briefing" mode
  var hdr = wrap.querySelector('.ahdr');
  if (hdr) {
    hdr.innerHTML = ''
      + '<div class="dot" style="background:#8c64b4"></div>'
      + '<span class="aname" style="color:#9a74dc">Planner</span>'
      + '<span class="amodel">Briefing</span>';
  }

  // BUG-2 FIX: planner-body existiert nicht mehr in Slim-Bubble.
  // Briefing-Text in standalone plan-block in #chat streamen.
  var _cBrf = document.getElementById('chat');
  if (!briefing || !_cBrf) {
    wrap.classList.remove('live');
    if (hdr) { var _am = hdr.querySelector('.amodel'); if (_am) _am.textContent = 'Briefing \u2713'; }
    wrap.id = '';
    _stripPlannerLiveIds(wrap);
    if (onDone) onDone();
    return;
  }
  // create the plan block if not present yet
  if (!document.getElementById('planner-plan-block')) {
    var _brfDiv = document.createElement('div');
    _brfDiv.id = 'planner-plan-block';
    _brfDiv.className = 'msg planner-plan-block';
    _brfDiv.innerHTML = '<div class="pplan-hdr">\uD83D\uDCCB Briefing</div><div id="planner-plan-content"></div>';
    _cBrf.appendChild(_brfDiv);
  }
  var _brfEl = document.getElementById('planner-plan-content');
  if (!_brfEl) { if (onDone) onDone(); return; }

  // Research chips below
  if (research && research.length) {
    var _resEl = document.createElement('div');
    _resEl.style.cssText = 'margin:4px 0 2px;font-family:"IBM Plex Mono",monospace;font-size:10px;color:#7080a0;line-height:1.9';
    _resEl.innerHTML = '<span style="color:#7a5aa8;text-transform:uppercase;letter-spacing:.07em;font-size:9px">Research: </span>'
      + research.map(function(q){ return '<span style="background:rgba(72,120,192,.12);border:1px solid rgba(72,120,192,.22);border-radius:3px;padding:1px 6px;margin:0 2px;color:#7090c0">' + esc(q) + '</span>'; }).join(' ');
    _cBrf.appendChild(_resEl);
  }

  // animate the briefing text character by character (O(1) text node append)
  var _i = 0;
  var _chars = briefing.split('');
  var _brfTextNode = document.createTextNode('');
  _brfEl.appendChild(_brfTextNode);
  function _tick() {
    if (_i >= _chars.length) {
      wrap.classList.remove('live');
      if (hdr) { var _am2 = hdr.querySelector('.amodel'); if (_am2) _am2.textContent = 'Briefing \u2713'; }
      wrap.id = '';
      _stripPlannerLiveIds(wrap);
      if (onDone) onDone();
      return;
    }
    var _chunk = _chars.slice(_i, _i + 6).join('');
    _brfTextNode.appendData(_chunk);
    _i += 6;
    requestAnimationFrame(_tick);
  }
  _tick();
}

function _updatePlannerTokRate() {
  var _pb = document.getElementById('planner-bubble');
  if (!_pb) return;
  var _realRate = 0;
  if (S.perfDecodeEma > 0) {
    _realRate = Number(S.perfDecodeEma);
  } else if (S.perfRealTokens > 0 && S.perfFirstTokenAt) {
    _realRate = _perfRealRate();
  }
  var _t = (typeof _perfLiveOutTokens === 'function')
    ? _perfLiveOutTokens(false)
    : (S.perfRealTokens > 0 ? S.perfRealTokens : (S.perfEstTokens || 0));
  var _r = _realRate > 0 ? _realRate.toFixed(1) + ' t/s' : (S.perfTokRate > 0 ? S.perfTokRate.toFixed(1) + ' t/s~' : '');
  var _pm = document.getElementById('planner-tokrate');
  if (_pm) _pm.textContent = (_r || _t ? _t + ' tok' + (_r ? ' \u00b7 ' + _r : '') : 'running\u2026');
}

function upsertPlannerBubble(txt) {
  rmEl('status-el');
  const c = document.getElementById('chat');
  let wrap = document.getElementById('planner-bubble');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'msg ablock planner-bubble planner-slim live';
    wrap.id = 'planner-bubble';
    const hdr = document.createElement('div');
    hdr.className = 'ahdr';
    hdr.innerHTML = ''
      + '<div class="dot" style="background:#8c64b4"></div>'
      + '<span class="aname" style="color:#9a74dc">Planner</span>'
      + '<span class="amodel"><span id="planner-tokrate">running\u2026</span> <span id="planner-elapsed">0s</span></span>';
    wrap.appendChild(hdr);
    c.appendChild(wrap);
  }
  wrap.classList.add('live');
  scrollBtmIfNearBottom(110);
}

function finalizePlannerBubble(summary) {
  // stop the client timer
  if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
  const wrap = document.getElementById('planner-bubble')
    || document.querySelector('[data-planner-decouple-pending="1"]');
  if (!wrap) return;
  var _doneText = (summary || 'Planning complete');
  try {
    wrap.classList.remove('live');
    // Remove any stray loading indicator (legacy / fallback)
    var _indEl = wrap.querySelector('#planner-thinking-indicator');
    if (_indEl) _indEl.remove();
    // collapse the thinking block in #chat (standalone, outside the bubble)
    if (!window._plannerThinkManualOpen) {
      var _detEl = document.getElementById('planner-think-block');
      if (_detEl) _detEl.open = false;
    }
    // Slim header: done state — no toggle anymore, only a status line
    var _hdrEl = wrap.querySelector('.ahdr');
    if (_hdrEl) {
      var _dotEl2 = _hdrEl.querySelector('.dot');
      if (_dotEl2) { _dotEl2.style.opacity = '0.45'; _dotEl2.style.animation = 'none'; }
      var _anameEl = _hdrEl.querySelector('.aname');
      if (_anameEl) _anameEl.style.opacity = '0.6';
      var _amodelEl = _hdrEl.querySelector('.amodel');
      if (_amodelEl) _amodelEl.textContent = _doneText + ' \u2713';
    }
  } catch(e) {}
  // decouple the ID — but only after planner_result (which injects the thinking).
  wrap.dataset.plannerDecouplePending = '1';
}

function _stripPlannerLiveIds(el) {
  if (!el) return;
  ['planner-elapsed', 'planner-tokrate'].forEach(function(id) {
    var _e = el.querySelector('#' + id);
    if (_e) _e.id = '';
  });
}

function _decoupleFinishedPlannerBubble() {
  var _pb = document.getElementById('planner-bubble');
  if (_pb) { _pb.id = ''; _stripPlannerLiveIds(_pb); delete _pb.dataset.plannerDecouplePending; }
  // fallback: search for pending if the id is already gone
  var _pending = document.querySelector('[data-planner-decouple-pending="1"]');
  if (_pending) { _pending.id = ''; _stripPlannerLiveIds(_pending); delete _pending.dataset.plannerDecouplePending; }
}

// SUBTASK-CHECKLIST (2026-08-31): renders/updates the compact plan
// checklist in the planner result. The current chunk is advanced per duo_round
// (✓ done / → current / ○ pending), so chunking is visible in the UI
// (previously it ran invisibly in the background).
function _planChkChunks() { return S._planChunks || []; }

function _renderPlanChecklist() {
  var _chunks = _planChkChunks();
  var _chat = document.getElementById('chat');
  if (!_chunks.length || !_chat) return;
  var _existing = document.getElementById('plan-checklist');
  if (_existing) _existing.remove();
  var _div = document.createElement('div');
  _div.id = 'plan-checklist';
  _div.className = 'msg plan-chk';
  _div.innerHTML = '<div class="plan-chk-hdr">\uD83D\uDCCB Subtask-Plan (' + _chunks.length + ')</div>';
  _chat.appendChild(_div);
  _updatePlanChecklist();
  scrollBtmIfNearBottom(120);
}

function _updatePlanChecklist() {
  var _chunks = _planChkChunks();
  var _el = document.getElementById('plan-checklist');
  if (!_chunks.length || !_el) return;
  var _currN = S._lastChunkN || 1;
  var _lines = '';
  for (var i = 0; i < _chunks.length; i++) {
    var _n = i + 1;
    var _cls = 'chk-pend'; var _mark = '\u25cb';
    if (_n < _currN)      { _cls = 'chk-done'; _mark = '\u2713'; }
    else if (_n === _currN) { _cls = 'chk-curr'; _mark = '\u25b6'; }
    _lines += '<div class="chk-line ' + _cls + '">'
      + '<span class="chk-num">' + _n + '</span>'
      + '<span class="chk-txt">' + esc(_mark) + ' ' + esc(String(_chunks[i] || '')) + '</span>'
      + '</div>';
  }
  _el.innerHTML = '<div class="plan-chk-hdr">\uD83D\uDCCB Subtask-Plan (' + _chunks.length + ')</div>' + _lines;
}

function addDiv(txt) {
  rmEl('status-el');
  const c = document.getElementById('chat');
  const d = document.createElement('div');
  d.className = 'msg divider'; d.textContent = txt;
  c.appendChild(d);
  scrollBtmIfNearBottom(120);
}

function startAgent(name, model, role, isRepair) {
  rmEl('status-el');
  _tokenQueue = [];
  if (_tokenRafId) { cancelAnimationFrame(_tokenRafId); _tokenRafId = null; }
  const color = agentColor(name);
  const c = document.getElementById('chat');
  const wrap = document.createElement('div');
  wrap.className = 'msg ablock';
  const tid = 't' + Date.now();

  if (isRepair) {
    const badge = document.createElement('div');
    badge.className = 'repair-badge';
    badge.textContent = '\u26A1 Constraint Repair Mode active';
    wrap.appendChild(badge);
  }

  const hdr = document.createElement('div'); hdr.className = 'ahdr';
  const dot = document.createElement('div'); dot.className = 'dot'; dot.style.background = color;
  const aname = document.createElement('span'); aname.className = 'aname'; aname.style.color = color; aname.textContent = name;
  const amodel = document.createElement('span'); amodel.className = 'amodel'; amodel.textContent = model;
  hdr.appendChild(dot); hdr.appendChild(aname); hdr.appendChild(amodel);
  // Non-TC badge: warning when the model does not support tool calls
  if (model && _isNoTc(model)) {
    var _noTcBadge = document.createElement('span');
    _noTcBadge.className = 'no-tc-badge';
    _noTcBadge.title = model + ' does not support tool calls (no function calling)';
    _noTcBadge.textContent = 'NO TC';
    hdr.appendChild(_noTcBadge);
  }
  if (role) {
    const arole = document.createElement('span'); arole.className = 'arole'; arole.textContent = '\u00B7 ' + role;
    hdr.appendChild(arole);
  }
  const atimer = document.createElement('span'); atimer.className = 'atimer'; atimer.id = tid;
  hdr.appendChild(atimer);
  // VOICE (1.4): the speak button lives in a FOOTER under the answer, not in the
  // header. In the header it floated top-right and, on an empty bubble, looked
  // like a stray icon next to the user's message (owner screenshot). It also only
  // appears once the bubble has text.
  var _speakWrap = document.createElement('div');
  _speakWrap.className = 'amsg-foot';
  _speakWrap.id = 'foot-' + tid;
  var _speakBtn = document.createElement('button');
  _speakBtn.className = 'msg-speak';
  _speakBtn.type = 'button';
  _speakBtn.title = 'Speak this answer';
  _speakBtn.textContent = '\uD83D\uDD0A';
  _speakBtn.style.display = 'none';
  _speakBtn.onclick = function () {
    var _body = document.getElementById('ab-' + tid);
    voiceSpeakText(_answerTextOnly(_body), _speakBtn);
  };
  _speakWrap.appendChild(_speakBtn);

  const _lt = _attachLoadTimer(hdr, tid, '#20b0a0');

  const body = document.createElement('div'); body.className = 'abody live'; body.id = 'ab-' + tid;
  const thinkBlock = _buildAgentThinkBlock(tid);
  wrap.appendChild(hdr); wrap.appendChild(thinkBlock); wrap.appendChild(body);
  wrap.appendChild(_speakWrap);
  c.appendChild(wrap);
  S.curAgent = {body: body, tid: tid, name: name, lt: _lt};
  if ((name === 'Answer' || name === 'Direct') && model) {
    S._lastDirectRunModel = model;
    if (typeof updateModelsChip === 'function') updateModelsChip();
    if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  }
  // THINK-BLOCK-FIX (2026-08-19): new agent → discard the old think-block reference.
  // Otherwise the next agent with thinking (e.g. Direct after Vision-Agent) streams into
  // the think block of the PREVIOUS agent at the top of the chat.
  S._thinkBlockId = null;
  S._thinkBlockCache = null;
  scrollBtmIfNearBottom(120);
}

function _attachLoadTimer(hdr, tid, baseColor) {
  const _lh = document.createElement('span');
  _lh.id = 'lh-' + tid;
  _lh.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:9px;color:' + baseColor + ';margin-left:8px;display:none';
  _lh.textContent = '⟳ Loading... 0s';
  hdr.appendChild(_lh);
  return setTimeout(function() {
    const h = document.getElementById('lh-' + tid);
    if (!h) return;
    h.style.display = 'inline';
    var _sec = 0;
    h._loadTimer = setInterval(function() {
      _sec++;
      if (!document.getElementById('lh-' + tid)) { clearInterval(h._loadTimer); h._loadTimer = null; return; }
      h.textContent = '⟳ Loading... ' + _sec + 's';
      h.style.color = _sec > 20 ? '#e09030' : baseColor;
    }, 1000);
  }, 2000);
}

function _toggleThinkBlock(tb) {
  if (!tb) return;
  var currentlyOpen = tb.classList.contains('open')
    || (tb.classList.contains('live') && !tb.classList.contains('user-collapsed'));
  var nextOpen = !currentlyOpen;
  tb.classList.toggle('open', nextOpen);
  tb.classList.toggle('user-collapsed', !nextOpen);
  tb.dataset.userToggled = '1';
}

function _buildAgentThinkBlock(tid) {
  const thinkBlock = document.createElement('div'); thinkBlock.className = 'think-block live'; thinkBlock.id = 'tk-' + tid; thinkBlock.style.display = 'none';
  const thinkHdr = document.createElement('div'); thinkHdr.className = 'think-hdr';
  thinkHdr.innerHTML = '<span class="th-label">🧠 Thinking</span><span class="th-tokens" id="tkt-' + tid + '"></span><span class="th-chevron">▲</span>';
  thinkHdr.onclick = function() { _toggleThinkBlock(thinkBlock); };
  const thinkBody = document.createElement('div'); thinkBody.className = 'think-body'; thinkBody.id = 'thb-' + tid;
  thinkBlock.appendChild(thinkHdr); thinkBlock.appendChild(thinkBody);
  return thinkBlock;
}

function startDuoAgent(role, label, model, roundInfo) {
  // Same structure as startAgent, but with the duo CSS class
  rmEl('status-el');
  _tokenQueue = [];
  if (_tokenRafId) { cancelAnimationFrame(_tokenRafId); _tokenRafId = null; }
  const isDuoCoder  = role === 'coder';
  const color       = isDuoCoder ? '#20b0a0' : '#d08020';
  const roleLabel   = isDuoCoder ? 'Write & improve code' : 'Find bugs & edge cases';
  const c           = document.getElementById('chat');
  const wrap        = document.createElement('div');
  wrap.className    = 'msg ablock ' + (isDuoCoder ? 'duo-coder' : 'duo-critic');
  const tid         = 't' + Date.now();

  const hdr = document.createElement('div'); hdr.className = 'ahdr';
  const dot = document.createElement('div'); dot.className = 'dot'; dot.style.background = color;
  const aname = document.createElement('span'); aname.className = 'aname'; aname.style.color = color;
  aname.textContent = label;
  const amodel = document.createElement('span'); amodel.className = 'amodel'; amodel.textContent = model;
  const arole  = document.createElement('span'); arole.className  = 'arole';
  arole.textContent = '\u00B7 ' + roleLabel;
  const arnd   = document.createElement('span'); arnd.className   = 'arole';
  arnd.textContent  = ' \u00B7 ' + roundInfo;
  const atimer = document.createElement('span'); atimer.className = 'atimer'; atimer.id = tid;

  hdr.appendChild(dot); hdr.appendChild(aname); hdr.appendChild(amodel);
  hdr.appendChild(arole); hdr.appendChild(arnd); hdr.appendChild(atimer);
  const _lt = _attachLoadTimer(hdr, tid, '#7a8fa8');
  const body = document.createElement('div'); body.className = 'abody live'; body.id = 'ab-' + tid;
  const thinkBlock = _buildAgentThinkBlock(tid);
  var _speakWrap = document.createElement('div');
  _speakWrap.className = 'amsg-foot';
  _speakWrap.id = 'foot-' + tid;
  var _speakBtn = document.createElement('button');
  _speakBtn.className = 'msg-speak';
  _speakBtn.type = 'button';
  _speakBtn.title = 'Speak this answer';
  _speakBtn.textContent = '\uD83D\uDD0A';
  _speakBtn.style.display = 'none';
  _speakBtn.onclick = function () {
    var _body = document.getElementById('ab-' + tid);
    voiceSpeakText(_answerTextOnly(_body), _speakBtn);
  };
  _speakWrap.appendChild(_speakBtn);
  wrap.appendChild(hdr); wrap.appendChild(thinkBlock); wrap.appendChild(body);
  wrap.appendChild(_speakWrap);
  c.appendChild(wrap);
  S.curAgent = {body: body, tid: tid, name: label, lt: _lt};
  S._thinkBlockId = null;
  S._thinkBlockCache = null;
  scrollBtmIfNearBottom(120);
}

function _renderThinkCodeBlocks(thinkBodyEl) {
  // THINK-CODE-FORMAT (2026-09-17): reasoning text often contains ```lang
  // fenced code (the model explains a bug by quoting code). Render those
  // fences as styled <pre> blocks once the block closes — streaming keeps
  // plain textContent for speed.
  try {
    var raw = thinkBodyEl.textContent || '';
    if (raw.indexOf('```') === -1) return;
    var out = '';
    var parts = raw.split('```');
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        var body = parts[i];
        var nl = body.indexOf('\n');
        if (nl !== -1 && nl <= 20 && /^[a-zA-Z0-9+#._-]*$/.test(body.slice(0, nl).trim())) {
          body = body.slice(nl + 1);  // strip the language tag line
        }
        out += '<pre class="think-code">' + body
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          + '</pre>';
      } else {
        out += document.createElement('div').appendChild(
          document.createTextNode(parts[i])).parentNode.innerHTML;
      }
    }
    thinkBodyEl.innerHTML = out;
  } catch (e) { /* formatting must never break the stream */ }
}

function _createThinkBlock() {
  var _body = document.getElementById('ab-' + S.curAgent.tid);
  if (!_body) return null;
  S._thinkSeq = (S._thinkSeq || 0) + 1;
  var _id  = 'thk-' + S.curAgent.tid + '-' + S._thinkSeq;
  var _tb  = document.createElement('div');
  _tb.className = 'think-block live open';
  _tb.id = _id;
  _tb.style.cssText = 'margin:4px 0 4px;';
  _tb.innerHTML = '<div class="think-hdr" onclick="_toggleThinkBlock(this.parentElement)">'
    + '<span class="th-label">\uD83E\uDDE0 Thinking</span>'
    + '<span class="th-tokens"></span>'
    + '<span class="th-chevron">\u25B2</span></div>'
    + '<div class="think-body"></div>';
  _body.appendChild(_tb);
  _liveMdReset(_body);  // next text token starts a fresh live-md segment after this think block
  scrollBtmIfNearBottom(80);
  return _id;
}

function renderDuoVerdict(verdict, elapsed, approved) {
  // Rendert das strukturierte Critic-Verdict direkt in den Chat —
  // kein Streaming, kein leerer Agent-Block.
  const c       = document.getElementById('chat');
  const issues  = verdict.issues  || [];
  const summary = verdict.verdict || (approved ? 'Code correct' : 'Issues found');

  const box = document.createElement('div');
  box.className = 'msg duo-verdict' + (approved ? ' approved' : '');

  const hdr = document.createElement('div');
  hdr.className = 'duo-verdict-hdr ' + (approved ? 'pass' : 'fail');
  hdr.innerHTML = (approved
      ? '\u2713 CRITIC: CODE APPROVED'
      : '\u26A0 CRITIC: ' + issues.length + ' ISSUE' + (issues.length !== 1 ? 'S' : '') + ' FOUND')
    + ' &nbsp;\u00B7 ' + esc(summary)
    + '<span class="duo-verdict-meta">' + (elapsed || '') + (elapsed ? 's' : '') + '</span>';
  box.appendChild(hdr);

  if (!approved && issues.length) {
    const issueWrap = document.createElement('div');
    issueWrap.className = 'duo-verdict-issues';
    issues.forEach(function(iss) {
      const el = document.createElement('div');
      el.className = 'duo-verdict-issue';
      el.textContent = iss;
      issueWrap.appendChild(el);
    });
    box.appendChild(issueWrap);
  }

  c.appendChild(box);
  scrollBtmIfNearBottom(120);
}

function _stripToolCallXmlChunk(t) {
  if (!S.curAgent) return t;
  if (!S.curAgent._toolXmlCarry) S.curAgent._toolXmlCarry = '';
  var raw = S.curAgent._toolXmlCarry + String(t || '');
  S.curAgent._toolXmlCarry = '';
  var OPEN = '<tool_call>';
  var CLOSE = '</tool_call>';
  while (true) {
    var s = raw.indexOf(OPEN);
    if (s < 0) break;
    var e = raw.indexOf(CLOSE, s);
    if (e < 0) {
      S.curAgent._toolXmlCarry = raw.slice(s);
      raw = raw.slice(0, s);
      break;
    }
    raw = raw.slice(0, s) + raw.slice(e + CLOSE.length);
  }
  return raw;
}

function _sanitizeRuntimeToken(t) {
  var txt = String(t || '');
  // remove tool badges completely from the flow text (they are shown visually in the fc panel).
  txt = txt.replace(/(?:^|\n)\s*🔧\s*`([\s\S]{0,10000}?)`\s*(?=\n|$)/g, '');

  // if plain-text write_file calls still occur, suppress them too.
  txt = txt.split('\n').map(function(line) {
    if (/(?:^|\s|[`'"])(write_file_append|edit_file)\(\s*path\s*=\s*['\"]([^'\"]+)['\"]/i.test(line)) {
      return '';
    }
    return line;
  }).join('\n');
  // hide internal retry/loop hints (dev diagnostics, not user content).
  txt = txt.replace(/(?:^|\n)\[(?:⚠\s*)?write_file[^\]]*(?:zu groß|Split-Loop)[^\]]*\](?=\n|$)/gi, '');
  txt = txt.replace(/(?:^|\n)\[(?:⚠\s*)?write_file_append[^\]]*(?:zu groß|Split-Loop)[^\]]*\](?=\n|$)/gi, '');
  txt = txt.replace(/(?:^|\n)\[JSON parse error #[^\]]*\](?=\n|$)/gi, '');
  txt = txt.replace(/(?:^|\n)\[Duplicate [^\]]+\](?=\n|$)/g, '');
  txt = txt.replace(/(?:^|\n)\[Tool[- ]loop: [^\]]+\](?=\n|$)/gi, '');
  txt = txt.replace(/\n[ \t]+\n/g, '\n\n');
  txt = txt.replace(/(?:\n\s*){3,}/g, '\n\n');
  return txt;
}

