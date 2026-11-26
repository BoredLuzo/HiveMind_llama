// -- Special Agents (Intent + Soul-Evolve) -------------------------

function toggleSpecialAgents() {
  const body  = document.getElementById('special-agents-body');
  const arrow = document.getElementById('special-agents-arrow');
  if (!body) return;
  const open = body.style.display !== 'none';
  body.style.display  = open ? 'none' : 'block';
  if (arrow) arrow.style.transform = open ? '' : 'rotate(180deg)';
}

function _updateSpecialAgentChips() {
  const c = document.getElementById('special-agents-active-chips');
  if (!c) return;
  c.innerHTML = '';
  if (S.intentEnabled && S.intentModel) {
    const chip = document.createElement('span');
    chip.className = 'intent-chip';
    chip.textContent = 'INTENT';
    c.appendChild(chip);
  }
  if (S.soulEvolveEnabled) {
    const chip = document.createElement('span');
    chip.style.cssText = 'display:inline-flex;align-items:center;background:rgba(136,88,192,.12);border:1px solid rgba(136,88,192,.35);border-radius:3px;padding:2px 7px;font-family:IBM Plex Mono,monospace;font-size:9px;color:#8858c0;text-transform:uppercase;letter-spacing:.06em';
    chip.textContent = 'SOUL-EVO';
    c.appendChild(chip);
  }
  if (S.explorationAgentEnabled) {
    const chip = document.createElement('span');
    chip.style.cssText = 'display:inline-flex;align-items:center;background:rgba(58,154,74,.12);border:1px solid rgba(58,154,74,.35);border-radius:3px;padding:2px 7px;font-family:IBM Plex Mono,monospace;font-size:9px;color:#3a9a4a;text-transform:uppercase;letter-spacing:.06em';
    chip.textContent = S.explorationAgentModel ? 'EXPLORE:' + S.explorationAgentModel.split(':')[0] : 'EXPLORE';
    c.appendChild(chip);
  }
}

async function loadSpecialAgentsConfig(cachedSettings) {
  try {
    const s = cachedSettings || await (await fetch('/settings')).json();

    // Intent agent
    const icfg = s.intent_agent || {};
    S.intentEnabled = icfg.enabled || false;
    S.intentModel   = icfg.model   || '';
    const itog = document.getElementById('intent-enabled');
    if (itog) itog.checked = S.intentEnabled;
    const isel = document.getElementById('intent-model-sel');
    if (isel) {
        isel.innerHTML = '<option value="">-- No model --</option>'
        + S.models.map(m => `<option value="${esc(m)}"${m===S.intentModel?' selected':''}>${esc(m)}</option>`).join('');
    }
    const itsl = document.getElementById('intent-temp-sl');
    if (itsl && icfg.temperature !== undefined) {
      itsl.value = icfg.temperature;
      const tv = document.getElementById('intent-tv');
      if (tv) tv.textContent = parseFloat(icfg.temperature).toFixed(2);
    }
    _updateIntentCard();

    // Soul-evolve agent
    const secfg = s.soul_evolve_agent || {};
    S.soulEvolveEnabled = secfg.enabled || false;
    S.soulEvolveModel   = secfg.model   || '';
    const setog = document.getElementById('soul-evolve-agent-enabled');
    if (setog) setog.checked = S.soulEvolveEnabled;
    const sesel = document.getElementById('soul-evolve-model-sel');
    if (sesel) {
      sesel.innerHTML = (function() {
        const rec = ['qwen3.5:4b-mtp','qwen3.5:2b-mtp','lfm3:1.7b-qad'].find(m => S.models.includes(m));
          let o = '<option value="">-- No model (Soul Engine fallback) --</option>';
        if (rec && rec !== S.soulEvolveModel) {
          o += '<option value="' + esc(rec) + '" style="color:#8858c0;font-weight:600">★ ' + esc(rec) + ' (Recommended)</option>';
          o += '<option disabled>────────────</option>';
        }
        S.models.forEach(function(m) {
          if (m === rec && m !== S.soulEvolveModel) return;
          o += '<option value="' + esc(m) + '"' + (m === S.soulEvolveModel ? ' selected' : '') + '>' + esc(m) + '</option>';
        });
        return o;
      })();
      if (S.soulEvolveModel) sesel.value = S.soulEvolveModel;
    }
    const setsl = document.getElementById('soul-evolve-temp-sl');
    if (setsl && secfg.temperature !== undefined) {
      setsl.value = secfg.temperature;
      const tv = document.getElementById('soul-evolve-tv');
      if (tv) tv.textContent = parseFloat(secfg.temperature).toFixed(2);
    }
    const setok = document.getElementById('soul-evolve-tok-sl');
    if (setok && secfg.max_tokens !== undefined) {
      setok.value = secfg.max_tokens;
      const tk = document.getElementById('soul-evolve-tk');
      if (tk) tk.textContent = secfg.max_tokens;
    }

    // Soul Skill Distillation + Skill Writing toggles
    const sdtog = document.getElementById('soul-distillation-toggle');
    if (sdtog) sdtog.checked = s.soul_skill_distillation === true;
    const sswtog = document.getElementById('soul-skill-writing-toggle');
    if (sswtog) sswtog.checked = s.soul_skill_writing === true;
    const dpratog = document.getElementById('duo-peer-ratings-agentic-toggle');
    if (dpratog) dpratog.checked = s.duo_peer_ratings_agentic === true;

    // Skills (C-6) — load list + toggle
    loadSkillsUI();

    // Exploration agent
    const excfg = s.exploration_agent || {};
    S.explorationAgentEnabled = excfg.enabled || false;
    S.explorationWorkers      = Array.isArray(excfg.workers) ? excfg.workers : [];
    const extog = document.getElementById('exploration-agent-enabled');
    if (extog) extog.checked = S.explorationAgentEnabled;
    const exctx = s.duo_pre_explore_ctx || 4096;
    const exctxsl = document.getElementById('exploration-ctx-sl');
    if (exctxsl) { exctxsl.value = exctx; document.getElementById('exploration-ctx-v').textContent = exctx; }
    const extok = s.duo_pre_explore_tokens || 700;
    const extoksl = document.getElementById('exploration-tok-sl');
    if (extoksl) { extoksl.value = extok; document.getElementById('exploration-tok-v').textContent = extok; }
    _renderExplorationWorkers();

    _updateSpecialAgentChips();
  } catch(e) { console.warn('loadSpecialAgentsConfig:', e); }
}

async function applySpecialAgent(key, btn) {
  btn.textContent = '...'; btn.disabled = true;
  let model, temp, toks, enabled, settingsKey;

  if (key === 'intent') {
    model      = document.getElementById('intent-model-sel').value;
    temp       = parseFloat(document.getElementById('intent-temp-sl').value);
    enabled    = document.getElementById('intent-enabled').checked;
    settingsKey = 'intent_agent';
    toks = 400;
    if (enabled && !model) {
      btn.textContent = 'Model missing';
      btn.disabled = false;
      setTimeout(() => { btn.textContent = 'Apply'; }, 1800);
      return;
    }
  } else if (key === 'exploration') {
    // exploration is now handled via applyExplorationWorkers() — fallback
    return applyExplorationWorkers(btn);
  } else {
    model      = (document.getElementById('soul-evolve-model-sel') || {}).value || '';
    temp       = parseFloat(document.getElementById('soul-evolve-temp-sl').value);
    toks       = parseInt(document.getElementById('soul-evolve-tok-sl').value);
    enabled    = document.getElementById('soul-evolve-agent-enabled').checked;
    settingsKey = 'soul_evolve_agent';
  }

  try {
    const r = await fetch('/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(
        key === 'exploration'
          ? { [settingsKey]: { enabled, model }, duo_pre_explore_ctx: toks }
          : { [settingsKey]: { enabled, model, temperature: temp, max_tokens: toks } }
      )
    });
    const d = await r.json();
    if (d.ok) {
      if (key === 'intent') { S.intentEnabled = enabled; S.intentModel = model; _updateIntentCard(); }
      else if (key === 'exploration') { S.explorationAgentEnabled = enabled; S.explorationAgentModel = model; }
      else                  { S.soulEvolveEnabled = enabled; S.soulEvolveModel = model; }
      _updateSpecialAgentChips();
      btn.textContent = 'OK \u2713'; btn.classList.add('ok');
    } else {
      btn.textContent = 'Error';
    }
  } catch(e) { btn.textContent = 'Error: ' + e.message; }
  btn.disabled = false;
  setTimeout(() => { btn.textContent = 'Apply'; btn.classList.remove('ok'); }, 2000);
}

// NOTE: setIntentEnabled is defined further down (one canonical version).
// This spot was cleaned up — duplicate definition removed (bug: 2nd def overwrote the 1st →
// _updateSpecialAgentChips() was never called + temperature never saved).

function setExplorationAgentEnabled(enabled) {
  _postSettingsFF({exploration_agent: {enabled, workers: S.explorationWorkers || []}}, function() {
    S.explorationAgentEnabled = !enabled;
    _updateSpecialAgentChips();
  });
}

function _renderExplorationWorkers() {
  const list = document.getElementById('exploration-workers-list');
  if (!list) return;
  const workers = S.explorationWorkers || [];
  if (!workers.length) {
    list.innerHTML = '<div style="font-size:9px;color:#556;font-family:monospace;padding:2px 0">No workers — sequential (agent model)</div>';
    return;
  }
  const SMALL = ['qwen3.5:2b','qwen3.5:0.8b','qwen3.5:2b-d','granite4:1b','smollm2:1.7b'];
  const variantSeen = Object.create(null);
  list.innerHTML = workers.map(function(w, i) {
    const variant = _workerVariantIndex((w && w.model) ? w.model : '?', variantSeen);
    const clr = _workerColorForVariant(variant);
    const opts = S.models.map(function(m) {
      const star = SMALL.includes(m) ? '★ ' : '';
      return '<option value="' + esc(m) + '"' + (m === w.model ? ' selected' : '') + '>' + star + esc(m) + '</option>';
    }).join('');
    return '<div style="display:flex;gap:4px;align-items:center">'
      + '<span style="font-size:9px;min-width:16px;padding:1px 4px;border-radius:3px;'
      + 'border:1px solid ' + clr.chipBd + ';background:' + clr.chipBg + ';color:' + clr.chipFg + '">W' + i + '</span>'
      + '<select style="flex:1;font-size:10px" onchange="S.explorationWorkers[' + i + '].model=this.value">'
      + opts + '</select>'
      + '<button onclick="removeExplorationWorker(' + i + ')" style="font-size:10px;padding:1px 6px;'
      + 'background:transparent;border:0.5px solid #a33;color:#a33;border-radius:3px;cursor:pointer">✕</button>'
      + '</div>';
  }).join('');
}

function addExplorationWorker() {
  S.explorationWorkers = S.explorationWorkers || [];
  const ctx = parseInt(document.getElementById('exploration-ctx-sl')?.value || 4096);
  const PREF = ['qwen3.5:2b','qwen3.5:0.8b','granite4:1b','smollm2:1.7b'];
  const def = PREF.find(function(m){ return S.models.includes(m); }) || S.models[0] || '';
  S.explorationWorkers.push({model: def, ctx: ctx});
  _renderExplorationWorkers();
}

function removeExplorationWorker(i) {
  (S.explorationWorkers || []).splice(i, 1);
  _renderExplorationWorkers();
}

async function applyExplorationWorkers(btn) {
  btn.textContent = '...'; btn.disabled = true;
  const ctx     = parseInt(document.getElementById('exploration-ctx-sl')?.value || 4096);
  const toks    = parseInt(document.getElementById('exploration-tok-sl')?.value || 700);
  const enabled = document.getElementById('exploration-agent-enabled')?.checked || false;
  // write ctx into all worker slots
  const workers = (S.explorationWorkers || []).map(function(w){ return {model: w.model, ctx: ctx}; });
  try {
    const r = await fetch('/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        exploration_agent:      {enabled, workers},
        duo_pre_explore_ctx:    ctx,
        duo_pre_explore_tokens: toks,
      })
    });
    const d = await r.json();
    if (d.ok) {
      S.explorationAgentEnabled = enabled;
      _updateSpecialAgentChips();
      btn.textContent = 'OK \u2713'; btn.classList.add('ok');
    } else { btn.textContent = 'Error'; }
  } catch(e) { btn.textContent = 'Error: ' + e.message; }
  btn.disabled = false;
  setTimeout(function(){ btn.textContent = 'Apply'; btn.classList.remove('ok'); }, 2000);
}

function setSoulEvolveAgentEnabled(enabled) {
  _postSettingsFF({soul_evolve_agent: {enabled, model: S.soulEvolveModel||'',
      temperature: parseFloat(document.getElementById('soul-evolve-temp-sl')?.value||0.65),
      max_tokens: parseInt(document.getElementById('soul-evolve-tok-sl')?.value||800)}}, function() {
    S.soulEvolveEnabled = !enabled;
    _updateSpecialAgentChips();
  });
}

// -- Intent Agent --------------------------------------------------
// Pre-pipeline agent that routes natural language intents to
// memory / soul-evolve / tool calls before the main pipeline runs.

const INTENT_MEMORY_KWS = [
  'remember that', 'forget', 'store this', 'save that', 'keep in mind',
];
const INTENT_EVOLVE_KWS = [
  'soul evolution', 'soul evolve', 'evolve yourself', 'learn from this',
];
// Tool keywords kept minimal — full detection lives in server
const INTENT_TOOL_KWS   = ['search for', 'search the web', 'google it', 'calculate', 'compute'];

function _detectIntentLocally(text) {
  const t = text.toLowerCase();
  if (INTENT_MEMORY_KWS.some(k => t.includes(k))) return 'memory';
  if (INTENT_EVOLVE_KWS.some(k => t.includes(k))) return 'evolve';
  if (INTENT_TOOL_KWS.some(k  => t.includes(k))) return 'tool';
  return null;
}

// loadIntentConfig() was removed — was dead code (never called).
// loadSpecialAgentsConfig() (called in init()) fully covers the intent-agent config.

function _updateIntentCard() {
  const card  = document.getElementById('intent-card');
  const chip  = document.getElementById('intent-active-chip');
  const prev  = document.getElementById('intent-model-preview');
  const memDot = document.getElementById('intent-mem-dot');
  const evoDot = document.getElementById('intent-evo-dot');
  const on = S.intentEnabled && !!S.intentModel;
  if (card) card.classList.toggle('active', on);
  if (chip) chip.style.display = on ? 'inline-flex' : 'none';
  if (prev) prev.textContent = S.intentModel ? ('(' + S.intentModel.replace(':latest','').split('/').pop() + ')') : '';
  // route dots
  const col = on ? '#20b0a0' : '#334';
  if (memDot) memDot.style.background = col;
  if (evoDot) evoDot.style.background = col;
}

// BUG-FIX: was defined twice — JS took the 2nd def → _updateSpecialAgentChips() never
// called (header chips did not update on toggle) + temperature never saved.
// Now: one canonical version, both updates + complete POST payload.
function setIntentEnabled(enabled) {
  if (enabled && !S.intentModel) {
    enabled = false;
    var t = document.getElementById('intent-enabled');
    if (t) t.checked = false;
    showStatus('Intent agent: select a model first, then enable it.');
  }
  S.intentEnabled = enabled;
  _updateIntentCard();          // intent-card border + chip in the special-agent panel
  _updateSpecialAgentChips();   // update header chips (AN●, VI● etc.)
  _postSettingsFF({intent_agent: {
      enabled,
      model: S.intentModel || '',
      temperature: parseFloat(document.getElementById('intent-temp-sl')?.value || 0.1)
    }}, function() {
    S.intentEnabled = !enabled;
    var t = document.getElementById('intent-enabled');
    if (t) t.checked = !enabled;
    _updateIntentCard();
    _updateSpecialAgentChips();
  });
}

async function testIntentModel() {
  const model  = document.getElementById('intent-model-sel').value;
  const btn    = document.getElementById('intent-test-btn');
  const resEl  = document.getElementById('intent-test-result');
  if (!model) { if (resEl) { resEl.textContent = 'No model.'; resEl.style.color='#e09030'; } return; }
  if (btn) btn.textContent = '...';
  try {
    // Quick local intent detection test
    const examples = ['Remember that I like coffee', 'Please evolve yourself'];
    const detected = examples.map(e => _detectIntentLocally(e));
    if (resEl) {
      resEl.textContent = '\u2713 Local: memory=' + (detected[0]||'?') + ', evolve=' + (detected[1]||'?');
      resEl.style.color = '#20b0a0';
    }
    // Also check if model is available
    const d = await fetch('/vision/test', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({model})
    }).then(r => r.json());
    if (resEl) {
      resEl.textContent = d.available
        ? '\u2713 Model OK \u2014 active locally'
        : '\u2717 Model not in Ollama';
      resEl.style.color = d.available ? '#20b0a0' : '#b04040';
    }
  } catch(e) {
    if (resEl) { resEl.textContent = 'Error: ' + e.message; resEl.style.color='#b04040'; }
  }
  if (btn) btn.textContent = '\u25BA';
  setTimeout(() => { if (resEl) resEl.textContent = ''; }, 4000);
}

// Hook: before sendMsg hits the server, check if intent routing should apply
// Returns true if the message was fully handled locally (no stream needed)
async function _checkIntentBeforeStream(txt) {
  if (!S.intentEnabled || !S.intentModel || !txt) return false;

  const intent = _detectIntentLocally(txt);
  if (!intent) return false;

  if (intent === 'evolve') {
    // Show a status and trigger evolution
    showStatus('\u9670 Intent detected: Soul Evolution...');
    try {
      const d = await fetch('/soul/evolve', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({force: false})
      }).then(r => r.json());
      if (d.ok) {
        addDiv('Soul evolviert \u2192 v' + (d.soul && d.soul.version || '?'));
        loadSoulStatus();
      } else {
        addDiv('Evolution not possible: ' + (d.reason || '?') + ' \u2192 use "FORCE" in the Soul tab.');
      }
    } catch(e) { addDiv('Error: ' + e.message); }
    rmEl('status-el');
    return true;
  }

  // memory + tools: let server handle via normal stream (intent flagged via _detect on server)
  // Just return false so stream runs; server picks up the intent natively
  return false;
}

