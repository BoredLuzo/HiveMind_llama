// HiveMind UI module: Routing, Vision Config, Pre-Explore/Planner Ctx & VRAM/Models manager.
// -- Image Description -----------------------------------------
function showImageDescription(text) {
  const c = document.getElementById('chat');
  const wrap = document.createElement('div');
  wrap.className = 'msg';
  const box = document.createElement('div');
  box.className = 'img-desc-box';
  const hdr = document.createElement('div');
  hdr.className = 'img-desc-hdr';
  const chevron = document.createElement('span');
  chevron.textContent = '▾';
  chevron.style.cssText = 'font-size:11px;transition:transform .2s';
  hdr.appendChild(chevron);
  const lbl = document.createElement('span');
  lbl.textContent = 'VISION-PREPROCESSING — IMAGE DESCRIPTION';
  hdr.appendChild(lbl);
  box.appendChild(hdr);
  const body = document.createElement('div');
  body.className = 'img-desc-body';
  body.textContent = text;
  box.appendChild(body);
  chevron.style.transform = 'rotate(180deg)';  // starts open
  hdr.onclick = function() {
    var collapsed = body.classList.toggle('collapsed');
    chevron.style.transform = collapsed ? '' : 'rotate(180deg)';
  };
  wrap.appendChild(box);
  c.appendChild(wrap);
  scrollBtmIfNearBottom(120);
}
function updatePreExploreAdv() {
    var wrap = document.getElementById('preexplore-adv-wrap');
    if (wrap) wrap.style.display = 'block';
    var passWrap = document.getElementById('duo-pass-files-wrap');
    if (passWrap) passWrap.style.display = S.duoPreExplore ? 'block' : 'none';
    // auto-open the advanced panel when pre-explore gets enabled
    if (S.duoPreExplore) {
      var body  = document.getElementById('duo-ctx-body');
      var arrow = document.getElementById('duo-ctx-arrow');
      if (body && body.style.display === 'none') {
        body.style.display = 'block';
        if (arrow) arrow.style.transform = 'rotate(90deg)';
      }
    }
}
function updateParallelPreexploreWrap() {
    var wrap = document.getElementById('duo-parallel-preexplore-wrap');
    if (wrap) wrap.style.display = S.duoPreExplore ? 'block' : 'none';
}

function setDuoParallelPreexplore(on) {
    S.duoParallelPreexplore = !!on;
    postSettings({duo_parallel_preexplore: S.duoParallelPreexplore});
}
// PERSIST-FLAG (2026-10-25, forensics): applySettings calls this on every
// render, and the posts below used to fire there too. With duo_chunking off,
// every page load re-posted duo_test_feedback_chunk:false — the saved true
// could not survive a reload and a preset saved from such a page snapshotted
// the reverted value (live 2026-10-24/25). Render calls pass persist=false;
// the chunking checkbox (index.html) keeps calling without the flag and
// persists as before.
function onChunkingChange(persist) {
    var _persist = persist !== false;
    var tpcToggle = document.getElementById('duo-thinking-per-chunk-toggle');
    var tpcLabel  = document.getElementById('tpc-label');
    var tpcHint   = document.getElementById('tpc-hint');
    var thinkTog  = document.getElementById('duo-agentic-thinking-toggle');
    var thinkRow  = thinkTog && thinkTog.closest('.opt-row');
    var thinkHint = thinkRow && thinkRow.querySelector('.opt-hint');
    var tfChunkRow     = document.getElementById('duo-test-feedback-chunk-row');
    var tfChunkToggle  = document.getElementById('duo-test-feedback-chunk-toggle');
    if (S.duoChunking) {
        // Chunking ON → per-chunk thinking activatable
        if (tpcToggle) { tpcToggle.disabled = false; }
        if (tpcLabel)  { tpcLabel.style.opacity  = ''; }
        if (tpcHint)   {
            tpcHint.style.opacity = '';
            tpcHint.textContent = 'Thinking before each subtask (slower, for complex dependencies)';
        }
        // planner thinking forced on — without reasoning no smart chunks
        // FIX: persist the original preference, do NOT persist the forced value
        if (S._thinkingBeforeChunking === undefined || S._thinkingBeforeChunking === null) {
            S._thinkingBeforeChunking = S.duoAgenticThinking;
            if (_persist) postSettings({_thinking_before_chunking: S.duoAgenticThinking}); // persist original preference
        }
        S.duoAgenticThinking = true; // UI-only override, NOT persisted
        if (thinkTog) { thinkTog.checked = true; thinkTog.disabled = true; }
        if (thinkHint) thinkHint.textContent = 'Forced during chunking — planner needs reasoning for smart splitting';
        // do NOT postSettings({duo_agentic_thinking: true}) — that corrupts the user value!
        // the server enforces thinking during chunking automatically (see server.py)
        // show the per-chunk test feedback
        if (tfChunkRow) tfChunkRow.style.display = '';
        if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
    } else {
        // Chunking OFF → disable + dim per-chunk thinking
        S.duoThinkingPerChunk = false;
        if (tpcToggle) { tpcToggle.checked = false; tpcToggle.disabled = true; }
        if (tpcLabel)  { tpcLabel.style.opacity  = '.4'; }
        if (tpcHint)   {
            tpcHint.style.opacity = '.4';
            tpcHint.textContent = 'Think before each subtask — requires chunking';
        }
        // planner thinking: restore the original preference
        if (thinkTog) {
            thinkTog.disabled = false;
            // FIX: restore from the persisted preference (survives page reload)
            var _restoreThinking = S._thinkingBeforeChunking;
            if (typeof _restoreThinking === 'boolean') {
                S.duoAgenticThinking = _restoreThinking;
                thinkTog.checked = S.duoAgenticThinking;
                if (_persist) postSettings({duo_agentic_thinking: S.duoAgenticThinking});
            }
            // cleanup: remove the stored preference
            S._thinkingBeforeChunking = undefined;
            if (_persist) postSettings({_thinking_before_chunking: null}); // clear persisted backup
        }
        if (thinkHint) thinkHint.textContent = 'Default when Planner Effort is Auto';
        if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
        // Test-Feedback pro Chunk ausblenden (nur bei Chunking relevant)
        S.duoTestFeedbackChunk = false;
        if (tfChunkToggle) { tfChunkToggle.checked = false; }
        if (tfChunkRow) tfChunkRow.style.display = 'none';
        if (_persist) postSettings({duo_test_feedback_chunk: false});
    }
}
function updatePlannerHint() {
    var chunkRow = document.getElementById('duo-chunking-row');
    if (chunkRow) chunkRow.style.display = S.duoPlannerEnabled ? '' : 'none';
    // PLANNER-OFF (2026-11-04): chunking needs a planner plan. Turning the
    // planner off must clear a previously enabled chunking setting too, not
    // just hide its row - otherwise the backend keeps planning silently.
    if (!S.duoPlannerEnabled && S.duoChunking) {
        S.duoChunking = false;
        postSettings({duo_chunking: false});
        onChunkingChange(false);
    }
    if (typeof _renderReasoningEffortUI === 'function') {
        _renderReasoningEffortUI(S.reasoningEffort);
    }
}
function updateAgenticCombinedWarn() {
    var warn = document.getElementById('duo-all3-warn');
    if (!warn) return;
    var slow = S.duoChunking && S.duoThinkingPerChunk && S.duoPreExplore;
    warn.style.display = slow ? 'block' : 'none';
}
function toggleDuoCtxAdv() {
  var body  = document.getElementById('duo-ctx-body');
  var arrow = document.getElementById('duo-ctx-arrow');
  if (!body) return;
  var open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  if (arrow) arrow.style.transform = open ? '' : 'rotate(90deg)';
}

function parseInputAsOptionalInt(val, fallback) {
  // empty / null / undefined → don't store, let server use model default from num_ctx_config.py
  if (val === '' || val === null || val === undefined) return null;
  var n = parseInt(val, 10);
  return isNaN(n) ? fallback : n;
}
function _ctxNum(id, fallback) {
  var el = document.getElementById(id);
  var v = el ? parseInputAsOptionalInt(el.value, fallback) : fallback;
  return (v === null || isNaN(v)) ? fallback : v;
}

function togglePlannerUseCoderCtx(on, persist) {
  S.duoPlannerUseCoderCtx = on;
  if (persist !== false) {
    postSettings({duo_planner_use_coder_ctx: on});
  }
  var plInput = document.getElementById('duo-ctx-planner');
  var plWrap = document.getElementById('wrapper-ctx-planner');
  if (on) {
    if (plWrap) plWrap.style.opacity = '0.5';
    if (plInput) { plInput.disabled = true; plInput.value = _ctxNum('duo-ctx-agentic', 16384); }
  } else {
    if (plWrap) plWrap.style.opacity = '1';
    if (plInput) { plInput.disabled = false; plInput.value = S.duoCtxPlanner || 16384; }
  }
  updateCtxScopeHint();
  if (typeof updateDuoImageUI === 'function') updateDuoImageUI();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
}

// CONTEXT-SLOT (2026-10-20): die Context-Felder leben in der Advanced-Leiste,
// gehoeren aber sichtbar in die Code/Agentic-Karte. Wir merken uns den
// Ursprungsplatz einmalig und schieben sie bei Bedarf hin und zurueck.
function _ctxMoveTo(wrap, target) {
  if (!wrap || !target) return;
  if (!wrap.dataset.ctxOrigin) {
    S._ctxOrig = S._ctxOrig || {};
    S._ctxOrig[wrap.id] = {parent: wrap.parentNode, next: wrap.nextSibling};
    wrap.dataset.ctxOrigin = '1';
  }
  if (wrap.parentNode !== target) target.appendChild(wrap);
}

function _ctxRestore(wrap) {
  if (!wrap) return;
  var orig = (S._ctxOrig || {})[wrap.id];
  if (!orig || !orig.parent) return;
  if (wrap.parentNode !== orig.parent) {
    try { orig.parent.insertBefore(wrap, orig.next || null); } catch (e) { orig.parent.appendChild(wrap); }
  }
}

// ── ROUTING (2026-10-21) ────────────────────────────────────────────────
// Until now nothing in the UI said WHO picks the route when the mode is
// "auto". These three selects plus a live line make it explicit.
function setRouting(key, value) {
  if (!key) return;
  var patch = {};
  patch[key] = value;
  postSettings(patch);
  if (key === 'routing_decider') S.routingDecider = value;
  if (key === 'routing_judge') S.routingJudge = value;
  if (key === 'routing_planner') S.routingPlanner = value;
  renderRoutingStatus();
  if (key === 'routing_planner') {
    if (typeof _renderReasoningEffortUI === 'function') {
      _renderReasoningEffortUI(S.reasoningEffort);
    }
  }
  if (typeof updateModeDesc === 'function') updateModeDesc();  // UX: the auto text names the router
}

function renderRoutingStatus() {
  var box = document.getElementById('routing-status');
  if (!box) return;
  var dec = S.routingDecider || 'off';
  var jud = S.routingJudge || 'on';
  var pln = S.routingPlanner || 'auto';
  var thr = (typeof S.routingThreshold === 'number') ? S.routingThreshold : 0.55;
  var mode = (S.mode || 'auto');

  var who;
  if (dec === 'on') {
    who = 'decider (encoder on CPU) decides; confident calls are final';
    if (jud === 'on') who += '; unsure calls go to the judge';
    else if (jud === 'shadow') who += '; unsure calls fall back to keyword rules (judge only logs)';
    else who += '; unsure calls fall back to keyword rules';
  } else if (dec === 'shadow') {
    who = 'keyword rules - the decider only logs what it would pick';
  } else {
    who = (jud === 'off') ? 'keyword rules (decider and judge are off)'
                          : 'LLM judge';
  }
  var plnLabel = {
    always: 'always plan', decider: 'decider decides', never: 'never plan',
    // legacy values still resolve, they are just not offered any more
    auto: 'heuristic (free)', heuristic: 'heuristic (free)', judge: 'judge decides'
  }[pln] || pln;
  if (S.routingPlannerLegacy) {
    plnLabel += ' <span class="rt-warn">(stored: ' + S.routingPlannerLegacy + ')</span>';
  }

  var line = 'Route: <b>' + who + '</b><br>Plan step: <b>' + plnLabel + '</b>';
  line += ' &middot; decider threshold ' + thr.toFixed(2);
  if (mode && mode !== 'auto') {
    line += '<br><span class="rt-warn">Mode "' + mode + '" skips this card entirely.</span>';
  }
  box.innerHTML = line;
}

function updateCtxScopeHint() {
  var agWrap = document.getElementById('wrapper-ctx-agentic');
  var noWrap = document.getElementById('wrapper-ctx-normal');
  var plWrap = document.getElementById('wrapper-ctx-planner');
  var crWrap = document.getElementById('wrapper-ctx-critic');
  var plTog = document.getElementById('wrapper-planner-coder-toggle');
  var hintBox = document.getElementById('duo-ctx-scope-hint');
  
  if (!agWrap || !noWrap || !hintBox) return;

  // hide everything
  agWrap.style.display = 'none';
  noWrap.style.display = 'none';
  if (plWrap) plWrap.style.display = 'none';
  if (crWrap) crWrap.style.display = 'none';
  if (plTog) plTog.style.display = 'none';
  hintBox.style.display = 'none';
  hintBox.innerHTML = '';

  var isAgentic = S.duoAgenticMode;
  var isUF = S.duoUntilFinished;
  var plannerUseCoder = S.duoPlannerUseCoderCtx !== false; // default true
  
  var _slot = document.getElementById('code-ctx-slot');
  var _card = document.getElementById('ctx-agentic-card');
  if (!isAgentic) {
    if (_card) { _ctxRestore(_card); _card.style.display = 'none'; }
    _ctxRestore(agWrap);
    if (plTog) _ctxRestore(plTog);
    if (plWrap) _ctxRestore(plWrap);
  }
  if (isAgentic) {
    // agentic mode: eine Karte mit Kopfzeile, nicht drei lose Bloecke
    if (_card) _card.style.display = 'block';
    agWrap.style.display = 'block';
    if (plTog) plTog.style.display = 'block';
    if (plWrap) plWrap.style.display = plannerUseCoder ? 'none' : 'block';
    if (_slot && _card) _ctxMoveTo(_card, _slot);
    
    var coderCtx = _ctxNum('duo-ctx-agentic', 16384);
    var plannerCtx = plannerUseCoder ? coderCtx : _ctxNum('duo-ctx-planner', 16384);
    var coderKV = (coderCtx / 1024 * 0.025).toFixed(1);
    var plannerKV = (plannerCtx / 1024 * 0.025).toFixed(1);
    
    // sync the planner input status
    if (plannerUseCoder) {
      var plInput = document.getElementById('duo-ctx-planner');
      if (plInput) { plInput.disabled = true; plInput.value = coderCtx; }
      if (plWrap) plWrap.style.opacity = '0.5';
    }
    
    var scopeLabel = isUF ? 'Agentic + Until-Finished' : 'Agentic Solo';
    hintBox.style.display = 'block';
    
    if (plannerUseCoder) {
      hintBox.innerHTML =
        '<b>' + scopeLabel + '</b> &middot; Ctx: <b>' + coderCtx + '</b>'
        + ' &middot; KV-Cache (Coder+Planner) ~' + coderKV + ' GB'
        + '<br><span style="color:var(--tx2)">Warning: too much context can cause OOM.</span>';
    } else {
      hintBox.innerHTML =
        '<b>' + scopeLabel + '</b> &middot; Ctx: <b>' + coderCtx + '</b> &middot; KV ~' + coderKV + ' GB'
        + '<br><span style="color:#9a74dc">Planner:</span> <b>' + plannerCtx + '</b> &middot; KV ~' + plannerKV + ' GB'
        + '<br><span style="color:var(--tx2)">Warning: too much context can cause OOM.</span>';
    }
  } else {
    // Critic-Duo mode: coder + critic
    noWrap.style.display = 'block';
    if (crWrap) crWrap.style.display = 'block';
    
    var coderCtx = _ctxNum('duo-ctx-normal', 8192);
    var criticCtx = _ctxNum('duo-ctx-critic', 8192);
    var coderKV = (coderCtx / 1024 * 0.025).toFixed(1);
    var criticKV = (criticCtx / 1024 * 0.025).toFixed(1);
    
    hintBox.style.display = 'block';
    hintBox.innerHTML =
      '<b>Dual (Coder + Critic)</b>'
      + ' &middot; Coder: <b>' + coderCtx + '</b> &middot; KV ~' + coderKV + ' GB'
      + '<br>Critic: <b>' + criticCtx + '</b> &middot; KV ~' + criticKV + ' GB'
      + '<br><span style="color:var(--tx2)">Warning: too much context can cause OOM.</span>';
  }
}

// -- Vision Accordion -----------------------------------------
function _setDescribePreview(preview, badge, inputEl, shortName) {
  if (shortName) {
    preview.textContent = '(' + shortName + ' · Describe)';
    if (badge) { badge.style.display = 'inline-block'; badge.classList.add('on'); badge.textContent = 'DESCRIBE'; }
    if (inputEl) inputEl.style.borderColor = '#8858c0';
  } else {
    preview.textContent = '';
    if (badge) badge.style.display = 'none';
    if (inputEl) inputEl.style.borderColor = '';
  }
}

function _applyImageModeUI(mode) {
  var bd = document.getElementById('im-direct');
  var bp = document.getElementById('im-preprocess');
  if (bd) bd.classList.toggle('on', mode !== 'preprocess');
  if (bp) bp.classList.toggle('on', mode === 'preprocess');
  var ds = document.getElementById('im-direct-sec');
  if (ds) ds.style.display = (mode !== 'preprocess') ? 'block' : 'none';
  if (typeof updateDuoImageUI === 'function') { try { updateDuoImageUI(); } catch (e) {} }
}

function setImageMode(mode) {
  var eff = (mode === 'preprocess') ? 'preprocess' : 'direct';
  S.imageMode = eff;
  S.visionEnabled = (eff === 'preprocess');
  fetch('/vision/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: S.visionEnabled})});
  _applyImageModeUI(eff);
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
}


// TG-ENABLED = PROCESS (2026-10-06, owner): toggling the master switch in
// the UI now STARTS/STOPS the gateway process itself (detached, supervised
// routes) - previously it was only a veto and the process needed the
// Start button or the engine autostart.
function setTgEnabled(on) {
  S.tgEnabled = !!on;
  postSettings({telegram_gateway_enabled: S.tgEnabled});
  fetch(S.tgEnabled ? '/gateway/start' : '/gateway/stop', {method: 'POST'})
    .then(function() { if (typeof refreshTgSupStatus === 'function') { try { refreshTgSupStatus(); } catch (e) {} } })
    .catch(function() {});
}

function setVisionAgentEnabled(enabled) {
  S.visionAgentEnabled = !!enabled;
  postSettings({vision_agent_enabled: S.visionAgentEnabled});
  updateDuoImageUI();
}

function setPipelineImageMode(m) {
  var isDirect = (m === 'direct');
  S.pipelineVisionDirect = isDirect;
  if (isDirect) {
    var _anyRole = ['analyst','refiner','critic','synthesizer'].some(function(r) {
      return S.pipelineVisionRoles && S.pipelineVisionRoles[r];
    });
    if (!_anyRole) {
      if (!S.pipelineVisionRoles) S.pipelineVisionRoles = {};
      S.pipelineVisionRoles.analyst = true;
      _applyPipelineVisionRolesUI();
    }
    S.visionAgentEnabled = false;
  } else {
    S.visionAgentEnabled = true;
  }
  var vaEl = document.getElementById('va-enabled-toggle');
  if (vaEl) vaEl.checked = S.visionAgentEnabled;
  postSettings({
    pipeline_vision_direct: S.pipelineVisionDirect,
    vision_agent_enabled: S.visionAgentEnabled,
    pipeline_vision_roles: S.pipelineVisionRoles || {}
  });
  updateDuoImageUI();
}

// CODER ALWAYS-VISION (2026-10-06): preload the projector for the coder so
// image steers inject without the mid-run vision-upgrade stall. Costs VRAM
// every run — opt-in.
function setCoderAlwaysVision(enabled) {
  S.coderAlwaysVision = !!enabled;
  postSettings({duo_coder_always_vision: !!enabled});
}

// CODER IMAGES OFF (2026-10-07, owner): hard blocker — resolve_image_plan
// forces coder:"none", the coder slot never loads the projector, image
// steers to the coder are dropped. Overrides the role checkbox + always
// vision (both UI copies get disabled while it is on).
function setCoderImagesOff(off) {
  S.coderImagesOff = !!off;
  postSettings({duo_coder_images_off: !!off});
  if (S.coderImagesOff && S.coderAlwaysVision) {
    S.coderAlwaysVision = false;
    postSettings({duo_coder_always_vision: false});
  }
  updateDuoImageUI();
}

function _applyPipelineVisionRolesUI() {
  ['analyst','refiner','critic','synthesizer'].forEach(function(r) {
    var el = document.getElementById('pv-role-' + r);
    if (el) el.checked = !!(S.pipelineVisionRoles && S.pipelineVisionRoles[r]);
  });
}

function setPipelineVisionRole(role, enabled) {
  if (!S.pipelineVisionRoles) S.pipelineVisionRoles = {};
  S.pipelineVisionRoles[role] = enabled;
  postSettings({
    pipeline_vision_roles: S.pipelineVisionRoles,
    pipeline_vision_direct: S.pipelineVisionDirect === true
  });
  updateDuoImageUI();
}

function setVisionAgentModel(model) {
  S.visionAgentModel = model;
  postSettings({vision_agent_model: model});
  updateDuoImageUI();
}

function setVisionAgentMode(mode) {
  // Client state only (2026-10-03): the pipeline runner is sequential;
  // nothing backend-side reads vision_agent_mode, so it is not posted.
  S.visionAgentMode = mode;
  _updateVisionAgentModeUI(mode);
}

function _updateVisionAgentModeUI(mode) {
  var seq = document.getElementById('va-mode-seq');
  var par = document.getElementById('va-mode-par');
  var desc = document.getElementById('va-mode-desc');
  if (seq) seq.style.borderColor = mode === 'sequential' ? 'var(--amber)' : '';
  if (par) par.style.borderColor = mode === 'parallel'   ? 'var(--amber)' : '';
  if (desc) desc.textContent = mode === 'sequential'
    ? 'Vision runs BEFORE the analyst — output is injected as context.'
    : 'Vision runs parallel to the analyst — both outputs go to the synthesizer.';
}

function populateVisionAgentModelSel() {
  var sel = document.getElementById('va-model-sel');
  if (!sel) return;
  var cur = S.visionAgentModel || '';
  sel.innerHTML = '<option value="">— choose model —</option>';
  var grpVision = document.createElement('optgroup');
  grpVision.label = '✓ Vision-capable';
  var grpOther = document.createElement('optgroup');
  grpOther.label = 'Other models';
  (S.models || []).forEach(function(m) {
    var opt = document.createElement('option');
    opt.value = m;
    var inAllowlist = S.visionAllowlist && S.visionAllowlist.has(m);
    var prof = S.modelProfiles && S.modelProfiles[m] || {};
    var looksVision = ['vl','llava','vision','moondream','minicpm','glm'].some(function(v){
      return m.toLowerCase().includes(v);
    });
    if (inAllowlist) {
      opt.textContent = '✓ ' + m;
      opt.style.color = '#4caf82';
      grpVision.appendChild(opt);
    } else if (prof.vision || looksVision) {
      opt.textContent = '👁 ' + m;
      grpVision.appendChild(opt);
    } else {
      opt.textContent = m;
      grpOther.appendChild(opt);
    }
    if (m === cur) opt.selected = true;
  });
  if (grpVision.children.length) sel.appendChild(grpVision);
  if (grpOther.children.length) sel.appendChild(grpOther);
}

function toggleVisionAccordion() {
  const body  = document.getElementById('vision-accordion-body');
  const arrow = document.getElementById('vision-accordion-arrow');
  if (!body) return;
  const isOpen = body.style.display !== 'none';
  body.style.display  = isOpen ? 'none' : 'block';
  if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(180deg)';
}

function _updateVisionPreview() {
  const preview = document.getElementById('vision-model-preview');
  const badge   = document.getElementById('vision-active-badge');
  const inputEl = document.getElementById('input');
  const flowCard = document.getElementById('vision-flow-card');
  if (!preview) return;

  var _dModel = (S.currentAssignments && S.currentAssignments.direct && S.currentAssignments.direct.model) || '';
  var _dVision = _isVisionCapableModel(_dModel);
  var _dDisp = _dModel ? _dModel.replace(':latest','').split('/').pop() : '—';
  var _duoMdls = _duoImageRoleModels();
  var _pModel = _duoMdls.planner;
  var _cModel = _duoMdls.coder;
  var _pVision = _isVisionCapableModel(_pModel);
  var _cVision = _isVisionCapableModel(_cModel);
  var _flow = '';

  if (S.mode === 'automap') {
    if (flowCard) {
      flowCard.style.borderColor = 'rgba(240,176,64,.45)';
      flowCard.style.background = 'rgba(240,176,64,.07)';
      _flow = '<span style="color:#f0b040">&#9654; AutoMap:</span> image runs prefer multimodal models for Direct / Analyst.<br>'
            + '<span style="color:var(--tx3)">Or enable Describe first below to turn images into text before the run.</span>';
      flowCard.innerHTML = _flow;
    }
    preview.textContent = (S.imageMode === 'preprocess' && S.visionModel)
      ? ('(' + S.visionModel.replace(':latest','').split('/').pop() + ' · Describe)')
      : '';
    if (badge) {
      var onAm = (S.imageMode === 'preprocess' && !!S.visionModel);
      badge.style.display = onAm ? 'inline-block' : 'none';
      badge.classList.toggle('on', onAm);
      if (onAm) badge.textContent = 'DESCRIBE';
    }
    if (inputEl) inputEl.style.borderColor = (S.imageMode === 'preprocess' && S.visionModel) ? '#8858c0' : '';
    return;
  }

  if (S.mode === 'code_duo') {
    var _dm = S.duoImageMode || ((S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct');
    if (flowCard) {
      if (_dm === 'preprocess') {
        flowCard.style.borderColor = 'rgba(224,144,48,.45)';
        flowCard.style.background = 'rgba(224,144,48,.07)';
        var _dsm = (S.visionModel || '').replace(':latest','').split('/').pop();
        _flow = '<span style="color:#e0a050">&#9654; Agentic Describe first:</span> ' + (_dsm || 'pick a Vision Describer below')
              + ' turns images into text once.<br>'
              + '<span style="color:var(--tx3)">Planner, Coder, and Critic stay text-only.</span>';
      } else {
        flowCard.style.borderColor = 'rgba(32,176,160,.45)';
        flowCard.style.background = 'rgba(32,176,160,.07)';
        var _rawTargets = [];
        if (S.duoImageToPlanner) _rawTargets.push('Planner' + (_pVision ? '' : ' (text-only)'));
        if (S.duoImageToCoder && S.coderImagesOff !== true) _rawTargets.push('Coder' + (_cVision ? '' : ' (text-only)'));
        _flow = '<span style="color:#20b0a0">&#9654; Agentic Direct:</span> '
              + (_rawTargets.length ? ('Raw image to ' + _rawTargets.join(' + ') + '.') : 'No role checked for raw images.')
              + '<br><span style="color:var(--tx3)">Projectors (--mmproj) load on demand per role.</span>';
      }
      flowCard.innerHTML = _flow;
    }
    _setDescribePreview(
      preview, badge, inputEl,
      (_dm === 'preprocess' && S.visionModel) ? S.visionModel.replace(':latest', '').split('/').pop() : ''
    );
    return;
  }

  if (S.mode === 'pipeline') {
    var _pipeDir = (S.pipelineVisionDirect === true);
    var _vm = (S.visionAgentModel || '').replace(':latest','').split('/').pop();
    var _roles = ['analyst','refiner','critic','synthesizer'].filter(function(r) {
      return S.pipelineVisionRoles && S.pipelineVisionRoles[r];
    });
    if (_pipeDir && !_roles.length) _roles = ['analyst'];
    if (flowCard) {
      if (!_pipeDir) {
        flowCard.style.borderColor = 'rgba(224,144,48,.45)';
        flowCard.style.background = 'rgba(224,144,48,.07)';
        _flow = '<span style="color:#e0a050">&#9654; Pipeline Describe first:</span> Vision-Agent (' + (_vm || 'pick model below') + ') turns images into text once.<br>'
              + '<span style="color:var(--tx3)">Pipeline stages stay text-only (no --mmproj loaded).</span>';
      } else {
        flowCard.style.borderColor = 'rgba(136,88,192,.45)';
        flowCard.style.background = 'rgba(136,88,192,.07)';
        var _pipeTargets = _roles.map(function(r) {
          var rm = (S.currentAssignments && S.currentAssignments[r] && S.currentAssignments[r].model) || '';
          return r.charAt(0).toUpperCase() + r.slice(1) + (_isVisionCapableModel(rm) ? '' : ' (text-only)');
        });
        _flow = '<span style="color:#b090d0">&#9654; Pipeline Direct:</span> Raw image to ' + _pipeTargets.join(' + ') + '.'
              + '<br><span style="color:var(--tx3)">Projectors (--mmproj) load on demand per checked multimodal stage'
              + (S.visionAgentEnabled && _vm ? ('; fallback: ' + _vm) : '') + '.</span>';
      }
      flowCard.innerHTML = _flow;
    }
    _setDescribePreview(preview, badge, inputEl, (!_pipeDir && S.visionAgentModel) ? _vm : '');
    return;
  }

  // Chat / Direct & Auto
  var mode = S.imageMode === 'preprocess' ? 'preprocess' : 'direct';
  if (flowCard) {
    var _autoNote = '';
    if (S.mode === 'auto') {
      var _adm = S.duoImageMode || ((S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct');
      if (_adm === 'preprocess') {
        _autoNote = ' Coding handoffs use Agentic Describe first.';
      } else {
        var _at = [];
        if (S.duoImageToPlanner) _at.push('Planner');
        if (S.duoImageToCoder && S.coderImagesOff !== true) _at.push('Coder');
        _autoNote = ' Coding handoffs use Agentic Direct (' + (_at.length ? _at.join('+') : 'no role checked') + ').';
      }
    }
    if (mode === 'preprocess') {
      flowCard.style.borderColor = 'rgba(224,144,48,.45)';
      flowCard.style.background = 'rgba(224,144,48,.07)';
      var _pm = (S.visionModel || '').replace(':latest','').split('/').pop();
      _flow = '<span style="color:#e0a050">&#9654; Describe first:</span> ' + (_pm || 'no Vision Describer selected')
            + ' turns attached images into text.<br>'
            + '<span style="color:var(--tx3)">Direct chat stays text-only (no mmproj on chat slot).' + _autoNote + '</span>';
    } else {
      flowCard.style.borderColor = 'rgba(72,120,192,.45)';
      flowCard.style.background = 'rgba(72,120,192,.07)';
      if (_dVision) {
        _flow = '<span style="color:#80b0e0">&#9654; Direct:</span> ' + _dDisp
              + ' is multimodal &mdash; raw images go straight to it.<br>'
              + '<span style="color:var(--tx3)">Vision projector loads on demand; no separate describer needed.' + _autoNote + '</span>';
      } else if (_anyInstalledVisionModel()) {
        _flow = '<span style="color:#80b0e0">&#9654; Direct (auto-fallback):</span> ' + _dDisp
              + ' is text-only &mdash; image runs switch to an installed multimodal model.<br>'
              + '<span style="color:var(--tx3)">Or choose Describe first above.' + _autoNote + '</span>';
      } else {
        _flow = '<span style="color:#d09090">&#9654; Direct:</span> ' + _dDisp
              + ' is not multimodal and no vision model is installed.<br>'
              + '<span style="color:var(--tx3)">Download a multimodal model or pick a Vision Describer under Describe first.' + _autoNote + '</span>';
      }
    }
    flowCard.innerHTML = _flow;
  }

  _setDescribePreview(
    preview, badge, inputEl,
    (mode === 'preprocess' && S.visionModel) ? S.visionModel.replace(':latest', '').split('/').pop() : ''
  );
}

// -- Vision Config ---------------------------------------------
async function loadVisionConfig() {
  try {
    const d = await (await fetch('/vision/config')).json();
    S.visionEnabled = !!d.enabled;
    S.visionModel   = d.model || '';
    const sel  = document.getElementById('vision-model-sel');
    S.imageMode = (S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct';
    if (!S._duoImageModeExplicit) {
      S.duoImageMode = (S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct';
    }
    _applyImageModeUI(S.imageMode);
    if (sel) {
      sel.innerHTML = '<option value="">-- No vision model --</option>';
      var grpPrepro = document.createElement('optgroup');
      grpPrepro.label = '✓ Recommended Vision Describers';
      var grpVision  = document.createElement('optgroup');
      grpVision.label = '👁 Multimodal / Vision';
      var grpOther   = document.createElement('optgroup');
      grpOther.label = 'Other models';
      S.models.forEach(function(m) {
        const o = document.createElement('option');
        o.value = m;
        const prof = S.modelProfiles[m] || {};
        const inAllowlist = S.visionAllowlist.has(m);
        if (inAllowlist) {
          o.textContent = '✓ ' + m;
          o.style.color = '#4caf82';
          grpPrepro.appendChild(o);
        } else if (prof.vision || _isVisionCapableModel(m)) {
          o.textContent = '👁 ' + m;
          o.style.color = '#8858c0';
          grpVision.appendChild(o);
        } else {
          o.textContent = m;
          grpOther.appendChild(o);
        }
      });
      if (grpPrepro.children.length) sel.appendChild(grpPrepro);
      if (grpVision.children.length)  sel.appendChild(grpVision);
      if (grpOther.children.length)   sel.appendChild(grpOther);
      if (S.visionModel) sel.value = S.visionModel;
      const warn = document.getElementById('vision-prepro-warn');
      if (warn) warn.style.display = (S.visionModel && !S.visionAllowlist.has(S.visionModel)) ? 'block' : 'none';
    }
    updateDuoImageUI();
  } catch(e) {}
}

function setVisionModel(model) {
  S.visionModel = model;
  var enable = !!model && (S.imageMode === 'preprocess');
  if (enable) S.visionEnabled = true;
  const warn = document.getElementById('vision-prepro-warn');
  if (warn) warn.style.display = (model && !S.visionAllowlist.has(model)) ? 'block' : 'none';
  fetch('/vision/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(enable ? {model: model, enabled: true} : {model: model})
  });
  updateDuoImageUI();
}

async function testVisionModel() {
  const model = document.getElementById('vision-model-sel').value;
  const btn   = document.getElementById('vision-test-btn');
  const res   = document.getElementById('vision-test-result');
  if (!model) { if (res) res.textContent = 'No model selected.'; return; }
  if (btn) btn.textContent = '...';
  try {
    const d = await (await fetch('/vision/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model})
    })).json();
    if (res) {
      if (d.available && d.vision_capable) {
        res.textContent = '\u2713 Available + vision-capable';
        res.style.color = '#3a9960';
      } else if (d.available) {
        res.textContent = '\u26A0 Available, but may not be a vision model';
        res.style.color = '#e09030';
      } else {
        res.textContent = '\u2717 Not found in Ollama';
        res.style.color = '#b04040';
      }
    }
  } catch(e) {
    if (res) { res.textContent = 'Error: ' + e.message; res.style.color = '#b04040'; }
  }
  if (btn) btn.textContent = '\u25BA';
  setTimeout(function() { if (res) res.textContent = ''; }, 4000);
}


// ── VRAM / Model Manager Functions ───────────────────────────

var _vramOpen = false;  // only poll once the VRAM panel is explicitly opened

function toggleVramPanel() {
  _vramOpen = !_vramOpen;
  var body  = document.getElementById('vram-body');
  var arrow = document.getElementById('vram-arrow');
  if (body)  body.style.display    = _vramOpen ? '' : 'none';
  if (arrow) arrow.style.transform = _vramOpen ? '' : 'rotate(-90deg)';
  if (_vramOpen) refreshVram();  // refresh immediately when opening
}

function setKeepAlive(ka, el) {
  document.querySelectorAll('.ka-btn').forEach(function(b) { b.classList.remove('on'); });
  if (el) el.classList.add('on');
  // FIX: set both keys — smart preload uses smart_preload_keep_alive,
  // startup preload uses default_keep_alive
  _postSettingsFF({smart_preload_keep_alive: ka, default_keep_alive: ka}, function() {
    document.querySelectorAll('.ka-btn').forEach(function(b) { b.classList.remove('on'); });
    if (el) el.classList.remove('on');
  });
}

// ── Model color palette (persistent across refreshes) ───────────────────────
var _MODEL_COLORS = ['#4878c0','#3a9960','#e09030','#8858c0','#b04040','#20a8a0','#c0784a','#7a8fa8'];
var _modelColorMap = {};
var _colorIdx = 0;
function _getModelColor(name) {
  var key = name.replace(':latest','').split(':')[0];  // base key without tag
  if (!_modelColorMap[key]) { _modelColorMap[key] = _MODEL_COLORS[_colorIdx++ % _MODEL_COLORS.length]; }
  return _modelColorMap[key];
}

// ETag for /vram/status — prevents log spam: identical responses → 304 no content
var _vramEtag = null;
// DECL-FIX: missing var declarations — without these, strict mode throws a ReferenceError
// because _vramPending was only set via assignment (not var).
var _vramPending = false;
var _vramRefreshInterval = 10000;  // default 10s, possibly overridden per settings
// adaptive polling: slow (10s) when idle, fast (2s) when pipeline/duo is active
// _vramFastMode is set by pipeline_start / agent / duo_start events
var _vramFastMode = false;
var _vramFastUntil = 0;  // timestamp until which fast polling runs

function _setVramFast(ms) {
  _vramFastMode = true;
  _vramFastUntil = Date.now() + (ms || 30000);
}

function setVramRefreshInterval(ms) {
  _vramRefreshInterval = Math.max(2000, parseInt(ms) || 8000);
}

var _vramTimerId = null;
var _vramVisWired = false;

async function refreshVram(fromTimer) {
  if (_vramPending) return;  // PERF: no stacking while a fetch is running
  if (!_vramVisWired && typeof document !== 'undefined' && document.addEventListener) {
    _vramVisWired = true;
    document.addEventListener('visibilitychange', function() {
      if (!document.hidden && _vramOpen && !_vramPending) refreshVram();
    });
  }
  if (fromTimer && typeof document !== 'undefined' && document.hidden) {
    if (_vramOpen) {
      if (_vramTimerId) clearTimeout(_vramTimerId);
      _vramTimerId = setTimeout(function() { refreshVram(true); }, 10000);
    }
    return;
  }
  _vramPending = true;
  if (Date.now() > _vramFastUntil) _vramFastMode = false;

  var listEl   = document.getElementById('vram-model-list');
  var barEl    = document.getElementById('vram-bar');
  var badge    = document.getElementById('vram-count-badge');
  var mmBar    = document.getElementById('mm-vram-bar');
  var mmLabel  = document.getElementById('mm-vram-label');
  var mmList   = document.getElementById('mm-loaded-list');
  var budgetEl = document.getElementById('vram-budget-display');
  var segBar   = document.getElementById('vram-seg-bar');

  try {
    var headers = {};
    if (_vramEtag) headers['If-None-Match'] = _vramEtag;
    var resp = await fetch('/vram/status', {headers: headers});

    // 304 Not Modified: nothing changed → skip the UI update, no log spam
    if (resp.status === 304) return;

    var data  = await resp.json();
    // remember the new ETag
    _vramEtag = resp.headers.get('ETag') || null;

    var TOTAL = data.budget_gb || 8.0;
    _vramBudgetGb = TOTAL;

    var models = (data.models || []).map(function(m) {
      return {
        name:        m.name,
        vram_gb:     m.vram_gb || 0,
        total_gb:    m.total_gb || 0,
        expires_at:  m.expires_at || '',
        from_lookup: m.from_lookup || false,
      };
    });

    var used = models.reduce(function(s,m){ return s + (m.vram_gb||0); }, 0);
    var free = data.free_gb != null ? data.free_gb : TOTAL - used;
    var pct  = TOTAL > 0 ? Math.min(100, used/TOTAL*100) : 0;
    var barColor = pct > 90 ? '#c04040' : pct > 75 ? '#e09030' : '#4878c0';

    // Legacy bars
    if (barEl)   { barEl.style.width = Math.round(pct)+'%'; barEl.style.background = barColor; }
    if (mmBar)   { mmBar.style.width = Math.round(pct)+'%'; mmBar.style.background = barColor; }
    if (mmLabel) mmLabel.textContent = used.toFixed(1)+' / '+TOTAL.toFixed(1)+' GB';
    if (budgetEl) budgetEl.textContent = TOTAL.toFixed(1)+' GB Budget';

    // ── Segmentierte Bar ─────────────────────────────────────────────────────
    if (segBar) {
      segBar.style.display = 'flex';
      if (!models.length) {
        segBar.innerHTML = '<div style="height:100%;width:100%;background:#1e2435;border-radius:4px"></div>';
      } else {
        var segs = models.map(function(m) {
          var col    = _getModelColor(m.name);
          var segPct = TOTAL > 0 ? Math.min(100, m.vram_gb/TOTAL*100).toFixed(2) : 0;
          var short  = m.name.replace(':latest','');
          return '<div title="'+esc(short)+': ~'+m.vram_gb.toFixed(1)+' GB (inkl. KV-Cache)" style="'
            +'height:100%;width:'+segPct+'%;background:'+col+';min-width:3px;'
            +'border-right:1px solid rgba(0,0,0,.4);box-sizing:border-box;'
            +'transition:width .4s;flex-shrink:0"></div>';
        }).join('');
        segs += '<div style="height:100%;flex:1;min-width:1px;background:#1a2030;border-radius:0 4px 4px 0"></div>';
        segBar.innerHTML = segs;
      }
    }

    var cnt = models.length;
    if (badge) {
      badge.textContent = used.toFixed(1)+' / '+TOTAL.toFixed(1)+' GB'+(cnt ? ' ('+cnt+' model'+(cnt>1?'s':'')+')' : '');
      badge.style.color = pct > 90 ? '#c04040' : pct > 75 ? '#e09030' : '#3a9960';
      var tipLines = ['GPU VRAM (real values incl. KV cache):'];
      models.forEach(function(m){
        var src = m.from_lookup ? '(lookup table)' : '(ollama raw)';
        tipLines.push('  '+m.name.replace(':latest','')+': ~'+m.vram_gb.toFixed(1)+' GB '+src);
      });
      tipLines.push('  free: ~'+free.toFixed(1)+' GB');
      if (data.solo_mode) {
        tipLines.push('');
        tipLines.push('⚠ SOLO mode: model too large for judge combo.');
        tipLines.push('  Judge ('+data.judge_gb+'GB) + model > budget → sequential.');
      }
      badge.title = tipLines.join('\n');
    }

    var budgetInp = document.getElementById('vram-budget-inp');
    if (budgetInp && document.activeElement !== budgetInp) budgetInp.value = TOTAL;

    if (mmList) mmList.innerHTML = '';

    var loadedAgents = data.loaded_agents || {};
    var modelAgentMap = {};
    Object.keys(loadedAgents).forEach(function(mf) {
      modelAgentMap[mf.replace(':latest','')] = loadedAgents[mf] || [];
    });
    var agentColors = {judge:'#7a8fa8',analyst:'#4878c0',refiner:'#3a9960',critic:'#b04040',synthesizer:'#e09030',direct:'#8858c0'};
    var agentShort  = {judge:'J',analyst:'A',refiner:'R',critic:'C',synthesizer:'S',direct:'D'};

    function fmtTtl(exp_str) {
      if (!exp_str) return '';
      var exp = new Date(exp_str), diff = exp - Date.now();
      if (isNaN(diff)) return '';
      if (diff > 365*24*3600*1000) return '\u221e';
      if (diff < 0) return 'exp.';
      var m = Math.ceil(diff/60000);
      return m > 60 ? Math.floor(m/60)+'h '+(m%60)+'m' : m+'m';
    }

    var rows = '';
    if (!models.length) {
      rows = '<div class="vram-empty">Nothing in VRAM</div>';
    } else {
      models.forEach(function(m){
        var modelColor = _getModelColor(m.name);
        var short      = m.name.replace(':latest','');
        var isVis      = /vl|llava|vision|moondream/i.test(m.name);
        var isThink    = /think|thinking|qwq|deepseek-r|qwen3\.5|qwen3:/i.test(m.name);
        var agents     = modelAgentMap[short] || modelAgentMap[m.name] || [];
        var badgeHtml = '';
        if (isVis)   badgeHtml += '<span class="vram-model-badge vis">VIS</span>';
        if (isThink) badgeHtml += '<span class="vram-model-badge think">THINK</span>';
        badgeHtml += agents.map(function(ak){
          var c = agentColors[ak] || '#7a8fa8';
          var l = agentShort[ak] || ak[0].toUpperCase();
          return '<span title="'+ak+'" class="vram-model-badge" style="background:'+c+'22;color:'+c+';border-color:'+c+'55">'+l+'</span>';
        }).join('');
        var ttl    = fmtTtl(m.expires_at);
        var ttlCol = ttl==='\u221e' ? '#3a9960' : (parseInt(ttl)<5 && ttl.includes('m')) ? '#b04040' : '#556677';
        var segW   = TOTAL > 0 ? Math.max(2, m.vram_gb/TOTAL*100).toFixed(1) : '0';
        var approx = m.from_lookup ? '~' : '';
        var safeTitle = esc(m.name);

        rows += '<div class="vram-model-card">'
          +'<div class="vram-model-top">'
          +'<div class="vram-model-main">'
          +'<span class="vram-model-swatch" style="background:'+modelColor+'"></span>'
          +'<span class="vram-model-name" title="'+safeTitle+'">'+esc(short)+'</span>'
          +(badgeHtml ? '<span class="vram-model-badges">'+badgeHtml+'</span>' : '')
          +'</div>'
          +'<div class="vram-model-meta">'
          +(ttl ? '<span class="vram-model-ttl" style="color:'+ttlCol+'">'+ttl+'</span>' : '')
          +'<span class="vram-model-size">'+approx+m.vram_gb.toFixed(1)+' GB</span>'
          +'<button type="button" class="vram-model-unload" data-model="'+safeTitle+'" title="Unload">&#215;</button>'
          +'</div>'
          +'</div>'
          +'<div class="vram-model-meter"><div class="vram-model-meter-fill" style="width:'+segW+'%;background:'+modelColor+'"></div></div>'
          +'</div>';

        if (mmList) mmList.innerHTML += '<div style="display:flex;align-items:center;gap:5px;padding:2px 0;font-family:IBM Plex Mono,monospace;font-size:11px">'
          +'<div style="width:6px;height:6px;border-radius:1px;background:'+modelColor+';flex-shrink:0"></div>'
          +'<span style="flex:1;color:#ccd;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(short)+'</span>'
          +'<span style="color:#aab">'+m.vram_gb.toFixed(1)+' GB</span></div>';
      });

      // ── Status line: solo-mode explanation or combo info ────────────────
      if (data.solo_mode) {
        var soloModel = models[0].name.replace(':latest','');
        rows += '<div style="margin-top:2px;padding:5px 8px;background:rgba(176,64,64,.08);'
          +'border:1px solid rgba(176,64,64,.25);border-radius:3px;font-family:IBM Plex Mono,monospace;font-size:9px;line-height:1.6">'
          +'<span style="color:#b04040;font-weight:600">⚠ SOLO-MODE</span> '
          +'<span style="color:#7a8fa8">'+esc(soloModel)+' ('+models[0].vram_gb.toFixed(1)+'GB) + '
          +'Judge ('+esc(data.judge_gb)+'GB) = '+(models[0].vram_gb+data.judge_gb).toFixed(1)+'GB &gt; '
          +TOTAL.toFixed(0)+'GB Budget.</span><br>'
          +'<span style="color:#556677">Judge is reloaded sequentially (~8s after Analyst).</span>'
          +'</div>';
      } else if (models.length >= 2) {
        var komboColor = used > TOTAL*0.92 ? '#b04040' : used > TOTAL*0.80 ? '#e09030' : '#3a9960';
        var komboIcon  = used > TOTAL*0.92 ? '✗' : used > TOTAL*0.80 ? '▲' : '✓';
        rows += '<div style="margin-top:2px;padding:4px 8px;background:rgba(0,0,0,.15);'
          +'border:1px solid #1e2435;border-radius:3px;font-family:IBM Plex Mono,monospace;font-size:9px;'
          +'display:flex;align-items:center;gap:6px">'
          +'<span style="color:'+komboColor+';font-size:12px">'+komboIcon+'</span>'
          +'<span style="color:#7a8fa8;flex:1">'+models.map(function(m){return esc(m.name.replace(':latest',''));}).join(' + ')+'</span>'
          +'<span style="color:'+komboColor+'">'+used.toFixed(1)+' / '+TOTAL.toFixed(1)+' GB</span>'
          +'</div>';
      }
    }
    // FIX: OLLAMA_MAX_LOADED_MODELS warning — Windows Radeon default=1 → 2 models never simultaneous
    if (data.warn_max_loaded) {
      rows += '<div id="warn-max-loaded" style="margin-top:6px;padding:6px 8px;'
        +'background:rgba(224,144,48,.10);border:1px solid rgba(224,144,48,.35);'
        +'border-radius:3px;font-family:IBM Plex Mono,monospace;font-size:9px;line-height:1.7">'
        +'<span style="color:#e09030;font-weight:600">⚠ Only 1 model can be loaded at a time</span><br>'
        +'<span style="color:#7a8fa8">Windows + Radeon: <code style="color:#ccd">OLLAMA_MAX_LOADED_MODELS</code> is not set.<br>'
        +'Control Panel → Environment Variables → New:<br>'
        +'<code style="color:#3a9960">OLLAMA_MAX_LOADED_MODELS = 3</code><br>'
        +'Then restart Ollama (tray → Quit).</span>'
        +'</div>';
    }
    if (listEl) {
      listEl.innerHTML = rows;
      if (!listEl._unloadDelegated) {
        listEl._unloadDelegated = true;
        listEl.addEventListener('click', function(ev) {
          var btn = ev.target && ev.target.closest ? ev.target.closest('.vram-model-unload') : null;
          if (!btn || !listEl.contains(btn)) return;
          var mName = btn.dataset.model || '';
          if (mName) unloadModel(mName);
        });
      }
    }

    // force kill button: show when the VRAM bar is >10% but no models are listed
    // (= suspected orphan process — e.g. after a server restart), OR whenever a
    // llama-server process runs without a matching loaded slot (zombie with
    // mmap RAM only, invisible to the VRAM list).
    var fkBtn = document.getElementById('force-kill-btn');
    if (fkBtn) {
      var _procs = (data.llama_procs != null) ? data.llama_procs : -1;
      var hasOrphan = (data.pct > 10 && !models.length) || (_procs > models.length);
      fkBtn.style.display = hasOrphan ? '' : 'none';
      fkBtn.title = _procs >= 0 ? (data.llama_procs + ' llama-server process(es), ' + models.length + ' loaded slot(s)') : fkBtn.title;
    }

  } catch(e) {
    if (badge) badge.textContent = '?? GB';
    console.warn('refreshVram:', e);
    // VRAM-LOAD-FIX: replace the "Loading..." placeholder when the fetch fails
    if (listEl && listEl.innerHTML.includes('Lade')) {
      listEl.innerHTML = '<div class="vram-empty" style="color:#c05050">⚠ Backend unreachable</div>';
    }
  } finally {
    _vramPending = false;
    if (_vramTimerId) { clearTimeout(_vramTimerId); _vramTimerId = null; }
    if (_vramOpen) {
      var _ms = _vramFastMode ? Math.min(_vramRefreshInterval, 2000) : _vramRefreshInterval;
      _vramTimerId = setTimeout(function() { refreshVram(true); }, _ms);
    }
  }
}
async function _vramEstimate(model, ctx, elId, compact) {
  if (!model || !ctx) return;
  var el = document.getElementById(elId);
  if (!el) return;
  el.textContent = '';
  el.classList.remove('ctx-est-over');
  el.classList.remove('ctx-est-compact');
  try {
    var r = await fetch('/vram/estimate?model=' + encodeURIComponent(model) + '&ctx=' + ctx);
    if (!r.ok) return;
    var d = await r.json();
    if (d.error) return;
    var kv = d.vram_kv_gb > 0.005 ? d.vram_kv_gb : 0;   // 0.00 GB = no KV row for this model
    // CTX-FIT-WARN (2026-10-23): non-blocking on purpose — a 35B @ 100k is a
    // deliberate choice some setups can make; the estimate only says what the
    // budget thinks. The run-start preflight still decides (and is toggleable).
    var budget = (_vramBudgetGb > 0 ? _vramBudgetGb : 8.0) - 0.4;
    var over = d.vram_gb > budget;
    var title = d.vram_gb.toFixed(1) + ' GB total (KV ' + d.vram_kv_gb.toFixed(2) + ' GB)'
      + (d.ram_gb > 0 ? ' + ' + d.ram_gb.toFixed(1) + ' GB CPU-expert RAM' : '')
      + ' vs ' + budget.toFixed(1) + ' GB budget'
      + (over ? ' \u2014 warning only, nothing is blocked' : '');
    var txt;
    if (compact) {
      // COMPACT: the per-agent value slot is one
      // small line — the full estimate wrapped it into three. RAM and budget
      // detail live in the hover title.
      el.classList.add('ctx-est-compact');
      txt = d.vram_gb.toFixed(1) + ' GB'
        + (kv ? ' \u00b7 KV ' + kv.toFixed(1) : '')
        + (over ? ' \u26a0 >' + budget.toFixed(1) : '');
    } else {
      txt = '\u2248 ' + d.vram_gb.toFixed(1) + ' GB'
        + (kv ? ' (KV: ' + kv.toFixed(2) + ' GB)' : '')
        + (d.ram_gb > 0 ? ' + ' + d.ram_gb.toFixed(1) + ' GB RAM' : '')
        + (over ? ' \u26a0 over ' + budget.toFixed(1) + ' GB budget' : '');
    }
    if (over) el.classList.add('ctx-est-over');
    el.textContent = txt;
    el.title = title;
  } catch(e) {}
}
// CTX-FIT-WARN (2026-10-23): populate every duo ctx estimate once on load and
// on coder/planner model change — before, the estimate only appeared after the
// user edited the field by hand.
function refreshCtxEstimates() {
  _vramEstimate(S.duoCoderModel, S.duoCtxAgentic, 'vram-est-agentic');
  _vramEstimate(S.duoPlannerModel, S.duoCtxPlanner, 'vram-est-planner');
  _vramEstimate(S.duoCoderModel, S.duoCtxNormal, 'vram-est-normal');
  var _crit = (S.agentsCfg && S.agentsCfg.duo_critic && S.agentsCfg.duo_critic.model) || S.duoCoderModel;
  _vramEstimate(_crit, S.duoCtxCritic, 'vram-est-critic');
}
async function unloadModel(name) {
  try {
    await fetch('/vram/unload', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({model: name})});
    setTimeout(function() { if (!_vramPending && _vramOpen) refreshVram(); }, 500);
  } catch(e) { console.warn('unloadModel:', e); }
}

async function unloadAllModels() {
  // /vram/status also shows orphan processes (after reload)
  try {
    var data   = await fetch('/vram/status').then(function(r) { return r.json(); });
    var models = (data.models||[]).map(function(m) { return m.name; });
    if (!models.length) {
      // no models detected but llama processes exist → offer force kill
      var fk = document.getElementById('force-kill-btn');
      if (fk) fk.style.display = '';
      showStatus('No models detected. Use \u26a0 Force Kill if VRAM is full.');
      return;
    }
    for (var i=0; i<models.length; i++) {
      await fetch('/vram/unload', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model: models[i]})});
    }
    setTimeout(function() { if (!_vramPending && _vramOpen) refreshVram(); }, 800);
  } catch(e) { console.warn('unloadAllModels:', e); }
}

async function forceKillAll(btn) {
  if (!confirm('Hard-kill all llama-server processes?\n\nThis removes all loaded models from VRAM immediately.\nThe next request restarts the server (~30s).')) return;
  if (btn) { btn.disabled=true; btn.textContent='Killing...'; }
  try {
    var r = await fetch('/vram/kill_all', {method:'POST'}).then(function(r) { return r.json(); });
    if (r.ok) {
      showStatus('Force Kill OK \u2014 VRAM cleared. The next request restarts the server.');
      var fk = document.getElementById('force-kill-btn');
      if (fk) fk.style.display = 'none';
    } else {
      showStatus('Force Kill error: ' + (r.error||'?'));
    }
    setTimeout(function() { if (!_vramPending && _vramOpen) refreshVram(); }, 1200);
  } catch(e) {
    showStatus('Force kill failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled=false; btn.innerHTML='\u26a0 Force Kill'; }
  }
}

async function preloadPipelineModels() {
  var btn = (typeof event !== 'undefined' && event) ? event.target : null;
  if (btn) { btn.disabled=true; btn.textContent='Loading...'; }
  try {
    await fetch('/vram/preload_pipeline', {method:'POST'});
    setTimeout(function() {
      refreshVram();
      if (btn) { btn.disabled=false; btn.innerHTML='&#9654; Load pipeline'; }
    }, 2500);
  } catch(e) {
    if (btn) { btn.disabled=false; btn.innerHTML='&#9654; Load pipeline'; }
  }
}

// ── Available Models List ─────────────────────────────────────────────────────
var _availModels = [];        // {name, vram_gb}
var _loadedModelNames = new Set();
var _vramTableCache = null;   // einmalig von /vram/table gecacht
var _availRefreshTimer = null; // Debounce-Timer
var _vramBudgetGb = 8.0;

async function _getVramTable() {
  if (_vramTableCache) return _vramTableCache;
  try {
    var d = await fetch('/vram/table').then(function(r){return r.json();});
    _vramTableCache = d.vram_gb || {};
  } catch(e) {
    _vramTableCache = {};
  }
  return _vramTableCache;
}

function _vramOf(name, table) {
  if (!table) return null;
  if (table[name] !== undefined) return table[name];
  var base = name.split(':')[0];
  for (var k in table) {
    if (k.split(':')[0] === base) return table[k];
  }
  return null;
}

async function refreshAvailableModels() {
  // Debounce: max 1 Aufruf pro 300ms
  if (_availRefreshTimer) return;
  _availRefreshTimer = setTimeout(function(){ _availRefreshTimer = null; }, 300);

  var list = document.getElementById('available-models-list');
  if (!list) return;
  try {
    // all three in parallel: models, VRAM status, VRAM table (cached from the 2nd call)
    var [modelsResp, vramResp, table] = await Promise.all([
      fetch('/models').then(function(r){return r.json();}),
      fetch('/vram/status').then(function(r){return r.json();}).catch(function(){return {models:[]};}).catch(function(){return {models:[]};})
    ].concat([_getVramTable()]));
    _availModels = (modelsResp.models || []).map(function(m) {
      var n = typeof m === 'string' ? m : m.name || m;
      return {name: n, vram_gb: _vramOf(n, table)};
    });
    _loadedModelNames = new Set((vramResp.models || []).map(function(m){return m.name;}));
    _renderAvailableModels(_availModels, document.getElementById('model-filter-input') ? document.getElementById('model-filter-input').value : '');
  } catch(e) {
    list.innerHTML = '<div class="vram-empty">Error loading</div>';
  }
}

function _modelMatchesFilter(name, filter) {
  // MULTI-TOKEN (2026-10-28, live): one includes() matched the WHOLE query,
  // so "qwen mtp" showed "no match" although qwen3.5:4b-mtp exists - no
  // model name contains a space. Every whitespace-separated token must hit.
  var tokens = (filter || '').toLowerCase().trim().split(/\s+/).filter(Boolean);
  var n = (name || '').toLowerCase();
  return tokens.every(function(t){ return n.includes(t); });
}

function _renderAvailableModels(models, filter) {
  var list = document.getElementById('available-models-list');
  if (!list) return;
  var f = (filter || '').trim();
  var filtered = f ? models.filter(function(m){return _modelMatchesFilter(m.name, f);}) : models;
  if (!filtered.length) {
    list.innerHTML = '<div class="vram-empty">' + (f ? 'no match' : 'No models found') + '</div>';
    return;
  }

  var budget = _vramBudgetGb > 0 ? _vramBudgetGb : 8.0;
  var rows = filtered.map(function(m) {
    var isLoaded = _loadedModelNames.has(m.name);
    var gb = m.vram_gb ? m.vram_gb.toFixed(1) + ' GB' : '—';
    // dot: green=loaded, empty=available; bar color per VRAM size
    var dotColor = isLoaded ? '#3a9960' : '#2a3548';
    var btnClass = isLoaded ? 'avail-load-btn loaded' : 'avail-load-btn';
    var btnLabel = isLoaded ? '&#215; Unload' : '&#9654; Load';
    var btnTitle = isLoaded ? 'Unload from VRAM' : 'Load into VRAM';
    var safeTitle = esc(m.name);
    var pct = m.vram_gb && budget > 0 ? Math.min(100, (m.vram_gb / budget) * 100) : 0;
    var barColor = m.vram_gb >= budget * 0.9 ? '#b04040' : (m.vram_gb >= budget * 0.75 ? '#e09030' : '#4878c0');
    var meterClass = m.vram_gb ? 'avail-model-meter' : 'avail-model-meter unknown';
    return '<div class="avail-model-row' + (isLoaded ? ' loaded' : '') + '">'
      + '<div class="avail-model-main">'
      + '<div class="avail-model-head">'
      + '<div class="avail-vram-dot" style="background:' + dotColor + '" title="' + (isLoaded ? 'In VRAM' : 'Not loaded') + '"></div>'
      + '<span class="avail-model-name" title="' + safeTitle + '">' + esc(m.name) + '</span>'
      + '</div>'
      + '<div class="' + meterClass + '"><div class="avail-model-meter-fill" style="width:' + pct.toFixed(1) + '%;background:' + barColor + '"></div></div>'
      + '</div>'
      + '<div class="avail-model-actions">'
      + '<span class="avail-model-gb">' + gb + '</span>'
      + '<button type="button" class="' + btnClass + '" title="' + btnTitle + '" data-model="' + safeTitle + '">' + btnLabel + '</button>'
      + '</div>'
      + '</div>';
  }).join('');
  list.innerHTML = rows;
  if (!list._availDelegated) {
    list._availDelegated = true;
    list.addEventListener('click', function(ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest('.avail-load-btn') : null;
      if (!btn || !list.contains(btn)) return;
      var mName = btn.dataset.model || '';
      if (mName) toggleAvailModel(mName, btn);
    });
  }
}

function filterAvailableModels(val) {
  _renderAvailableModels(_availModels, val);
}

async function toggleAvailModel(name, btn) {
  if (!btn || btn.disabled || btn.classList.contains('loading')) return;
  var isLoaded = btn.classList.contains('loaded');
  btn.disabled = true;
  btn.classList.add('loading');
  btn.textContent = isLoaded ? 'Unloading...' : 'Loading...';
  try {
    if (isLoaded) {
      var r = await fetch('/vram/unload', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model: name})});
      if (!r.ok) throw new Error('Unload failed: ' + r.status);
      showStatus(name.split(':')[0] + ' unloaded');
      await Promise.all([refreshVram(), refreshAvailableModels()]);
      btn.disabled = false;
      btn.classList.remove('loading');
    } else {
      var ka = document.getElementById('model-keepalive-sel') ? document.getElementById('model-keepalive-sel').value : '10m';
      // start the load request (returns immediately, llama-server loads in the background)
      var r = await fetch('/vram/load', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model: name, keep_alive: ka})});
      if (!r.ok) throw new Error('Load failed: ' + r.status);
      // polling: wait up to 120s until the model really appears in VRAM
      var loaded = false;
      var shortName = name.split(':')[0];
      for (var i = 0; i < 40; i++) {
        await new Promise(function(res){setTimeout(res, 3000);});
        try {
          var ps = await fetch('/vram/status').then(function(r){return r.json();});
          var found = (ps.models || []).some(function(m){return m.name === name;});
          if (found) { loaded = true; break; }
          // show progress
          btn.textContent = 'Loading... ' + Math.round((i+1)*3) + 's';
        } catch(pe) { /* ignore */ }
      }
      if (loaded) {
        showStatus(shortName + ' loaded (' + (ka === '-1' ? '∞' : ka) + ')');
      } else {
        // timeout — fetch the log file and show the error
        var logMsg = shortName + ' Timeout (120s)';
        try {
          var logResp = await fetch('/vram/log?model=' + encodeURIComponent(name));
          if (logResp.ok) {
            var logData = await logResp.json();
            if (logData.error) logMsg += ': ' + logData.error.slice(0, 120);
          }
        } catch(le) {}
        showStatus(logMsg);
      }
      await Promise.all([refreshVram(), refreshAvailableModels()]);
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  } catch(e) {
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.classList.toggle('loaded', isLoaded);
    btn.innerHTML = isLoaded ? '&#215; Unload' : '&#9654; Load';
    var msg = e && e.message ? e.message : String(e);
    showStatus((isLoaded ? 'Unload' : 'Load') + ' failed: ' + msg.slice(0, 80));
    console.warn('toggleAvailModel:', e);
  }
}



async function refreshModelsAutomap() {
  var panel = document.getElementById('models-automap-panel');
  if (!panel) return;
  try {
    var d = await fetch('/automap/current').then(function(r) { return r.json(); });
    var assignments = d.assignments || {};
    var order = ['judge','analyst','refiner','critic','synthesizer','direct'];
    var colors = {judge:'#7a8fa8',analyst:'#4878c0',refiner:'#3a9960',critic:'#b04040',synthesizer:'#e09030',direct:'#8858c0'};
    var html = '';
    order.forEach(function(a) {
      var info = assignments[a];
      if (!info) return;
      var model = (info.display || info.model || '').replace(':latest','');
      var tags = [];
      if (info.vision)   tags.push('<span style="font-size:8px;padding:1px 4px;border-radius:2px;background:rgba(136,88,192,.2);color:#8858c0;border:1px solid rgba(136,88,192,.3)">VIS</span>');
      if (info.thinking) tags.push('<span style="font-size:8px;padding:1px 4px;border-radius:2px;background:rgba(72,120,192,.2);color:#4878c0;border:1px solid rgba(72,120,192,.3)">THINK</span>');
      html += '<div style="display:flex;align-items:center;gap:5px;padding:4px 0;border-bottom:1px solid #1e2435;font-family:IBM Plex Mono,monospace;font-size:10px">'
            + '<span style="color:'+colors[a]+';min-width:74px;flex-shrink:0">'+a+'</span>'
            + '<span style="color:#c8d4e0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(info.model||'')+'">'+esc(model)+'</span>'
            + tags.join(' ')
            + '</div>';
    });
    panel.innerHTML = html || '<div class="vram-empty">No assignments</div>';
  } catch(e) {
    if (panel) panel.innerHTML = '<div class="vram-empty" style="color:var(--red)">Error</div>';
  }
}

async function saveVramBudget() {
  var inp = document.getElementById('vram-budget-inp');
  if (!inp) return;
  var gb = parseFloat(inp.value) || 6.0;
  // set _vramBudgetGb immediately so the bar calculation does not wait for the old value
  _vramBudgetGb = gb;
  try {
    await fetch('/vram/budget', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({gb: gb})
    });
    showInfo('VRAM budget: ' + gb + ' GB saved');
    // reset the ETag: a budget change would otherwise return 304 (ETag bug: only models+used_gb in the hash)
    _vramEtag = null;
    refreshVram();
  } catch(e) {
    // fallback: via settings
    await fetch('/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({vram_budget_gb: gb})
    });
    showInfo('VRAM budget: ' + gb + ' GB saved');
    _vramEtag = null;
    refreshVram();
  }
}

