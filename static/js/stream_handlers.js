// stream_handlers.js — SSE event dispatch table (EVENT_HANDLERS) and handleEvent(d)

var EVENT_HANDLERS = {

  'run_id': function(d) {
    S.currentRunId = d.run_id;
    S._attachedRunId = d.run_id;
  },

  'heartbeat': function(d) {
    var _hbEl = document.getElementById('status-el');
    if (!_hbEl) {
      _hbEl = document.createElement('div');
      _hbEl.id = 'status-el';
      _hbEl.className = 'msg status-txt';
      _hbEl.style.cssText = 'color:#e0a030;font-style:italic;font-weight:600';
      document.getElementById('chat').appendChild(_hbEl);
    }
    var _pf = d.prefill;
    _hbEl.textContent = '\u23F3 Processing context\u2026 ' + (d.elapsed || '') + 's'
      + (_pf ? ' \u00b7 Prefill ' + Math.round((d.prefill.progress || 0) * 100) + '% ('
        + ((d.prefill.n || 0) >= 1000 ? ((d.prefill.n / 1000).toFixed(1) + 'k') : (d.prefill.n || 0))
        + (d.prefill.total ? '/' + ((d.prefill.total >= 1000) ? ((d.prefill.total / 1000).toFixed(1) + 'k') : d.prefill.total) : '')
        + ' tok \u00b7 ' + Math.round(d.prefill.tps || 0) + ' tok/s)' : '');
    scrollBtmIfNearBottom(120);
  },

  'usage_meta': function(d) {
    if (d.completion_tokens) S.perfRealTokens = (S.perfRealTokens || 0) + parseInt(d.completion_tokens, 10);
    S.perfInFlightChars = 0;
    S.perfInFlightTokens = 0;
    // DECODE-RATE (2026-10-07): the engine forwards llama.cpp timings
    // (predicted_per_second) per coder call - EMA smooths per-call noise.
    if (d.decode_tps) {
      var _dt = parseFloat(d.decode_tps);
      if (isFinite(_dt) && _dt > 0) {
        S.perfDecodeEma = S.perfDecodeEma > 0 ? (S.perfDecodeEma * 0.7 + _dt * 0.3) : _dt;
        _perfRender(false);
      }
    }
    // TOKEN-TRACKER UI (2026-08-25): collect the input/cache dimension too —
    // previously discarded even though the server accumulates it per phase.
    if (d.prompt_tokens) S.runPromptTokens = (S.runPromptTokens || 0) + parseInt(d.prompt_tokens, 10);
    if (d.cached_tokens) S.runCachedTokens = (S.runCachedTokens || 0) + parseInt(d.cached_tokens, 10);
    // AUDIT-R2+ (2026-08-25): request counters + live render of the perf pills —
    // in/cached/reqs are now visible DURING the run, not only in done.
    S.runRequestCount = (S.runRequestCount || 0) + 1;
    _perfRender(false);
    // TOKEN-DISPLAY-FIX (2026-08-31): set the thinking block to the REAL llama.cpp
    // reasoning-token count (phase=planner). Guard >0: the NT retry
    // (reasoning=0) must not overwrite the real value of the thinking run.
    if (d.phase === 'planner') {
      if (typeof _updatePlannerTokRate === 'function') _updatePlannerTokRate();
      if (d.reasoning_tokens > 0) {
        var _tcReal = document.getElementById('planner-think-token-count');
        if (_tcReal) {
          var _realTok = parseInt(d.reasoning_tokens, 10) || 0;
          // real llama.cpp value (without ≈) — replaces the chars/3 estimate.
          _tcReal.textContent = (_realTok > 999 ? (_realTok / 1000).toFixed(1) + 'k' : _realTok) + ' tok';
          _tcReal.style.color = '#4a3060';
        }
      }
    }
  },

  'steer': function(d) {
    // STEER CARD (2026-10-06): steering is part of the run's story — the
    // injected text renders as a persistent card in the chat flow instead
    // of a transient status divider. _autosaveChatMessage picks the card
    // up as an assistant part, so it survives reload via the existing
    // restore path (same trick the planner blocks use).
    var _steerTxt = String(d.content || '').trim();
    var _steerN = parseInt(d.images, 10) || 0;
    var _steerCard = document.createElement('div');
    _steerCard.className = 'msg steer-note';
    _steerCard.style.cssText = 'border:1px solid rgba(90,150,220,.4);background:rgba(90,150,220,.08);border-radius:6px;padding:6px 10px;margin:4px 0;font-size:12px;';
    var _steerHead = '\uD83D\uDED9 steer' + (_steerN > 0 ? ' (+' + _steerN + ' image' + (_steerN > 1 ? 's' : '') + ')' : '');
    _steerCard.innerHTML = '<div style="font-size:10px;font-weight:700;letter-spacing:.06em;color:#6a9ad0;text-transform:uppercase;">' + esc(_steerHead) + '</div>'
      + '<div style="white-space:pre-wrap;word-break:break-word;">' + esc(_steerTxt || '(image only)') + '</div>';
    // STEER PLACEMENT (2026-10-08, user feedback: the card belongs inside the run
    // block, next to the tool rows - not dangling at the chat end). Live in
    // the active coder bubble when one exists; chat flow as fallback.
    var _steerHost = (S.curAgent && document.getElementById('ab-' + S.curAgent.tid)) || document.getElementById('chat');
    _steerHost.appendChild(_steerCard);
    scrollBtmIfNearBottom(60);
  },

  'status': function(d) {
      // F5: render system hints as colored dividers
      var _sc = String(d.content || '');
      // COMPRESSION-CUT / TRUNCATION-CUT: render as an inline cut in the coder bubble
      var _isCutComp = _sc.indexOf('Context compressed') >= 0 || _sc.indexOf('Compression failed') >= 0;
      var _isCutWarn = _sc.indexOf('malformed/truncated') >= 0 || _sc.indexOf('repetition drift in tool-call arguments') >= 0 || _sc.indexOf('[Tool loop:') >= 0;
      if (_isCutComp || _isCutWarn) {
        var _cutBody = null;
        if (S.curAgent) { _cutBody = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body; }
        if (_cutBody) {
          if (S._thinkBlockId) {
            var _tkCut = document.getElementById(S._thinkBlockId);
            if (_tkCut) _tkCut.classList.remove('live');
            S._thinkBlockId = null;
          }
          if (typeof rmEl === 'function') rmEl('status-el');
          var _cutDiv = document.createElement('div');
          _cutDiv.className = 'ctx-cut' + (_sc.indexOf('failed') >= 0 ? ' err' : (_isCutWarn ? ' warn' : ''));
          _cutDiv.innerHTML = '<span>' + esc(_sc) + '</span>';
          _cutBody.appendChild(_cutDiv);
          _liveMdReset(_cutBody);
          scrollBtmIfNearBottom(60);
          return;
        }
      }
      var _stKeywords = {
        'AUTO-STOP':       { icon: '\u26A0', color: '#d0a020', border: 'rgba(208,160,32,.35)' },
        'VERIFY REQUIRED': { icon: '\u2714', color: '#3b82f6', border: 'rgba(59,130,246,.35)' },
        'CRITICAL STALL':  { icon: '\u2716', color: '#c04040', border: 'rgba(200,64,64,.4)' },
        'GRACE ROUND':     { icon: '\u23F1', color: '#808080', border: 'rgba(128,128,128,.35)' },
        '[CTX':            { icon: '\uD83D\uDCE6', color: '#808080', border: 'rgba(128,128,128,.3)' },
      };
      for (var _stKw in _stKeywords) {
        if (_sc.indexOf(_stKw) !== -1) {
          var _stCfg = _stKeywords[_stKw];
          var _stDiv = document.createElement('div');
          _stDiv.className = 'msg divider';
          _stDiv.style.cssText = 'color:' + _stCfg.color + ';border-color:' + _stCfg.border + ';font-size:10px;letter-spacing:.05em;font-weight:600;padding:3px 0;';
          _stDiv.textContent = _stCfg.icon + ' ' + _sc;
          document.getElementById('chat').appendChild(_stDiv);
          scrollBtmIfNearBottom(60);
          return;
        }
      }
      // Exploration completion → mark prex-info bar as done
      if (_sc.indexOf('Exploration complete') >= 0 || _sc.indexOf('exploration complete') >= 0) {
        _prexMarkDone(false);
      } else if (_sc.indexOf('time budget reached') >= 0 || _sc.indexOf('timeout guard') >= 0 || _sc.indexOf('Timeout') >= 0 && _sc.indexOf('Pre-Explore') >= 0) {
        _prexMarkDone(true);
      }
      showStatus(d.content);
  },

  'planner_start': function(d) {
    // Planner beginnt — slim Bubble + standalone Thinking-Block in #chat
    rmEl('status-el');
    rmEl('soft-plan-panel');
    // remove old standalone planner blocks from the chat
    rmEl('planner-think-block');
    rmEl('planner-plan-block');
    if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
    window._plannerTickStart = Date.now();
    window._plannerThinking = !!d.thinking;
    window._plannerFirstRealToken = false;
    window._plannerId = d.planner_id || null;
    window._plannerStreamingExpected = (typeof d.streaming_expected === 'undefined') ? true : !!d.streaming_expected;
    window._plannerWillBuffer = !!d.will_buffer;
    window._plannerPlanStarted = false;
    window._plannerThinkManualOpen = false;
    window._plannerThinkCharCount = 0;
    if (!S.perfRunStartedAt) _perfResetRuntimeState();
    else S.perfFirstTokenAt = 0;
    _addFlowConnector('#4870c0', 0.07, 'Planner', 'Split task', d.thinking ? 'Thinking' : 'Plan');
    var _mdlShort = (d.model || '').split(':')[0] || 'Planner';
    window._plannerModelShort = _mdlShort;
    // ── Slim planner bubble (status strip) ────────────────────────────────
    upsertPlannerBubble('');
    var _pb = document.getElementById('planner-bubble');
    if (_pb) {
      var _phdr = _pb.querySelector('.ahdr');
      if (_phdr) {
        var _modeLabel = (d.thinking ? 'Thinking' : 'Plan') + ' \u00b7 ' + _mdlShort;
        _phdr.innerHTML = ''
          + '<div class="dot" style="background:#8c64b4;animation:pulse 1.2s ease-in-out infinite"></div>'
          + '<span class="aname" style="color:#9a74dc">Planner</span>'
          + '<span class="amodel"><span id="planner-tokrate">&nbsp;</span> ' + _modeLabel + ' &middot; <span id="planner-elapsed">0s</span></span>';
      }
    }
    // ── Standalone thinking block in #chat (directly after the bubble) ─────────
    if (d.thinking) {  // only create if the model really uses thinking (fallback creation in planner_thinking_token)
      var _c = document.getElementById('chat');
      var _det = document.createElement('details');
      _det.id = 'planner-think-block';
      _det.className = 'msg planner-think-block';
      _det.open = true;
      _det.innerHTML = '<summary>'
        + '<span id="planner-think-toggle-icon" style="font-size:10px">\u25bc</span>'
        + '<span>Thinking</span>'
        + '<span id="planner-think-token-count" style="color:#6a5080;font-size:10px;margin-left:4px">0 tok</span>'
        + '<span id="planner-thinking-indicator" style="margin-left:auto;opacity:.65;font-size:10px">'
        + (d.thinking ? '\uD83E\uDDE0 thinking\u2026 ' : '\uD83E\uDDE9 planning\u2026 ')
        + '</span>'
        + '</summary>'
        + '<pre id="planner-think-content" style=""></pre>';
      _det.addEventListener('toggle', function() {
        var _ico = document.getElementById('planner-think-toggle-icon');
        if (_ico) _ico.textContent = _det.open ? '\u25bc' : '\u25b6';
        if (_det.open) window._plannerThinkManualOpen = true;
      });
      _c.appendChild(_det);
    }
    // client-side second ticker
    window._plannerTickInterval = setInterval(function() {
      var _el = document.getElementById('planner-elapsed');
      if (_el) {
        var _sec = Math.round((Date.now() - (window._plannerTickStart || Date.now())) / 1000);
        var _m = Math.floor(_sec / 60);
        var _s = _sec % 60;
        _el.textContent = _m > 0 ? (_m + 'm ' + _s + 's') : (_s + 's');
      }
    }, 1000);
    scrollBtmIfNearBottom(120);
  },

  'planner_thinking': function(d) {
    // heartbeat: planner is running — update the pulse indicator while no real tokens are there yet
    var _thinkContent = document.getElementById('planner-think-content');
    if (_thinkContent && !window._plannerFirstRealToken) {
      var _hbSec = d.elapsed || Math.round((Date.now() - (window._plannerTickStart || Date.now())) / 1000);
      _thinkContent.textContent = '\u23f3 Thinking\u2026 ' + _hbSec + 's';
      var _detHb = document.getElementById('planner-think-block');
      if (_detHb && !_detHb.open) _detHb.open = true;
    }
  },

  'planner_thinking_progress': function(d) {
    // BUG-2-FIX: progress event with buffered thinking (distilled models, llama.cpp).
    // The model thinks internally — no token stream until the <think> block is done (up to 120s).
    // This event arrives every ~5s and shows progress in the UI instead of permanently "0 tok · thinking..."
    var _thinkProg = document.getElementById('planner-think-content');
    if (_thinkProg && !window._plannerFirstRealToken) {
      var _progSec = d.elapsed || Math.round((Date.now() - (window._plannerTickStart || Date.now())) / 1000);
      _thinkProg.textContent = '\u23f3 Model is thinking internally (buffered)\u2026 ' + _progSec + 's\n'
        + '(tokens appear only after the thinking block finishes)';
      var _detProg = document.getElementById('planner-think-block');
      if (_detProg && !_detProg.open) _detProg.open = true;
    }
    // Token-Count auf "buffered" setzen solange kein echter Token da ist
    var _tcProg = document.getElementById('planner-think-token-count');
    if (_tcProg && !window._plannerFirstRealToken) {
      _tcProg.textContent = 'buffered\u2026';
      _tcProg.style.color = '#8c64b4';
    }
  },

  'planner_thinking_token': function(d) {
    _perfOnToken(d.content);
    _updatePlannerTokRate();
    var _thinkEl = document.getElementById('planner-think-content');
    // fallback: create the standalone block if planner_start has not created it yet
    if (!_thinkEl) {
      var _c2 = document.getElementById('chat');
      if (_c2 && !document.getElementById('planner-think-block')) {
        var _detFb = document.createElement('details');
        _detFb.id = 'planner-think-block';
        _detFb.className = 'msg planner-think-block';
        _detFb.open = true;
        _detFb.innerHTML = '<summary>'
          + '<span id="planner-think-toggle-icon" style="font-size:10px">\u25bc</span>'
          + '<span>Thinking</span>'
          + '<span id="planner-think-token-count" style="color:#6a5080;font-size:10px;margin-left:4px">0 tok</span>'
          + '</summary>'
          + '<pre id="planner-think-content"></pre>';
        _detFb.addEventListener('toggle', function() {
          var _ico = document.getElementById('planner-think-toggle-icon');
          if (_ico) _ico.textContent = _detFb.open ? '\u25bc' : '\u25b6';
          if (_detFb.open) window._plannerThinkManualOpen = true;
        });
        // FIX (order fix): insert the thinking block BEFORE the plan block if it already exists.
        // Without the fix: _detFb gets appended at the end → appears AFTER the plan text when
        // planner_start is missing (e.g. llama.cpp buffering) and plan_token already arrived.
        var _existingPlanBlock = document.getElementById('planner-plan-block');
        if (_existingPlanBlock && _existingPlanBlock.parentNode === _c2) {
          _c2.insertBefore(_detFb, _existingPlanBlock);
        } else {
          _c2.appendChild(_detFb);
        }
        _thinkEl = document.getElementById('planner-think-content');
      }
    }
    if (_thinkEl) {
      if (!window._plannerFirstRealToken) {
        window._plannerFirstRealToken = true;
        _thinkEl.textContent = '';  // clear the pulse placeholder
        _thinkEl._tn = document.createTextNode('');
        _thinkEl.appendChild(_thinkEl._tn);
        _thinkEl._followBottom = true;
      }
      if (!_thinkEl._tn) { _thinkEl._tn = document.createTextNode(''); _thinkEl.appendChild(_thinkEl._tn); }
      _thinkEl._tn.appendData(d.content);
      var _tcEl = document.getElementById('planner-think-token-count');
      if (_tcEl) {
        window._plannerThinkCharCount = (window._plannerThinkCharCount || 0) + (d.content || '').length;
        // TOKEN-DISPLAY-FIX: chars/3 like bubble + server estimator (chars/4
        // underestimated live and diverged from #planner-tokrate).
        // ≈ marks the estimate; the real reasoning_tokens value (done)
        // overwrites the counter without ≈.
        var _approxTok = Math.round(window._plannerThinkCharCount / 3);
        _tcEl.textContent = '\u2248 ' + (_approxTok > 999 ? (_approxTok / 1000).toFixed(1) + 'k' : _approxTok) + ' tok';
      }
      var _det2 = document.getElementById('planner-think-block');
      if (_det2) {
        if (!_det2.open) _det2.open = true;
        if (_chatFollow) _scrollElToBottomSoon(_thinkEl);
      }
    }
    scrollBtmIfNearBottom(60);
  },

  'planner_plan_token': function(d) {
    _perfOnToken(d.content);
    _updatePlannerTokRate();
    // first plan token: close the thinking block, create the plan block in #chat
    if (!window._plannerPlanStarted) {
      window._plannerPlanStarted = true;
      // Thinking-Block finalisieren
      var _indEl2 = document.getElementById('planner-thinking-indicator');
      if (_indEl2) _indEl2.remove();
      var _tcFin = document.getElementById('planner-think-token-count');
      if (_tcFin) _tcFin.style.color = '#4a3060';
      var _det3 = document.getElementById('planner-think-block');
      if (_det3 && !window._plannerThinkManualOpen) _det3.open = false;
      // TOKEN-DISPLAY-FIX (2026-08-31): switch the bubble label from "Thinking" to "Plan".
      // Previously "Thinking · model" stayed, although plan tokens were already
      // counted during plan generation → apparent token inflation compared to the thinking block.
      var _pb2 = document.getElementById('planner-bubble');
      var _phdr2 = _pb2 ? _pb2.querySelector('.ahdr') : null;
      if (_phdr2) {
        var _am2 = _phdr2.querySelector('.amodel');
        if (_am2) {
          var _trEl2 = _phdr2.querySelector('#planner-tokrate');
          var _elEl2 = _phdr2.querySelector('#planner-elapsed');
          var _trTxt2 = _trEl2 ? _trEl2.textContent : '\u00a0';
          var _elTxt2 = _elEl2 ? _elEl2.textContent : '0s';
          _am2.innerHTML = '<span id="planner-tokrate"></span> Plan \u00b7 ' + esc(window._plannerModelShort || '') + ' &middot; <span id="planner-elapsed"></span>';
          var _newTr2 = _am2.querySelector('#planner-tokrate');
          var _newEl2 = _am2.querySelector('#planner-elapsed');
          if (_newTr2) _newTr2.textContent = _trTxt2;
          if (_newEl2) _newEl2.textContent = _elTxt2;
        }
      }
      // create the standalone plan block in #chat
      if (!document.getElementById('planner-plan-block')) {
        var _cPlan = document.getElementById('chat');
        if (_cPlan) {
          var _planDiv = document.createElement('div');
          _planDiv.id = 'planner-plan-block';
          _planDiv.className = 'msg planner-plan-block';
          _planDiv.innerHTML = '<div class="pplan-hdr">\uD83D\uDCCB Plan</div>'
            + '<div id="planner-plan-content"></div>';
          _cPlan.appendChild(_planDiv);
        }
      }
    }
    var _planEl = document.getElementById('planner-plan-content');
    if (_planEl) {
      if (!_planEl._tn) { _planEl._tn = document.createTextNode(''); _planEl.appendChild(_planEl._tn); }
      _planEl._tn.appendData(d.content);
      scrollBtmIfNearBottom(120);
    }
  },

  'planner_token': function(d) {
    _perfOnToken(d.content);
    _updatePlannerTokRate();
    // legacy: live tokens from the soft planner
    if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
    // create the standalone plan block if not present
    if (!window._plannerPlanStarted) {
      window._plannerPlanStarted = true;
      var _indLeg = document.getElementById('planner-thinking-indicator');
      if (_indLeg) _indLeg.remove();
      if (!document.getElementById('planner-plan-block')) {
        var _cLeg = document.getElementById('chat');
        if (_cLeg) {
          var _pLeg = document.createElement('div');
          _pLeg.id = 'planner-plan-block';
          _pLeg.className = 'msg planner-plan-block';
          _pLeg.innerHTML = '<div class="pplan-hdr">\uD83D\uDCCB Plan</div><div id="planner-plan-content"></div>';
          _cLeg.appendChild(_pLeg);
        }
      }
    }
    var _pb2 = document.getElementById('planner-plan-content');
    if (_pb2) {
      if (!_pb2._tn) { _pb2._tn = document.createTextNode(''); _pb2.appendChild(_pb2._tn); }
      _pb2._tn.appendData(d.content);
      scrollBtmIfNearBottom(120);
    }
  },

  'planner_done': function(d) {
    if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
    var _wasManualOpen = !!window._plannerThinkManualOpen;  // BUG-1 FIX: save BEFORE reset
    window._plannerPlanStarted = false;
    window._plannerThinkTokCount = 0;
    window._plannerThinkCharCount = 0;
    window._plannerFirstRealToken = false;
    window._plannerThinkManualOpen = false;
    // remove the thinking indicator in the standalone block
    var _indDone = document.getElementById('planner-thinking-indicator');
    if (_indDone) _indDone.remove();
    // collapse the thinking block (keep open if the user interacted manually)
    var _detDone = document.getElementById('planner-think-block');
    if (_detDone && !_wasManualOpen) _detDone.open = false;
    _renderPlanMarkdown();
    finalizePlannerBubble(d.summary || 'Planning complete');
  },

  'worker_slot_failed': function(d) {
    var _wfModel = d.model || '?';
    var _wfPort  = (typeof d.port === 'number') ? (' @' + d.port) : '';
    var _wfPhase = d.phase ? (' [' + d.phase + ']') : '';
    var _wfReason = d.reason ? (' — ' + d.reason) : '';
    addDiv('⚠ Worker failed: ' + _wfModel + _wfPort + _wfPhase + _wfReason);
    showStatus('⚠ Worker error: ' + _wfModel + _wfReason);
    var _wfPortN = (typeof d.port === 'number') ? d.port : null;
    if (_wfPortN !== null) {
      var _wfChip = document.querySelector('.prex-chip[data-port="' + _wfPortN + '"]');
      if (_wfChip) _wfChip.classList.add('fail');
    }
    var _pf = document.getElementById('prex-fail');
    if (_pf) {
      var _n = parseInt(_pf.dataset.n || '0', 10);
      _n = isNaN(_n) ? 1 : (_n + 1);
      _pf.dataset.n = String(_n);
      _pf.style.display = 'inline-flex';
      _pf.textContent = '⚠ ' + _n + ' workers failed';
    }
  },

  'worker_pool_state': function(d) {
    S.prexPoolState = d;
    _applyWorkerPoolStateToUi();
  },
  // UI-BUG-3 FIX: pre_explore_done — dedicated event instead of fragile status-string matching.
  // Before: _prexMarkDone() only on "Exploration complete" in the status text →
  // with empty/broken _explore_ctx this status was never sent → the bar stayed stuck.

  'pre_explore_done': function(d) {
    _prexMarkDone(d.status === 'timeout' || d.status === 'error');
    if (d.status === 'error' || !d.has_ctx) {
      var _pxWarn = document.getElementById('prex-info');
      if (_pxWarn) _pxWarn.style.opacity = '0.6';
    }
  },

  'pre_explore_info': function(d) {
    // UI-FIX (2026-08-08): if a bar already existed and files were already
    // counted (double pre_explore_info event), do not reset the counter to 0 —
    // otherwise "0 read" flashes briefly at the end.
    var _prevBarRead = (document.getElementById('prex-info') !== null) ? (window._prexFilesRead || 0) : 0;
    rmEl('prex-info');
    var _chat = document.getElementById('chat');
    var _wrap = document.createElement('div');
    _wrap.className = 'prex-info';
    _wrap.id = 'prex-info';

    var _mode = d.mode === 'parallel' ? 'PRE-EXPLORE PARALLEL' : 'PRE-EXPLORE';
    var _parts = (typeof d.n_partitions === 'number') ? (d.n_partitions + ' partitions') : '';
    var _files = '';
    var _nAssigned = (typeof d.n_files_unique === 'number') ? d.n_files_unique
                   : (typeof d.n_files_total === 'number') ? d.n_files_total : 0;
    if (_nAssigned > 0) {
      _files = _nAssigned + ' assigned';
    }
    window._prexFilesAssigned = (typeof d.n_files_unique === 'number') ? d.n_files_unique
                               : (typeof d.n_files_total === 'number') ? d.n_files_total : 0;
    if (window._prexFilesRead === undefined) window._prexFilesRead = 0;

    var _workers = Array.isArray(d.workers) ? d.workers : [];
    var _variantSeen = Object.create(null);
    var _laneVariants = Object.create(null);
    var _chips = _workers.map(function(w) {
      var _rawModel = (w && w.model) ? String(w.model) : '?';
      var _m = esc(_rawModel);
      var _portNum = (w && typeof w.port === 'number' && w.port > 0) ? w.port : 0;
      var _p = _portNum > 0 ? ('@' + _portNum) : '@?';
      var _np = (w && typeof w.n_parallel === 'number') ? ('p=' + w.n_parallel) : 'p=1';
      var _pdata = _portNum > 0 ? (' data-port="' + _portNum + '"') : '';
      var _variant = _workerVariantIndex(_rawModel, _variantSeen);
      var _clr = _workerColorForVariant(_variant);
      _laneVariants[_rawModel + '@' + _portNum] = _variant;
      var _style = ' style="--w-bd:' + _clr.chipBd + ';--w-bg:' + _clr.chipBg + ';--w-fg:' + _clr.chipFg + ';--w-port:' + _clr.port + '"';
      return '<span class="prex-chip"' + _pdata + _style + '><span class="px-model">' + _m + '</span><span class="px-port">' + _p + ' · ' + _np + '</span></span>';
    }).join('');
    window._prexModelVariantSeen = _variantSeen;
    window._prexLaneVariantByKey = _laneVariants;

    var _initReadTxt = _prevBarRead > 0
      ? (_prevBarRead + ' read / ' + _nAssigned + ' (' + Math.round(_prevBarRead / _nAssigned * 100) + '%)')
      : '0 read';
    _wrap.innerHTML = ''
      + '<span class="prex-title">' + _mode + '</span>'
      + (_parts ? '<span class="prex-sep">|</span><span class="prex-stat">' + esc(_parts) + '</span>' : '')
      + (_files ? '<span class="prex-sep">|</span><span class="prex-stat" id="prex-assigned">' + esc(_files) + '</span>' : '')
      + '<span class="prex-sep">|</span><span class="prex-stat" id="prex-read-count" style="color:var(--color-text-secondary)">' + _initReadTxt + '</span>'
      + '<span class="prex-sep">|</span><span class=        "prex-workers">' + (_chips || '<span class="prex-stat">no workers</span>') + '</span>'
      + '<span class="prex-fail" id="prex-fail" data-n="0"></span>';

    _chat.appendChild(_wrap);
    _chat.scrollTop = _chat.scrollHeight;
    _applyWorkerPoolStateToUi();
  },

  'pre_explore_partition_shape': function(d) {
    var _sc = parseInt(d.shared_count || 0, 10) || 0;
    var _nc = parseInt(d.normal_count || 0, 10) || 0;
    var _cfg = parseInt(d.configured_partitions || 0, 10) || 0;
    var _eff = parseInt(d.effective_partitions || 0, 10) || 0;
    var _wa = parseInt(d.worker_active || 0, 10) || 0;
    var _wt = parseInt(d.worker_target || 0, 10) || 0;
    showStatus(
      '🔎 Partition-Shape: shared=' + _sc
      + ', normal=' + _nc
      + ', cfg=' + _cfg
      + ', eff=' + _eff
      + ', worker=' + _wa + '/' + _wt
    );
    // set the worker count in the part-grid header as soon as it is known —
    // prevents it jumping from "1 worker" to "2 workers" when W2 starts.
    var _pgHdrPre = document.getElementById('part-grid-hdr');
    if (_pgHdrPre && (_wa > 0 || _wt > 0)) {
      _pgHdrPre.textContent = 'Pre-Explore · ' + (_wa || _wt) + ' workers';
    }
  },

  'image_description': function(d) {
    // Show vision preprocessing result in chat
    showImageDescription(d.content);
  },

  'tool_gen': function(d) {
    // TOOL-GEN-STREAM (2026-09-17): live streaming of write-family tool call
    // arguments — the user sees the code being generated instead of waiting.
    if (d && d.content) _perfOnToken(d.content);
    _toolGenStream(d);
  },

  'tool_gen_reset': function(d) {
    _toolGenDone();
    if (S._thinkBlockId) {
      var _tkRst = document.getElementById(S._thinkBlockId);
      if (_tkRst) _tkRst.classList.remove('live');
      S._thinkBlockId = null;
    }
  },

  'complexity': function(d) {
    // ROUTE-FOR-SPEECH: "complex" (pipeline) and "code_duo" are multi-agent
    // runs - their final answer belongs to the agentic read-aloud switch.
    S._routeMultiAgent = (d.content === 'code_duo' || d.content === 'complex');
    // COMPLEXITY-UI (2026-08-27): badge only in the pipeline/automap/duo context
    // — in "simple" and agentic mode no complexity is shown.
    const _cxShow = !(S.mode === 'simple' || (S.mode === 'code_duo' && S.duoAgenticMode));
    const el = document.getElementById('h-complexity');
    if (!_cxShow) { if (el) el.style.display = 'none'; return; }
    const isComplex = d.content === 'complex';
    const isSimple  = d.content === 'simple' || d.content === 'trivial';
    // FIX: show the correct source — shortcut ≠ judge call
    const src = d.source || 'judge';
    let label;
    if (src === 'shortcut')     label = '\u26A1 ' + d.content;          // ⚡ trivial (no judge)
    else if (src === 'mode')    label = '\u25B6 ' + d.content;          // ▶ pipeline/simple (mode forces)
    else if (src === 'manual')  label = '\u270E ' + d.content;          // ✎ set manually
    else if (src === 'tool')    label = '\uD83D\uDD27 tool';            // 🔧 tool-agent routing
    else                        label = 'JUDGE \u2192 ' + d.content;   // judge decided
    el.textContent = label;
    el.className   = isComplex ? 'is-complex' : isSimple ? 'is-simple' : '';
    el.style.display = 'inline-block';
  },

  'pipeline_start': function(d) {
    S._routeMultiAgent = true;
    _setVramFast(60000);  // 60s fast polling while the pipeline runs
    if (S.mode === 'code_duo') {
      // duo → pipeline synthesis badge
      rmEl('status-el');
      var _synthBadge = document.createElement('div');
      _synthBadge.className = 'duo-synth-badge';
      _synthBadge.innerHTML = '\u2699 DUO \u2192 PIPELINE SYNTHESIS';
      document.getElementById('chat').appendChild(_synthBadge);
      scrollBtmIfNearBottom(120);
    } else {
      addDiv('Pipeline \u2014 ' + (d.content || '').slice(0, 50));
    }
  },

  'round': function(d) { addDiv('Round ' + d.n + ' / ' + d.total); },

  'agent': function(d) { startAgent(d.content, d.model || '', d.role || '', d.repair || false); },

  'token': function(d) {
    appendToken(d.content);
    _perfOnToken(d.content);
  },
  // THINKING-STREAM: live display of reasoning tokens in the 🧠 think block.
  // Thinking tokens live in inline blocks in the body — chronologically between tool calls.
  // Each new think block is created after the previous tool call.

  'thinking_token': function(d) {
    _perfOnToken(d.content);
    if (Object.keys(_tgCards).length > 0) _toolGenDone();
    if (S.curAgent) {
      if (typeof rmEl === 'function') rmEl('status-el');
      if (!S._thinkBlockId) {
        S._thinkBlockId = _createThinkBlock();
      }
      var _tbEl = S._thinkBlockId ? document.getElementById(S._thinkBlockId) : null;
      if (!_tbEl) return;
      _tbEl.style.display = '';
      _tbEl.classList.add('live');
      if (_tbEl.dataset.userToggled !== '1') _tbEl.classList.add('open');
      var _thbEl = _tbEl.querySelector('.think-body');
      if (_thbEl) {
        // PERF (2026-10-23): O(delta) append on a persistent text node — the
        // old `textContent +=` copied the whole block on EVERY event (O(n²)
        // over long thinking streams), plus a layout-forcing scroll per event.
        var _tn = _thbEl.firstChild;
        if (!(_tn && _tn.nodeType === Node.TEXT_NODE)) {
          _thbEl.textContent = '';
          _tn = document.createTextNode('');
          _thbEl.appendChild(_tn);
        }
        _tn.appendData(d.content || '');
        var _tktEl = _tbEl.querySelector('.th-tokens');
        var _nowTk = Date.now();
        if (_tktEl && (!_thbEl._tktAt || _nowTk - _thbEl._tktAt >= 500)) {
          _thbEl._tktAt = _nowTk;
          _tktEl.textContent = ' \u00b7 ' + _tn.length + ' chars';
        }
        if (_chatFollow) _scrollElToBottomSoon(_thbEl);
      }
      scrollBtmIfNearBottom(120);
    }
  },

  'agent_done': function(d) {
    _toolGenDone();
    // tune format: critic_tune arrives in the agent_done event
    if (d.critic_tune && S.curAgent) {
      const body = S.curAgent.body;
      if (body) body._criticTune = d.critic_tune;
    }
    doneAgent(d.elapsed);
    // FIX: refresh VRAM after an agent
    if (!_vramPending && _vramOpen) refreshVram();
  },
  // BUG-20 FIX: clear_agent empties the current bubble before the retry stream.
  // Prevents double output: [first attempt][retry] in the same bubble.

  'clear_agent': function(d) {
    if (S.curAgent) {
      const body = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
      if (body) {
        body.textContent = '';
        _liveMdReset(body);
        if (typeof _syncSpeakButtons === 'function') _syncSpeakButtons(body.closest('.ablock'));
      }
      S.curAgent._thinkBuf = '';
      S.curAgent._inThink  = false;
      S.curAgent._toolXmlCarry = '';
    }
  },

  'automap': function(d) {
    showAutomapEvent(d.task_type || d.content, d.assignments || {});
  },
  // ── Code Events ──────────────────────────────────────────

  'duo_start': function(d) {
    S._runIsDuo = true;   // chunked agentic run → graceful stop-after-chunk applies
    S._routeMultiAgent = true;
    _setVramFast(120000);  // 2min fast polling for the duo loop
    window._prexModelVariantSeen = Object.create(null);
    window._prexLaneVariantByKey = Object.create(null);
    S.prexPoolState = null;
    S._chunkingActive = false;  // Reset chunking state for new run
    S._lastChunkN = 0;          // Reset chunk counter
    // SUBTASK-CHECKLIST-RESET (2026-08-31): remove the old plan checklist of the previous
    // run (S._planChunks + DOM), otherwise it stays for the next run and shows
    // outdated subtasks (observed live "subtask not clean").
    S._planChunks = [];
    var _planChkStale = document.getElementById('plan-checklist');
    if (_planChkStale) _planChkStale.remove();
    S._coderHadToolCall = false; // Reset coder phase indicator
    S._coderHadWriteCall = false; // Reset write-phase separator
    rmEl('status-el');
    const c = document.getElementById('chat');

    // main banner — only model names, no round/tool/think spam
    // skip the banner if pre-explore is active — starts directly without model info
    var _showBanner = !(d.agentic_mode && !d.label);
    if (_showBanner) {
      const banner = document.createElement('div');
      banner.className = 'duo-banner';
      var _bannerParts = [
        '\u21C4 CODE',
        '<span class="duo-sep">\u2502</span>',
        esc(d.label || (d.coder + ' + ' + d.critic)),
      ];
      if (d.use_pipeline) _bannerParts.push('<span class="duo-sep">\u2502</span> +Pipeline');
      banner.innerHTML = _bannerParts.join(' ');
      if (d.no_think) banner.innerHTML += '<span class="duo-badge badge-nothink">NO THINK</span>';
      if (d.websearch_active) banner.innerHTML += '<span class="duo-badge badge-ws">WS</span>';
      var _ttm2 = S.duoToolThinkingMode || 'on_fail';
      if (_ttm2 !== 'off') banner.innerHTML += '<span class="duo-badge" style="background:rgba(72,120,192,.12);border:1px solid rgba(72,120,192,.3);color:#6ea0e0">THINK:' + esc(_ttm2.toUpperCase()) + '</span>';
      c.appendChild(banner);
    }
    var _runtimeInfo = document.createElement('div');
    _runtimeInfo.className = 'msg divider';
    _runtimeInfo.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:9px;letter-spacing:.05em;color:#7a8fa8;border-color:rgba(122,143,168,.25)';
    var _rtModel = (d.runtime_model_source === 'dropdown' && d.runtime_model_override)
      ? ('Coder: ' + d.runtime_model_override) : '';
    var _ctxText = d.runtime_ctx_target ? String(d.runtime_ctx_target) : '?';
    _runtimeInfo.textContent = [_rtModel, ('ctx=' + _ctxText)].filter(Boolean).join(' | ');
    c.appendChild(_runtimeInfo);
    // PERF-CONSOLIDATION (2026-08-26): thin ctx bar on top removed —
    // only the lower performance bar remains (ctx-% there, colored in the summary).
    _perfEnsureUiRow();
    _perfResetRuntimeState();
    var _rtCtx = parseInt(d.runtime_ctx_target || 0, 10);
    if (!isNaN(_rtCtx) && _rtCtx > 0) S.perfCtxLimit = _rtCtx;
    _perfRender(false);
    // the part grid is NOT created here — only on the first partition_start event.
    // This avoids an empty grid when the fallback to sequential applies.
    // remember n_partitions only as a hint for the grid (max columns)
    c.dataset.nPartitions = d.n_partitions || 0;

    // Non-TC warning: deepseek-r1 is not a tool-call model
    // if tool rounds are active but the coder has no TC → warning
    if (d.tool_rounds && !d.coder_tc) {
      var _tcWarn = document.createElement('div');
      _tcWarn.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:9px;color:#b04040;padding:3px 12px 6px;display:flex;align-items:center;gap:6px';
      _tcWarn.innerHTML = '<span class="no-tc-badge">NO TC</span> ' + esc(d.coder) + ' does not support tool calls \u2014 tool rounds are ignored';
      c.appendChild(_tcWarn);
    }
    // in agentic mode no critic runs → skip the NO-TC warning
    if (!d.critic_tc && !d.agentic_mode) {
      var _tcWarn2 = document.createElement('div');
      _tcWarn2.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:9px;color:#7a8fa8;padding:2px 12px 5px;display:flex;align-items:center;gap:6px';
      _tcWarn2.innerHTML = '<span class="no-tc-badge">NO TC</span> ' + esc(d.critic) + ' (Critic) no tool-call support \u2014 JSON analysis only';
      c.appendChild(_tcWarn2);
    }
    // Im Agentic-Mode: exec==coder → kein separater Executor
    if (d.executor && d.executor !== d.coder && d.tool_rounds && !d.agentic_mode) {
      var _execInfo = document.createElement('div');
      _execInfo.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:9px;color:#7a8fa8;padding:2px 12px 5px;display:flex;align-items:center;gap:6px';
      _execInfo.innerHTML = '<span style="color:#4878c0;padding:1px 4px;border-radius:2px;background:rgba(72,120,192,.15);border:1px solid rgba(72,120,192,.25);font-size:8px">EXEC</span> ' + esc(d.executor) + ' \u2014 tool calls &amp; pre-explore (no thinking)';
      c.appendChild(_execInfo);
    }
    // agentic mode: no separate info block needed (visible via the model name in the banner)
    scrollBtmIfNearBottom(120);
  },

  'planner_result': function(d) {
    var _chunkCount = Array.isArray(d.chunks) ? d.chunks.length : 0;
    var _quality = d.quality || {};
    var _fallback = !!_quality.used_fallback;
    var _parseMode = _quality.parse_mode ? String(_quality.parse_mode) : '';
    var _stepLimit = (typeof _quality.step_limit === 'number') ? _quality.step_limit : null;
    rmEl('status-el');
    _prexMarkDone(false); // pre-explore is complete when the planner is done

    // ── Soft-Planner: Briefing finalisieren ────────────────────────────────
    if (d.soft_plan) {
      var _briefing = (d.briefing || '').trim();
      var _research = Array.isArray(d.research) ? d.research : [];
      var _wasStreamed = !!d.briefing_streamed;
      _prexMarkDone(false);
      rmEl('soft-plan-panel');

      if (_wasStreamed) {
        // text already streamed live into the plan block — only finalize the slim bubble
        var _pw3 = document.getElementById('planner-bubble');
        if (_pw3) {
          _pw3.classList.remove('live');
          var _phdr3 = _pw3.querySelector('.amodel');
          if (_phdr3) _phdr3.textContent = 'Briefing \u2713';
          _pw3.id = '';  // decouple
          _stripPlannerLiveIds(_pw3);
        }
        var _chat3 = document.getElementById('chat');
        if (_research.length) {
          var _resEl3 = document.createElement('div');
          _resEl3.style.cssText = 'margin:2px 0 4px 14px;font-family:"IBM Plex Mono",monospace;font-size:10px;color:#7080a0;line-height:1.9';
          _resEl3.innerHTML = '<span style="color:#7a5aa8;text-transform:uppercase;letter-spacing:.07em;font-size:9px">Research: </span>'
            + _research.map(function(q){ return '<span style="background:rgba(72,120,192,.12);border:1px solid rgba(72,120,192,.22);border-radius:3px;padding:1px 6px;margin:0 2px;color:#7090c0">' + esc(q) + '</span>'; }).join(' ');
          _chat3.appendChild(_resEl3);
        }
        _renderPlanMarkdown();
        scrollBtmIfNearBottom(200);
      } else {
        // fallback: animate the briefing afterwards (no live stream)
        _streamBriefingToBubble(_briefing, _research, function() {
          _renderPlanMarkdown();
          scrollBtmIfNearBottom(200);
        });
      }
      return;
    }

    // ── Planner Result — clean plan display (no chunk-plan panel) ────────────
    // chunking runs invisibly in the background. UI shows only plan text + coder stream.
    // thinking block optionally as expandable details
    if (d.thinking && !document.getElementById('planner-think-content')) {
      var _cInj = document.getElementById('chat');
      if (_cInj) {
        var _injDet = document.createElement('details');
        _injDet.id = 'planner-think-block';
        _injDet.className = 'msg planner-think-block';
        _injDet.open = false;
        _injDet.innerHTML = '<summary>'
          + '<span id="planner-think-toggle-icon" style="font-size:10px">\u25b6</span>'
          + '<span>Thinking</span>'
          + '<span style="color:#6a5080;font-size:10px;margin-left:4px">' + Math.round(String(d.thinking).length / 4) + ' tok</span>'
          + '</summary>'
          + '<pre id="planner-think-content">'
          + d.thinking.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          + '</pre>';
        _injDet.addEventListener('toggle', function() {
          var _ico = document.getElementById('planner-think-toggle-icon');
          if (_ico) _ico.textContent = _injDet.open ? '\u25bc' : '\u25b6';
        });
        var _existPlan = document.getElementById('planner-plan-block');
        if (_existPlan) _cInj.insertBefore(_injDet, _existPlan);
        else _cInj.appendChild(_injDet);
      }
    }
    _decoupleFinishedPlannerBubble();
    var _planLabel = _chunkCount > 0
      ? ('Plan created: ' + _chunkCount + ' step' + (_chunkCount === 1 ? '' : 's'))
      : 'Briefing created';
    _renderPlanMarkdown();
    finalizePlannerBubble(_planLabel);
    // SUBTASK-CHECKLIST (2026-08-31): chunks merken + Checkliste im UI rendern.
    if (_chunkCount > 0) {
      S._planChunks = (Array.isArray(d.chunks) ? d.chunks : []).map(function(c){ return (c && c.title) ? c.title : String(c || ''); });
      _renderPlanChecklist();
    }
  },

  'duo_round': function(d) {
  if (S.curAgent) {
    S.curAgent._thinkBuf = '';
    S.curAgent._inThink  = false;
  }
  S._thinkBlockId = null;
  S._coderHadToolCall = false;
  var _isChunkRound = !!d.subtask;
  // CHUNK-WINDOW-DEDUP-FIX (2026-08-31): save the chunk number BEFORE advancing it
  // — previously the checklist block set S._lastChunkN = d.n, so the
  // comparison "d.n !== _lastChunkN" below was always false and the
  // chunk window was NEVER created (subtask bug in the UI).
  var _prevChunkN = S._lastChunkN || 0;
  // SUBTASK-CHECKLIST (2026-08-31): advance the current chunk per duo_round.
  // Even if d.subtask is empty (planner fallback without subtasks), the
  // chunk progress stays visible — previously nothing appeared with an empty subtask.
  if (S._planChunks && S._planChunks.length) {
    S._lastChunkN = d.n || 1;
    _updatePlanChecklist();
  }
  if (_isChunkRound) {
    // ── chunking mode: chunk window + separate coder block per chunk ──
    S._chunkingActive = true;
    // finalize the previous coder (if still open)
    if (S.curAgent) {
      doneAgent(S._coderElapsed || '');
      S._coderElapsed = null;
    }
    // only create the chunk window when the chunk number changes
    // (self-fix retry sends duo_round with the same number → no duplicate)
    var _lastChunkN = _prevChunkN;
    if (d.n !== _lastChunkN) {
      S._lastChunkN = d.n;
      var _cw = document.createElement('div');
      _cw.className = 'msg chunk-window';
      var _cwHdr = document.createElement('div');
      _cwHdr.className = 'chunk-window-hdr';
      var _cwTotal = d.total > 1 ? ('<span class="cw-total">/ ' + d.total + '</span>') : '';
      _cwHdr.innerHTML = '<div class="cw-dot"></div>'
        + '<span class="cw-num">Chunk ' + d.n + '</span>'
        + _cwTotal;
      var _cwBody = document.createElement('div');
      _cwBody.className = 'chunk-window-body';
      // subtask text: title + possibly multiline description
      var _subtaskText = String(d.subtask || '').trim();
      _cwBody.textContent = _subtaskText;
      _cw.appendChild(_cwHdr);
      _cw.appendChild(_cwBody);
      document.getElementById('chat').appendChild(_cw);
    }
    scrollBtmIfNearBottom(120);
  } else {
    // ── Normal mode: flow connector as before ──
    S._chunkingActive = false;
    var _rndLabel = 'Writing code';
    var _rndRound = d.total > 1 ? ('R' + d.n + ' / ' + d.total) : ('R' + d.n);
    _addFlowConnector('#20b0a0', 0.07, 'Coder', _rndLabel, _rndRound);
    scrollBtmIfNearBottom(120);
  }
  },

  'agent_asking': function(d) {
    S.agentPaused = true;
    S.agentQuestion = d.question;
    S.currentRunId = d.run_id || S.currentRunId;
    S.activeDecisionId = d.decision_id || '';
    setPauseBtnState('paused_by_ask_user');
    // ASK-CARD (2026-09-19): question, input and buttons in one card —
    // same lifecycle as the approval card (pinned at the content end,
    // update in place, cleared on resume/done).
    _renderAskCard({ run_id: d.run_id || S.currentRunId, question: d.question, decision_id: d.decision_id });
    if (S.duoUntilFinished && S.askUserTimeoutSeconds > 0) {
      startAskUserCountdown(S.askUserTimeoutSeconds);
    }
  },

  'ask_user_timeout_reached': function(d) {
    stopAskUserCountdown();
    showInfo('Auto-answer sent: "' + (d.auto_answer || S.askUserAutoAnswer) + '"');
    var _qDivTo = document.getElementById('ask-user-question');
    if (_qDivTo) _qDivTo.remove();
    var _askTo = document.getElementById('ask-user-card');
    if (_askTo) _askTo.remove();
    S.agentPaused = false;
    S.agentQuestion = null;
    var _inpTo = document.getElementById('input');
    if (_inpTo) { _inpTo.placeholder = 'Message...'; _inpTo.value = ''; _inpTo.disabled = false; }
    setPauseBtnState('running');
  },

  'agent_throttled': function(d) {
    stopAskUserCountdown();
    S.activeDecisionId = d.decision_id || '';
    setPauseBtnState('paused_by_throttle');
    // FIX (2026-08-31): agent_throttled now sets S.agentPaused/agentQuestion,
    // so Enter in the input goes through sendMsg() → resumeWithAnswer() instead of
    // blocking (S.streaming was still true). Previously the user had to click the ⚠-icon
    // — unclear because the input did not react.
    S.agentPaused = true;
    S.agentQuestion = d.message || 'Agent is asking too many questions.';
    S.currentRunId = d.run_id || S.currentRunId;
    var _thDiv = document.getElementById('ask-user-question');
    if (_thDiv) {
      _thDiv.style.cssText = 'color:#d9534f;border:2px solid #d9534f;border-left:3px solid #d9534f;background:rgba(217,83,79,.07);border-radius:8px;padding:10px 14px;font-size:13px;margin:4px 0';
      _thDiv.textContent = '\u26A0 ' + (d.message || 'Agent is asking too many questions.') + ' (' + (d.ask_user_count || '?') + ' questions in 10min)';
    } else {
      var _thNew = document.createElement('div');
      _thNew.id = 'ask-user-question';
      _thNew.className = 'msg divider';
      _thNew.style.cssText = 'color:#d9534f;border:2px solid #d9534f;border-left:3px solid #d9534f;background:rgba(217,83,79,.07);border-radius:8px;padding:10px 14px;font-size:13px;margin:4px 0';
      _thNew.textContent = '\u26A0 ' + (d.message || 'Agent is asking too many questions.') + ' (' + (d.ask_user_count || '?') + ' questions in 10min)';
      document.getElementById('chat').appendChild(_thNew);
    }
    scrollBtmIfNearBottom(120);
    var _inpTh = document.getElementById('input');
    if (_inpTh) { _inpTh.placeholder = 'Clarification (min. 10 chars)\u2026'; _inpTh.focus(); }
    showInfo('Agent throttled \u2014 ' + (d.ask_user_count || '?') + ' questions in 10min.');
  },

  'agent_resumed': function(d) {
    stopAskUserCountdown();
    S.agentPaused = false;
    S.agentQuestion = null;
    setPauseBtnState('running');
    var _inpRes = document.getElementById('input');
    if (_inpRes) { _inpRes.placeholder = 'Message...'; _inpRes.value = ''; _inpRes.disabled = false; }
    var _qDiv = document.getElementById('ask-user-question');
    if (_qDiv) _qDiv.remove();
    var _apCard = document.getElementById('approval-request-card');
    if (_apCard) _apCard.remove();
    var _askCard = document.getElementById('ask-user-card');
    if (_askCard) _askCard.remove();
    S.activeDecisionId = '';
  },

  'approval_request': function(d) {
    // ACTION-APPROVAL (2026-09-18): buttons instead of typed answers —
    // posts the decision to the existing resume endpoint.
    // STALE-HEARTBEAT (2026-09-20): the run is PAUSED here — a leftover
    // "Processing context… Ns" line must not keep implying work.
    rmEl('status-el');
    _renderApprovalCard(d);
  },

  'run_paused_manual': function(d) {
    setPauseBtnState('paused_manual');
    var _pauseDiv = document.createElement('div');
    _pauseDiv.className = 'msg divider';
    _pauseDiv.style.cssText = 'color:#f0ad4e;border-color:rgba(240,173,78,.35);font-size:11px;font-weight:600';
    // DIRECT PAUSE (A1, 2026-11-11): direct runs have no chunks — the event
    // carries phase:"direct" and gets a plain suspended/continued wording.
    _pauseDiv.textContent = (d.phase === 'direct')
      ? '\u23F8 Run paused. Click Resume to continue.'
      : '\u23F8 Run paused after chunk ' + d.chunks_done + '. ' + d.chunks_remaining + ' chunks remaining. Click Resume to continue.';
    document.getElementById('chat').appendChild(_pauseDiv);
    scrollBtmIfNearBottom(120);
  },

  'run_resumed_manual': function(d) {
    setPauseBtnState('running');
    var _resDiv = document.createElement('div');
    _resDiv.className = 'msg divider';
    _resDiv.style.cssText = 'color:#20b0a0;border-color:rgba(32,176,160,.35);font-size:11px;font-weight:600';
    _resDiv.textContent = (d.phase === 'direct')
      ? '\u25B6 Run resumed.'
      : '\u25B6 Run resumed \u2014 chunk ' + (d.chunks_done + 1) + ' starting.';
    document.getElementById('chat').appendChild(_resDiv);
    scrollBtmIfNearBottom(120);
  },

  'run_halted_graceful': function(d) {
    _cleanupLoadTimers();
    setPauseBtnState('idle');
    setStopBtnState('idle');
    var _haltDiv = document.createElement('div');
    _haltDiv.className = 'msg divider';
    _haltDiv.style.cssText = 'color:#808080;border-color:rgba(128,128,128,.35);font-size:11px;font-weight:600';
    _haltDiv.textContent = '\u23F9 Graceful stop: ' + d.chunks_done + ' chunks completed, ' + d.chunks_remaining + ' remaining.';
    document.getElementById('chat').appendChild(_haltDiv);
    scrollBtmIfNearBottom(120);
  },

  'duo_coder': function(d) {
    // DUO-CODE-DEDUP (2026-09-02): the backend sends a generic agent event
    // content:"Code" as a coder phase switch right before the first duo_coder
    // event. startAgent() opens an empty purple "Code" bubble for it; the real
    // duo-coder block below ("Coder") is what receives the stream. Remove only
    // that first stray empty Code bubble so no duplicate header is shown.
    var _strayCode = null;
    var _allBlocks = document.querySelectorAll('#chat .msg.ablock');
    for (var _si = _allBlocks.length - 1; _si >= 0; _si--) {
      var _blk = _allBlocks[_si];
      if (_blk.classList.contains('duo-coder') || _blk.classList.contains('duo-critic')) continue;
      var _an = _blk.querySelector('.aname');
      if (!_an || (_an.textContent || '').trim() !== 'Code') continue;
      var _ab = _blk.querySelector('.abody');
      if (_ab && (_ab.textContent || '').trim()) continue; // has content — keep
      _strayCode = _blk;
      break;
    }
    if (_strayCode) {
      _strayCode.remove();
      if (S.curAgent && S.curAgent.name === 'Code') S.curAgent = null;
    }
    if (S._chunkingActive) {
      // chunking: EVERY chunk gets its own coder block (no reuse)
      startDuoAgent('coder', 'Coder', d.model || '', 'R' + d.round);
    } else {
      // normal: reuse only an active coder bubble from the current run
      var _coderBlocks = document.querySelectorAll('#chat .ablock.duo-coder:not([data-turn-done="1"])');
      var _existingCoderBlock = _coderBlocks.length ? _coderBlocks[_coderBlocks.length - 1] : null;
      if (_existingCoderBlock && S.curAgent && S.curAgent.body && S.curAgent.body.closest('.duo-coder') === _existingCoderBlock) {
        var _arnd = _existingCoderBlock.querySelector('.arole:last-of-type');
        if (_arnd) _arnd.textContent = ' \u00B7 R' + d.round;
        _existingCoderBlock.classList.add('live');
        if (S.curAgent.body) S.curAgent.body.classList.add('live');
        if (typeof _syncSpeakButtons === 'function') _syncSpeakButtons(_existingCoderBlock);
        S._thinkBlockId = null;
      } else {
        startDuoAgent('coder', 'Coder', d.model || '', 'R' + d.round);
      }
    }
  },

  'duo_coder_done': function(d) {
    if (S._chunkingActive) {
      // chunking: close the coder block immediately — the next chunk gets its own block
      doneAgent(d.elapsed || '');
      S._coderElapsed = null;
    } else {
      if (S.curAgent) {
        if (S.curAgent.lt) { clearTimeout(S.curAgent.lt); S.curAgent.lt = null; }
        var _lhEl = document.getElementById('lh-' + S.curAgent.tid);
        if (_lhEl) {
          if (_lhEl._loadTimer) { clearInterval(_lhEl._loadTimer); _lhEl._loadTimer = null; }
          _lhEl.style.display = 'none';
        }
        S._coderElapsed = d.elapsed;
      }
    }
    if (!_vramPending && _vramOpen) refreshVram();
  },

  'duo_critic': function(d) {
    // Critic starts — close coder bubble
    if (S.curAgent && S.curAgent.body && S.curAgent.body.closest('.duo-coder')) {
      doneAgent(S._coderElapsed || '');
    }
    S._coderElapsed = null;
    _addFlowConnector('#d08020', 0.07, 'Critic', 'Review code', 'R' + d.round);
    showStatus('\u23F3 Critic is reviewing the code (R' + d.round + ')...');
  },

  'duo_critic_done': function(d) {
    // build the critic block now with the final verdict
    rmEl('status-el');
    renderDuoVerdict(d.verdict || {}, d.elapsed, d.approved);
    if (!_vramPending && _vramOpen) refreshVram();
  },

  'test_result': function(d) {
    // Test-Runner Resultat — sauberer Pass/Fail Divider
    rmEl('status-el');
    var _tPass = d.passed;
    var _tFail = d.failures || 0;
    var _tLang = d.language || '?';
    var _tDiv = document.createElement('div');
    _tDiv.className = 'msg divider';
    _tDiv.style.cssText = _tPass
      ? 'color:#3a9960;border-color:rgba(58,153,96,.25);font-size:9px;letter-spacing:.05em'
      : 'color:#d04040;border-color:rgba(208,64,64,.25);font-size:9px;letter-spacing:.05em';
    _tDiv.textContent = _tPass
      ? '✅ Tests passed (' + _tLang + ')'
      : '❌ ' + _tFail + ' test failure(s) (' + _tLang + ')';
    document.getElementById('chat').appendChild(_tDiv);
    scrollBtmIfNearBottom(120);
  },
  // ── Partition Events (paralleler Pre-Explore) ─────────────────────────────

  'partition_start': function(d) {
    window._partRunning = true;
    var _pg = document.getElementById('part-grid');
    if (!_pg) {
      _pg = document.createElement('div');
      _pg.className = 'part-grid'; _pg.id = 'part-grid';
      var _pghdr = document.createElement('div');
      _pghdr.className = 'part-grid-hdr'; _pghdr.id = 'part-grid-hdr'; _pghdr.textContent = 'Pre-Explore';
      _pg.appendChild(_pghdr);
      var _lanes = document.createElement('div');
      _lanes.className = 'part-lanes';
      _lanes.id = 'part-lanes';
      _pg.appendChild(_lanes);
      var _chat = document.getElementById('chat');
      _chat.appendChild(_pg);
      _chat.scrollTop = _chat.scrollHeight;
    }

    var _hdr = document.getElementById('part-grid-hdr');
    // the header text is set by pre_explore_partition_shape (knows all workers upfront).
    // No update here — would jump from "1" to "2" when W1/W2 arrive sequentially.

    var _laneKey = d.worker_key || ((d.worker_model || '?') + '@' + ((typeof d.worker_port === 'number') ? d.worker_port : 0));
    var _laneVariantByKey = window._prexLaneVariantByKey || (window._prexLaneVariantByKey = Object.create(null));
    var _laneVariantSeen = window._prexModelVariantSeen || (window._prexModelVariantSeen = Object.create(null));
    var _laneVariant = _laneVariantByKey[_laneKey];
    if (typeof _laneVariant !== 'number') {
      _laneVariant = _workerVariantIndex(d.worker_model || '?', _laneVariantSeen);
      _laneVariantByKey[_laneKey] = _laneVariant;
    }
    var _laneColor = _workerColorForVariant(_laneVariant);
    var _safeLane = String(_laneKey).replace(/[^a-zA-Z0-9]/g,'_');
    var _laneId = 'plane-' + _safeLane;
    var _lane = document.getElementById(_laneId);
    if (!_lane) {
      var _ws = (typeof d.worker_idx === 'number') ? ('W' + (d.worker_idx + 1)) : 'W';
      var _wm = esc(d.worker_model || '?');
      var _wp = (typeof d.worker_port === 'number' && d.worker_port > 0) ? ('@' + d.worker_port) : '@?';
      _lane = document.createElement('div');
      _lane.className = 'part-lane';
      _lane.id = _laneId;
      _lane.style.setProperty('--lane-bd', _laneColor.chipBd);
      _lane.style.setProperty('--lane-bg', _laneColor.laneBg);
      _lane.style.setProperty('--lane-hdr-bd', _laneColor.chipBg);
      _lane.style.setProperty('--lane-fg', _laneColor.chipFg);
      _lane.style.setProperty('--lane-chip-bd', _laneColor.chipBd);
      _lane.style.setProperty('--lane-chip-bg', _laneColor.chipBg);
      _lane.style.setProperty('--lane-chip-fg', _laneColor.chipFg);
      _lane.style.setProperty('--lane-model', _laneColor.model);
      _lane.style.setProperty('--lane-port', _laneColor.port);
      _lane.dataset.workerIdx = String((typeof d.worker_idx === 'number') ? d.worker_idx : 999);
      _lane.dataset.workerKey = String(_laneKey || '');
      _lane.dataset.partsDone = '0';
      _lane.dataset.filesRead = '0';
      _lane.innerHTML = '<div class="part-lane-hdr">'
        + '<span class="part-worker-chip">' + _ws + '</span>'
        + '<span class="part-lane-model" title="' + _wm + '">' + _wm + '</span>'
        + '<span class="part-lane-port">' + _wp + '</span>'
        + '<span class="part-lane-sum">0 partitions · 0 files read</span>'
        + '</div>'
        + '<div class="part-lane-body" id="plb-' + _safeLane + '"></div>';
      var _lanesHost = document.getElementById('part-lanes');
      if (_lanesHost) {
        _lanesHost.appendChild(_lane);
        // sort once on first insert — no re-append afterwards.
        // prevents a visual flash when W2 starts while W1 is already running.
        var _laneNodes = Array.prototype.slice.call(_lanesHost.children || []);
        _laneNodes.sort(function(a, b) {
          var ai = parseInt(a.dataset.workerIdx || '999', 10);
          var bi = parseInt(b.dataset.workerIdx || '999', 10);
          ai = isNaN(ai) ? 999 : ai;
          bi = isNaN(bi) ? 999 : bi;
          if (ai !== bi) return ai - bi;
          var ak = String(a.dataset.workerKey || a.id || '');
          var bk = String(b.dataset.workerKey || b.id || '');
          return ak.localeCompare(bk);
        });
        _laneNodes.forEach(function(n) { _lanesHost.appendChild(n); });
      }
    }
    // Lane existiert bereits — kein Re-Sort, kein DOM-Glitch

    var _pid = 'part-' + _safeLane + '-' + d.label.replace(/[^a-zA-Z0-9]/g,'_');
    if (!document.getElementById(_pid)) {
      var _pi = document.createElement('div');
      _pi.className = 'part-item running'; _pi.id = _pid;
      _pi.dataset.partLabel = String(d.label || '');
      _pi.dataset.workerKey = String(_laneKey || '');
      var _dotId = 'pdot-' + _pid;
      var _logDivId = 'plog-' + _pid;
      var _bodyId = 'pbody-' + _pid;
      var _nfHint = d.n_files ? '<span class="part-badge cx-lo" id="pcount-'+_pid+'">0 / '+d.n_files+'</span>' : '';
      _pi.innerHTML = '<div class="part-card-hdr">'
        + '<span class="part-dot running" id="'+_dotId+'"></span>'
        + '<span class="part-label" title="'+esc(d.label)+'">'+esc(d.label)+'</span>'
        + '<div class="part-meta">'+_nfHint+'</div>'
        + '</div>'
        + '<div class="part-body" id="'+_bodyId+'">'
        + '<div class="part-log" id="'+_logDivId+'"></div>'
        + '</div>';
      var _laneBody = document.getElementById('plb-' + _safeLane);
      if (_laneBody) {
        _laneBody.appendChild(_pi);
        // no alphabetical re-sort here — partitions appear in arrival order.
        // Re-appending on every new start would briefly hide running cards (flash).
      }
      else _pg.appendChild(_pi);
    }
  },

  'partition_done': function(d) {
    // Live-Read-Counter in prex-info-Bar aktualisieren
    if (typeof d.n_files_read === 'number' && d.n_files_read > 0) {
      window._prexFilesRead = (window._prexFilesRead || 0) + d.n_files_read;
      var _prexRc = document.getElementById('prex-read-count');
      if (_prexRc) {
        var _assigned = window._prexFilesAssigned || 0;
        var _pct = _assigned > 0 ? Math.round(window._prexFilesRead / _assigned * 100) : 0;
        _prexRc.textContent = window._prexFilesRead + ' gelesen'
          + (_assigned > 0 ? ' / ' + _assigned + ' (' + _pct + '%)' : '');
        _prexRc.style.color = 'var(--color-text-primary)';
      }
    }
    var _laneKey2 = d.worker_key || ((d.worker_model || '?') + '@' + ((typeof d.worker_port === 'number') ? d.worker_port : 0));
    var _safeLane2 = String(_laneKey2).replace(/[^a-zA-Z0-9]/g,'_');
    var _pid2 = 'part-' + _safeLane2 + '-' + (d.label || '').replace(/[^a-zA-Z0-9]/g,'_');
    var _pi2 = document.getElementById(_pid2);
    if (!_pi2) {
      document.querySelectorAll('.part-item').forEach(function(_el) {
        if (_pi2) return;
        var _matchLabel = (_el.dataset.partLabel || '') === String(d.label || '');
        var _matchWorker = !_laneKey2 || (_el.dataset.workerKey || '') === String(_laneKey2);
        if (_matchLabel && _matchWorker) _pi2 = _el;
      });
    }
    if (_pi2) {
      _pi2.className = 'part-item done' + (d.zero_reads ? ' zero-reads' : '');
      if (!_pi2.dataset.laneSummed) {
        _pi2.dataset.laneSummed = '1';
        var _laneWrap = _pi2.closest('.part-lane');
        if (_laneWrap) {
          var _partsDone = parseInt(_laneWrap.dataset.partsDone || '0', 10);
          var _filesRead = parseInt(_laneWrap.dataset.filesRead || '0', 10);
          _partsDone = isNaN(_partsDone) ? 0 : _partsDone;
          _filesRead = isNaN(_filesRead) ? 0 : _filesRead;
          _partsDone += 1;
          if (typeof d.n_files_read === 'number' && d.n_files_read > 0) {
            _filesRead += d.n_files_read;
          }
          _laneWrap.dataset.partsDone = String(_partsDone);
          _laneWrap.dataset.filesRead = String(_filesRead);
          var _laneSum = _laneWrap.querySelector('.part-lane-sum');
          if (_laneSum) {
            _laneSum.textContent = _partsDone + ' partitions · ' + _filesRead + ' files read';
          }
        }
      }
      var _dot2 = _pi2.querySelector('.part-dot');
      if (_dot2) _dot2.className = 'part-dot done';
      // badges in header
      var _meta = _pi2.querySelector('.part-meta');
      if (_meta) {
        // innerHTML-clear deletes all children including the pcount badge.
        // _oldCount would then be detached → replaceWith never worked.
        // Fix: after innerHTML='' always just appendChild.
        _meta.innerHTML = '';
        if (d.n_files_read) {
          var _nTotal = typeof d.files_total === 'number' ? d.files_total : 0;
          var _nr = document.createElement('span');
          var _allRead = _nTotal === 0 || d.n_files_read >= _nTotal;
          _nr.className = 'part-badge ' + (_allRead ? 'cx-lo' : 'cx-hi');
          _nr.textContent = _nTotal > 0
            ? d.n_files_read + '/' + _nTotal + 'f' + (_allRead ? ' \u2713' : ' \u26a0')
            : d.n_files_read + 'f \u2713';
          _nr.title = _allRead
            ? 'All ' + d.n_files_read + ' files read'
            : ((_nTotal - d.n_files_read) + ' files skipped');
          _meta.appendChild(_nr);
        } else if (d.zero_reads) {
          // zero reads: no file read at all despite assigned paths
          var _zrBadge = document.createElement('span');
          _zrBadge.className = 'part-badge cx-hi';
          _zrBadge.style.cssText = 'background:rgba(200,50,50,.18);border-color:rgba(200,50,50,.5);color:#d04040;font-weight:600;';
          _zrBadge.textContent = '0/' + (d.files_total || '?') + 'f \u26a0';
          _zrBadge.title = '0 files read — path issue or no tool call from the model';
          _meta.appendChild(_zrBadge);
        }
        if (typeof d.touched !== 'undefined') {
          var _tb = document.createElement('span');
          var _isTask = d.touched || (typeof d.complexity === 'number' && d.complexity >= 0.65);
          _tb.className = 'part-badge ' + (_isTask ? 'touched' : 'skip');
          _tb.textContent = _isTask ? '\u2714 TASK' : 'skip';
          _tb.title = d.touched ? 'Partition needs changes' : (_isTask ? 'Complex — likely relevant' : 'No changes needed');
          _meta.appendChild(_tb);
        }
        if (typeof d.complexity === 'number') {
          var _cx = document.createElement('span');
          var _cxClass = d.complexity >= 0.7 ? 'cx-hi' : d.complexity >= 0.4 ? 'cx-mid' : 'cx-lo';
          _cx.className = 'part-badge ' + _cxClass;
          _cx.textContent = 'cx ' + d.complexity.toFixed(1);
          _cx.title = 'Complexity: ' + d.complexity.toFixed(2);
          _meta.appendChild(_cx);
        }
      }
      // Replace body: show read files as list
      var _body2 = _pi2.querySelector('.part-body');
      if (_body2) {
        _body2.innerHTML = '';
        var _files = d.files_read || [];
        if (_files.length > 0) {
          var _fl = document.createElement('div');
          _fl.className = 'part-file-list';
          _files.forEach(function(fp) {
            var _segs = (fp || '').replace(/\\/g,'/').split('/').filter(Boolean);
            var _fname = _segs[_segs.length - 1] || fp;
            var _ext   = _fname.indexOf('.') > -1 ? _fname.split('.').pop() : '';
            var _icon  = _ext === 'py' ? '🐍' : _ext === 'ts' || _ext === 'tsx' ? '📘' :
                         _ext === 'js' || _ext === 'jsx' ? '📒' : _ext === 'json' ? '📋' :
                         _ext === 'md' ? '📄' : _ext === 'css' ? '🎨' : _ext === 'html' ? '🌐' :
                         _ext === 'sql' ? '🗃' : _ext === 'prisma' ? '🔷' : '📁';
            var _fe = document.createElement('div');
            _fe.className = 'part-file-entry';
            _fe.title = fp;
            _fe.innerHTML = '<div class="pfe-left"><span class="pfe-icon">'+_icon+'</span>'
              + (_ext ? '<span class="pfe-ext">'+esc('.'+_ext)+'</span>' : '')
              + '</div>'
              + '<span class="pfe-path">'+esc(fp)+'</span>';
            _fl.appendChild(_fe);
          });
          _body2.appendChild(_fl);
        } else if (d.n_files_read) {
          var _pe = document.createElement('div');
          _pe.className = 'part-empty';
          _pe.textContent = d.n_files_read + ' files read';
          _body2.appendChild(_pe);
        }
        // zero reads: show all assigned paths as "unread"
        if (d.zero_reads && (d.paths || []).length > 0) {
          var _zrDiv = document.createElement('div');
          _zrDiv.style.cssText = 'margin-bottom:4px;padding:4px 6px;border-left:2px solid rgba(200,50,50,.6);background:rgba(200,50,50,.06);border-radius:0 3px 3px 0;';
          _zrDiv.innerHTML = '<div style="font-size:9px;color:#c04040;font-weight:600;margin-bottom:3px">⚠ 0 files read — path problem?</div>';
          (d.paths || []).slice(0, 12).forEach(function(sp) {
            var _ss = sp.replace(/\\/g,'/').split('/');
            var _sname = _ss[_ss.length-1] || sp;
            var _sd = document.createElement('div');
            _sd.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:9px;color:#a03030;opacity:.7;padding:1px 0 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
            _sd.title = sp;
            _sd.textContent = _sname;
            _zrDiv.appendChild(_sd);
          });
          if ((d.paths || []).length > 12) {
            var _zrMore = document.createElement('div');
            _zrMore.style.cssText = 'font-size:9px;color:#a03030;opacity:.5;padding:1px 0 0 6px;';
            _zrMore.textContent = '… +' + ((d.paths.length - 12)) + ' more';
            _zrDiv.appendChild(_zrMore);
          }
          _body2.appendChild(_zrDiv);
        }
        // show skipped files when n_files_read < files_total
        if (typeof d.files_total === 'number' && d.files_total > 0 && d.n_files_read < d.files_total) {
          // files_read = absolute paths, paths = relative paths → normalize to basename+parent for comparison
          var _readNorms = new Set();
          (d.files_read || []).forEach(function(p) {
            var _n = p.replace(/\\/g,'/').toLowerCase();
            _readNorms.add(_n);                           // full absolute
            var _segs = _n.split('/').filter(Boolean);
            if (_segs.length >= 2) _readNorms.add(_segs.slice(-2).join('/'));  // parent/file
            if (_segs.length >= 1) _readNorms.add(_segs[_segs.length-1]);     // filename only
          });
          var _skippedPaths = (d.paths || []).filter(function(p) {
            var _n = p.replace(/\\/g,'/').toLowerCase();
            var _segs = _n.split('/').filter(Boolean);
            var _fname = _segs[_segs.length-1] || '';
            var _parent2 = _segs.length >= 2 ? _segs.slice(-2).join('/') : _fname;
            // PREX-SKIP-FIX: also check parent/file variants with and without Windows separators
            var _parent3 = _segs.length >= 2 ? _segs[_segs.length-2] + '/' + _fname : _fname;
            return !_readNorms.has(_n) && !_readNorms.has(_parent2)
                && !_readNorms.has(_parent3) && !_readNorms.has(_fname);
          });
          if (_skippedPaths.length > 0) {
            var _skipDiv = document.createElement('div');
            _skipDiv.style.cssText = 'margin-top:4px;padding:3px 4px;border-left:2px solid rgba(200,80,80,.4);';
            _skipDiv.innerHTML = '<span style="font-size:9px;opacity:.55;color:#c05050">⚠ ' + _skippedPaths.length + ' not read:</span>';
            _skippedPaths.forEach(function(sp) {
              var _ss = sp.replace(/\\/g,'/').split('/');
              var _sname = _ss[_ss.length-1] || sp;
              var _sd = document.createElement('div');
              _sd.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:9px;opacity:.45;padding:1px 0 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
              _sd.title = sp;
              _sd.textContent = _sname;
              _skipDiv.appendChild(_sd);
            });
            _body2.appendChild(_skipDiv);
          }
        }
        // hint below
        if (d.hint) {
          var _hintEl2 = document.createElement('div');
          _hintEl2.className = 'part-hint';
          _hintEl2.textContent = d.hint;
          _hintEl2.title = d.hint;
          _body2.appendChild(_hintEl2);
        }
        // exports + entry_points as tag chips
        var _exports = d.exports || [];
        var _entries = d.entry_points || [];
        if (_exports.length > 0 || _entries.length > 0) {
          var _chipRow = document.createElement('div');
          _chipRow.style.cssText = 'display:flex;flex-wrap:wrap;gap:3px;padding:3px 2px 2px;';
          if (_entries.length > 0) {
            _entries.slice(0, 4).forEach(function(ep) {
              var _c2 = document.createElement('span');
              _c2.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:8px;padding:1px 5px;border-radius:2px;background:rgba(72,120,192,.15);border:1px solid rgba(72,120,192,.3);color:#6090c0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px;';
              _c2.title = 'entry: ' + ep;
              _c2.textContent = '⬡ ' + ep.split('/').pop();
              _chipRow.appendChild(_c2);
            });
          }
          if (_exports.length > 0) {
            _exports.slice(0, 8).forEach(function(ex) {
              var _c3 = document.createElement('span');
              _c3.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:8px;padding:1px 5px;border-radius:2px;background:rgba(58,153,96,.1);border:1px solid rgba(58,153,96,.25);color:#4a9970;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px;';
              _c3.title = 'export: ' + ex;
              _c3.textContent = ex;
              _chipRow.appendChild(_c3);
            });
            if (_exports.length > 8) {
              var _more = document.createElement('span');
              _more.style.cssText = 'font-size:8px;color:#304850;padding:1px 4px;';
              _more.textContent = '+' + (_exports.length - 8) + ' more';
              _chipRow.appendChild(_more);
            }
          }
          _body2.appendChild(_chipRow);
        }
        // plan steps
        var _planSteps = d.plan_steps || [];
        if (_planSteps.length > 0) {
          var _planDiv = document.createElement('div');
          _planDiv.style.cssText = 'margin-top:3px;display:flex;flex-direction:column;gap:2px;';
          _planSteps.slice(0, 5).forEach(function(ps) {
            var _ps = document.createElement('div');
            _ps.style.cssText = 'font-family:"IBM Plex Mono",monospace;font-size:8px;color:#4a6070;line-height:1.4;display:flex;gap:4px;';
            var _stepNum = document.createElement('span');
            _stepNum.style.cssText = 'color:#3a5060;flex-shrink:0;min-width:12px;';
            _stepNum.textContent = (ps.step || '') + '.';
            var _stepTxt = document.createElement('span');
            _stepTxt.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
            var _fname2 = (ps.file || '').split('/').pop().split('\\').pop();
            _stepTxt.title = (ps.file || '') + ': ' + (ps.action || '');
            _stepTxt.textContent = (_fname2 ? _fname2 + ': ' : '') + (ps.action || '');
            _ps.appendChild(_stepNum);
            _ps.appendChild(_stepTxt);
            _planDiv.appendChild(_ps);
          });
          if (_planSteps.length > 5) {
            var _morePs = document.createElement('div');
            _morePs.style.cssText = 'font-size:8px;color:#304050;padding:1px 4px;';
            _morePs.textContent = '… +' + (_planSteps.length - 5) + ' more steps';
            _planDiv.appendChild(_morePs);
          }
          _body2.appendChild(_planDiv);
        }
      }
      if (!document.querySelector('.part-dot.running')) {
        window._partRunning = false;
        // FIX: scroll after the last partition — ensures the finished
        // part grid and all done badges are visible when pre-explore ends.
        scrollBtmIfNearBottom(120);
      }
    }
  },

  'parallel_cancelled': function(d) {
    window._partRunning = false;
    rmEl('part-grid');
    scrollBtmIfNearBottom(120);
  },

  'partition_dedup': function(d) {
    var _ddLaneKey = d.worker_key || ((d.worker_model || '?') + '@' + ((typeof d.worker_port === 'number') ? d.worker_port : 0));
    var _ddSafeLane = String(_ddLaneKey).replace(/[^a-zA-Z0-9]/g,'_');
    var _ddPid = 'part-' + _ddSafeLane + '-' + (d.label || '').replace(/[^a-zA-Z0-9]/g,'_');
    var _ddCard = document.getElementById(_ddPid);
    if (!_ddCard) {
      var _ddCandidates = [];
      document.querySelectorAll('.part-item').forEach(function(_el) {
        if ((_el.dataset.partLabel || '') === String(d.label || '')) _ddCandidates.push(_el);
      });
      if (_ddLaneKey) {
        _ddCandidates.forEach(function(_el) {
          if (_ddCard) return;
          if ((_el.dataset.workerKey || '') === String(_ddLaneKey)) _ddCard = _el;
        });
      }
      if (!_ddCard && _ddCandidates.length === 1) _ddCard = _ddCandidates[0];
    }
    if (_ddCard) {
      var _meta3 = _ddCard.querySelector('.part-meta');
      if (_meta3) {
        var _badge = _meta3.querySelector('.part-badge.dedup');
        var _cnt3 = parseInt((_badge && _badge.dataset.n) || '0', 10);
        _cnt3 = isNaN(_cnt3) ? 1 : (_cnt3 + 1);
        if (!_badge) {
          _badge = document.createElement('span');
          _badge.className = 'part-badge cx-mid dedup';
          _meta3.appendChild(_badge);
        }
        _badge.dataset.n = String(_cnt3);
        _badge.textContent = 'dedup ' + _cnt3;
      }
      var _ddLog = _ddCard.querySelector('.part-log');
      if (_ddLog) {
        var _target = String(d.target || 'scope');
        var _owner = String(d.owner || '?');
        var _line = document.createElement('div');
        _line.className = 'part-log-line';
        _line.textContent = '↺ dedup (' + (d.kind || 'scope') + '): ' + _target + ' → owner ' + _owner;
        _line.title = _line.textContent;
        _ddLog.appendChild(_line);
        while (_ddLog.children.length > 8) _ddLog.removeChild(_ddLog.firstChild);
      }
    }
  },
  // ── Partition-Token (Tool-Call-Log in Card-Log, nicht in Haupt-Stream) ────

  'partition_token': function(d) {
    var _ptLaneKey = d.worker_key || ((d.worker_model || '?') + '@' + ((typeof d.worker_port === 'number') ? d.worker_port : 0));
    var _ptSafeLane = String(_ptLaneKey).replace(/[^a-zA-Z0-9]/g,'_');
    var _ptPid = 'part-' + _ptSafeLane + '-' + (d.label || '').replace(/[^a-zA-Z0-9]/g,'_');
    var _ptLog = document.getElementById('plog-' + _ptPid);
    if (!_ptLog) {
      var _ptCandidates = [];
      var _ptFallback = null;
      document.querySelectorAll('.part-item').forEach(function(_el) {
        if ((_el.dataset.partLabel || '') === String(d.label || '')) _ptCandidates.push(_el);
      });
      if (_ptLaneKey) {
        _ptCandidates.forEach(function(_el) {
          if (_ptFallback) return;
          if ((_el.dataset.workerKey || '') === String(_ptLaneKey)) _ptFallback = _el;
        });
      }
      if (!_ptFallback && _ptCandidates.length === 1) _ptFallback = _ptCandidates[0];
      if (_ptFallback) _ptLog = _ptFallback.querySelector('.part-log');
    }
    if (_ptLog) {
      var _ptRaw = (d.content || '').replace(/\n/g,' ').trim();
      if (!_ptRaw) return;
      // Coverage-forcing messages (📂 Coverage X/Y, ⚠ No tool call etc.) are
      // system status, not real reads — own CSS class so they do not look like
      // read files (was the cause of the "7 shown, 1 read" confusion).
      var _ptIsStatus = _ptRaw.charAt(0) === '\uD83D\uDCC2' // 📂
                     || _ptRaw.charAt(0) === '\u26A0'        // ⚠
                     || _ptRaw.indexOf('Coverage ') > -1
                     || _ptRaw.indexOf('erzwinge Read') > -1
                     || _ptRaw.indexOf('CONTRACT') > -1
                     || _ptRaw.indexOf('vervollst') > -1;
      if (_ptIsStatus) {
        // status line: compact, dimmed, no file-icon parsing
        var _ptLast0 = _ptLog.lastElementChild;
        var _ptKey0 = 'status:' + _ptRaw.slice(0, 40).toLowerCase();
        if (_ptLast0 && _ptLast0.dataset.key === _ptKey0) {
          // same status message — merge instead of repeat
          var _c0 = parseInt(_ptLast0.dataset.count || '1', 10);
          _ptLast0.dataset.count = String(isNaN(_c0) ? 2 : _c0 + 1);
        } else {
          var _ptSLine = document.createElement('div');
          _ptSLine.className = 'part-log-line pll-status';
          _ptSLine.dataset.key = _ptKey0;
          _ptSLine.dataset.count = '1';
          _ptSLine.title = _ptRaw;
          _ptSLine.innerHTML = '<span class="pll-icon" style="opacity:.4">⚙</span>'
            + '<span class="pll-name" style="opacity:.55;font-style:italic">'+esc(_ptRaw.slice(0,60))+'</span>';
          _ptLog.appendChild(_ptSLine);
        }
        while (_ptLog.children.length > 8) _ptLog.removeChild(_ptLog.firstChild);
        return;
      }
      // extract the filename from the content (read_file, read_file(path=...) etc.)
      var _ptFile = _ptRaw.match(/(?:path=["']?|read_file\(["']?)([^\s"',\)]+)/);
      var _ptDisplay = _ptFile ? _ptFile[1] : _ptRaw;
      var _ptSegs = _ptDisplay.replace(/\\/g,'/').split('/');
      var _ptName = _ptSegs[_ptSegs.length-1] || _ptDisplay;
      // parent path as context — prevents package.json in /root and /server looking the same
      var _ptDir = _ptSegs.length > 1 ? _ptSegs.slice(0, -1).join('/') : '';
      var _ptKey = String(_ptDisplay || _ptName || '').toLowerCase();
      var _ptExt = _ptName.indexOf('.') > -1 ? _ptName.split('.').pop() : '';
      var _ptIcon = _ptExt === 'py' ? '🐍' : _ptExt === 'ts' || _ptExt === 'tsx' ? '📘' :
                   _ptExt === 'js' || _ptExt === 'jsx' ? '📒' : _ptExt === 'json' ? '📋' :
                   _ptExt === 'md' ? '📄' : _ptExt === 'css' ? '🎨' : _ptExt === 'html' ? '🌐' :
                   _ptRaw.indexOf('read') > -1 ? '👁' : '⚙';
      // global dedup per partition log: each file is shown only once,
      // even if the model reads it multiple times (alternating or directly).
      if (!_ptLog._seenKeys) _ptLog._seenKeys = new Set();
      if (_ptLog._seenKeys.has(_ptKey)) {
        // already known file — update the entry with the ×N marker instead of appending again
        var _existing = null;
        _ptLog.querySelectorAll('.part-log-line').forEach(function(el) {
          if (el.dataset.key === _ptKey) _existing = el;
        });
        if (_existing) {
          var _c = parseInt(_existing.dataset.count || '1', 10);
          _existing.dataset.count = String(_c + 1);
          var _rep = _existing.querySelector('.pll-repeat');
          if (_rep) {
            _rep.textContent = '×' + (_c + 1);
            _rep.style.display = '';
          }
        }
      } else {
        _ptLog._seenKeys.add(_ptKey);
        var _ptLine = document.createElement('div');
        _ptLine.className = 'part-log-line';
        _ptLine.dataset.key = _ptKey;
        _ptLine.dataset.count = '1';
        _ptLine.title = _ptDisplay;
        _ptLine.innerHTML = '<div class="pll-left"><span class="pll-icon">'+_ptIcon+'</span>'
          + (_ptExt ? '<span class="pll-ext">'+esc('.'+_ptExt)+'</span>' : '')
          + '</div>'
          + '<span class="pll-path">'+esc(_ptDisplay)+'</span>'
          + '<span class="pll-repeat" style="display:none;font-size:9px;opacity:.45;padding:0 2px;font-family:monospace;color:#7ab890;white-space:nowrap;justify-self:end;"></span>';
        _ptLog.appendChild(_ptLine);
      }
      // compact history: keep only the last 8 visible token lines.
      while (_ptLog.children.length > 8) _ptLog.removeChild(_ptLog.firstChild);
      // FIX: scroll after token insertion — so new entries stay visible
      // while pre-explore runs. scrollBtmIfNearBottom only scrolls if the user
      // is already at the bottom (respects manual scrolling).
      scrollBtmIfNearBottom(200);
    }
  },
  // ── File-Read: Live-Counter im Card-Header updaten ────────────────────────

  'file_read': function(d) {
    var _frLaneKey = d.worker_key || ((d.worker_model || '?') + '@' + ((typeof d.worker_port === 'number') ? d.worker_port : 0));
    var _frSafeLane = String(_frLaneKey).replace(/[^a-zA-Z0-9]/g,'_');
    var _frPid = 'part-' + _frSafeLane + '-' + (d.label || '').replace(/[^a-zA-Z0-9]/g,'_');
    var _frBadge = document.getElementById('pcount-' + _frPid);
    if (!_frBadge) {
      // fallback: label match
      document.querySelectorAll('.part-item').forEach(function(_el) {
        if (_frBadge) return;
        if ((_el.dataset.partLabel || '') === String(d.label || '') &&
            (_el.dataset.workerKey || '') === String(_frLaneKey)) {
          _frBadge = _el.querySelector('[id^="pcount-"]');
        }
      });
    }
    if (_frBadge && typeof d.n_read === 'number' && typeof d.n_total === 'number') {
      _frBadge.textContent = d.n_read + ' / ' + d.n_total;
    }
  },
  // ── File-Change (live during tool loop) ──────────────────────────────────

  'file_change': function(d) {
    // 1. inline badge in the active coder bubble
    if (S.curAgent && S.curAgent.body) {
      var _tcBody = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
      var _tcPath = (d.path || '').replace(/\\/g, '/');
      var _tcSegs = _tcPath.split('/').filter(Boolean);
      var _tcShort = _tcSegs.length > 2 ? '\u2026/' + _tcSegs.slice(-2).join('/') : _tcPath;
      var _tcOp = (d.op || 'write').toLowerCase();
      var _tcLabel = {write: 'WRITE', edit: 'EDIT', append: 'APPEND'}[_tcOp] || _tcOp.toUpperCase();
      var _tcLine = document.createElement('div');
      _tcLine.className = 'tool-act';
      var _tcDelta = '';
      if (typeof d.lines_added === 'number' && (d.lines_added || d.lines_removed)) {
        _tcDelta = ' <span class="tool-act-delta">+' + (d.lines_added || 0)
          + '/-' + (d.lines_removed || 0) + '</span>';
      }
      _tcLine.innerHTML = '<span class="tool-act-op ' + _tcOp + '">' + _tcLabel + '</span>'
        + '<span class="tool-act-path" title="' + esc(d.path) + '">' + esc(_tcShort) + '</span>' + _tcDelta;
      _appendToolEl(_tcBody, _tcLine);
      // 1b. file content as expandable preview (2026-08-25: starts COLLAPSED
      //     — no auto-open/auto-close anymore; clicking "📄 N lines · X KB" opens it)
      if (d.content) {
        var _fcPreview = document.createElement('details');
        _fcPreview.className = 'fc-preview';
        var _fcLines = d.lines || 0;
        var _fcSize = d.content.length;
        var _fcSizeStr = _fcSize > 1024 ? (_fcSize/1024).toFixed(1)+' KB' : _fcSize+' B';
        var _fcSum = document.createElement('summary');
        _fcSum.textContent = '\uD83D\uDCC4 ' + _fcLines + ' lines \u00B7 ' + _fcSizeStr;
        _fcPreview.appendChild(_fcSum);
        var _fcPre = document.createElement('pre');
        _fcPre.textContent = d.content;
        _fcPreview.appendChild(_fcPre);
        _appendToolEl(_tcBody, _fcPreview);
      }
      scrollBtmIfNearBottom(80);
    }
    // 2a. live code panel — show content if present
    if (d.content) {
      _cpAddOrUpdateFile(d.path || '', d.content, d.op || 'write');
      // keep the stream-follow position across the stream→real-file swap
      if (typeof _cpFollowStream === 'boolean' && _cpFollowStream &&
          document.body.classList.contains('code-panel-open')) {
        var _cpb = document.getElementById('code-panel-body');
        if (_cpb) _cpb.scrollTop = _cpb.scrollHeight;
      }
    }
  },
  // ── Files summary (after the tool loop, shows all touched files) ──────────

  'files_summary': function(d) {
    rmEl('fc-panel');
    var _fs = document.createElement('div');
    _fs.className = 'files-summary';
    var _created  = (d.files||[]).filter(function(f){return f.op==='created'}).length;
    var _edited   = (d.files||[]).filter(function(f){return f.op==='edited'}).length;
    var _rewrote  = (d.files||[]).filter(function(f){return f.op==='rewrote'}).length;
    var _appended = (d.files||[]).filter(function(f){return f.op==='append'}).length;
    var _summ = d.summary || {};
    var _cats = [];
    if (_created)  _cats.push('<span class="fs-stat">'+_created+'</span> created');
    if (_edited)   _cats.push('<span class="fs-stat">'+_edited+'</span> edited');
    if (_rewrote)  _cats.push('<span class="fs-stat">'+_rewrote+'</span> rewritten');
    if (_appended) _cats.push('<span class="fs-stat">'+_appended+'</span> appended');
    var _delta = '';
    if (_summ.lines_added || _summ.lines_removed) {
      var _dp = [];
      if (_summ.lines_added)   _dp.push('+'+_summ.lines_added);
      if (_summ.lines_removed) _dp.push('-'+_summ.lines_removed);
      _delta = ' &middot; <span class="fs-stat">'+_dp.join('/')+'</span> lines';
    }
    _fs.innerHTML = '\uD83D\uDCC1 <span class="fs-stat">'+d.n_files+'</span> '
      + (d.n_files===1?'file':'files') + ' changed'
      + (_cats.length>1 ? ' &middot; '+_cats.join(', ') : '')
      + _delta;
    document.getElementById('chat').appendChild(_fs);
    scrollBtmIfNearBottom(120);
  },
  // ── Context meter (PERF-CONSOLIDATION: thin bar on top removed,
  //     values live in the lower performance bar) ─────────────────────────

  'ctx_meter': function(d) {
    _perfOnCtxMeter(d.est_tokens, d.ctx_limit, !!d.compressing, d.real_tokens, d.cached_tokens);
  },

  'memory_saved': function(d) {
    loadMemory();
  },

  'stream_gap': function(d) {
    // STREAM-GAP (audit 2026-11-03): the server dropped frames because the
    // consumer fell behind. Say so instead of showing a silently truncated
    // answer; the run journal still has the full text.
    _journalNote((d.dropped || 'Some') + ' stream frame(s) were dropped - the answer may be incomplete. The run journal has the full text.');
  },

  'done': function(d) {
    S._sawDoneEvent = true;
    _toolGenDone();
    try { sessionStorage.removeItem('hm_run_id'); } catch (e) {}
    _cleanupLoadTimers();
    _perfOnDone(d);
    setPauseBtnState('idle');
    setStopBtnState('idle');
    stopAskUserCountdown();
    var _qDivDone = document.getElementById('ask-user-question');
    if (_qDivDone) _qDivDone.remove();
    // APPROVAL-CARD-CLEAR (2026-09-19): a run can end while a card is still
    // up (abort paths that bypass agent_resumed, watchdog stop). Streaming
    // and poll are gone at this point, so clear it here.
    var _apDone = document.getElementById('approval-request-card');
    if (_apDone) {
      if (_apDone._apAutoInt) { clearInterval(_apDone._apAutoInt); _apDone._apAutoInt = null; }
      _apDone.remove();
    }
    var _askDone = document.getElementById('ask-user-card');
    if (_askDone) _askDone.remove();
    // Close any still-open coder bubble (no-critic runs skip duo_critic event)
    if (S.curAgent && S.curAgent.body && S.curAgent.body.closest('.duo-coder')) {
      doneAgent(S._coderElapsed || '');
    }
    document.querySelectorAll('#chat .ablock.duo-coder').forEach(function(el) {
      el.setAttribute('data-turn-done', '1');
    });
    S._coderElapsed = null;
    finalizePlannerBubble();
    // F2: run-end marker — all stop reasons.
    // UX (2026-10-24): every non-green reason carries a one-line hint —
    // a label alone never told the user what to DO next.
    var _stopMap = {
      'completed':       { icon: '\u2713', color: '#22c55e', border: 'rgba(34,197,94,.35)',  label: 'Run completed' },
      'graceful_stop':   { icon: '\u23F8', color: '#f0ad4e', border: 'rgba(240,173,78,.35)',  label: 'Graceful stop after chunk', hint: 'Continue with a follow-up message to pick up where it stopped.' },
      'loop_detected':   { icon: '\u21BA', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Loop detected - run stopped', hint: 'The model repeated itself. Retry and steer it explicitly, or use a stronger model.' },
      'blocked':         { icon: '\u26A0', color: '#e09030', border: 'rgba(224,144,48,.35)',  label: 'Run blocked', hint: 'A gate refused a tool call — check the approval card above or the approval settings.' },
      'max_tool_rounds': { icon: '\u23F1', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Max tool rounds reached', hint: 'The round budget is spent. Continue with a follow-up message.' },
      'stuck_in_loop':   { icon: '\u21BA', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Stuck loop aborted', hint: 'No progress for several rounds. Retry with a clearer, smaller task.' },
      'error':           { icon: '\u2716', color: '#c04040', border: 'rgba(200,64,64,.35)',   label: 'Run error', hint: 'Check logs/hivemind.log for the traceback, then retry.' },
      'user_aborted':    { icon: '\u23F9', color: '#808080', border: 'rgba(128,128,128,.35)', label: 'Run aborted (user)' },
      'aborted':         { icon: '\u25A0', color: '#c04040', border: 'rgba(200,64,64,.3)',   label: 'Run aborted', hint: 'The run died mid-flight (engine or model slot lost). Check logs/hivemind.log, then retry.' },
      'timeout':         { icon: '\u23F1', color: '#e09030', border: 'rgba(224,144,48,.3)',  label: 'Run timeout', hint: 'Time budget spent. Split the task or raise the duo timeout in Configs.' },
      // B11 (2026-08-04): error/stop reasons that previously fell silently on the green
      // 'completed' checkmark (_stopMap fallback) — "silently wrong".
      'hard_stop':         { icon: '\u26D4', color: '#c04040', border: 'rgba(200,64,64,.35)',   label: 'Run hard-stopped - resume available', hint: 'Continue this chat with a follow-up message to resume.' },
      'tool_round_error':  { icon: '\u26D4', color: '#c04040', border: 'rgba(200,64,64,.35)',   label: 'Tool round error - run stopped', hint: 'The last tool error is in the chat above. Fix the cause and continue.' },
      'verification_required': { icon: '\u26A0', color: '#f0ad4e', border: 'rgba(240,173,78,.35)', label: 'Verification pending - resume available', hint: 'Answer the open question or continue with a follow-up message.' },
      'halted':            { icon: '\u23F9', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Run halted', hint: 'A guard halted the run — the reason is in the status lines above.' },
      'timeout_guard':     { icon: '\u23F1', color: '#e09030', border: 'rgba(224,144,48,.3)',  label: 'Run timeout (guard)', hint: 'Continue with a follow-up message to resume.' },
      'vram_guard':        { icon: '\u26D4', color: '#c04040', border: 'rgba(200,64,64,.35)',   label: 'VRAM too small for coder ctx — reduce the ctx slider', hint: 'Lower the coder context (Agents tab) or free VRAM, then retry.' },
    };
    var _sr = _stopMap[d.stop_reason] || _stopMap['completed'];
    if (_sr && d.stop_reason) {
      var _endDiv = document.createElement('div');
      _endDiv.className = 'msg divider';
      _endDiv.style.cssText = 'color:' + _sr.color + ';border-color:' + _sr.border + ';font-size:10px;letter-spacing:.05em;font-weight:600';
      _endDiv.textContent = _sr.icon + ' ' + _sr.label;
      var _hintTxt = (d.stop_reason === 'blocked' && d.summary) ? d.summary : _sr.hint;
      if (_hintTxt) {
        var _hintEl = document.createElement('div');
        _hintEl.className = 'hint-line';
        _hintEl.textContent = _hintTxt;
        _endDiv.appendChild(_hintEl);
      }
      document.getElementById('chat').appendChild(_endDiv);
    }
    // B11 (2026-08-04): explicitly mark non-finished runs — no more
    // silent ending on hard_stop/tool_round_error/verification_required/
    // halted/timeout_guard (these reasons write resume blocks in the backend).
    var _resumeHints = { hard_stop: 1, tool_round_error: 1, verification_required: 1, halted: 1, timeout_guard: 1 };
    if (d.stop_reason && _resumeHints[d.stop_reason]) {
      var _hintDiv = document.createElement('div');
      _hintDiv.className = 'msg divider';
      _hintDiv.style.cssText = 'color:#b05050;border-color:rgba(200,64,64,.25);font-size:10px;letter-spacing:.05em;font-weight:600';
      _hintDiv.textContent = '\u26D4 Run not finished \u2014 resume possible (chat load offers to continue)';
      document.getElementById('chat').appendChild(_hintDiv);
    }
    // RETRY-BUTTON (2026-08-31): offer a "Retry" on error/abort runs,
    // which resends the last prompt (help with coder-load failures etc.).
    var _retryReasons = { error: 1, aborted: 1, hard_stop: 1, tool_round_error: 1, timeout: 1, timeout_guard: 1, stuck_in_loop: 1, loop_detected: 1, blocked: 1, max_tool_rounds: 1, verification_required: 1, halted: 1, vram_guard: 1 };
    if (d.stop_reason && _retryReasons[d.stop_reason] && S.lastPrompt) {
      var _rtWrap = document.createElement('div');
      _rtWrap.className = 'msg divider';
      _rtWrap.style.cssText = 'color:#c04040;border-color:rgba(200,64,64,.25);font-size:10px;font-weight:600;display:flex;align-items:center;gap:10px';
      var _rtBtn = document.createElement('button');
      _rtBtn.textContent = '\u21BA Retry';
      _rtBtn.style.cssText = 'background:none;border:1px solid #c06060;border-radius:4px;color:#c06060;font-family:\'IBM Plex Mono\',monospace;font-size:10px;font-weight:600;padding:4px 12px;cursor:pointer;letter-spacing:.05em';
      _rtBtn.onclick = function() { _rtBtn.disabled = true; _rtBtn.textContent = '\u21BA Retrying\u2026'; _retryLastPrompt(); };
      _rtWrap.appendChild(_rtBtn);
      // RETRY-IN-NEW-CHAT (2026-09-01): second button — same prompt,
      // but a fresh chat. Only offer it when the chat already has an
      // assistant turn; on a first-turn failure it only duplicated the
      // one-message history entry.
      var _hasAssistantTurn = (S.currentChatMessages || []).some(function(m) {
        return m && m.role === 'assistant' && String(m.content || '').trim();
      });
      if (_hasAssistantTurn) {
        var _rtNCBtn = document.createElement('button');
        _rtNCBtn.textContent = '\u21BA Retry in new chat';
        _rtNCBtn.style.cssText = 'background:none;border:1px solid #c09050;border-radius:4px;color:#d0a060;font-family:\'IBM Plex Mono\',monospace;font-size:10px;font-weight:600;padding:4px 12px;cursor:pointer;letter-spacing:.05em';
        _rtNCBtn.onclick = function() { _rtNCBtn.disabled = true; _rtNCBtn.textContent = '\u21BA Retrying\u2026'; _retryLastPromptNewChat(); };
        _rtWrap.appendChild(_rtNCBtn);
      }
      _rtWrap.appendChild(document.createTextNode('Run ended with "' + d.stop_reason + '"'));
      document.getElementById('chat').appendChild(_rtWrap);
    }
    // perf-bar: run finished → hide the agent-phase badge
    var _pbEl = document.getElementById('perf-bar');
    if (_pbEl) { _pbEl.style.opacity = '0'; _pbEl.style.transition = 'opacity 2s'; setTimeout(function(){ _pbEl.style.display='none'; _pbEl.style.opacity=''; var _pap=document.getElementById('perf-agent-phase'); if(_pap){_pap.className='';_pap.innerHTML='';} }, 2100); }
    // AUDIT-R2+ (2026-08-25): the big bottom bar fades out too
    var _pbbEl = document.getElementById('perf-bar-bottom');
    if (_pbbEl) { _pbbEl.style.opacity = '0'; _pbbEl.style.transition = 'opacity 1s'; setTimeout(function(){ _pbbEl.style.display='none'; _pbbEl.style.opacity=''; }, 1000); }
    // partitions grid: set all dots to done if still running
    document.querySelectorAll('.part-dot.running').forEach(function(el) {
      el.className = 'part-dot done';
    });
    document.querySelectorAll('.part-item.running').forEach(function(el) {
      el.className = 'part-item done';
    });
    // show the elapsed badge in the header
    if (d.elapsed) {
      const el = document.getElementById('h-elapsed');
      const val = document.getElementById('h-elapsed-val');
      var _elapsedTxt = d.elapsed + 's';
      // TOKEN-TRACKER UI (2026-08-25): prefer the server totals (real
      // eval_counts), fallback to the frontend usage_meta accumulation.
      var _genTok = parseInt(d.tokens_generated, 10) || 0;
      var _pTok = parseInt(d.prompt_tokens_total, 10) || S.runPromptTokens || 0;
      var _cTok = parseInt(d.cached_tokens_total, 10) || S.runCachedTokens || 0;
      var _rCnt = parseInt(d.requests_total, 10) || S.runRequestCount || 0;
      if (_genTok > 0) _elapsedTxt += ' \u00b7 ' + _fmtTokens(_genTok) + ' tok';
      if (_pTok > 0)  _elapsedTxt += ' \u00b7 <span class="tok-in">' + _fmtTokens(_pTok) + ' in</span>';
      if (_cTok > 0)  _elapsedTxt += ' \u00b7 <span class="tok-cached">' + _fmtTokens(_cTok) + ' cached</span>';
      if (_rCnt > 0)  _elapsedTxt += ' \u00b7 <span style="color:#6a7a8a">' + _rCnt + ' reqs</span>';
      if (el && val) { val.innerHTML = _elapsedTxt; el.style.display = 'flex'; }
      // RUN-STATS LINE (2026-10-23): stash + muted line under the final answer.
      // Model may be '' on an older engine — the line then just shows fewer bits.
      S._lastRunStats = {elapsed: parseFloat(d.elapsed) || 0, out: _genTok,
                         prompt: _pTok, cached: _cTok, requests: _rCnt,
                         model: String(d.model || ''),
                         tps: (S.perfTokRate > 0 ? S.perfTokRate : 0)};
      if (typeof renderRunStatsLine === 'function') renderRunStatsLine(S._lastRunStats);
    }
    S.runPromptTokens = 0;
    S.runCachedTokens = 0;
    S.runRequestCount = 0;
    setTimeout(loadMemory, 300);
    if (cfgState.learningMode) {
      setTimeout(function() {
        if (document.querySelector('.tab[data-p="configs"]').classList.contains('on')) {
          renderLog(cfgState.currentModel);
        }
      }, 800);
    }
  },

  'tool_call': function(d) {
    // TOOL METRICS (2026-10-07, owner): stamp the start so the expanded
    // metric panel can show per-tool duration; before early returns.
    S.perfToolStart = Date.now();
    S.perfToolLabel = String(d.name || 'tool');
    _perfRender(false);
    // model-initiated tool call — as a chip in the current coder bubble
    // Flush pending text tokens first so mid-sentence text is committed before the tool chip
    _flushTokenQueueSync();
    // TOOL-GEN-DONE: the streaming card is replaced by this real chip
    _toolGenDone();
    if (!S.curAgent) return;
    if (typeof rmEl === 'function') rmEl('status-el');
    // phase switch: first tool call → header changes from "Code" to "Execution"
    if (!S._coderHadToolCall) {
      S._coderHadToolCall = true;
      var _anEl = document.getElementById('ab-' + S.curAgent.tid);
      if (_anEl) {
        var _anName = _anEl.parentElement && _anEl.parentElement.querySelector('.aname');
        if (_anName) _anName.textContent = '\uD83D\uDEE0\uFE0F Execution';
      }
    }
    var _tcTid  = S.curAgent.tid; // BUG-4 FIX: snapshot tid NOW, before any async event
    var _tcBody = document.getElementById('ab-' + _tcTid) || S.curAgent.body;
    if (!_tcBody) return;
    if (S.curAgent.lt) {
      clearTimeout(S.curAgent.lt); S.curAgent.lt = null;
      var _tcLh = document.getElementById('lh-' + _tcTid);
      if (_tcLh) _tcLh.style.display = 'none';
    }
    var _tcIcon  = _TOOL_ICONS[d.name] || '\uD83D\uDD27';
    var _tcLabel = (d.label || '').trim();
    var _tcDet   = (d.detail || '').trim();
    var _tcRow   = document.createElement('div');
    _tcRow.className = 'tool-call-row';
    // BUG-4 FIX: stamp the row with the tid so tool_result can find
    // this exact bubble even if S.curAgent has changed by then.
    _tcRow.dataset.tid = _tcTid;
    _tcRow.dataset.toolName = d.name;
    _tcRow.dataset.callId = 'cp' + (++_cpCallSeq);
    var _tcChip  = document.createElement('span');
    _tcChip.className = 'tool-call-chip';
    // Read-Tool-Calls de-emphasized
    if (/^(read_file|find_files|list_dir|get_signatures|find_references|search_code|web_search|web_fetch)$/.test(d.name)) {
      _tcChip.classList.add('tc-read');
    }
    // Write/Edit tools — blue highlight
    else if (/^(write_file|write_file_append|edit_file|patch_file|replace_lines|edit_ast)$/.test(d.name)) {
      _tcChip.classList.add('tc-write');
      // 2026-08-25: remember the path — clicking the chip opens the live-code
      // panel with exactly this file (label=path per sse/events.py; for
      // edit_ast the path sits in the detail).
      var _tcPath = _tcLabel || _tcDet;
      if (_tcPath) {
        _tcRow.dataset.toolPath = _tcPath;
      }
    }
    // Run/bash/test tools — amber highlight
    else if (/^(run_bash|run_python|run_tests|git_commit|git_status)$/.test(d.name)) {
      _tcChip.classList.add('tc-run');
    }
    _tcChip.title = d.name + (_tcLabel ? '  ' + _tcLabel : '') + (_tcDet ? '  (' + _tcDet + ')' : '');
    if (_tcRow.dataset.toolPath) _tcChip.title += '  — click: file in live-code panel';
    _tcChip.innerHTML =
      '<span class="tc-icon">'  + _tcIcon + '</span>' +
      '<span class="tc-name">'  + esc(d.name) + '</span>' +
      (_tcLabel ? '<span class="tc-sep"> · </span><span class="tc-label">' + esc(_tcLabel) + '</span>' : '') +
      (_tcDet   ? '<span class="tc-detail">&thinsp;(' + esc(_tcDet) + ')</span>' : '');

    // Build extra info panel from server-sent extra dict (new in this patch)
    var _tcExtra = null;
    var _tcExtraData = d.extra || {};
    var _tcExtraKeys = Object.keys(_tcExtraData);
    if (_tcExtraKeys.length > 0) {
      _tcExtra = document.createElement('div');
      _tcExtra.className = 'tc-extra';
      // Table rows for scalar values
      var _tcScalars = _tcExtraKeys.filter(function(k) {
        return typeof _tcExtraData[k] !== 'object' && String(_tcExtraData[k]).indexOf('\n') === -1 && String(_tcExtraData[k]).length < 120;
      });
      var _tcLong = _tcExtraKeys.filter(function(k) { return _tcScalars.indexOf(k) === -1; });
      if (_tcScalars.length > 0) {
        var _tbl = '<table class="tc-extra-table">';
        _tcScalars.forEach(function(k) { _tbl += '<tr><td>' + esc(k) + '</td><td>' + esc(String(_tcExtraData[k])) + '</td></tr>'; });
        _tbl += '</table>';
        _tcExtra.innerHTML = _tbl;
      }
      _tcLong.forEach(function(k) {
        var _pre = document.createElement('pre');
        _pre.className = 'tc-extra-pre';
        _pre.textContent = k + ':\n' + String(_tcExtraData[k]);
        _tcExtra.appendChild(_pre);
      });
    }
    // chip click (2026-08-25, updated 2026-09-19): write/edit chips open the
    // live-code panel — with THIS call's diff when the call already finished
    // (pinned in the ±Diff view), or the streaming file view while it runs.
    // Other chips keep showing the server-delivered extra details.
    _tcChip.addEventListener('click', function(e) {
      e.stopPropagation();
      if (_tcRow.dataset.toolPath) {
        var _cpKey = _cpFindEntry(_tcRow.dataset.toolPath);
        if (_cpKey) {
          var entry = _cpFiles[_cpKey];
          var d = (entry.diffs || []).filter(function(x) { return x.id === (_tcRow.dataset.cpDiffId || _tcRow.dataset.callId); })[0];
          if (d) entry.pinnedCallId = d.id;   // pin THIS call for the ±Diff view
          entry.view = _cpLastView;           // open in the last used view mode
          _cpShowFile(_cpKey);
        }
        toggleCodePanel(true);
        return;
      }
      if (_tcExtra) _tcExtra.classList.toggle('open');
    });
    _tcRow.appendChild(_tcChip);
    if (_tcExtra) _tcRow.appendChild(_tcExtra);
    // Pre-Call Reasoning: Text vor dem ersten Tool-Call kollabieren
    var _preText = (_tcBody._liveRaw || (_tcBody._textRun && _tcBody._textRun.textContent) || '').trim();
    if (_preText) {
      var _preWrap = document.createElement('div');
      _preWrap.className = 'pre-call-reasoning';
      var _preHdr = document.createElement('div');
      _preHdr.className = 'pcr-hdr';
      var _preIcon = document.createElement('span');
      _preIcon.className = 'pcr-icon';
      _preIcon.textContent = '\uD83D\uDCAD';
      var _preToggle = document.createElement('span');
      _preToggle.className = 'pcr-toggle';
      _preToggle.textContent = '\u25b8';
      var _preLabel = document.createElement('span');
      _preLabel.className = 'pcr-label';
      _preLabel.textContent = 'Reasoning';
      var _preCount = document.createElement('span');
      _preCount.className = 'pcr-count';
      _preCount.textContent = '\u00b7 ' + _preText.length + ' chars';
      var _preSummary = document.createElement('span');
      _preSummary.className = 'pcr-summary';
      _preSummary.textContent = _preText.length > 80 ? _preText.slice(0,80) + '\u2026' : _preText;
      var _preBody = document.createElement('div');
      _preBody.className = 'pcr-body';
      _preBody.textContent = _preText;
      _preHdr.appendChild(_preIcon);
      _preHdr.appendChild(_preToggle);
      _preHdr.appendChild(_preLabel);
      _preHdr.appendChild(_preCount);
      _preHdr.appendChild(_preSummary);
      _preWrap.appendChild(_preHdr);
      _preWrap.appendChild(_preBody);
      _preWrap.addEventListener('click', function() {
        var open = _preWrap.classList.toggle('open');
        _preToggle.textContent = open ? '\u25be' : '\u25b8';
      });
    if (_tcBody._liveEl) { _tcBody._liveEl.remove(); _tcBody._liveEl = null; }
    _liveMdReset(_tcBody);
    _tcBody.appendChild(_preWrap);
    }
    // Insert AFTER current think-block so chronological order stays [Think] -> [Tool] -> [Result]
    var _tkCurrent = S._thinkBlockId ? document.getElementById(S._thinkBlockId) : null;
    if (_tkCurrent && _tkCurrent.parentNode === _tcBody) {
      _tcBody.insertBefore(_tcRow, _tkCurrent.nextSibling);
    } else {
      _tcBody.appendChild(_tcRow);
    }
    _liveMdReset(_tcBody);  // next text token starts a fresh live-md segment after this tool chip
    // Close current think-block: naechster thinking_token erzeugt neuen Inline-Block
    if (S._thinkBlockId) {
      var _tkOld = document.getElementById(S._thinkBlockId);
      if (_tkOld) _tkOld.classList.remove('live');
      S._thinkBlockId = null;
    }
    // Write-phase separator: visual marker before first write after reads-only phase
    if (/^(edit_file|patch_file|replace_lines|write_file_append|edit_ast)$/.test(d.name) && !S._coderHadWriteCall) {
      S._coderHadWriteCall = true;
      var _wsSep = document.createElement('div');
      _wsSep.style.cssText = 'margin:5px 0 3px;height:1px;background:rgba(58,153,96,.15);';
      _tcBody.appendChild(_wsSep);
    }
    _pruneToolCallRows(_tcBody, 200);
    if (typeof _syncSpeakButtons === 'function') _syncSpeakButtons(_tcBody.closest('.ablock'));
    scrollBtmIfNearBottom(60);
  },

  'tool_result': function(d) {
    // TOOL METRICS (2026-10-07, owner): close the interval - duration,
    // count and last-tool line for the expanded metric panel.
    if (S.perfToolStart > 0) {
      var _td = Date.now() - S.perfToolStart;
      S.perfToolStart = 0;
      if (_td >= 0 && _td < 3600000) {
        S.perfToolCalls = (S.perfToolCalls || 0) + 1;
        S.perfToolMs = (S.perfToolMs || 0) + _td;
        S.perfLastToolTxt = S.perfToolLabel + ' ' + (_td / 1000).toFixed(1) + 's';
      }
    }
    // Output des Tool-Calls — aufklappbar unter dem letzten Chip
    // Flush pending text tokens first
    _flushTokenQueueSync();
    // TOOL-GEN-DONE: close the live streaming block
    _toolGenDone();
    // BUG-4 FIX: don't rely on S.curAgent which may have changed between
    // tool_call and tool_result events. Instead find the most recent
    // tool-call-row in the DOM and use its stamped data-tid to resolve
    // the correct bubble body.
    var _trLastRow = null;
    if (S.curAgent) { var _trAB = document.getElementById("ab-" + S.curAgent.tid); if (_trAB) { var _trRR = _trAB.querySelectorAll(".tool-call-row"); if (_trRR.length) _trLastRow = _trRR[_trRR.length-1]; } }

    var _trBody;
    if (_trLastRow && _trLastRow.dataset && _trLastRow.dataset.tid) {
      _trBody = document.getElementById('ab-' + _trLastRow.dataset.tid);
    }
    if (!_trBody && S.curAgent) {
      _trBody = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
    }
    if (!_trBody) return;
    var _trOk   = d.ok !== false;
    var _trText = (d.text || '').trim();
    var _trFull = (d.full || d.text || '').trim();
    if (!_trText) return;
    // F1: Mark tool-call-chip with error class when result is error
    if (!_trOk && _trLastRow) {
      var _trChip = _trLastRow.querySelector('.tool-call-chip');
      if (_trChip) _trChip.classList.add('tc-error');
    }
    // Short: single-line <120 chars AND text matches full (multi-line output
    // must render in <pre>, never jammed into the flex <summary> row).
    var _trLines = _trFull.split('\n').length;
    var _trShort = _trFull.length < 120 && _trLines === 1 && _trText === _trFull;
    var _trBlock = document.createElement('details');
    _trBlock.className = 'tool-result-block' + (_trOk ? '' : ' err') + (_trShort ? ' short' : '');
    // BUG-1 FIX: always open short results — content is in _trPre which is
    // always appended now. Previously _trPre was skipped for short results,
    // leaving an open <details> with no body content at all.
    if (_trShort) _trBlock.open = true;
    // Determine tool name from the stamped row
    var _trToolName = (_trLastRow && _trLastRow.dataset && _trLastRow.dataset.toolName) || '';
    var _trIcon = _TOOL_ICONS[_trToolName] || '\uD83D\uDD27';
    var _trNameHtml = '<span class="tr-name">' + _trIcon + ' ' + esc(_trToolName || 'tool') + '</span>';
    // ── DIFFSTAT metrics (2026-08-25): write/edit results show compact
    // metrics instead of the raw +/- diff text ([DIFFSTAT] header from
    // tools/workspace.diff_for). The code itself is readable in the live-code
    // panel — clicking the tool chip opens it with this file.
    var _dsM = _trOk ? _trFull.match(/^\[DIFFSTAT\] added=(\d+) removed=(\d+) hunks=(\d+)(?: tokens=(\d+))? truncated=([01])$/m) : null;
    if (/^(write_file|write_file_append|edit_file|patch_file|replace_lines|edit_ast)$/.test(_trToolName) && _dsM) {
      var _dsAdd = parseInt(_dsM[1], 10), _dsRem = parseInt(_dsM[2], 10),
          _dsHunks = parseInt(_dsM[3], 10),
          _dsTok = _dsM[4] != null ? parseInt(_dsM[4], 10) : 0,
          _dsTrunc = _dsM[5] === '1';
      var _trSumDs = document.createElement('summary');
      _trSumDs.innerHTML = '<span class="tr-name">' + (_TOOL_ICONS[_trToolName] || '\uD83D\uDD27') + ' ' + esc(_trToolName) + '</span>'
        + '<span style="color:#22c55e;font-weight:700;margin-left:8px">+' + _dsAdd + '</span>'
        + '<span style="color:#e06060;font-weight:700;margin-left:6px">&minus;' + _dsRem + '</span>'
        + '<span style="color:var(--tx2);margin-left:10px">' + _dsHunks + (_dsHunks === 1 ? ' changed block' : ' changed blocks') + '</span>'
        + (_dsTok > 0 ? '<span style="color:#60a0e0;margin-left:10px">~' + (_dsTok >= 1000 ? (_dsTok / 1000).toFixed(1) + 'k' : _dsTok) + ' tok call</span>' : '')
        + (_dsTrunc ? '<span style="color:#f0ad4e;margin-left:10px;font-size:10px">(diff truncated)</span>' : '')
        + '<span data-cp-pin="1" style="float:right;opacity:.5;cursor:pointer;font-size:11px" title="Pin this exact diff in the code panel">\u2931</span>';
      var _cpPinKey = (_trLastRow && _trLastRow.dataset) ? _trLastRow.dataset.toolPath : '';
      // CALL-ID ORDER FIX (2026-09-20): _cpCallId was read before its
      // declaration (hoisted undefined) — the ⇱ pin stored undefined and
      // the panel never pinned to this exact call.
      _cpCallSeq++;
      var _cpCallId = (_trLastRow && _trLastRow.dataset && _trLastRow.dataset.callId) || ('cp' + _cpCallSeq);
      var _cpPinId = _cpCallId;
      var _cpPinEl = _trSumDs.querySelector('[data-cp-pin]');
      if (_cpPinEl) _cpPinEl.onclick = function(ev) {
        ev.preventDefault(); ev.stopPropagation();
        var _ek = _cpFindEntry(_cpPinKey);
        if (!_ek || !_cpFiles[_ek]) return;
        _cpFiles[_ek].pinnedCallId = _cpPinId;
        _cpFiles[_ek].view = 'diff';
        _cpLastView = 'diff';  // an explicit pin is a diff-mode choice
        _cpShowFile(_ek);
        toggleCodePanel(true);
      };
      var _dsIdx = _trFull.indexOf('[DIFFSTAT]');
      var _dsBody = _dsIdx >= 0 ? _trFull.slice(_dsIdx).replace(/^\[DIFFSTAT\][^\n]*\n?/, '') : _trFull;
      _dsBody = _dsBody.replace(/```[a-z]*\s*$/, '').trim();
      _dsBody = _cleanDiffBody(_dsBody);
      // DIFF-VIEW FEED (2026-09-19): record this file's diff so the code
      // panel's ±Diff view has something to render. Each call keeps its
      // own entry; the summary's ⇱ icon pins the panel to EXACTLY this
      // call's diff (no pin = latest call wins). _cpCallId is computed
      // above (CALL-ID ORDER FIX).
      var _cpKeyD = (_trLastRow && _trLastRow.dataset) ? _trLastRow.dataset.toolPath : '';
      var _cpEntryKey = _cpKeyD ? _cpFindEntry(_cpKeyD) : null;
      if (_cpEntryKey && _cpFiles[_cpEntryKey]) {
        var _cpEntry = _cpFiles[_cpEntryKey];
        _cpEntry.diffText = _dsBody;
        _cpEntry._diffCache = null;
        _cpEntry.diffs = _cpEntry.diffs || [];
        _cpEntry.diffs.push({ id: _cpCallId, name: _trToolName, text: _dsBody });
        // PER-CALL CHIP STAMP (2026-09-20): tool_call events carry no
        // call_id, so chip clicks could never find THEIR call's diff and
        // every click showed the pinned/first one. The result row stamps
        // its diff id here — the click handler reads it back.
        if (_trLastRow && _trLastRow.dataset) _trLastRow.dataset.cpDiffId = _cpCallId;
        if (_cpEntry.diffs.length > 12) _cpEntry.diffs.shift();
        if (_cpEntry.pinnedCallId && !_cpEntry.diffs.some(function(d) { return d.id === _cpEntry.pinnedCallId; })) _cpEntry.pinnedCallId = null;
        if (_cpActive === _cpEntryKey && _cpEntry.view === 'diff' && !_cpEntry.pinnedCallId) _cpShowFile(_cpEntryKey);
        _cpUpdateViewToggle();
      }
      var _trPre = document.createElement('pre');
      _trPre.className = 'tool-result-pre';
      _trPre.innerHTML = _renderDiffBody(_dsBody);
      // 2026-09-18: collapsed by default — the summary carries the metrics
      // (+N/−M/blocks); the diff body opens on click for those interested.
      _trBlock.appendChild(_trSumDs);
      _trBlock.appendChild(_trPre);
      _appendToolEl(_trBody, _trBlock);
      _liveMdReset(_trBody);
      scrollBtmIfNearBottom(60);
      return;
    }
    var _trSum = document.createElement('summary');
    if (_trOk) {
      if (_trShort) {
        _trSum.innerHTML = _trNameHtml + '<span class="tr-status-ok">\u2713</span> ' + esc(_trText);
      } else {
        _trSum.innerHTML = _trNameHtml + esc(_trLines + (_trLines === 1 ? ' line' : ' lines'));
      }
    } else {
      // Error: show first error line in summary for quick scan without opening
      var _trErrFirst = _trFull.split('\n')[0].trim().slice(0, 80);
      _trSum.innerHTML = _trNameHtml + '<span class="tr-status-err">\u2717</span> ' + esc(_trErrFirst || 'error');
    }
    var _trPre = document.createElement('pre');
    _trPre.className = 'tool-result-pre';
    if (_trToolName === 'list_dir' && _trOk) {
      // Render as colour-coded directory tree
      _trPre.innerHTML = _renderDirTree(_trFull);
    } else {
      _trPre.textContent = _trFull;
    }
    // F4: task_complete status badge
    var _trTcBadge = '';
    if (_trToolName === 'task_complete' && _trOk) {
      var _trTcStatus = 'untested';
      try { var _trJ = JSON.parse(_trFull); _trTcStatus = (_trJ.build_status || _trJ.status || '').toLowerCase(); } catch(_) {
        var _trRe = /(?:build_status|build|status)["\s:]+(\w+)/i; var _trM = _trFull.match(_trRe); if (_trM) _trTcStatus = _trM[1].toLowerCase();
      }
      var _trTcColor = /^(passing|completed|success)$/i.test(_trTcStatus) ? '#22c55e' : /^(failing|blocked|error)$/i.test(_trTcStatus) ? '#c04040' : '#686868';
      _trTcBadge = '<span style="display:inline-block;font-size:10px;font-weight:700;background:' + _trTcColor + '1a;color:' + _trTcColor + ';border:1px solid ' + _trTcColor + '40;border-radius:3px;padding:0 5px;margin-right:4px;">' + esc(_trTcStatus) + '</span>';
      _trSum.innerHTML = _trTcBadge + _trNameHtml;
      _trBlock.appendChild(_trSum);
      _trBlock.appendChild(_trPre);
      _appendToolEl(_trBody, _trBlock);
      _liveMdReset(_trBody);
      scrollBtmIfNearBottom(60);
      return;
    }
    // Better summary: per-tool detail + generic content preview
    if (_trOk && !_trShort) {
      var _trInner = '';
      if (_trToolName === 'read_file' || _trToolName === 'edit_file') {
        var _trBytes = _trFull.length;
        _trInner = esc(_trLines + ' lines \u00b7 ' + (_trBytes > 1024 ? (_trBytes/1024).toFixed(1)+'kb' : _trBytes+'b'));
      } else if (_trToolName === 'list_dir') {
        var _trDirCount = (_trFull.match(/📁|DIR\b/g) || []).length;
        _trInner = esc(_trDirCount + ' dirs \u00b7 ' + Math.max(0,_trLines - _trDirCount) + ' files');
      } else if (_trToolName === 'run_bash') {
        // F3: Exit-Code-Badge
        var _trExitMatch = _trFull.match(/\[exit code:\s*(\d+)\]/i);
        var _trBadge = '';
        if (_trExitMatch) {
          var _trExitCode = parseInt(_trExitMatch[1], 10);
          var _trExColor = _trExitCode === 0 ? '#22c55e' : '#c04040';
          _trBadge = '<span style="display:inline-block;font-size:10px;font-weight:700;background:' + _trExColor + '1a;color:' + _trExColor + ';border:1px solid ' + _trExColor + '40;border-radius:3px;padding:0 5px;margin-right:4px;">exit ' + _trExitCode + '</span>';
        }
        _trInner = _trBadge + esc(_trLines + (_trLines === 1 ? ' line' : ' lines'));
      } else if (_trToolName === 'web_search') {
        var _wsHits = (_trFull.match(/^\[\d+\]\s/gm) || []).length;
        if (_wsHits > 0) {
          _trInner = esc(_wsHits + (_wsHits === 1 ? ' result' : ' results'));
        } else {
          var _wsFirst = '';
          var _wsArr = _trFull.split('\n');
          for (var _wsi = 0; _wsi < _wsArr.length; _wsi++) { if (_wsArr[_wsi].trim()) { _wsFirst = _wsArr[_wsi].trim(); break; } }
          _trInner = '<span class="tr-preview">' + esc(_wsFirst.slice(0, 80) + (_wsFirst.length > 80 ? '\u2026' : '')) + '</span>';
        }
      } else {
        // Generic: 1-line content preview
        var _trFirst = '';
        var _trLinesArr = _trFull.split('\n');
        for (var _li = 0; _li < _trLinesArr.length; _li++) { if (_trLinesArr[_li].trim()) { _trFirst = _trLinesArr[_li].trim(); break; } }
        _trInner = '<span class="tr-preview">' + esc(_trFirst.slice(0, 80) + (_trFirst.length > 80 ? '\u2026' : '')) + '</span>';
      }
      _trSum.innerHTML = _trNameHtml + _trInner;
    }
    _trBlock.appendChild(_trSum);
    // SHORT-RESULT DEDUP (2026-10-14): short results force the block open
    // (_trShort sets open=true) — the summary already carries the FULL text,
    // so the pre rendered the same line a second time (seen on every green
    // run_tests result). Skip the pre when the summary IS the content.
    if (!(_trShort && _trText === _trFull)) _trBlock.appendChild(_trPre);
    _appendToolEl(_trBody, _trBlock);
    _liveMdReset(_trBody);  // next text token starts a fresh live-md segment after this result
    scrollBtmIfNearBottom(60);
  },
};

function handleEvent(d) {
  // RATE FIX v2 (2026-10-07, user feedback: git fields must not reset on save):
  // active decode time must count EVERY stream activity - tokens,
  // tool_gen bursts and thinking tokens are all model output, while
  // usage_meta counts ALL completion tokens. Counting only text token
  // events made tokens/active-time explode.
  if (d && (
    d.type === 'token' || d.type === 'thinking_token' || d.type === 'tool_gen'
    || d.type === 'planner_token' || d.type === 'planner_thinking_token'
    || d.type === 'planner_plan_token'
  )) {
    var _an = Date.now();
    if (!S.perfFirstTokenAt) { S.perfFirstTokenAt = _an; S.perfLastTokAt = _an; }
    var _ag = _an - (S.perfLastTokAt || _an);
    if (_ag >= 0 && _ag < 2000) S.perfActiveMs += _ag;
    S.perfLastTokAt = _an;
  }
  var handler = (d && d.type && Object.prototype.hasOwnProperty.call(EVENT_HANDLERS, d.type))
    ? EVENT_HANDLERS[d.type]
    : null;
  if (typeof handler === 'function') handler(d);
}
