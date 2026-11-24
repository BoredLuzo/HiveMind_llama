// agent_cards_presets.js — agent role cards, presets CRUD, prompt editor & session memory UI

// -- Agent Cards ------------------------------------------------
function renderAgentCards(agents) {
  const c = document.getElementById('agent-cards');
  if (!c) { console.error('[renderAgentCards] #agent-cards not found!'); return; }
  c.innerHTML = '';
  const entries = Object.entries(agents || {});
  console.log('[renderAgentCards] entries:', entries.length, 'keys:', entries.map(function(e){return e[0];}));
  if (!entries.length) {
    c.innerHTML = '<div class="empty">No agents loaded</div>';
    return;
  }
  for (let i = 0; i < entries.length; i++) {
    try {
    const key = entries[i][0];
    const cfg = entries[i][1];
    const m = AGENT_META[key];
    if (!m) continue;
    const color = m.color;
    const hasTempOverride = (cfg.temperature !== null && cfg.temperature !== undefined && cfg.temperature !== '');
    const temp = hasTempOverride ? parseFloat(cfg.temperature) : 0.6;
    const toks = parseInt(cfg.max_tokens || 400);

    const div = document.createElement('div');
    div.className = 'acard';
    // PRESET-AGENT-OVERLAY (2026-10-18): the role id on the DOM node lets the
    // preset save read the on-screen sliders per agent.
    div.setAttribute('data-agent', key);

    // Header
    const hdr = document.createElement('div');
    hdr.className = 'acard-hdr';
    hdr.innerHTML =
      '<div style="width:7px;height:7px;border-radius:50%;flex-shrink:0;background:' + color + '"></div>' +
      '<div class="acard-name" style="color:' + color + '">' + m.label + '</div>' +
      '<div class="acard-role">' + m.role + '</div>';
    div.appendChild(hdr);

    // Model dropdown
    const modLbl = document.createElement('div');
    modLbl.className = 'fl';
    div.appendChild(modLbl);
    const sel = document.createElement('select');
    sel.setAttribute('data-agent-model', key);
    sel.dataset.agent = key;

    // registryModel = what is really running right now (from /automap/current)
    // savedModel    = what is stored in settings.json
    const registryModel = (S.currentAssignments[key] || {}).model || '';
    const savedModel    = cfg.model || '';
    const activeModel   = registryModel || savedModel;

    // BUG-FIX: _capBadges MUST come after the activeModel declaration.
    // const has a temporal dead zone — access before declaration → ReferenceError.
    // Previously the IIFE stood 20 lines too early → agent cards did not load at all.
    const _capBadges = (function() {
      const prof = S.modelProfiles[activeModel] || S.modelProfiles[(activeModel||'').split(':')[0]] || {};
      var b = '';
      // VISION-FIX: badge only for verified vision capability (MODEL_PROFILES / /models),
      // plus name heuristic as fallback. Tooltip explains the meaning.
      if (prof.vision || ['vl','llava','vision','moondream','minicpm','qwen3.5','qwen3.6','hermes3.6','hermes','gemma-4'].some(function(v){return (activeModel||'').toLowerCase().includes(v);}))
        b += '<span title="Multimodal — processes images directly" style="font-size:8px;padding:1px 4px;border-radius:2px;background:rgba(136,88,192,.15);color:#8858c0;border:1px solid rgba(136,88,192,.3);margin-left:4px">VISION</span>';
      if (prof.thinking || /think|qwq|deepseek-r|qwen3\.5|qwen3:/i.test(activeModel||''))
        b += '<span style="font-size:8px;padding:1px 4px;border-radius:2px;background:rgba(72,120,192,.15);color:#4878c0;border:1px solid rgba(72,120,192,.3);margin-left:4px">THINK</span>';
      if (_isNoTc(activeModel))
        b += '<span style="font-size:8px;padding:1px 4px;border-radius:2px;background:rgba(176,64,64,.15);color:#c05050;border:1px solid rgba(176,64,64,.3);margin-left:4px">NO TC</span>';
      return b;
    })();
    modLbl.innerHTML = 'Model' + _capBadges;
    sel.dataset.cur     = activeModel;

    // Automap suggestion (registryModel) at the top in blue, only if present
    let selOpts = '';
    if (key === 'duo_critic') {
      selOpts += '<option value=""' + (!activeModel ? ' selected' : '') + '>Follow Duo-Coder</option>';
    }
    if (registryModel) {
      selOpts += '<option value="' + esc(registryModel) + '" style="color:#4878c0;font-weight:600"'
               + ' selected>⟳ ' + esc(registryModel) + ' (active)</option>';
      if (S.models.filter(m => m !== registryModel).length)
        selOpts += '<option disabled>────────────</option>';
    }
    // All available models (registryModel not duplicated)
    S.models.forEach(function(m) {
      if (m === registryModel) return;
      selOpts += '<option value="' + esc(m) + '"'
               + (m === savedModel && !registryModel ? ' selected' : '')
               + '>' + esc(m) + '</option>';
    });
    // Fallback: saved model as option when S.models is empty
    if (!selOpts && savedModel)
      selOpts = '<option value="' + esc(savedModel) + '" selected>' + esc(savedModel) + '</option>';
    sel.innerHTML = selOpts;
    sel.value = activeModel;
    // AGENT-DROPDOWN-SAVE (2026-09-01): save the model change immediately,
    // without an apply click — only the model is sent (sliders/toggles stay on Apply).
    sel.addEventListener('change', function() {
      saveAgentModelDirect(key, this);
    });
    div.appendChild(sel);

    // AGENT-EXPLAIN (2026-08-19): short model-dependent explanation + vision hint.
    // Shows what the model can do (multimodal/thinking) and whether it works
    // directly on images or a vision agent/preprocessing is needed.
    try {
      const expl = document.createElement('div');
      expl.dataset.agentExplain = key;
      expl.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:8.5px;color:var(--tx3);margin:2px 0 4px;line-height:1.6';
      expl.innerHTML = _agentExplainHtml(activeModel);
      div.appendChild(expl);
    } catch(e) {}

    // AGENT-CARD-BUDGET (2026-08-27): temperature | output budget (row 1),
    // context | think budget (row 2) in a 2-column grid. Output budget and
    // context are linked: effective budget = max_tokens + thinking_budget,
    // slider cap = effective context, warning on overflow.
    const effCtxDefault = parseInt((S.ctxDefaults || {})[key] || 0) || 8192;
    const ctxOverride   = parseInt((S.ctxOverrides || {})[key] || 0);
    const effCtx        = ctxOverride > 0 ? ctxOverride : effCtxDefault;

    const grid = document.createElement('div');
    grid.className = 'acard-grid';

    // ── Cell: temperature ──
    const tvId = 'tv-' + key;
    const tempCell = document.createElement('div');
    tempCell.className = 'acard-cell';
    const tempLbl = document.createElement('div');
    tempLbl.className = 'fl';
    tempLbl.style.cssText = 'display:flex;align-items:center;gap:6px';
    const tempLabelTxt = document.createElement('span');
    tempLabelTxt.innerHTML = 'Temperature <span id="' + tvId + '">' + (hasTempOverride ? temp.toFixed(2) : 'Default') + '</span>';
    const tempResetBtn = document.createElement('button');
    tempResetBtn.type = 'button';
    tempResetBtn.className = 'btn';
    tempResetBtn.textContent = '\u21BA Default';
    tempResetBtn.style.cssText = 'margin-left:auto;padding:1px 5px;font-size:8px;line-height:1.2' + (hasTempOverride ? '' : ';opacity:0.45');
    tempResetBtn.title = 'Clear card override and use Model Default (null)';
    tempLbl.appendChild(tempLabelTxt);
    tempLbl.appendChild(tempResetBtn);
    tempCell.appendChild(tempLbl);
    const tempRow = document.createElement('div');
    tempRow.className = 'sl-row';
    const tempSlider = document.createElement('input');
    tempSlider.type = 'range'; tempSlider.min = '0'; tempSlider.max = '2'; tempSlider.step = '0.05';
    tempSlider.value = String(temp);
    tempSlider.dataset.temp = key;
    tempSlider.dataset.isDefault = hasTempOverride ? '0' : '1';
    const tempVal = document.createElement('div');
    tempVal.className = 'sl-val';
    tempVal.id = tvId + '-val';
    tempVal.textContent = hasTempOverride ? temp.toFixed(2) : 'Auto';
    tempSlider.addEventListener('input', (function(id, rst) {
      return function() {
        this.dataset.isDefault = '0';
        rst.style.opacity = '1';
        const vTxt = parseFloat(this.value).toFixed(2);
        const el = document.getElementById(id);
        if (el) el.textContent = vTxt;
        const v = document.getElementById(id + '-val');
        if (v) v.textContent = vTxt;
      };
    })(tvId, tempResetBtn));
    tempResetBtn.addEventListener('click', (function(roleKey, sl, id, rst) {
      return async function() {
        sl.dataset.isDefault = '1';
        rst.style.opacity = '0.45';
        const el = document.getElementById(id);
        if (el) el.textContent = 'Default';
        const v = document.getElementById(id + '-val');
        if (v) v.textContent = 'Auto';
        try {
          await fetch('/settings/agent', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({agent: roleKey, temperature: null})
          });
        } catch(e) { console.warn('[resetTempDefault]', e); }
      };
    })(key, tempSlider, tvId, tempResetBtn));
    tempRow.appendChild(tempSlider);
    tempRow.appendChild(tempVal);
    tempCell.appendChild(tempRow);
    grid.appendChild(tempCell);

    // ── Zelle: Output-Budget (max_tokens + thinking_budget vs Context) ──
    const tkId = 'tk-' + key;
    const tokCell = document.createElement('div');
    tokCell.className = 'acard-cell';
    const tokLbl = document.createElement('div');
    tokLbl.className = 'fl';
    tokLbl.innerHTML = 'Output-Budget <span id="' + tkId + '">' + toks + '</span>';
    tokCell.appendChild(tokLbl);
    const tokRow = document.createElement('div');
    tokRow.className = 'sl-row';
    const tokSlider = document.createElement('input');
    tokSlider.type = 'range'; tokSlider.min = '100'; tokSlider.max = String(Math.max(100, effCtx)); tokSlider.step = '50';
    tokSlider.value = Math.min(toks, effCtx);
    tokSlider.dataset.tok = key;
    tokSlider.addEventListener('input', (function(id) {
      return function() {
        const el = document.getElementById(id);
        if (el) el.textContent = this.value;
        const v = document.getElementById(id + '-val');
        if (v) v.textContent = this.value;
        refreshBudget();
      };
    })(tkId));
    const tokVal = document.createElement('div');
    tokVal.className = 'sl-val';
    tokVal.id = tkId + '-val';
    tokVal.textContent = Math.min(toks, effCtx);
    tokRow.appendChild(tokSlider);
    tokRow.appendChild(tokVal);
    tokCell.appendChild(tokRow);

    // status line: output X / context Y (Z%) + bar + warning
    const budgetStatus = document.createElement('div');
    budgetStatus.className = 'budget-status';
    const budgetBar = document.createElement('div');
    budgetBar.className = 'budget-bar';
    const budgetFill = document.createElement('div');
    budgetFill.className = 'budget-bar-fill';
    budgetBar.appendChild(budgetFill);
    const budgetPct = document.createElement('span');
    budgetStatus.appendChild(budgetBar);
    budgetStatus.appendChild(budgetPct);
    tokCell.appendChild(budgetStatus);
    const budgetWarn = document.createElement('div');
    budgetWarn.className = 'budget-warn';
    tokCell.appendChild(budgetWarn);
    grid.appendChild(tokCell);

    // ── Cell: context (0 = Auto = model default from num_ctx_config) ──
    const ctxId = 'ctx-' + key;
    const ctxCell = document.createElement('div');
    ctxCell.className = 'acard-cell';
    const ctxLbl = document.createElement('div');
    ctxLbl.className = 'fl';
    const _initKvTxt = _agentKvLabel(activeModel, ctxOverride || effCtxDefault);
    ctxLbl.innerHTML = 'Context <span id="' + ctxId + '">' + (ctxOverride ? ctxOverride : 'Auto') + '</span>'
      + '<span class="ctx-eff">' + (ctxOverride ? '' : '= ' + effCtxDefault) + '</span>'
      + '<span id="' + ctxId + '-hdr-kv" style="font-size:8px;color:var(--tx3);margin-left:auto">' + _initKvTxt + '</span>';
    ctxCell.appendChild(ctxLbl);
    const ctxRow = document.createElement('div');
    ctxRow.className = 'sl-row';
    const ctxSlider = document.createElement('input');
    ctxSlider.type = 'range'; ctxSlider.min = '0'; ctxSlider.max = '131072'; ctxSlider.step = '1024';
    ctxSlider.value = ctxOverride;
    ctxSlider.dataset.ctx = key;
    ctxSlider.addEventListener('input', (function(id, refresh, selEl, defCtx) {
      return function() {
        const el = document.getElementById(id);
        const v = parseInt(this.value);
        if (el) el.textContent = v ? v : 'Auto';
        const curMdl = (selEl && selEl.value) ? selEl.value : activeModel;
        const kvTxt = _agentKvLabel(curMdl, v || defCtx);
        const kv = document.getElementById(id + '-kv');
        if (kv) kv.textContent = kvTxt;
        const hkv = document.getElementById(id + '-hdr-kv');
        if (hkv) hkv.textContent = kvTxt;
        refresh();
      };
    })(ctxId, refreshBudget, sel, effCtxDefault));
    // CTX-FIT-WARN (2026-10-23): on commit (change, not every input tick) the
    // heuristic is replaced by the real /vram/estimate for THIS model,
    // marked red when it exceeds the budget. Warning only — never blocks.
    ctxSlider.addEventListener('change', (function(id, selEl) {
      return function() {
        var v = parseInt(this.value);
        var mdl = (selEl && selEl.value) ? selEl.value : activeModel;
        if (v && mdl && typeof _vramEstimate === 'function') _vramEstimate(mdl, v, id + '-kv', true);
      };
    })(ctxId, sel));
    const ctxVal = document.createElement('div');
    ctxVal.className = 'sl-val';
    ctxVal.id = ctxId + '-kv';
    ctxVal.textContent = _initKvTxt;
    ctxRow.appendChild(ctxSlider);
    ctxRow.appendChild(ctxVal);
    ctxCell.appendChild(ctxRow);
    grid.appendChild(ctxCell);

    // ── Cell: thinking toggle + think budget ──
    // Thinking budget is added to max_tokens (thinking eats tokens) and
    // flows into the output-budget warning.
    const thkId = 'thk-' + key;
    const think = cfg.thinking === true;
    const thinkBudget = parseInt(cfg.thinking_budget || 0);
    const thkCell = document.createElement('div');
    thkCell.className = 'acard-cell';
    if (key === 'direct' || key === 'duo_coder' || key === 'synthesizer') {
      thkCell.setAttribute('data-reasoning-ovr-row', key);
    }
    const thinkRow = document.createElement('div');
    thinkRow.className = 'fl';
    thinkRow.innerHTML = 'Thinking <input type="checkbox" data-agent="' + key + '"'
      + (think ? ' checked' : '') + ' style="margin-left:6px;accent-color:#8858c0">'
      + '<span class="thk-badge" style="font-size:8px;padding:1px 4px;border-radius:2px;'
      + 'background:rgba(136,88,192,.15);color:#8858c0;border:1px solid rgba(136,88,192,.3);'
      + 'margin-left:8px">\uD83E\uDDE0</span>'
      + ((key === 'direct' || key === 'duo_coder' || key === 'synthesizer')
        ? '<span class="reasoning-override-slot" data-agent-override="' + key + '"></span>'
        : '');
    thkCell.appendChild(thinkRow);

    const thkBudgetId = 'tkb-' + key;
    const budgetWrap = document.createElement('div');
    budgetWrap.dataset.budgetWrap = key;
    budgetWrap.style.display = think ? 'block' : 'none';
    const budgetLbl = document.createElement('div');
    budgetLbl.className = 'fl';
    budgetLbl.innerHTML = 'Think-Budget <span id="' + thkBudgetId + '">' + thinkBudget + '</span>';
    budgetWrap.appendChild(budgetLbl);
    const budgetRow = document.createElement('div');
    budgetRow.className = 'sl-row';
    const budgetSlider = document.createElement('input');
    budgetSlider.type = 'range'; budgetSlider.min = '0'; budgetSlider.max = '16000'; budgetSlider.step = '500';
    budgetSlider.value = thinkBudget;
    budgetSlider.dataset.budget = key;
    budgetSlider.addEventListener('input', (function(id, refresh) {
      return function() {
        const el = document.getElementById(id);
        if (el) el.textContent = this.value;
        const v = document.getElementById(id + '-val');
        if (v) v.textContent = this.value;
        refresh();
      };
    })(thkBudgetId, refreshBudget));
    const budgetVal = document.createElement('div');
    budgetVal.className = 'sl-val';
    budgetVal.id = thkBudgetId + '-val';
    budgetVal.textContent = thinkBudget;
    budgetRow.appendChild(budgetSlider);
    budgetRow.appendChild(budgetVal);
    budgetWrap.appendChild(budgetRow);
    thkCell.appendChild(budgetWrap);
    grid.appendChild(thkCell);

    div.appendChild(grid);

    // ── Output budget vs context: live sync, cap and warning ──
    function refreshBudget() {
      const tok = parseInt(tokSlider.value) || 0;
      const bud = parseInt(budgetSlider ? budgetSlider.value : '0') || 0;
      const ctx = (parseInt(ctxSlider.value) || 0) > 0 ? parseInt(ctxSlider.value) : effCtx;
      // Cap: the output budget must not exceed the context → slider clamps
      var effTok = tok;
      if (effTok > ctx) {
        effTok = ctx;
        tokSlider.value = ctx;
        const el = document.getElementById(tkId);
        if (el) el.textContent = ctx;
        const v = document.getElementById(tkId + '-val');
        if (v) v.textContent = ctx;
      }
      const effTotal = effTok + bud;
      const effPct = ctx > 0 ? Math.round((effTotal / ctx) * 100) : 0;
      budgetFill.className = 'budget-bar-fill' + (effPct >= 100 ? ' over' : effPct >= 90 ? ' warn' : '');
      budgetFill.style.width = Math.min(100, effPct) + '%';
      budgetPct.textContent = 'Output ' + effTotal + ' / Context ' + ctx + ' (' + effPct + '%)';
      budgetWarn.textContent = effPct >= 100
        ? '\u26A0 Output budget reaches the context \u2014 the answer will be truncated. Increase the context or lower the budget.'
        : '';
    }

    // The thinking toggle shows/hides the budget slider
    (function(key, refresh) {
      const cb = div.querySelector('input[type=checkbox][data-agent="' + key + '"]');
      if (cb) cb.addEventListener('change', function() {
        const w = div.querySelector('[data-budget-wrap="' + key + '"]');
        if (w) w.style.display = this.checked ? 'block' : 'none';
        refresh();
        if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
      });
    })(key, refreshBudget);

    refreshBudget();

    // Apply button
    const btn = document.createElement('button');
    btn.className = 'apply';
    btn.textContent = 'Apply';
    btn.setAttribute('onclick', 'applyAgent("' + key + '",this)');
    div.appendChild(btn);

    c.appendChild(div);
    } catch(cardErr) { console.error('[renderAgentCards] error for agent', entries[i] && entries[i][0], cardErr); }
  }
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  // fill all selects with the current models (if S.models is already filled)
  refreshAllSelects();
  // update the agent map after rendering
  setTimeout(buildAgentMapFromSettings, 50);
  // VISION-HINT: update the image path (multimodal direct / prepro / vision agent)
  if (typeof updateDuoImageUI === 'function') { try { updateDuoImageUI(); } catch(e) {} }
  var _critCardAfterRender = document.querySelector('.acard[data-agent="duo_critic"]');
  if (_critCardAfterRender) _critCardAfterRender.style.display = (S.mode === 'code_duo' && S.duoAgenticMode) ? 'none' : '';
}

function _agentExplainHtml(modelName) {
  const prof2 = (S.modelProfiles && (S.modelProfiles[modelName] || S.modelProfiles[(modelName || '').split(':')[0]])) || {};
  const _isVis = !!(prof2.vision);
  const _isThk = !!(prof2.thinking);
  var _expl = [];
  if (_isVis) _expl.push('<span style="color:#8858c0">🖼 processes images directly</span>');
  else _expl.push('<span style="color:var(--tx3)">no vision — images via vision-agent/prepro</span>');
  if (_isThk) _expl.push('<span style="color:#4878c0">🧠 can think (Thinking toggle)</span>');
  else _expl.push('<span style="color:var(--tx3)">without thinking</span>');
  return _expl.join(' · ');
}

function _agentKvLabel(modelName, ctxTokens) {
  const ctx = parseInt(ctxTokens || 0) || 8192;
  const prof = (S.modelProfiles && (S.modelProfiles[modelName] || S.modelProfiles[(modelName || '').split(':')[0]])) || {};
  const bpt = Number(prof.kv_bytes_per_token || 0);
  const gb = bpt > 0 ? ((ctx * bpt) / 1073741824) : (ctx / 8192);
  return 'KV: ~' + gb.toFixed(1) + 'GB';
}

function _syncAgentModelState(key, model, sel) {
  if (sel) sel.dataset.cur = model;
  if (!S.currentAssignments[key]) S.currentAssignments[key] = {};
  S.currentAssignments[key].model = model;
  if (S.agentsCfg && S.agentsCfg[key]) S.agentsCfg[key].model = model;
  if (key === 'direct') S._lastDirectRunModel = '';
  try {
    const _explEl = document.querySelector('[data-agent-explain="' + key + '"]');
    if (_explEl) _explEl.innerHTML = _agentExplainHtml(model);
    const _ctxSlider = document.querySelector('input[type=range][data-ctx="' + key + '"]');
    const _curCtx = (_ctxSlider && parseInt(_ctxSlider.value)) || parseInt((S.ctxDefaults || {})[key] || 0) || 8192;
    const _kvTxt = _agentKvLabel(model, _curCtx);
    const _kvEl = document.getElementById('ctx-' + key + '-kv');
    if (_kvEl) _kvEl.textContent = _kvTxt;
    const _hkvEl = document.getElementById('ctx-' + key + '-hdr-kv');
    if (_hkvEl) _hkvEl.textContent = _kvTxt;
  } catch(e) {}
  if (['direct', 'duo_planner', 'duo_coder', 'analyst', 'refiner', 'critic', 'synthesizer'].indexOf(key) >= 0) {
    if (typeof updateDuoImageUI === 'function') { try { updateDuoImageUI(); } catch(e) {} }
  }
  if (key === 'duo_coder') {
    S.duoCoderModel = model;
    var _dcm = document.getElementById('duo-coder-model-sel');
    if (_dcm) _dcm.value = model;
    updateDuoPairHint();
    updateMoeVisibility();
  }
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  if (key === 'duo_critic') {
    S.duoCriticModel = model || '';
    var _dcrm = document.getElementById('duo-critic-model-sel');
    if (_dcrm) _dcrm.value = S.duoCriticModel;
    updateDuoPairHint();
  }
  if (typeof renderAgentMap === 'function') renderAgentMap();
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
}

// AGENT-DROPDOWN-SAVE (2026-09-01): save the model in the agent-card dropdown directly
// (only model — sliders/toggles stay with the Apply button).
async function saveAgentModelDirect(key, sel) {
  if (!sel || (!sel.value && key !== 'duo_critic')) return;
  const model = sel.value;
  const old = sel.dataset.cur || '';
  if (model === old) return;
  sel.disabled = true;
  try {
    const res = await fetch('/settings/agent', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent:key, model:model})
    });
    const data = await res.json();
    if (data.ok) {
      _syncAgentModelState(key, model, sel);
      sel.style.borderColor = '#22c55e';
      setTimeout(function() { sel.style.borderColor = ''; }, 1200);
    } else {
      sel.value = old;
      console.warn('[saveAgentModelDirect]', data.error || 'save failed');
    }
  } catch(e) {
    sel.value = old;
    console.warn('[saveAgentModelDirect]', e.message);
  }
  sel.disabled = false;
}

async function applyAgent(key, btn) {
  const card  = btn.closest('.acard');
  const sel   = card.querySelector('select');
  const model = sel ? sel.value : '';
  const tempSlider = card.querySelector('input[type=range][data-temp="' + key + '"]');
  const tokSlider  = card.querySelector('input[type=range][data-tok="' + key + '"]');
  const temperature = (tempSlider && tempSlider.dataset.isDefault === '1') ? null : (tempSlider ? parseFloat(tempSlider.value) : null);
  const max_tokens  = tokSlider ? parseInt(tokSlider.value) : 400;
  // AGENT-THINKING: read the toggle + budget (3rd slider = thinking budget)
  const thkCb = card.querySelector('input[type=checkbox][data-agent="' + key + '"]');
  const thinking = thkCb ? thkCb.checked : false;
  const budgetSlider = card.querySelector('input[type=range][data-budget="' + key + '"]');
  const thinking_budget = budgetSlider ? parseInt(budgetSlider.value || '0') : 0;
  // AGENT-CTX: read the context size (0 = auto / model default)
  const ctxSlider = card.querySelector('input[type=range][data-ctx="' + key + '"]');
  const ctx = ctxSlider ? parseInt(ctxSlider.value || '0') : 0;

  if (!model && key !== 'duo_critic') { btn.textContent = 'No model!'; setTimeout(function(){btn.textContent='Apply';},1800); return; }

  btn.textContent = '...';
  btn.disabled = true;
  try {
    const res = await fetch('/settings/agent', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent:key, model:model, temperature:temperature, max_tokens:max_tokens,
                            thinking:thinking, thinking_budget:thinking_budget, ctx:ctx})
    });
    const data = await res.json();
    if (data.ok) {
      btn.textContent = 'OK - ' + (model ? model.slice(0,12) : 'Follow');
      btn.classList.add('ok');
      if (S.agentsCfg && S.agentsCfg[key]) S.agentsCfg[key].thinking = thinking;
      _syncAgentModelState(key, model, sel);
      if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
      if (!S.ctxOverrides) S.ctxOverrides = {};
      if (ctx > 0) S.ctxOverrides[key] = ctx; else delete S.ctxOverrides[key];
      if (ctx > 0 && key === 'duo_coder') {
        S.duoCtxAgentic = ctx;
        S.duoCtxNormal = ctx;
        var _dAg = document.getElementById('duo-ctx-agentic');
        if (_dAg) _dAg.value = ctx;
        var _dNo = document.getElementById('duo-ctx-normal');
        if (_dNo) _dNo.value = ctx;
        updateDuoPairHint();
      } else if (ctx > 0 && key === 'duo_critic') {
        S.duoCtxCritic = ctx;
        var _dCr = document.getElementById('duo-ctx-critic');
        if (_dCr) _dCr.value = ctx;
      }
      renderAgentMap();
    } else {
      btn.textContent = 'Error: ' + (data.error || '?');
    }
  } catch(e) {
    btn.textContent = 'Error: ' + e.message;
  }
  btn.disabled = false;
  setTimeout(function() { btn.textContent='Apply'; btn.classList.remove('ok'); }, 2500);
}

async function applyAll() {
  const m = document.getElementById('g-model').value;
  if (!m) return;
  await fetch('/settings/all_model', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({model:m})
  });
  await loadSettings();
}

// -- Presets ----------------------------------------------------
function _setPresetHeader() {
  var el = document.getElementById('h-preset-label');
  if (!el) return;
  // PRESET PILL CLARITY (2026-10-05, user): saved != loaded. The pill shows
  // the LOADED preset only; explain how to activate one on hover.
  if (S.activePreset) {
    el.textContent = S.activePreset;
    el.title = 'Loaded preset: ' + S.activePreset + ' (re-applied at startup)';
  } else {
    el.textContent = 'no preset';
    el.title = 'No preset loaded - open PRESETS and press Load to activate one (it then survives restarts)';
  }
}

var _PROMPT_ROLES = null;
async function fillPromptRoleSelect() {
  var sel = document.getElementById('prompt-agent-sel');
  if (!sel) return;
  if (!_PROMPT_ROLES) {
    try {
      var d = await (await fetch('/presets/prompt-roles')).json();
      _PROMPT_ROLES = (d && d.roles) || [];
    } catch (e) { _PROMPT_ROLES = []; }
  }
  var roles = _PROMPT_ROLES;
  if (!roles.length) {
    // Fallback if the endpoint is unavailable: at least the roles the runner
    // reads, with the base prompt key as label.
    roles = [{key:'duo_coder',label:'Duo Coder'},{key:'duo_critic_code',label:'Duo Critic (code)'},
             {key:'duo_critic_general',label:'Duo Critic (general)'},{key:'duo_synthesizer',label:'Duo Synthesizer'},
             {key:'analyst',label:'Analyst'},{key:'refiner',label:'Refiner'},{key:'critic',label:'Critic'},
             {key:'synthesizer',label:'Synthesizer'},{key:'direct',label:'Direct'},
             {key:'vision_agent',label:'Vision Agent'}];
  }
  var keep = sel.value;
  sel.innerHTML = '';
  roles.forEach(function(r) {
    var opt = document.createElement('option');
    opt.value = r.key;
    opt.textContent = r.label || r.key;
    if (r.hint) opt.title = r.hint;
    sel.appendChild(opt);
  });
  if (keep && roles.some(function(r) { return r.key === keep; })) sel.value = keep;
}

async function loadPresets() {
  try {
    const d = await (await fetch('/presets')).json();
    // PRESET-VISIBILITY (2026-11-01): fetch the active-preset state so the
    // tab shows WHICH preset is in effect and whether settings were edited
    // since (autoload skipped) — after a startup this was invisible.
    var _pstatus = null;
    try { _pstatus = await (await fetch('/presets/active')).json(); } catch (e) {}
    renderPresets(d, _pstatus);
    // PROMPT-ROLES (2026-11-01): the dropdown is driven by the SERVER's list of
    // preset-overridable prompt keys, not by the agent-card metadata. The old
    // list offered `intent` and `duo_critic` (files no runner reads) and hid
    // duo_critic_code / duo_critic_general / duo_synthesizer.
    await fillPromptRoleSelect();
  } catch(e) {
    showStatus('Could not load presets: ' + e);
  }
}

function renderPresets(presets, pstatus) {
  var c = document.getElementById('preset-list');
  if (!c) return;
  var _ps = pstatus || {};
  var _activeName = _ps.name || S.activePreset || '';
  var _head = document.getElementById('preset-active-line');
  if (_head) {
    _head.innerHTML = '';
    if (_activeName) {
      var _b = document.createElement('span');
      _b.className = 'preset-active-badge';
      _b.textContent = 'ACTIVE: ' + _activeName;
      _head.appendChild(_b);
      if (_ps.stale) {
        var _s2 = document.createElement('span');
        _s2.className = 'preset-stale-note';
        _s2.textContent = ' settings edited after apply \u2014 the edited settings win (autoload skipped).';
        _head.appendChild(_s2);
      } else if (_ps.applied_at) {
        var _t = document.createElement('span');
        _t.className = 'preset-stale-note';
        _t.textContent = ' applied ' + String(_ps.applied_at).replace('T', ' ').slice(0, 16);
        _head.appendChild(_t);
      }
    } else {
      var _n = document.createElement('span');
      _n.className = 'preset-stale-note';
      _n.textContent = 'No preset active \u2014 the settings run as edited.';
      _head.appendChild(_n);
    }
  }
  var keys = Object.keys(presets || {});
  if (!keys.length) { c.innerHTML = '<div class="empty">No presets saved</div>'; return; }
  c.innerHTML = '';
  keys.forEach(function(name) {
    var isActive = name === _activeName;
    var item = document.createElement('div');
    item.className = 'preset-item';
    var nd = document.createElement('div');
    nd.className = 'preset-name';
    nd.textContent = name;
    if (isActive) {
      var dot = document.createElement('span');
      dot.className = 'preset-active';
      dot.textContent = ' \u25CF ' + (_ps.stale ? 'applied (edited since)' : 'ACTIVE');
      nd.appendChild(dot);
    }
    var bp = document.createElement('button'); bp.className = 'ghost'; bp.textContent = 'Prompts';
    var bs = document.createElement('button'); bs.className = 'pload'; bs.textContent = 'Save';
    bs.title = 'Overwrite this preset with the current config';
    var bl = document.createElement('button'); bl.className = 'pload'; bl.textContent = 'Load';
    var bd = document.createElement('button'); bd.className = 'pdel'; bd.textContent = '\u00D7';
    (function(n) {
      bp.addEventListener('click', function() { openPromptEditor(n); });
      bs.addEventListener('click', function() { savePresetAs(n); });
      bl.addEventListener('click', function() { loadPreset(n); });
      bd.addEventListener('click', function() { delPreset(n); });
    })(name);
    item.appendChild(nd); item.appendChild(bp); item.appendChild(bs); item.appendChild(bl); item.appendChild(bd);
    c.appendChild(item);
  });
}

async function savePreset() {
  var name = (document.getElementById('preset-name-in').value || '').trim();
  if (!name) return;
  await savePresetAs(name);
  document.getElementById('preset-name-in').value = '';
  await loadPresets();
}

// PRESET-AGENT-OVERLAY (2026-10-18): read the agent cards that are on screen so
// a just-moved budget/temperature slider lands in the preset snapshot even when
// its POST /settings/agent has not been flushed yet (the ctx fields already do
// this with _duoCtxPayload).
function _agentCardsPayload() {
  var out = {};
  document.querySelectorAll('.acard').forEach(function (card) {
    var sel = card.querySelector('select[data-agent-model]')
           || card.querySelector('select');
    var role = card.getAttribute('data-agent')
            || (sel && sel.getAttribute('data-agent')) || '';
    if (!role || role === 'null') return;
    var entry = {};
    if (sel && sel.value) entry.model = sel.value;
    var t = card.querySelector('input[type=range][data-temp="' + role + '"]');
    if (t) entry.temperature = (t.dataset.isDefault === '1') ? null : parseFloat(t.value);
    var k = card.querySelector('input[type=range][data-tok="' + role + '"]');
    if (k) entry.max_tokens = parseInt(k.value, 10);
    var b = card.querySelector('input[type=range][data-budget="' + role + '"]');
    if (b) entry.thinking_budget = parseInt(b.value || '0', 10);
    var c = card.querySelector('input[type=range][data-ctx="' + role + '"]');
    if (c) entry.ctx = parseInt(c.value || '0', 10);
    var cb = card.querySelector('input[type=checkbox][data-agent="' + role + '"]');
    if (cb) entry.thinking = cb.checked;
    if (Object.keys(entry).length) out[role] = entry;
  });
  return out;
}


async function savePresetAs(name) {
  if (!name) return;
  // PRESET-CTX-FIX (2026-09-04): die Ctx-Werte leben in den DOM-Feldern und
  // werden nur debounced (180ms) nach /settings gepostet. Beim Preset-Save
  // wuerde der Server-Snapshot sonst den noch nicht geflushten (alten) Stand
  // speichern. Deshalb: erst den laufenden Ctx-Commit erzwingen+abwarten,
  // dann die aktuellen Werte explizit als Overlay-Body mitschicken.
  try {
    var _ctxCommit = commitDuoCtx();
    if (_ctxCommit && typeof _ctxCommit.then === 'function') await _ctxCommit;
  } catch (e) { /* stillen Fehler ignorieren - Overlay-Body sichert den Wert */ }
  var body = _duoCtxPayload();
  try {
    var _agCards = _agentCardsPayload();
    if (_agCards && Object.keys(_agCards).length) body.agents = _agCards;
  } catch (e) { /* keep the server-side snapshot */ }
  var r = await fetch('/presets/' + encodeURIComponent(name), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  // SAVE-FEEDBACK (2026-10-28, preset audit): the response used to be
  // ignored - a failed save looked exactly like a saved preset.
  if (!r.ok) { showStatus('Preset "' + name + '" save failed (' + r.status + ')'); return; }
  showStatus('Preset "' + name + '" saved');
  await loadPresets();
}

async function loadPreset(name) {
  // Flush pending settings/ctx patches first, so they can't land AFTER the
  // preset was applied and overwrite its values via settings.update.
  if (_settingsPostTimer) {
    clearTimeout(_settingsPostTimer);
    _settingsPostTimer = null;
    await _flushQueuedSettings();
  }
  if (_duoCtxCommitTimer) {
    clearTimeout(_duoCtxCommitTimer);
    _duoCtxCommitTimer = null;
    try { await commitDuoCtx(); } catch(e) {}
  }
  var r = await fetch('/presets/' + encodeURIComponent(name) + '/load', { method: 'POST' });
  var d = await r.json();
  if (d.ok) {
    S.activePreset = name;
    _setPresetHeader();
    await loadSettings();
    await loadPresets();
    // PRESET-CTX-FIX (2026-09-04): abgeleitete Ctx-Zustaende nach dem Load neu
    // auswerten, damit das Duo-Tab-Feld den geladenen Wert zeigt und bei
    // "Planner = coder" korrekt gespiegelt/disabled ist.
    var _plAfter = document.getElementById('duo-ctx-planner');
    if (_plAfter) {
      if (S.duoPlannerUseCoderCtx) { _plAfter.disabled = true; _plAfter.value = S.duoCtxAgentic != null ? S.duoCtxAgentic : ''; }
      else { _plAfter.disabled = false; }
    }
    var _agAfter = document.getElementById('duo-ctx-agentic');
    if (_agAfter && S.duoCtxAgentic != null) _agAfter.value = S.duoCtxAgentic;
    var _noAfter = document.getElementById('duo-ctx-normal');
    if (_noAfter && S.duoCtxNormal != null) _noAfter.value = S.duoCtxNormal;
    // PRESET-VISIBILITY (2026-09-08): show what the preset restored — a
    // "planner off + chunking off" snapshot used to disable the planner phase
    // without any feedback.
    var _warnTxt = (d.warnings && d.warnings.length)
      ? ' | ⚠ ' + d.warnings.join(' ⚠ ') : '';
    // no alert(): it blocked the UI for a full click-through and felt like lag
    showStatus('Preset "' + name + '" loaded - planner '
      + (d.planner_enabled ? 'ON' : 'OFF')
      + (d.planner_model ? ' (' + d.planner_model + ')' : '')
      + ', chunking ' + (d.chunking ? 'ON' : 'OFF') + _warnTxt);
  } else {
    alert('Preset "' + name + '" could not be loaded' + (d && d.error ? ': ' + d.error : ''));
  }
}

async function delPreset(name) {
  if (!confirm('Delete preset "' + name + '"?')) return;
  var r = await fetch('/presets/' + encodeURIComponent(name), { method: 'DELETE' });
  if (!r.ok) { showStatus('Preset "' + name + '" delete failed (' + r.status + ')'); }
  if (S.activePreset === name) { S.activePreset = null; _setPresetHeader(); }
  var pe = document.getElementById('prompt-editor-section');
  if (pe) pe.style.display = 'none';
  await loadPresets();
}

// -- Prompt Editor ----------------------------------------------
async function openPromptEditor(name) {
  S.editingPreset = name;
  var pe = document.getElementById('prompt-editor-section');
  if (pe) pe.style.display = 'block';
  await loadPrompt();
}

async function loadPrompt() {
  if (!S.editingPreset) return;
  var agent = document.getElementById('prompt-agent-sel').value;
  try {
    var d = await (await fetch('/presets/' + encodeURIComponent(S.editingPreset) + '/prompts/' + agent)).json();
    var ta = document.getElementById('prompt-textarea');
    if (ta) ta.value = d.content || '';
    var cb = document.getElementById('custom-badge');
    if (cb) cb.style.display = d.is_custom ? 'inline' : 'none';
  } catch(e) {}
}

function promptChanged() {
  var cb = document.getElementById('custom-badge');
  if (cb) cb.style.display = 'inline';
}

async function savePrompt() {
  if (!S.editingPreset) return;
  var agent = document.getElementById('prompt-agent-sel').value;
  var ta = document.getElementById('prompt-textarea');
  var r = await fetch('/presets/' + encodeURIComponent(S.editingPreset) + '/prompts/' + agent, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: ta ? ta.value : '' })
  });
  if (!r.ok) { showStatus('Prompt save failed (' + r.status + ')'); return; }
  var cb = document.getElementById('custom-badge');
  if (cb) cb.style.display = 'inline';
}

async function resetPrompt() { return loadPrompt(); }

// -- Memory -----------------------------------------------------
async function loadMemory() {
  try {
    const d = await (await fetch('/memory')).json();
    const c = document.getElementById('memory-list');
    if (!c) return;
    c.innerHTML = '';
    if (!d.memories || !d.memories.length) {
      c.innerHTML = '<div class="empty">&#11043;<br>Nothing stored<br><span style="font-size:9px;margin-top:4px;display:block">Say: Remember that...</span></div>';
      const mt = document.querySelector('.tab[data-p="memory"]');
      if (mt) mt.textContent = 'Memory';
      return;
    }
    d.memories.forEach(function(m) {
      const item = document.createElement('div');
      item.className = 'mem-item';
      const kEl = document.createElement('div'); kEl.className = 'mem-key'; kEl.textContent = m.key || '?';
      const vEl = document.createElement('div'); vEl.className = 'mem-val'; vEl.textContent = m.value || '';
      const dEl = document.createElement('span'); dEl.className = 'mem-date'; dEl.textContent = m.saved_at || '';
      vEl.appendChild(dEl);
      const dBtn = document.createElement('button'); dBtn.className = 'mem-del'; dBtn.textContent = '\u00D7';
      (function(key) { dBtn.addEventListener('click', function() { delMem(key); }); })(m.key);
      item.appendChild(kEl); item.appendChild(vEl); item.appendChild(dBtn);
      c.appendChild(item);
    });
    // show the memory count in the tab
    const mt = document.querySelector('.tab[data-p="memory"]');
    if (mt) mt.textContent = 'Memory (' + d.memories.length + ')';
  } catch(e) {
    console.error('loadMemory:', e);
    const c = document.getElementById('memory-list');
    if (c) c.innerHTML = '<div style="font-family:monospace;font-size:10px;color:#b04040;padding:8px">Error: ' + esc(e.message || String(e)) + '</div>';
  }
}

async function delMem(key) {
  await fetch('/memory/' + encodeURIComponent(key), {method:'DELETE'});
  await loadMemory();
}

async function clearSession() {
  await fetch('/memory/clear_session', {method:'POST'});
  showStatus('Session history cleared.');
}

