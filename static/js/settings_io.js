// settings_io.js — model selectors, settings load/save queueing, workspace/duo/tool-tier controls & MCP status

// -- Force Controls --------------------------------------------
const PIPELINE_AGENTS = [
  {key:'analyst',     label:'Analyst',     color:'#4878c0'},
  {key:'refiner',     label:'Refiner',     color:'#3a9960'},
  {key:'critic',      label:'Kritiker',    color:'#b04040'},
  {key:'synthesizer', label:'Synthesizer', color:'#e09030'},
];

function buildAgentForceButtons() {
  const c = document.getElementById('force-agent-btns');
  if (!c) return;
  c.innerHTML = '';
  PIPELINE_AGENTS.forEach(function(ag) {
    const btn = document.createElement('button');
    btn.className = 'fagent-btn';
    btn.id = 'fagent-' + ag.key;
    btn.title = S.skippedAgents[ag.key]
      ? ag.label + ' is disabled -- click to enable'
      : ag.label + ' is active -- click to disable';

    const dot = document.createElement('span');
    dot.className = 'fagent-dot';
    dot.style.background = S.skippedAgents[ag.key] ? '#b04040' : ag.color;

    const lbl = document.createElement('span');
    lbl.textContent = ag.label;

    btn.appendChild(dot);
    btn.appendChild(lbl);

    if (S.skippedAgents[ag.key]) {
      btn.classList.add('skip');
      const x = document.createElement('span');
      x.textContent = 'OFF';
      x.style.marginLeft = 'auto';
      x.style.fontSize   = '8px';
      btn.appendChild(x);
    }

    btn.onclick = function() { toggleAgentSkip(ag.key); };
    c.appendChild(btn);
  });
}

function toggleAgentSkip(key) {
  S.skippedAgents[key] = !S.skippedAgents[key];
  if (!S.skippedAgents[key]) delete S.skippedAgents[key];
  buildAgentForceButtons();
  updateForceHeaderBadge();
}

function resetAgentForce() {
  S.skippedAgents = {};
  buildAgentForceButtons();
  updateForceHeaderBadge();
}

function setComplexityForce(val, el) {
  S.forcedComplexity = val;
  document.querySelectorAll('.cforce-btn').forEach(function(b) {
    b.classList.remove('on-auto','on-simple','on-complex');
  });
  el.classList.add('on-' + val);
  updateForceHeaderBadge();
  if (val !== 'auto') {
    const el2 = document.getElementById('h-complexity');
    el2.textContent = 'FORCE ' + val.toUpperCase();
    el2.className = val === 'complex' ? 'is-complex' : 'is-simple';
    el2.style.display = 'inline-block';
  } else {
    document.getElementById('h-complexity').style.display = 'none';
  }
}

function updateComplexityVisibility() {
  // COMPLEXITY-UI (2026-08-27): the complexity widget (force panel in the
  // config tab + #h-complexity badge) only makes sense in the pipeline/automap/duo
  // context — in "simple" and in agentic mode (code_duo + duo_agentic_mode)
  // no complexity decision is made, so hide it.
  var show = !(S.mode === 'simple' || (S.mode === 'code_duo' && S.duoAgenticMode));
  var fs = document.querySelector('.force-section');
  if (fs) fs.style.display = show ? '' : 'none';
  // Pipeline agents force (enable/disable buttons) only in the pipeline context.
  var pa = document.getElementById('pipeline-agents-sec');
  if (pa) pa.style.display = show ? '' : 'none';
  if (!show) {
    var hc = document.getElementById('h-complexity');
    if (hc) hc.style.display = 'none';
  }
}

function toggleAgentConfig() {
  var body = document.getElementById('agent-config-body');
  var arrow = document.getElementById('agent-config-arrow');
  if (!body) return;
  var open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  if (arrow) arrow.style.transform = open ? '' : 'rotate(180deg)';
}

function setJudgeBias(val) {
  S.judgeBias = parseInt(val);
  document.getElementById('judge-bias-val').textContent = val;
}

function normalizeToolThinkingMode(v) {
  var mode = String(v || 'off').toLowerCase();
  // Legacy migration: critical/balanced → on_fail
  if (mode === 'critical' || mode === 'balanced') mode = 'on_fail';
  return (mode === 'off' || mode === 'on_fail' || mode === 'always')
    ? mode
    : 'off';
}

function onDuoToolThinkingModeChange(v) {
  S.duoToolThinkingMode = normalizeToolThinkingMode(v);
  S.duoToolThinkingEnabled = (S.duoToolThinkingMode !== 'off');
  var rb = document.getElementById('ttm_' + S.duoToolThinkingMode);
  if (rb) rb.checked = true;
  // Wenn Toggle "always" an ist, bleibt always aktiv — segmented control ist Fallback
  postSettings({
    duo_coder_tool_thinking: S.duoToolThinkingAlways || S.duoToolThinkingEnabled,
    duo_coder_tool_thinking_auto_mode: S.duoToolThinkingAlways ? 'always' : S.duoToolThinkingMode,
  });
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
}


var _duoCtxCommitTimer = null;
function scheduleDuoCtxCommit() {
  if (_duoCtxCommitTimer) clearTimeout(_duoCtxCommitTimer);
  _duoCtxCommitTimer = setTimeout(commitDuoCtx, 450);
}
function _duoCtxPayload() {
  function _num(id, fb) {
    var el = document.getElementById(id);
    if (!el) return fb;
    var v = parseInt(el.value, 10);
    return (isFinite(v) && v > 0) ? v : fb;
  }
  var a = _num('duo-ctx-agentic', 16384);
  var n = _num('duo-ctx-normal', 8192);
  var p = _num('duo-ctx-planner', 16384);
  var c = _num('duo-ctx-critic', 8192);
  S.duoCtxAgentic = a;
  S.duoCtxUntilFinished = a;
  S.duoCtxNormal = n;
  S.duoCtxPlanner = p;
  S.duoCtxCritic = c;
  var payload = {
    duo_coder_ctx_agentic: a,
    duo_coder_ctx_normal: n,
    duo_critic_ctx: c,
  };
  // Planner target only when "Planner = coder" is off (field enabled) — otherwise
  // a stale/disabled value would silently override the agentic context.
  var pl = document.getElementById('duo-ctx-planner');
  if (pl && !pl.disabled) payload.duo_planner_ctx_target = p;
  return payload;
}

function commitDuoCtx() {
  if (_duoCtxCommitTimer) { clearTimeout(_duoCtxCommitTimer); _duoCtxCommitTimer = null; }
  return postSettings(_duoCtxPayload());
}

function toggleWsServerOpts() {
  S._wsServerOptsOpen = !S._wsServerOptsOpen;
  updateWebsearchHint();
}

function onDuoWebsearchToggle(on) {
  S.duoWebsearch = !!on;
  postSettings({duo_websearch_enabled: S.duoWebsearch}).then(function() {
    if (S.duoWebsearch) checkWebsearchStatus();
  });
  updateWebsearchHint();
  updateDuoCoderTierHint();
  updateDirectToolsTierHint();
  updateChatToolsBadge();
}

function onSearxngHostChange(val) {
  var host = String(val || '').trim() || 'http://localhost:8888';
  S.searxngHost = host;
  var inp = document.getElementById('searxng-host-inp');
  if (inp && inp.value !== host) inp.value = host;
  postSettings({searxng_host: host}).then(function() {
    if (S.duoWebsearch) checkWebsearchStatus();
  });
  updateWebsearchHint();
}

function updateWebsearchHint() {
  var h = document.getElementById('duo-websearch-hint');
  var _wsActive = !!S.duoWebsearch;
  if (h) h.style.display = '';
  var hostEl = document.getElementById('ws-searxng-host');
  if (hostEl) {
    hostEl.textContent = (S.searxngHost || 'http://localhost:8888').replace(/^https?:\/\//i, '');
  }
  var led = document.getElementById('ws-status-led');
  var dot = document.getElementById('ws-status-dot');
  if (!_wsActive) {
    if (led) led.className = 'tools-led is-off';
    if (dot && (!dot.textContent || dot.textContent === 'OK' || dot.textContent === 'SearXNG' || dot.textContent === 'OFF')) {
      dot.textContent = 'OFF';
      dot.style.color = 'var(--tx3)';
    }
  } else if (dot && dot.textContent === 'OFF') {
    dot.textContent = 'Pending check';
    dot.style.color = 'var(--tx2)';
  }
  var to = document.getElementById('ws-timeout-opts');
  if (to) to.style.display = (_wsActive || !!S._wsServerOptsOpen) ? 'block' : 'none';
  var ah = document.getElementById('automap-websearch-hint');
  if (ah) ah.style.display = (S.mode === 'automap' && S.automapPipelineWebsearch) ? 'block' : 'none';
}

function updateDirectToolsHint() {
  var opts = document.getElementById('direct-tools-opts');
  if (opts) opts.style.display = S.directToolsEnabled ? 'block' : 'none';
}

// TOOLS-OPTIONALS-CARD (2026-11-12): single card in #chat-tools-section for
// Direct/Auto, moved into #duo-tools-slot in Code-Duo (Critic-Duo & Agentic).
// Hidden in Pipeline/AutoMap.
function updateChatToolsSectionVisibility() {
  var sec = document.getElementById('chat-tools-section');
  var card = document.getElementById('tools-optionals-card');
  var duoSlot = document.getElementById('duo-tools-slot');
  var directBlock = document.getElementById('tools-direct-block');
  var coderBlock = document.getElementById('tools-coder-block');
  var optBlock = document.getElementById('tools-optionals-block');
  var scopeSub = document.getElementById('tools-opt-scope-sub');
  if (!sec || !card) return;

  var mode = S.mode || 'auto';
  if (mode === 'simple' || mode === 'auto') {
    _ctxRestore(card);
    sec.style.display = 'block';
    card.style.display = 'block';
    if (directBlock) directBlock.style.display = 'block';
    if (coderBlock) coderBlock.style.display = (mode === 'auto') ? 'block' : 'none';
    if (optBlock) optBlock.style.display = 'block';
    if (scopeSub) scopeSub.textContent = (mode === 'auto') ? 'Direct + Agentic' : 'Direct';
  } else if (mode === 'code_duo') {
    sec.style.display = 'none';
    if (duoSlot) _ctxMoveTo(card, duoSlot);
    card.style.display = 'block';
    if (directBlock) directBlock.style.display = 'none';
    if (coderBlock) coderBlock.style.display = 'block';
    if (optBlock) optBlock.style.display = 'block';
    if (scopeSub) scopeSub.textContent = S.duoAgenticMode ? 'Agentic' : 'Critic-Duo';
  } else {
    _ctxRestore(card);
    sec.style.display = 'none';
  }
}

// REASONING-CARD (2026-11-25): single card in #chat-reasoning-section for
// Direct/Auto/Pipeline, moved into #duo-reasoning-slot in Code-Duo. Hidden in AutoMap.
function updateReasoningCardVisibility() {
  var sec = document.getElementById('chat-reasoning-section');
  var card = document.getElementById('reasoning-card');
  var duoSlot = document.getElementById('duo-reasoning-slot');
  if (!sec || !card) return;
  var mode = S.mode || 'auto';
  if (mode === 'simple' || mode === 'auto' || mode === 'pipeline') {
    _ctxRestore(card);
    sec.style.display = 'block';
    card.style.display = 'block';
  } else if (mode === 'code_duo') {
    sec.style.display = 'none';
    if (duoSlot) _ctxMoveTo(card, duoSlot);
    card.style.display = 'block';
  } else {
    _ctxRestore(card);
    sec.style.display = 'none';
  }
  if (typeof _renderReasoningEffortUI === 'function') {
    _renderReasoningEffortUI(S.reasoningEffort);
  }
}

function setDuoCoderToolTier(v) {
  var tier = (v === 'lean' || v === 'core' || v === 'full') ? v : 'core';
  S.duoCoderToolTier = tier;
  var rad = document.getElementById('dct-tier-' + tier);
  if (rad && !rad.checked) rad.checked = true;
  postSettings({duo_coder_tool_tier: tier});
  updateDuoCoderTierHint();
}

var _CODER_TIER_HINTS = {
  lean: 'Lean (8) \u2014 Minimal core: read, edit, write, search, find, bash, tests.',
  core: 'Core (18) \u2014 Default: Lean + apply_patch, batch, AST/symbols, diagnostics.',
  full: 'Full (All) \u2014 All inline, browser, full LSP, scratchpad & MCP tools.',
};

function _activeOptionalsSuffix(directTier) {
  if (directTier === 'off') return '';
  var parts = [];
  if (S.duoWebsearch) parts.push('+Websearch');
  if (directTier !== 'readonly' && directTier !== 'mcp'
      && (typeof isGitToolsEffective === 'function' && isGitToolsEffective())) {
    parts.push('+Git');
  }
  if (directTier !== 'readonly' && directTier !== 'mcp' && S.subagentLite) {
    parts.push('+Subagent');
  }
  return parts.length ? (' [' + parts.join(' ') + ']') : '';
}

function updateDuoCoderTierHint() {
  var h = document.getElementById('duo-coder-tool-tier-hint');
  if (!h) return;
  var tier = (S.duoCoderToolTier || 'core').toString();
  if (!_CODER_TIER_HINTS[tier]) tier = 'core';
  h.textContent = (_CODER_TIER_HINTS[tier] || '') + _activeOptionalsSuffix();
  h.style.color = tier === 'full' ? 'var(--amber)' : 'var(--tx3)';
}

// DIRECT-TOOLS-TIER (2026-08-31): segmented control instead of dropdown. sets the
// state, persists and updates badge + tier hint + composer chip.
function setDirectToolsTier(v) {
  S.directToolsTier = v;
  postSettings({direct_tools_tier: v});
  updateDirectToolsTierHint();
  updateChatToolsBadge();
}

var _DIRECT_TIER_HINTS = {
  off:      'Off \u2014 pure chat, no tools.',
  readonly: 'Basic \u2014 ask_user, task_complete, datetime (+ Websearch if ON).',
  research: 'Research \u2014 read/search tools (no write, no exec).',
  mcp:      'MCP \u2014 Read + Websearch + MCP servers + send_file.',
  python:   'Python \u2014 Read + run_python (run code snippets).',
  full:     'Full \u2014 Read/Write/Exec incl. edit_file + run_bash. Needed for real coding requests.',
};

// TIER-LABEL (2026-09-02): display names for the stored tier values.
var _TIER_LABEL = { off: 'Off', readonly: 'Basic', research: 'Research', mcp: 'MCP', python: 'Python', full: 'Full' };

function updateDirectToolsTierHint() {
  var h = document.getElementById('direct-tools-tier-hint');
  if (!h) return;
  var tier = (S.directToolsTier || 'readonly').toString();
  h.textContent = (_DIRECT_TIER_HINTS[tier] || '') + _activeOptionalsSuffix(tier);
  if (tier === 'full') h.style.color = 'var(--amber)';
  else if (tier === 'off') h.style.color = 'var(--tx3)';
  else h.style.color = 'var(--tx3)';
}

function updateChatToolsBadge() {
  var b = document.getElementById('h-tools');
  if (!b) return;
  var tier = (S.directToolsTier || 'readonly').toString();
  var active = (S.mode === 'simple' || S.mode === 'auto')
    && S.directToolsEnabled && tier !== 'off';
  b.classList.toggle('off', !active);
  if (!active) { b.style.display = 'none'; b.textContent = ''; }
  else { b.textContent = '\u2699 Tools: ' + (_TIER_LABEL[tier] || tier); b.style.display = ''; }
}

// MODUS-BESCHREIBUNG (2026-08-31): Ein-Zeilen-Text unter den Mode-Buttons.
var _MODE_DESC = {
  auto:      'Auto \u2014 the decider picks direct or agentic (see Routing below). Tool requests run in the Direct chat loop (when Chat Tools on).',
  simple:    'Chat (Direct) \u2014 fast single-model chat. Chat Tools (read/python/web) available.',
  pipeline:  'Pipeline \u2014 multi-agent: Analyst \u2192 Refiner \u2192 Critic \u2192 Synthesizer.',
  automap:   'AutoMap \u2014 heuristic scorer picks a model per task type.',
  code_duo:  'Code \u2014 coding mode with Agentic tool loop or Critic-Duo review (optional Planner). Models load on demand.',
};
// ROUTING (2026-10-21): the routing card lives under MODE in the Agents tab.
// It only matters while the mode is "auto", so it is hidden otherwise.
function updateRoutingVisibility() {
  var box = document.getElementById('mode-routing');
  if (!box) return;
  var on = (S.mode === 'auto');
  box.style.display = on ? 'block' : 'none';
  if (on) renderRoutingStatus();
}

function updateModeDesc() {
  var el = document.getElementById('mode-desc');
  if (el) {
    var _txt = _MODE_DESC[S.mode] || '';
    // UX (2026-10-24): auto must not promise a decider that ships off —
    // say what actually routes, and where to change it.
    if (S.mode === 'auto') {
      _txt = (S.routingDecider === 'on')
        ? 'Auto — the decider (local encoder) routes each message to direct or agentic. Tool requests run in the Direct chat loop (when Chat Tools on).'
        : 'Auto — keyword rules route direct vs agentic. Enable the decider in Agents → Routing for smarter routing.';
    }
    el.textContent = _txt;
  }
  updateRoutingVisibility();
}

function updateAutomapAdvancedVisibility() {
  var panel = document.getElementById('automap-adv-panel');
  if (!panel) return;
  panel.style.display = S.mode === 'automap' ? 'block' : 'none';
}

function updateAutomapAdvancedInterlocks() {
  var duoOn = !!S.automapCodeDuoEnabled;
  var preOn = !!S.automapDuoPreExplore;
  var sub = document.getElementById('automap-duo-sub');
  if (sub) sub.classList.toggle('disabled', !duoOn);

  var preToggle = document.getElementById('automap-duo-preexplore-toggle');
  var parToggle = document.getElementById('automap-duo-parallel-toggle');
  if (preToggle) preToggle.disabled = !duoOn;
  if (parToggle) parToggle.disabled = !duoOn || !preOn;

  if (!duoOn) {
    S.automapDuoPreExplore = false;
    S.automapDuoParallelPreexplore = false;
    if (preToggle) preToggle.checked = false;
    if (parToggle) parToggle.checked = false;
  }
  if (duoOn && !preOn) {
    S.automapDuoParallelPreexplore = false;
    if (parToggle) parToggle.checked = false;
  }
}

function setAutomapCodeDuo(on) {
  // 1.4.1: UI-only master switch. It used to persist automap_code_duo_enabled,
  // which no engine code ever read, and to switch the dead automap twins off.
  S.automapCodeDuoEnabled = !!on;
  if (!S.automapCodeDuoEnabled) {
    S.automapDuoPreExplore = false;
    S.automapDuoParallelPreexplore = false;
  }
  updateAutomapAdvancedInterlocks();
  updateWebsearchHint();
}

function setAutomapDuoPreExplore(on) {
  S.automapDuoPreExplore = !!on;
  // 1.4.1: the engine reads duo_pre_explore (the automap_* twin was dead)
  postSettings({duo_pre_explore: S.automapDuoPreExplore});
  if (!S.automapDuoPreExplore) {
    S.automapDuoParallelPreexplore = false;
    postSettings({
      duo_parallel_preexplore: false,
    });
  }
  updateAutomapAdvancedInterlocks();
  updateWebsearchHint();
}

function setAutomapDuoParallel(on) {
  S.automapDuoParallelPreexplore = !!on;
  postSettings({duo_parallel_preexplore: S.automapDuoParallelPreexplore});
  updateAutomapAdvancedInterlocks();
}

function setAutomapPipelineWebsearch(on) {
  S.automapPipelineWebsearch = !!on;
  postSettings({pipeline_websearch_enabled: S.automapPipelineWebsearch});
  updateWebsearchHint();
}

function updateCriticToolsHint() {
  var h = document.getElementById('duo-critic-tools-hint');
  if (h) h.style.display = S.duoCriticTools ? 'block' : 'none';
}
function checkWebsearchStatus() {
  var dot = document.getElementById('ws-status-dot');
  var ad = document.getElementById('automap-ws-status-dot');
  var led = document.getElementById('ws-status-led');
  var _set = function(d, txt, color) {
    if (!d) return;
    d.textContent = txt;
    d.style.textDecoration = 'none';
    d.style.color = color || '';
  };
  var _setLed = function(cls) {
    if (led) led.className = 'tools-led ' + (cls || 'is-off');
  };
  _set(dot, 'Checking...', '');
  _set(ad, 'Checking...', '');
  _setLed('is-off');
  fetch('/websearch/status').then(r=>r.json()).then(d=>{
    // STATUS-FIX (2026-08-12): the real search-engine config is checked too —
    // the display now distinguishes disabled/unreachable/HTTP-error/ok.
    if (d.ok) {
      _set(dot, 'OK', '#60c080');
      _set(ad, 'Reachable OK', '#60c080');
      _setLed('is-on');
    } else if (d.reason === 'disabled') {
      _set(dot, 'Disabled — turn on websearch toggle!', '#c0a060');
      _set(ad, 'Disabled', '#c0a060');
      _setLed('is-warn');
    } else if (d.reason === 'search_http' && d.status === 400) {
      _set(dot, 'ERROR: SearXNG replies 400 - check engine config', '#d05050');
      _set(ad, 'SearXNG 400', '#d05050');
      _setLed('is-err');
    } else if (d.reason === 'search_error') {
      _set(dot, 'Search error: ' + (d.detail || d.reason), '#d05050');
      _set(ad, 'Search error', '#d05050');
      _setLed('is-err');
    } else {
      _set(dot, 'Unreachable (' + (S.searxngHost || 'localhost:8888') + ')', '#d05050');
      _set(ad, 'Unreachable', '#d05050');
      _setLed('is-err');
    }
  }).catch(()=>{
    _set(dot, 'Error — server unreachable?', '#d05050');
    _set(ad, 'Error', '#d05050');
    _setLed('is-err');
  });
}

function updateForceHeaderBadge() {
  const badge = document.getElementById('force-badge');
  if (!badge) return;
  const hasSkips   = Object.keys(S.skippedAgents).length > 0;
  const hasForce   = S.forcedComplexity !== 'auto';
  badge.classList.toggle('on', hasSkips || hasForce);
}


// -- Constraint Mode --------------------------------------------
function setConstraintMode(enabled) {
  S.constraintMode = enabled;
  postSettings({constraint_mode: enabled});
}

// -- Agent Map (Header) -----------------------------------------
async function loadAgentMap() {
  try {
    const d = await (await fetch('/automap/current')).json();
    S.currentAssignments = d.assignments || {};
    renderAgentMap();
    if (typeof updateModelsChip === 'function') updateModelsChip();
    if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  } catch(e) {
    buildAgentMapFromSettings();
  }
}

function buildAgentMapFromSettings() {
  document.querySelectorAll('select[data-agent]').forEach(function(sel) {
    const agent = sel.dataset.agent;
    const model = sel.value || '';
    S.currentAssignments[agent] = {model: model, display: model, tags: [], vision: false, thinking: false};
  });
  renderAgentMap();
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
}

const AGENT_COLORS = {
  analyst: '#4878c0', refiner: '#3a9960', critic: '#b04040',
  synthesizer: '#e09030', direct: '#8858c0', judge: '#7a8fa8'
};

function renderAgentMap(highlightAgents) {
  const c = document.getElementById('h-agentmap');
  if (!c) return;
  const order = ['judge','analyst','refiner','critic','synthesizer','direct','vision'];
  // 2-char labels — more readable than 5-char truncation
  const AGENT_SHORT = {
    judge:'JD', analyst:'AN', critic:'CR', refiner:'RF', synthesizer:'SY', direct:'DI', vision:'VI'
  };
  // model → short display form
  function shortModel(m) {
    if (!m) return '—';
    return m
      .replace(':latest','')
      .replace('granite4','g4')
      .replace('granite3.2-vision','g3v')
      .replace('gemma3','gm3')
      .replace('qwen3.5','q3.5')
      .replace('qwen3-vl','q3vl')
      .replace('qwen3','q3')
      .replace('ministral-3','min3')
      .replace('rnj-1','rnj')
      .replace('-thinking','⚡')
      .replace('-think','*');
  }
  c.innerHTML = '';
  order.forEach(function(agent) {
    const info = S.currentAssignments[agent];
    if (!info || !info.model) return;
    const chip = document.createElement('div');
    chip.className = 'amap-chip';
    if (highlightAgents && highlightAgents.includes(agent)) chip.classList.add('automap-active');

    // Colored dot
    const dot = document.createElement('div');
    dot.className = 'amap-dot';
    dot.style.background = AGENT_COLORS[agent] || '#7a8fa8';

    // Agent label (2 chars)
    const agentEl = document.createElement('div');
    agentEl.className = 'amap-agent';
    agentEl.textContent = AGENT_SHORT[agent] || agent.slice(0,2).toUpperCase();

    // Model name (shortened)
    const modelEl = document.createElement('div');
    modelEl.className = 'amap-model';
    modelEl.textContent = shortModel(info.model);
    // Tooltip: full model + tags
    const tagStr = info.tags && info.tags.length ? ' [' + info.tags.join(',') + ']' : '';
    modelEl.title = agent + ': ' + info.model + tagStr;

    chip.appendChild(dot);
    chip.appendChild(agentEl);
    chip.appendChild(modelEl);

    // Capability badges — only thinking (vision is implicit in model name)
    if (info.thinking) {
      const t = document.createElement('span');
      t.className = 'amap-tag thinking'; t.textContent = '⚡';
      t.title = 'Thinking model';
      chip.appendChild(t);
    }
    c.appendChild(chip);
  });
}

// -- Automap ----------------------------------------------------
async function runAutomap(query, images) {
  try {
    const res = await fetch('/automap/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: query, images: images})
    });
    const data = await res.json();
    return data;
  } catch(e) {
    return null;
  }
}

async function applyAutomap(assignments) {
  try {
    await fetch('/automap/apply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({assignments: assignments})
    });
    Object.keys(assignments).forEach(function(agent) {
      if (!S.currentAssignments[agent]) S.currentAssignments[agent] = {};
      S.currentAssignments[agent].model = assignments[agent];
    });
    S._lastDirectRunModel = '';
    await loadAgentMap();
  } catch(e) {}
}

// Nach Automap-Query: Dropdown mit ⟳-Label aktualisieren
function updateAgentDropdownAutomap(agent, automapModel) {
  const sel = document.querySelector('select[data-agent="' + agent + '"]');
  if (!sel) return;
  let opts = '<option value="' + esc(automapModel) + '" style="color:#4878c0;font-weight:600" selected>'
           + '⟳ ' + esc(automapModel) + ' (Automap)</option>';
  if (S.models.filter(m => m !== automapModel).length)
    opts += '<option disabled>────────────</option>';
  S.models.forEach(function(m) {
    if (m !== automapModel) opts += '<option value="' + esc(m) + '">' + esc(m) + '</option>';
  });
  sel.innerHTML = opts;
  sel.value = automapModel;
  sel.dataset.cur = automapModel;
}

function showAutomapEvent(taskType, assignments) {
  const badge = document.getElementById('h-tasktype');
  if (badge) {
    badge.textContent = taskType.toUpperCase();
    badge.style.display = 'inline-block';
  }
  const changed = Object.keys(assignments);
  changed.forEach(function(agent) {
    if (S.currentAssignments[agent]) {
      S.currentAssignments[agent].model = assignments[agent];
    } else {
      S.currentAssignments[agent] = {model: assignments[agent], display: assignments[agent], tags: [], vision: false, thinking: false};
    }
  });
  S._lastDirectRunModel = '';
  renderAgentMap(changed);
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  changed.forEach(function(agent) {
    updateAgentDropdownAutomap(agent, assignments[agent]);
  });
}

// -- Models -----------------------------------------------------
async function loadModels() {
  // Async retry with backoff: Windows startup takes several seconds until Ollama is ready.
  // After a successful retry: re-render the agent cards so dropdowns show all models.
  async function _fetchModels() {
    try {
      const d = await (await fetch('/models')).json();
      // show server-side errors in the console
      if (d.error) console.warn('[/models] Server error:', d.error);
      // cache profiles for agent dropdowns
      S._mmprojCache = {};
      if (d.profiles && d.profiles.length) {
        S.modelProfiles = {};
        d.profiles.forEach(function(p) { if (p.name) S.modelProfiles[p.name] = p; });
        if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
      }
      if (d.vision_preprocessing_allowlist) {
        S.visionAllowlist = new Set(d.vision_preprocessing_allowlist);
        populateVisionAgentModelSel();
      }
      return d.models || [];
    } catch(e) { console.error('[/models] fetch error:', e); return []; }
  }

  var wasEmpty = !S.models.length;
  S.models = await _fetchModels();
  if (S.models.length) { refreshAllSelects(); populateDuoModelGroup(); return; }

  // Retry with growing wait times: 1s → 3s → 8s → 15s
  var delays = [1000, 3000, 8000, 15000];
  for (var ri = 0; ri < delays.length; ri++) {
    // dot shows warn status during retry
    var dot = document.getElementById('h-dot');
    if (dot && ri >= 1) dot.className = 'warn';
    await new Promise(function(r) { setTimeout(r, delays[ri]); });
    var m2 = await _fetchModels();
    if (m2.length) {
      S.models = m2;
      refreshAllSelects();
      // IMPORTANT: re-render agent cards if they were built with empty S.models before
      // (happens on slow Windows startup where Ollama becomes ready only after the server)
      if (wasEmpty) {
        try {
          var sResp = await fetch('/settings');
          var sData = await sResp.json();
          if (sData.agents && Object.keys(sData.agents).length) {
            renderAgentCards(sData.agents);
          }
        } catch(e) {}
      }
      if (dot) dot.className = 'on';
      return;
    }
  }
  // All retries failed → show the error
  var dot2 = document.getElementById('h-dot');
  if (dot2) { dot2.className = 'err'; dot2.title = 'Backend unreachable — console (F12) for details'; }
  var cardsEl2 = document.getElementById('agent-cards');
  if (cardsEl2 && cardsEl2.innerHTML.includes('Load models')) {
    cardsEl2.innerHTML = '<div class="empty" style="color:#e05">&#9888; No models loaded.<br><span style="font-size:9px">F12 → Console for error details. Check whether models.json exists and llama-server starts.</span></div>';
  }
}

// FIX: modelOpts ensures the current value is always present as an option,
// even if Ollama does not (yet) list the model.
function modelOpts(selected) {
  let opts = '';
  // ensure the current value is always an option
  if (selected && !S.models.includes(selected)) {
    opts += '<option value="' + esc(selected) + '">' + esc(selected) + '</option>';
  }
  if (!S.models.length) {
    if (!selected) opts += '<option value="">-- No model found --</option>';
    return opts;
  }
  opts += S.models.map(function(m) {
    // VISION-UI FIX: mark vision-capable models purple (also in agent dropdowns)
    const isVL = ['vl','llava','vision','moondream','minicpm'].some(function(v){
      return m.toLowerCase().includes(v);
    });
    const style = isVL ? ' style="color:#8858c0"' : '';
    return '<option value="' + esc(m) + '"' + (m === selected ? ' selected' : '') + style + '>' + esc(m) + '</option>';
  }).join('');
  return opts;
}

function refreshAllSelects() {
  // agent model selects — keep the automap label if present
  document.querySelectorAll('select[data-agent]').forEach(function(sel) {
    const agent = sel.dataset.agent;
    const cur   = sel.dataset.cur || sel.value;
    const reg   = (S.currentAssignments[agent] || {}).model || '';
    const followOpt = agent === 'duo_critic'
      ? '<option value=""' + (!cur ? ' selected' : '') + '>Follow Duo-Coder</option>'
      : '';
    if (reg && reg === cur) {
      // automap active: keep the label on top, rebuild the remaining options
      let opts = followOpt
               + '<option value="' + esc(reg) + '" style="color:#4878c0;font-weight:600" selected>'
               + '⟳ ' + esc(reg) + ' (active)</option>';
      if (S.models.filter(m => m !== reg).length)
        opts += '<option disabled>────────────</option>';
      S.models.forEach(function(m) {
        if (m !== reg) opts += '<option value="' + esc(m) + '">' + esc(m) + '</option>';
      });
      sel.innerHTML = opts;
      sel.value = reg;
    } else {
      sel.innerHTML = followOpt + modelOpts(cur);
      if (cur || agent === 'duo_critic') sel.value = cur;
    }
  });
  // Global model select
  const g = document.getElementById('g-model');
  if (g) { const c = g.value || (S.models[0] || ''); g.innerHTML = modelOpts(c); if (c) g.value = c; }
  // Vision model select
  // DROPDOWN-RESET-FIX: refreshAllSelects() must not destroy the optgroup structure of loadVisionConfig().
  // If optgroups exist → only restore the selected value, touching the structure is forbidden.
  const v = document.getElementById('vision-model-sel');
  if (v) {
    const c = v.value || '';
    if (v.querySelector('optgroup')) {
      // keep loadVisionConfig()'s structure intact — only preserve the selection
      if (c) { v.value = c; if (v.value !== c) v.value = ''; }
    } else {
      // no optgroups yet (e.g. loadVisionConfig not run) → flat list as before
      v.innerHTML = '<option value="">-- Auto --</option>' + modelOpts(c);
      if (c) v.value = c;
    }
  }
  // Manual load select (Models tab)
  const ml = document.getElementById('mm-load-sel');
  if (ml) { const c = ml.value || ''; ml.innerHTML = modelOpts(c); if (c) ml.value = c; }
  // Config model select
  const cm = document.getElementById('cfg-model-sel');
  if (cm) { const c = cm.value || ''; cm.innerHTML = modelOpts(c); if (c) cm.value = c; }
  // Special agent selects
  const isel = document.getElementById('intent-model-sel');
  if (isel) {
    const rec = ['granite-4.1:3b','granite4:1b','qwen3.5:2b'].find(m => S.models.includes(m));
    let o = '<option value="">-- No model --</option>';
    if (rec && rec !== S.intentModel) {
      o += '<option value="' + esc(rec) + '" style="color:#20b0a0;font-weight:600">★ ' + esc(rec) + ' (Recommended)</option>';
      o += '<option disabled>────────────</option>';
    }
    S.models.forEach(function(m) {
      if (m === rec && m !== S.intentModel) return;
      o += '<option value="' + esc(m) + '"' + (m === S.intentModel ? ' selected' : '') + '>' + esc(m) + '</option>';
    });
    isel.innerHTML = o;
    if (S.intentModel) isel.value = S.intentModel;
  }
  // MoE model dropdowns follow the actually installed model set.
  rebuildMoeModelDropdowns();
}

async function loadSettings() {
  try {
    const s = await (await fetch('/settings')).json();

    S.iters          = s.max_iterations || 2;
    S.mode           = (s.mode === 'automap' ? 'auto'
                        : (s.mode === 'chat' ? 'simple' : (s.mode || 'auto')));
    S.activePreset   = s.active_preset || '';
    S.constraintMode = s.constraint_mode !== false;
    // OPT-IN (2026-10-18): prefetch is a VRAM gamble on a tight budget, so the
    // default is OFF and only an explicit true turns it on.
    S.smartPreload   = s.smart_preload_enabled === true;

    _setPresetHeader();
    document.getElementById('iters-in').value = S.iters;
    document.getElementById('h-iters-val').textContent = S.iters;
    document.getElementById('cfl-toggle').checked = S.constraintMode;

    // Auto-save chats (on by default, can be turned off)
    S.chatAutosave = s.chat_autosave_enabled !== false;
    var _chatAutosaveEl = document.getElementById('chat-autosave-toggle');
    if (_chatAutosaveEl) _chatAutosaveEl.checked = S.chatAutosave;
    S.imagePersist = s.image_uploads_persistent === true;
    var _imgPersistEl = document.getElementById('image-persist-toggle');
    if (_imgPersistEl) _imgPersistEl.checked = S.imagePersist;

    // Image → pipeline toggle
    var imgPipeEl = document.getElementById('img-pipeline-toggle');
    if (imgPipeEl) imgPipeEl.checked = s.image_desc_full_pipeline || false;
    // Duo: load all options from the server
    if (s.duo_pair) {
      S.duoPair = s.duo_pair;
      syncDuoPairSelectWithState();
    }
    var dtrSel = document.getElementById('duo-tool-rounds-sel');
    if (dtrSel) {
      S.duoToolRounds = parseInt(s.duo_tool_rounds ?? 50);
      // a saved 50 (the new default) had no option and showed blank
      var _hasOpt = false;
      for (var _oi = 0; _oi < dtrSel.options.length; _oi++) {
        if (dtrSel.options[_oi].value === String(S.duoToolRounds)) _hasOpt = true;
      }
      if (!_hasOpt) {
        var _o = document.createElement('option');
        _o.value = String(S.duoToolRounds);
        _o.textContent = String(S.duoToolRounds);
        dtrSel.appendChild(_o);
      }
      dtrSel.value = String(S.duoToolRounds);
    }
    S.duoCoderToolTier = (s.duo_coder_tool_tier || 'core').toString();
    if (S.duoCoderToolTier !== 'lean' && S.duoCoderToolTier !== 'core'
        && S.duoCoderToolTier !== 'full' && S.duoCoderToolTier !== 'mcp'
        && S.duoCoderToolTier !== 'readonly') {
      S.duoCoderToolTier = 'core';
    }
    var _dctRad = document.getElementById('dct-tier-' + S.duoCoderToolTier);
    if (_dctRad) _dctRad.checked = true;
    updateDuoCoderTierHint();
    var dpipeEl = document.getElementById('duo-pipeline-toggle');
    if (dpipeEl) { S.duoUsePipeline = s.duo_use_pipeline || false; dpipeEl.checked = S.duoUsePipeline; }
    var dctEl = document.getElementById('duo-critic-tools-toggle');
    if (dctEl) { S.duoCriticTools = s.duo_critic_tools || false; dctEl.checked = S.duoCriticTools; updateCriticToolsHint(); }
    var dcEl = document.getElementById('duo-chunking-toggle');
    if (dcEl) { S.duoChunking = s.duo_chunking || false; dcEl.checked = S.duoChunking; }
    var dtfChunkEl = document.getElementById('duo-test-feedback-chunk-toggle');
    if (dtfChunkEl) { S.duoTestFeedbackChunk = s.duo_test_feedback_chunk || false; dtfChunkEl.checked = S.duoTestFeedbackChunk; }
    var dtfFinalEl = document.getElementById('duo-test-feedback-final-toggle');
    if (dtfFinalEl) { S.duoTestFeedbackFinal = s.duo_test_feedback_final || false; dtfFinalEl.checked = S.duoTestFeedbackFinal; }
        S.duoPlannerEnabled = s.duo_planner_enabled !== undefined
    ? s.duo_planner_enabled
    : (!!(s.duo_soft_planner) || S.duoChunking);  // migration: old value as default
        var _plannerTog = document.getElementById('duo-planner-toggle');
        if (_plannerTog) { _plannerTog.checked = S.duoPlannerEnabled; }
    S.duoCodingMode = s.duo_coding_mode !== false;   // default true
    S.duoPreExplore  = s.duo_pre_explore   || false;
    updatePreExploreAdv();
    S.duoPreExploreMaxTools = s.duo_pre_explore_max_tools || 12;
    S.duoPassFiles = s.duo_pass_explore_files || 'touched';
    updatePassFilesButtons();
    var dpetEl = document.getElementById('duo-preexplore-toggle');
    if (dpetEl) dpetEl.checked = S.duoPreExplore;
    var dpemEl = document.getElementById('duo-preexplore-max-tools');
    if (dpemEl) dpemEl.value = S.duoPreExploreMaxTools;
    // UI-WORKSPACE (2026-08-07): fill the field with the stored value
    S.workspace = (s.workspace || '').toString();
    var _wsEl = document.getElementById('workspace-input');
    if (_wsEl) _wsEl.value = S.workspace;
    // WORKSPACE-FORCE (2026-08-25): load the toggle status (default ON)
    S.wsForceUi = s.workspace_force_ui !== false;
    var _wsfTog = document.getElementById('ws-force-toggle');
    if (_wsfTog) _wsfTog.checked = S.wsForceUi;
    // Parallel pre-explore
    S.duoParallelPreexplore = !!s.duo_parallel_preexplore;
    var _dppTog = document.getElementById('duo-parallel-preexplore-toggle');
    if (_dppTog) _dppTog.checked = !!S.duoParallelPreexplore;
    updatePreExploreAdv();
    updateParallelPreexploreWrap();
    S.duoAgenticMode = s.duo_agentic_mode || false;
        S.duoAgenticThinking = s.duo_agentic_thinking || false;
        // FIX: load the persisted original preference before chunking (survives page reload)
        // If _thinking_before_chunking exists, duo_agentic_thinking may be corrupted
        // by the old bug — we overwrite it with the original value
        if (typeof s._thinking_before_chunking === 'boolean') {
            S._thinkingBeforeChunking = s._thinking_before_chunking;
            S.duoAgenticThinking = s._thinking_before_chunking; // original preference instead of the forced value
        }
        var _datTog = document.getElementById('duo-agentic-thinking-toggle');
        if (_datTog) _datTog.checked = S.duoAgenticThinking;
        S.duoThinkingPerChunk = s.duo_thinking_per_chunk || false;
        var _dtpcTog = document.getElementById('duo-thinking-per-chunk-toggle');
        if (_dtpcTog) _dtpcTog.checked = S.duoThinkingPerChunk;
    // Tool thinking: toggle "always" + segmented control (off/balanced/on-crit fail)
  var _ttmRaw = normalizeToolThinkingMode(s.duo_coder_tool_thinking_auto_mode || 'off');
  if (s.duo_coder_tool_thinking && _ttmRaw === 'off') _ttmRaw = 'on_fail'; // legacy migration
  S.duoToolThinkingAlways = (_ttmRaw === 'always' || s.duo_coder_tool_thinking === true);
  S.duoToolThinkingMode = (_ttmRaw === 'always') ? 'on_fail' : _ttmRaw;
  S.duoToolThinkingEnabled = (S.duoToolThinkingMode !== 'off');
  var _ttTog = document.getElementById('duo-tool-thinking-toggle');
  if (_ttTog) _ttTog.checked = S.duoToolThinkingAlways;
  var _ttmRb = document.getElementById('ttm_' + S.duoToolThinkingMode);
  if (_ttmRb) _ttmRb.checked = true;
  S.reasoningEffort = _normalizeReasoningEffort(s.reasoning_effort || 'auto');
  S.plannerReasoningEffort = _normalizeReasoningEffort(s.planner_reasoning_effort || 'auto');
  _renderReasoningEffortUI(S.reasoningEffort);
    // UNTIL-FINISHED ON BY DEFAULT (2026-10-27, owner): a coding run should
    // finish the job. An explicit false from settings still wins.
    S.duoUntilFinished = s.until_finished !== false;
    var _dufTog = document.getElementById('duo-until-finished-toggle');
    if (_dufTog) _dufTog.checked = S.duoUntilFinished;
    S.askUserTimeoutSeconds = s.ask_user_timeout_until_finished_seconds || 300;
    S.askUserMaxPer10min = s.ask_user_max_per_10min || 5;
    S.askUserAutoAnswer = s.ask_user_auto_answer || 'Use best judgment, document decision in commit message.';
    _syncAskUserAutoProceedUI(!!s.ask_user_auto_proceed);
    // Ensure chunk-dependent toggles are correctly shown/hidden.
    // persist=false: a render must never write derived state back (see
    // onChunkingChange header — this post reverted saved values on reload).
    onChunkingChange(false);
    // Until-finished is always ∞ — no cap field anymore
        // restore chunking state
        onChunkingChange(false);
    updateAgenticCombinedWarn();
    setDuoSubMode(S.duoAgenticMode ? 'agentic' : 'critic', {persist: false});
    updateComplexityVisibility();
    var dmtrEl = document.getElementById('duo-max-tool-rounds');
    if (dmtrEl) dmtrEl.value = s.duo_max_tool_rounds || 64;
    var dmtrCapEl = document.getElementById('duo-max-tool-rounds-runtime-cap');
    if (dmtrCapEl) dmtrCapEl.value = s.duo_max_tool_rounds_runtime_cap || 300;
    var dpmsEl = document.getElementById('duo-planner-max-steps');
    if (dpmsEl) dpmsEl.value = typeof s.duo_planner_max_steps === 'number' ? s.duo_planner_max_steps : 0;
    S.duoProfileSpeedModel = (s.duo_profile_speed_model || 'qwen3.5:4b-mtp');
    S.duoProfileQualityModel = (s.duo_profile_quality_model || 'qwen3.5:9b-mtp');
    updateDuoPairHint();
    S.duoCtxAgentic = parseInputAsOptionalInt(s.duo_coder_ctx_agentic, 16384);
    var dctxAgEl = document.getElementById('duo-ctx-agentic');
    if (dctxAgEl) dctxAgEl.value = S.duoCtxAgentic != null ? S.duoCtxAgentic : 16384;
    S.duoCtxNormal = parseInputAsOptionalInt(s.duo_coder_ctx_normal, 8192);
    var dctxNoEl = document.getElementById('duo-ctx-normal');
    if (dctxNoEl) dctxNoEl.value = S.duoCtxNormal != null ? S.duoCtxNormal : 8192;
    // Planner ctx (separate)
    S.duoCtxPlanner = parseInputAsOptionalInt(s.duo_planner_ctx_target, 16384);
    var dctxPlEl = document.getElementById('duo-ctx-planner');
    if (dctxPlEl) dctxPlEl.value = S.duoCtxPlanner != null ? S.duoCtxPlanner : 16384;
    // Critic ctx (separate, only critic-duo)
    S.duoCtxCritic = parseInputAsOptionalInt(s.duo_critic_ctx, 8192);
    // Fallback chain for Critic: explicit critic_ctx -> coder_ctx_normal -> 8192
    if (S.duoCtxCritic == null) S.duoCtxCritic = parseInputAsOptionalInt(s.duo_coder_ctx_normal, 8192);
    var dctxCrEl = document.getElementById('duo-ctx-critic');
    if (dctxCrEl) dctxCrEl.value = S.duoCtxCritic != null ? S.duoCtxCritic : 8192;
    // Planner = coder toggle
    S.duoPlannerUseCoderCtx = (s.duo_planner_use_coder_ctx !== false);
    var plUseCoderEl = document.getElementById('duo-planner-use-coder-ctx');
    if (plUseCoderEl) plUseCoderEl.checked = S.duoPlannerUseCoderCtx;
    // apply the FULL toggle state (disabled + value + opacity + ctx-scope
    // hint) — the old inline copy only half-applied it, leaving the planner
    // ctx select greyed until the checkbox was toggled once by hand.
    togglePlannerUseCoderCtx(S.duoPlannerUseCoderCtx, false);
    // Planner / coder model
    S.duoPlannerModel = s.duo_planner_model || '';
    // PLANNER-MAX-TOKENS (0.99.2): visible output budget (0 = context as limit)
    S.duoPlannerMaxTokens = parseInt(s.duo_planner_max_tokens) || 8000;
    var dpmtEl = document.getElementById('duo-planner-maxtokens-inp');
    if (dpmtEl) dpmtEl.value = S.duoPlannerMaxTokens;
    S.duoCoderModel = s.duo_coder_model || (s.agents && s.agents.duo_coder && s.agents.duo_coder.model) || '';
    S.duoCriticModel = s.duo_critic_model || (s.agents && s.agents.duo_critic && s.agents.duo_critic.model) || '';
    var _moeRaw = s.moe_cpu_experts;
    S.moeCpuExpertsMap = (typeof _moeRaw === 'object' && _moeRaw !== null && !Array.isArray(_moeRaw)) ? _moeRaw : {};
    var _moeCacheEl = document.getElementById('moe-cache-mib');
    if (_moeCacheEl) _moeCacheEl.value = parseInt(s.moe_cache_mib) || 0;
    var _fitEl = document.getElementById('llama-fit-toggle');
    if (_fitEl) _fitEl.checked = !!s.llama_fit;
    var _fitTargetEl = document.getElementById('llama-fit-target');
    if (_fitTargetEl) _fitTargetEl.value = parseInt(s.llama_fit_target_mib) || 0;
    var _cfgCacheEl = document.getElementById('config-moe-cache-mib');
    if (_cfgCacheEl) _cfgCacheEl.value = parseInt(s.moe_cache_mib) || 0;
    var _cfgFitEl = document.getElementById('config-llama-fit-toggle');
    if (_cfgFitEl) _cfgFitEl.checked = !!s.llama_fit;
    var _cfgFitTargetEl = document.getElementById('config-llama-fit-target');
    if (_cfgFitTargetEl) _cfgFitTargetEl.value = parseInt(s.llama_fit_target_mib) || 0;
    S.moeExpertDefaults = s.moe_expert_defaults || {};
    S.moeAutodetect = s.moe_autodetect || {};
    S.moePins = s.moe_pins || {};
    S.ctxDefaults = s.ctx_defaults || {};
    var configMoeWrap = document.getElementById('config-moe-experts');
    if (configMoeWrap) configMoeWrap.style.display = 'block';
    rebuildMoeModelDropdowns();
    updateMoeVisibility();
    // LSP + AUTO-LINT (2026-11-01): the card was settings.json-only before.
    S.lspEnabled = s.lsp_enabled !== false;
    S.lspAutoDiag = s.lsp_auto_diagnostics !== false;
    var _lspT = document.getElementById('lsp-enabled-toggle');
    if (_lspT) _lspT.checked = S.lspEnabled;
    var _lspA = document.getElementById('lsp-auto-diag-toggle');
    if (_lspA) _lspA.checked = S.lspAutoDiag;
    var _lspW = document.getElementById('lsp-wait-input');
    if (_lspW) _lspW.value = parseInt(s.lsp_diagnostics_wait_s, 10) || 5;
    var _lspI = document.getElementById('lsp-idle-input');
    if (_lspI) _lspI.value = parseInt(s.lsp_idle_timeout_s, 10) || 600;
    updateLspHint();
    S.llamaMlock = s.llama_mlock !== false;
    var mlockEl = document.getElementById('llama-mlock-toggle');
    if (mlockEl) mlockEl.checked = S.llamaMlock;
    var configMlockEl = document.getElementById('config-mlock-toggle');
    if (configMlockEl) configMlockEl.checked = S.llamaMlock;
    updateMlockHint();
    S.llamaCacheReuse = parseInt(s.llama_cache_reuse, 10);
    S.vramPreflightGate = s.vram_preflight_gate !== false;
    var _pfGateEl = document.getElementById('vram-preflight-gate-toggle');
    if (_pfGateEl) _pfGateEl.checked = S.vramPreflightGate;
    if (isNaN(S.llamaCacheReuse) || S.llamaCacheReuse < 0) S.llamaCacheReuse = 0;
    var cacheReuseEl = document.getElementById('llama-cache-reuse-input');
    if (cacheReuseEl) cacheReuseEl.value = S.llamaCacheReuse;
    updateMoeExpertDefaultHint();
    var dplmEl = document.getElementById('duo-planner-model-sel');
    if (dplmEl) dplmEl.value = S.duoPlannerModel || '';
    var dcmEl = document.getElementById('duo-coder-model-sel');
    if (dcmEl && S.duoCoderModel) dcmEl.value = S.duoCoderModel;
    var dcrmEl = document.getElementById('duo-critic-model-sel');
    if (dcrmEl) dcrmEl.value = S.duoCriticModel || '';
    // COMPRESSION-MODELL + FIX-FLAGS (2026-09-07)
    S.duoCompressModel = s.duo_compress_model || '';
    S.duoErrorRollup   = s.duo_error_rollup !== false;
    S.duoPinStaticMap  = s.duo_pin_static_map !== false;
    S.duoWriteGuard    = s.duo_write_guard_enabled !== false;
    S.duoNoopHint      = s.duo_noop_hint_enabled !== false;
    // WEDGE-HANDOFF: ON by default (audit 2026-10-24 - it converts the #1
    // small-model death spiral into a fresh-context fix agent). Mirror the
    // backend default so an absent key still shows the true state.
    S.duoWedgeHandoff  = s.duo_wedge_handoff_enabled !== false;
    S.duoPartialCompr  = s.duo_partial_compression === true;
    S.duoCompressLocal = s.duo_compress_local_only === true;
    // MCP client (beta): off unless enabled in settings.
    S.mcpClientEnabled = s.mcp_client_enabled === true;
    if (s.settings_rev !== undefined && s.settings_rev !== null) {
      var _loadedRev = parseInt(s.settings_rev, 10);
      if (!isNaN(_loadedRev) && _loadedRev >= 0) S.settingsRev = _loadedRev;
    }
    var _dcmsEl = document.getElementById('duo-compress-model-sel');
    if (_dcmsEl) {
      var _storedModel = (S.duoCompressModel && _dcmsEl.querySelector('option[value="' + S.duoCompressModel + '"]')) ? S.duoCompressModel : 'auto';
      _dcmsEl.value = _storedModel;
    }
    _syncFlagCheck('duo-error-rollup-toggle', S.duoErrorRollup);
    _syncFlagCheck('duo-pin-static-map-toggle', S.duoPinStaticMap);
    _syncFlagCheck('duo-write-guard-toggle', S.duoWriteGuard);
    _syncFlagCheck('duo-noop-hint-toggle', S.duoNoopHint);
    _syncFlagCheck('duo-wedge-handoff-toggle', S.duoWedgeHandoff);
    _syncFlagCheck('duo-partial-compression-toggle', S.duoPartialCompr);
    _syncFlagCheck('duo-compress-local-only-toggle', S.duoCompressLocal);
_syncFlagCheck('mcp-client-toggle', S.mcpClientEnabled);
    // Planner / Coder TTL
    S.duoPlannerTtl = parseInt(s.duo_planner_ttl_seconds || 0) || 0;
    S.duoCoderTtl = parseInt(s.duo_coder_ttl_seconds || 0) || 0;
    // CODER LIFETIME (2026-10-17): OFF = warm after the run (default), ON =
    // release the slot so the next prompt can load any model/mode.
    S.duoCoderAfterRun = (String(s.duo_coder_after_run || 'keep').toLowerCase() === 'evict') ? 'evict' : 'keep';
    // VOICE (1.4): opt-in. Nothing runs while stt_enabled is off - the card
    // only renders a status line and the controls stay inert.
    S.sttEnabled = s.stt_enabled === true;
    S.sttModel = String(s.stt_model || '');
    S.sttLanguage = String(s.stt_language || 'en');
    S.sttPreload = s.stt_preload === true;
    // TTS (1.4): opt-in, same rule - nothing happens while it is off.
    S.ttsEnabled = s.tts_enabled === true;
    S.ttsBackend = String(s.tts_backend || 'pocket');
    S.ttsVoice = String(s.tts_voice || 'anna');
    S.ttsSpeed = _clampTtsSpeed(s.tts_speed);
    S.tgVoiceIn = s.telegram_voice_in === true;
    S.tgImageIn = s.telegram_image_in !== false;
    var _tgImg = document.getElementById('tg-image-in-toggle');
    if (_tgImg) _tgImg.checked = S.tgImageIn;
    S.tgVoiceOut = s.telegram_voice_out === true;
    S.tgResultMode = String(s.telegram_result_mode || 'both');
    S.tgProgress = s.telegram_progress_msgs !== false;
    S.ttsPreloadRun = s.tts_preload_run !== false;
    S.ttsVolume = (typeof s.tts_volume === 'number')
      ? Math.max(0, Math.min(1, s.tts_volume)) : 1.0;
    // Read-aloud (frontend): which surfaces speak on their own.
    S.ttsReadDirect = s.tts_read_direct === true;
    S.ttsReadAgentic = s.tts_read_agentic === true;
    S.ttsReadAsk = s.tts_read_ask === true;
    S.ttsReadGate = s.tts_read_gate === true;
    // ROUTING (2026-10-21): wer entscheidet im auto-Modus
    S.routingDecider = s.routing_decider || 'off';
    S.routingJudge = s.routing_judge || 'on';
    S.routingPlanner = s.routing_planner || 'auto';
    S.routingThreshold = (typeof s.routing_decider_threshold === 'number')
      ? s.routing_decider_threshold : 0.55;
    var _rdSel = document.getElementById('routing-decider-sel');
    if (_rdSel) _rdSel.value = S.routingDecider;
    var _rjSel = document.getElementById('routing-judge-sel');
    if (_rjSel) _rjSel.value = S.routingJudge;
    var _rpSel = document.getElementById('routing-planner-sel');
    // The UI offers three choices (always / decider / never). Older settings can
    // still hold auto|heuristic|judge - show the closest option and keep the
    // stored value visible in the status line instead of silently rewriting it.
    var _plnThree = {always: 1, decider: 1, never: 1};
    S.routingPlannerLegacy = _plnThree[S.routingPlanner] ? '' : S.routingPlanner;
    if (_rpSel) _rpSel.value = _plnThree[S.routingPlanner] ? S.routingPlanner : 'decider';
    renderRoutingStatus();
    _syncFlagCheck('duo-coder-after-run-toggle', S.duoCoderAfterRun === 'evict');
    _syncFlagCheck('stt-enabled-toggle', S.sttEnabled);
    _syncFlagCheck('tts-enabled-toggle', S.ttsEnabled);
    if (typeof _syncSpeakButtons === 'function') _syncSpeakButtons();
    if (typeof _syncTtsCard === 'function') _syncTtsCard();
    _syncFlagCheck('stt-preload-toggle', S.sttPreload);
    if (typeof _voiceCardRefresh === 'function') _voiceCardRefresh();
    if (S.ttsEnabled && typeof voiceWarmup === 'function') setTimeout(voiceWarmup, 4000);
    if (typeof _syncTgVoice === 'function') _syncTgVoice();
    var _rms = document.getElementById('tg-result-mode-sel');
    if (_rms) _rms.value = S.tgResultMode || 'both';
    _syncFlagCheck('tg-progress-toggle', S.tgProgress);
    _syncFlagCheck('tts-preload-run-toggle', S.ttsPreloadRun);
    var _volInp = document.getElementById('tts-volume-inp');
    if (_volInp) _volInp.value = String(S.ttsVolume);
    var _volOut = document.getElementById('tts-volume-out');
    if (_volOut) _volOut.textContent = Math.round((typeof S.ttsVolume === 'number' ? S.ttsVolume : 1) * 100) + ' %';
    var _spdInp = document.getElementById('tts-speed-inp');
    if (_spdInp) _spdInp.value = String(S.ttsSpeed);
    var dpttlEl = document.getElementById('duo-planner-ttl');
    if (dpttlEl) dpttlEl.value = S.duoPlannerTtl;
    var dcttlEl = document.getElementById('duo-coder-ttl');
    if (dcttlEl) dcttlEl.value = S.duoCoderTtl;
    // Ctx-Cap: entfernt — Nutzer kontrolliert Context selbst
    S.duoVramGuard = 0;
    updateCtxScopeHint();
    var drtEl = document.getElementById('duo-read-timeout');
    if (drtEl) drtEl.value = s.duo_read_timeout || 300;
    // Pre-Explore / Planner Overrides
    S.duoPartitionMaxFiles = parseInt(s.duo_partition_max_files || 30) || 30;
    var dpmfEl = document.getElementById('duo-partition-max-files');
    if (dpmfEl) dpmfEl.value = S.duoPartitionMaxFiles;
    S.duoStaticMapChars = parseInt(s.duo_static_map_chars || 0) || 0;
    var dsmcEl = document.getElementById('duo-static-map-chars');
    if (dsmcEl) dsmcEl.value = S.duoStaticMapChars;
    S.duoCoderExploreChars = parseInt(s.duo_coder_explore_chars || 0) || 0;
    var dcecEl = document.getElementById('duo-coder-explore-chars');
    if (dcecEl) dcecEl.value = S.duoCoderExploreChars;
        // Websearch Toggles
    S.duoWebsearch      = s.duo_websearch_enabled      || false;
    S.pipelineWebsearch = s.pipeline_websearch_enabled || false;
    var _dwsEl = document.getElementById('duo-websearch-toggle');
    if (_dwsEl) _dwsEl.checked = S.duoWebsearch;
    var _pwsEl = document.getElementById('pipeline-websearch-toggle');
    if (_pwsEl) _pwsEl.checked = S.pipelineWebsearch;
    // Action approval gate (2026-09-18)
    S.duoActionApproval = s.duo_action_approval_enabled || false;
    var _aaEl = document.getElementById('duo-action-approval-toggle');
    if (_aaEl) _aaEl.checked = S.duoActionApproval;
    updateApprovalUI();
    // Websearch timeout values — single slider (v0.96.5)
    var _wsSlider = document.getElementById('ws-timeout-slider');
    var _wsV = document.getElementById('ws-timeout-v');
    if (_wsSlider) _wsSlider.value = parseFloat(s.duo_websearch_timeout_seconds || 20);
    if (_wsV) _wsV.textContent = parseFloat(s.duo_websearch_timeout_seconds || 20);
    // Direct Chat Tools (2026-08-31)
    S.directToolsEnabled = s.direct_tools_enabled !== false;
    S.directToolsTier = (s.direct_tools_tier || 'readonly').toString();
    S.directToolsRounds = parseInt(s.direct_tools_max_rounds || 12) || 12;
    var _dtEl = document.getElementById('direct-tools-toggle');
    if (_dtEl) _dtEl.checked = S.directToolsEnabled;
    var _dtTierRadio = document.getElementById('dt-tier-' + ((S.directToolsTier === 'research' || S.directToolsTier === 'mcp' || S.directToolsTier === 'python' || S.directToolsTier === 'full' || S.directToolsTier === 'off') ? S.directToolsTier : 'readonly'));
    if (_dtTierRadio) _dtTierRadio.checked = true;
    var _dtR = document.getElementById('direct-tools-rounds');
    var _dtRV = document.getElementById('direct-tools-rounds-v');
    if (_dtR) _dtR.value = S.directToolsRounds;
    if (_dtRV) _dtRV.textContent = S.directToolsRounds;
    updateDirectToolsHint();
    updateDirectToolsTierHint();
    updateChatToolsBadge();
    updateChatToolsSectionVisibility();
    updateReasoningCardVisibility();
    updatePassFilesButtons();

  // AutoMap Advanced
  S.automapCodeDuoEnabled = s.duo_pre_explore === true;  // 1.4.1: mirrors the real switch
  S.automapDuoPreExplore = s.duo_pre_explore === true;
  S.automapDuoParallelPreexplore = s.duo_parallel_preexplore === true;
  S.automapPipelineWebsearch = s.pipeline_websearch_enabled === true;
  var _amCode = document.getElementById('automap-code-duo-toggle');
  if (_amCode) _amCode.checked = S.automapCodeDuoEnabled;
  var _amPre = document.getElementById('automap-duo-preexplore-toggle');
  if (_amPre) _amPre.checked = S.automapDuoPreExplore;
  var _amPar = document.getElementById('automap-duo-parallel-toggle');
  if (_amPar) _amPar.checked = S.automapDuoParallelPreexplore;
  var _amPipeWs = document.getElementById('automap-pipeline-websearch-toggle');
  if (_amPipeWs) _amPipeWs.checked = S.automapPipelineWebsearch;
  updateAutomapAdvancedInterlocks();
  updateAutomapAdvancedVisibility();

    updateWebsearchHint();
    // Keep-Awake (2026-08-24)
    var _kaEl = document.getElementById('keepawake-toggle');
    if (_kaEl) _kaEl.checked = s.keep_awake_during_run !== false;  // default an
    // Subagent-lite (2026-08-24)
    S.subagentLite = !!s.subagent_lite_enabled;  // default off
    var _saTgl = document.getElementById('subagent-lite-toggle');
    if (_saTgl) _saTgl.checked = S.subagentLite;
    var _saLadder = document.getElementById('subagent-ladder-inp');
    if (_saLadder) _saLadder.value = (s.subagent_lite_model_ladder || []).join(', ');
    var _saCtx = document.getElementById('subagent-ctx-inp');
    if (_saCtx) _saCtx.value = s.subagent_lite_ctx_default || 8192;
    syncSubagentUI();
    // Tool Sandbox (2026-08-24)
    S.duoToolSandbox = s.duo_tool_sandbox !== false;  // default an
    var _sbTgl = document.getElementById('tool-sandbox-toggle');
    if (_sbTgl) _sbTgl.checked = S.duoToolSandbox;
    // Git Integration
    loadGitConfig(s);
    // LPM Toggle
    var lpmEl = document.getElementById('lpm-toggle');
    if (lpmEl) {
      lpmEl.checked = s.learning_preset_mode || false;
      document.getElementById('lpm-info').style.display = lpmEl.checked ? 'block' : 'none';
    }
    // Startup Preload Toggle
        var suEl = document.getElementById('startup-preload-toggle');
    if (suEl) suEl.checked = s.startup_preload_enabled === true;
    var jkaEl = document.getElementById('judge-keepalive-toggle');
    if (jkaEl) jkaEl.checked = s.judge_keepalive_enabled === true;
    // Analyst Sub-Toggle
    var pdEl = document.getElementById('pin-direct-toggle');
    if (pdEl) pdEl.checked = s.pin_direct_after_response === true;
    // Vision-Agent state
    S.visionAgentEnabled = s.vision_agent_enabled || false;
    S.visionAgentModel   = s.vision_agent_model   || '';
    S.visionAgentMode    = 'sequential';  // runner is sequential-only; radio hidden
    // PIPELINE-VISION: pass images directly to multimodal pipeline agents?
    S.pipelineVisionDirect = s.pipeline_vision_direct === true;
    // PIPELINE-VISION per role: which roles get raw images directly.
    S.pipelineVisionRoles = s.pipeline_vision_roles || {};
    _applyPipelineVisionRolesUI();
    // AGENT-CTX: per-agent context-size overrides from settings.ctx_overrides
    S.ctxOverrides = (s.ctx_overrides && s.ctx_overrides.roles) ? s.ctx_overrides.roles : {};
    var vaEl = document.getElementById('va-enabled-toggle');
    if (vaEl) vaEl.checked = S.visionAgentEnabled;
    // CODER ALWAYS-VISION (2026-10-06): preload the projector for the coder
    // so image steers don't pay the mid-run upgrade stall. Opt-in (VRAM).
    S.coderAlwaysVision = !!s.duo_coder_always_vision;
    // CODER IMAGES OFF (2026-10-07, owner): hard "coder takes no images" —
    // wins over the role checkbox and the always-vision toggle (backend
    // resolve_image_plan + steer drains honor it).
    S.coderImagesOff = !!s.duo_coder_images_off;
    var _cioEl = document.getElementById('coder-images-off-toggle');
    if (_cioEl) _cioEl.checked = S.coderImagesOff;
    // IMAGE-PROCESSING-MODE (2026-10-03): DERIVED, never persisted.
    S.imageMode = '';
    // DUO IMAGE PLAN (2026-10-01): sentinel None = derived client-side the
    // same way the backend does it (active vision cfg -> preprocess)
    S._duoImageModeExplicit = (s.duo_image_mode !== undefined && s.duo_image_mode !== null && s.duo_image_mode !== '');
    S.duoImageMode = S._duoImageModeExplicit
      ? s.duo_image_mode
      : ((S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct');
    S.duoImageToPlanner = s.duo_image_to_planner === true;
    S.duoImageToCoder = s.duo_image_to_coder === true;
    updateDuoImageUI();
    populateVisionAgentModelSel();
    _updateVisionAgentModeUI(S.visionAgentMode);
    // Smart Preload Toggle + Prefetch-Lead Slider
    var spEl = document.getElementById('smart-preload-toggle');
    if (spEl) {
      spEl.checked = S.smartPreload;
      document.getElementById('prefetch-cfg').style.display = S.smartPreload ? 'block' : 'none';
    }
    var warEl = document.getElementById('workers-after-run-toggle');
    if (warEl) {
      S.workersAfterRun = s.preload_workers_after_run === true;
      warEl.checked = S.workersAfterRun;
    }
    var lead = parseFloat(s.prefetch_lead_seconds || 8.0);
    var slEl = document.getElementById('prefetch-lead-sl');
    if (slEl) {
      slEl.value = lead;
      document.getElementById('pfl-val2').textContent  = lead.toFixed(1) + 's';
      document.getElementById('prefetch-lead-val').textContent = lead.toFixed(1);
    }
    if (S.smartPreload) loadPrefetchAvgs();

    // FIX: sync the TTL buttons with the current value from settings
    var currentKa = s.smart_preload_keep_alive || s.default_keep_alive || '10m';
    document.querySelectorAll('.ka-btn').forEach(function(b) {
      b.classList.toggle('on', b.dataset.ka === currentKa);
    });

    setModeUI(S.mode);

    // Adopt registry data into currentAssignments BEFORE renderAgentCards so dropdowns reflect active assignments.
    const reg = s._registry || {};
    Object.keys(reg).forEach(function(agent) {
      if (!S.currentAssignments[agent]) S.currentAssignments[agent] = {};
      S.currentAssignments[agent].model = reg[agent];
    });
    if (Object.keys(reg).length) renderAgentMap();

    // call renderAgentCards - S.models & S.currentAssignments are already filled now
    const agents = s.agents || {};
    S.agentsCfg = agents;   // CTX-FIT-WARN: critic model for the ctx estimates
    console.log('[loadSettings] agents keys:', Object.keys(agents), 'S.models:', S.models.length);
    if (Object.keys(agents).length > 0) {
      try { renderAgentCards(agents); } catch(e) { console.error('[loadSettings] renderAgentCards error:', e); }
    } else {
      const cardsEl = document.getElementById('agent-cards');
      if (cardsEl) cardsEl.innerHTML = '<div class="empty">No agent configuration from server.</div>';
    }
    if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);

    // initialize g-model with the first agent
    const g = document.getElementById('g-model');
    const firstAgent = Object.values(agents)[0];
    if (g && firstAgent && firstAgent.model) {
      g.innerHTML = modelOpts(firstAgent.model);
      g.value = firstAgent.model;
    }

    // Fallback: if S.models is still empty (list_available_models found nothing),
    // collect known model names from registry + settings.agents and use them immediately.
    // renderAgentCards is called again afterwards so the dropdowns get filled.
    if (!S.models.length) {
      var knownModels = new Set();
      Object.values(reg).forEach(function(m) { if (m) knownModels.add(m); });
      Object.values(agents).forEach(function(a) { if (a && a.model) knownModels.add(a.model); });
      Object.values(S.currentAssignments).forEach(function(a) { if (a && a.model) knownModels.add(a.model); });
      if (knownModels.size) {
        S.models = Array.from(knownModels).sort();
        console.log('[Hivemind] S.models fallback from registry:', S.models);
        refreshAllSelects();
        if (Object.keys(agents).length) renderAgentCards(agents);
      }
    }

    // Duo-pair VRAM hint: after loading the budget, mark options with a warning
    updateDuoPairVramHint(s.vram_budget_gb || 7.5);
    syncDuoPairSelectWithState();
    updateDuoPairHint();

    // Restore the VRAM budget field from settings (HTML has hardcoded value="7.0")
    var _vbInp = document.getElementById('vram-budget-inp');
    if (_vbInp && s.vram_budget_gb) {
      _vbInp.value = parseFloat(s.vram_budget_gb).toFixed(1);
      _vramBudgetGb = parseFloat(s.vram_budget_gb);
      // initial ctx-fit estimates (needs the budget loaded first)
      if (typeof refreshCtxEstimates === 'function') refreshCtxEstimates();
      if (typeof updateModelsChip === 'function') updateModelsChip();
    }
    // Per-model thinking overrides are no longer persisted
    // User toggles (duo_agentic_thinking etc.) control thinking at runtime
    // restore the VRAM refresh slider
    var _vrSl = document.getElementById('vram-refresh-sl');
    var _vrLbl = document.getElementById('vram-refresh-label');
    var _vrVal = document.getElementById('vram-refresh-val');
    if (_vrSl && s.vram_refresh_interval_s) {
      var _vrSec = Math.max(2, Math.min(60, parseInt(s.vram_refresh_interval_s) || 8));
      _vrSl.value = _vrSec;
      if (_vrLbl) _vrLbl.textContent = _vrSec + 's';
      if (_vrVal) _vrVal.textContent = _vrSec + 's';
      setVramRefreshInterval(_vrSec * 1000);
    }
    if (typeof loadSpecialAgentsConfig === 'function') {
      await loadSpecialAgentsConfig(s);
    }
  } catch(e) {
    console.error('loadSettings error:', e);
    const cardsEl = document.getElementById('agent-cards');
    if (cardsEl) cardsEl.innerHTML = '<div class="empty">&#9888; Server unreachable.<br><span style="font-size:9px">Is FastAPI started?</span></div>';
  }
}

let _settingsPatchQueue = {};
let _settingsPostTimer = null;
let _settingsPostWaiters = [];
let _settingsFlushPromise = null;
let _settingsFlushPending = false;

async function _doFlushQueuedSettings() {
  try {
    while (true) {
      _settingsFlushPending = false;
      var _payload = _settingsPatchQueue;
      _settingsPatchQueue = {};
      var _waiters = _settingsPostWaiters.splice(0);
      if (!_payload || !Object.keys(_payload).length) {
        _waiters.forEach(function(_resolve){ try { _resolve(); } catch(e) {} });
      } else {
        // UI-REV (2026-09-09): tag the patch with the loaded settings revision —
        // the server strips protected keys from stale revisions (old tabs).
        if (typeof S.settingsRev === 'number' && !isNaN(S.settingsRev) && S.settingsRev >= 0) {
          _payload.ui_rev = S.settingsRev;
        }
        try {
          var _resp = await fetch('/settings', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(_payload)
          });
          if (_resp && _resp.ok === false) {
            var _errDetail = 'HTTP ' + (_resp.status || 'error');
            try {
              var _ej = await _resp.json();
              if (_ej && (_ej.error || _ej.detail)) _errDetail = String(_ej.error || _ej.detail);
            } catch (_eje) {}
            console.warn('postSettings HTTP error:', _errDetail);
            try { showInfo('⚠ Settings change could NOT be saved (' + _errDetail + ').'); } catch (_se) {}
          } else {
            try {
              var _rj = await _resp.json();
              if (_rj && _rj.settings_rev !== undefined && _rj.settings_rev !== null) {
                var _nextRev = parseInt(_rj.settings_rev, 10);
                if (!isNaN(_nextRev) && _nextRev >= 0) S.settingsRev = _nextRev;
              }
              if (_rj && _rj.stale_rejected) console.warn('postSettings: stale patch rejected for', _rj.stale_rejected);
              // STALE-SNAPSHOT WARNING (2026-10-07, user feedback: git fields must not reset on save): the server strips protected keys from an
              // older tab's patch - the user must know the save did NOT take.
              if (_rj && Array.isArray(_rj.stripped) && _rj.stripped.length) {
                if (_rj.settings_rev !== undefined && _rj.settings_rev !== null) {
                  var _sRev = parseInt(_rj.settings_rev, 10);
                  if (!isNaN(_sRev) && _sRev >= 0) S.settingsRev = _sRev;
                }
                var _msg = 'Stale tab state: ' + _rj.stripped.join(', ')
                  + ' was NOT saved (a newer value was set elsewhere). '
                  + 'Reload the page (F5) and save again.';
                if (window._showErrorToast) window._showErrorToast(_msg);
                else alert(_msg);
              }
            } catch (_je) {}
          }
        } catch(e) {
          console.warn('postSettings flush failed:', e);
          try { showInfo('⚠ Settings change could NOT be saved (' + (e && e.message || e) + ').'); } catch (_se) {}
        }
        _waiters.forEach(function(_resolve){ try { _resolve(); } catch(e) {} });
      }
      if (_settingsFlushPending || (!_settingsPostTimer && Object.keys(_settingsPatchQueue).length)) {
        continue;
      }
      break;
    }
  } finally {
    _settingsFlushPromise = null;
  }
}

function _flushQueuedSettings() {
  if (_settingsFlushPromise) {
    _settingsFlushPending = true;
    return _settingsFlushPromise;
  }
  _settingsFlushPromise = _doFlushQueuedSettings();
  return _settingsFlushPromise;
}

function postSettings(patch) {
  if (!patch || typeof patch !== 'object') return Promise.resolve();
  // a voice/read-aloud write is re-read from the server once it lands, so the
  // client cannot keep a value the engine rejected
  var _voicePatch = Object.keys(patch).some(function(k) {
    return k.indexOf('tts_') === 0;
  });
  _settingsPatchQueue = Object.assign(_settingsPatchQueue || {}, patch);
  if (_voicePatch) {
    setTimeout(function() { refreshVoiceFlags(); }, 400);
  }
  return new Promise(function(resolve) {
    _settingsPostWaiters.push(resolve);
    if (_settingsPostTimer) clearTimeout(_settingsPostTimer);
    _settingsPostTimer = setTimeout(function() {
      _settingsPostTimer = null;
      _flushQueuedSettings();
    }, 180);
  });
}

// WS-GUARD-FEEDBACK (2026-08-25): the server rejects invalid workspace paths
// ('sampleproj#' finding). postSettings is debounced/batched → feedback via
// re-fetch: if the stored value differs, the patch was rejected.
function commitWorkspaceInput() {
  var _sent = S.workspace;
  postSettings({workspace: _sent});
  setTimeout(async function() {
    try {
      var s = await (await fetch('/settings')).json();
      var stored = (s.workspace || '').toString();
      if (stored !== _sent) {
        alert('Workspace "' + _sent + '" was rejected (path does not exist).\nActive remains: ' + (stored || '(empty = env/default)'));
        S.workspace = stored;
        var el = document.getElementById('workspace-input');
        if (el) el.value = stored;
      }
    } catch(e) { /* Netzwerkfehler: still */ }
  }, 450);
}

function updateDuoPairVramHint(budgetGb) {
  var eff = (parseFloat(budgetGb) || 7.5) - 0.4;
  var pairGb = { focused: 6.4, omni: 6.4, fast: 7.2, speed2: 4.0, power: 7.4, distilled_a: 4.3, distilled_b: 5.6, distilled_s: 2.3, solo_9b: 5.5, solo_4b: 4.2, solo_2b: 2.0, free: 0 };
  var warn = document.getElementById('duo-pair-vram-warn');
  var sel  = document.getElementById('duo-pair-sel');
  if (!sel) return;
  Array.from(sel.options).forEach(function(opt) {
    var gb = pairGb[opt.value];
    if (gb == null) return;
    var fits = gb <= eff;
    opt.text = opt.text.replace(/\s*⚠$/, '');
    if (!fits) opt.text += ' ⚠';
    opt.style.color = fits ? '' : '#b04040';
  });
  if (!warn) {
    warn = document.createElement('div');
    warn.id = 'duo-pair-vram-warn';
    warn.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:9px;color:#b04040;margin-top:3px;display:none';
    sel.parentNode.insertBefore(warn, sel.nextSibling);
  }
  var curGb = pairGb[sel.value] || 0;
  if (curGb > eff) {
    warn.textContent = '⚠ ' + curGb.toFixed(1) + ' GB > budget (' + eff.toFixed(1) + ' GB eff.) — loading models sequentially';
    warn.style.display = 'block';
  } else {
    warn.style.display = 'none';
  }
}

function _syncAskUserAutoProceedUI(checked) {
  S.askUserAutoProceed = !!checked;
  var cb = document.getElementById('ask-user-auto-proceed-cb');
  if (cb) cb.checked = S.askUserAutoProceed;
  document.querySelectorAll('.ask-aup-cb').forEach(function(el) {
    el.checked = S.askUserAutoProceed;
    var wrap = el.closest && el.closest('label');
    if (wrap) {
      var tm = wrap.querySelector('.ask-aup-tm');
      if (tm) tm.textContent = S.askUserAutoProceed ? 'engine 30s' : '';
    }
  });
}

function _getDuoOverrideModel() {
  return ((S.currentAssignments || {}).duo_coder || {}).model || '';
}

function syncDuoPairSelectWithState() {
  var sel = document.getElementById('duo-pair-sel');
  if (!sel) return;
  if (S.duoPair === 'free') {
    var model = _getDuoOverrideModel();
    var modelVal = model ? ('model:' + model) : '';
    if (modelVal && sel.querySelector('option[value="' + modelVal + '"]')) {
      sel.value = modelVal;
    } else {
      sel.value = 'free';
    }
  } else if (S.duoPair && sel.querySelector('option[value="' + S.duoPair + '"]')) {
    sel.value = S.duoPair;
  }
}

function updateDuoPairHint() {
  var el = document.getElementById('duo-pair-hint');
  if (!el) return;
  var planner = S.duoPlannerModel || '–';
  var coder = S.duoCoderModel || '–';
  var html = '<span style="color:#9a74dc">P:</span> ' + esc(planner)
    + ' &nbsp;|&nbsp; <span style="color:#20b0a0">C:</span> ' + esc(coder);
  if (!S.duoAgenticMode) {
    var critic = S.duoCriticModel || ((S.currentAssignments && S.currentAssignments.duo_critic && S.currentAssignments.duo_critic.model) || '') || coder || '–';
    html += ' &nbsp;|&nbsp; <span style="color:#e06c60">Cr:</span> ' + esc(critic);
  }
  el.innerHTML = html;
}

function populateDuoModelGroup() {
  // fill selects (planner + coder + critic) with all available models
  var plannerSel = document.getElementById('duo-planner-model-sel');
  var coderSel = document.getElementById('duo-coder-model-sel');
  var criticSel = document.getElementById('duo-critic-model-sel');
  var allModels = (S.models || []).filter(function(m) {
    var base = m.split(':')[0].toLowerCase();
    if (base === 'granite4' || base === 'granite3.2-vision') return false;
    return true;
  });

  function buildOptions(sel, noneLabel) {
    if (!sel) return;
    var current = sel.value;
    sel.innerHTML = '';
    if (noneLabel) {
      var noneOpt = document.createElement('option');
      noneOpt.value = ''; noneOpt.textContent = noneLabel;
      sel.appendChild(noneOpt);
    }
    allModels.forEach(function(m) {
      var opt = document.createElement('option');
      opt.value = m;
      var prof = S.modelProfiles[m] || S.modelProfiles[m.split(':')[0]] || {};
      var tags = '';
      if (prof.thinking || /think|qwen3\.5|qwen3:|qwq/i.test(m)) tags += ' 🧠';
      if (prof.vision || /vl|vision/i.test(m)) tags += ' 👁';
      opt.textContent = m + tags;
      sel.appendChild(opt);
    });
    // restore the stored value
    if (current && sel.querySelector('option[value="'+current+'"]')) sel.value = current;
  }
  buildOptions(plannerSel, '– no planner –');
  buildOptions(coderSel, '');
  buildOptions(criticSel, 'Follow Duo-Coder');
  // COMPRESSION-MODELL (2026-09-07): eigener Select — "auto" = Coder, sonst Light-Modell.
  var compressSel = document.getElementById('duo-compress-model-sel');
  if (compressSel) {
    var _curComp = compressSel.value || S.duoCompressModel || 'auto';
    compressSel.innerHTML = '';
    var _autoComp = document.createElement('option');
    _autoComp.value = 'auto'; _autoComp.textContent = 'Auto (Coder)';
    compressSel.appendChild(_autoComp);
    allModels.forEach(function(m) {
      var opt = document.createElement('option');
      opt.value = m; opt.textContent = m;
      compressSel.appendChild(opt);
    });
    compressSel.value = (compressSel.querySelector('option[value="' + _curComp + '"]')) ? _curComp : 'auto';
  }
  // apply the stored state
  if (plannerSel) plannerSel.value = S.duoPlannerModel || '';
  if (S.duoCoderModel && coderSel) coderSel.value = S.duoCoderModel;
  if (criticSel) criticSel.value = S.duoCriticModel || '';
  updateDuoPairHint();
}

function onDuoPlannerModelChange(model) {
  S.duoPlannerModel = model || '';
  postSettings({duo_planner_model: S.duoPlannerModel});
  // HINT LIVENESS (2026-10-04, user): the chat image hint reads
  // currentAssignments - keep it in sync here too, or the hint shows the
  // previous planner until the image is re-attached.
  if (S.currentAssignments.duo_planner) S.currentAssignments.duo_planner.model = S.duoPlannerModel;
  updateDuoPairHint();
  updateDuoImageUI();
  if (typeof refreshCtxEstimates === 'function') refreshCtxEstimates();
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
}

function syncEngineTwinInputs(peerId, settingKey, rawVal, isBool) {
  var val = isBool ? !!rawVal : (parseInt(rawVal, 10) || 0);
  var peer = document.getElementById(peerId);
  if (peer) {
    if (isBool) peer.checked = val;
    else peer.value = val;
  }
  var patch = {};
  patch[settingKey] = val;
  postSettings(patch);
}

function _applyDuoModelChange(agentKey, stateField, settingKey, model, extraPatch) {
  S[stateField] = model || '';
  var val = S[stateField];
  fetch('/settings/agent', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({agent: agentKey, model: val})
  }).catch(function(){});
  var patch = Object.assign({}, extraPatch || {});
  patch[settingKey] = val;
  if (!patch.agents) patch.agents = {};
  patch.agents[agentKey] = {model: val};
  postSettings(patch);
  if (!S.currentAssignments[agentKey]) S.currentAssignments[agentKey] = {};
  S.currentAssignments[agentKey].model = val;
  var cardSel = document.querySelector('select[data-agent="' + agentKey + '"]');
  if (cardSel) { cardSel.dataset.cur = val; cardSel.value = val; }
  updateDuoPairHint();
  updateDuoImageUI();
  if (agentKey === 'duo_coder') updateMoeVisibility();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  if (typeof refreshCtxEstimates === 'function') refreshCtxEstimates();
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
}

function onDuoCoderModelChange(model) {
  _applyDuoModelChange('duo_coder', 'duoCoderModel', 'duo_coder_model', model, {duo_pair: 'free'});
}

function onDuoCriticModelChange(model) {
  _applyDuoModelChange('duo_critic', 'duoCriticModel', 'duo_critic_model', model);
}

// COMPRESSION-MODELL + FIX-FLAGS (2026-09-07): UI-Schalter fuer Backend-Keys.


function _syncFlagCheck(id, on) {
  var e = document.getElementById(id);
  if (e) e.checked = !!on;
}

function onDuoCompressModelChange(model) {
  S.duoCompressModel = (model === 'auto') ? '' : (model || '');
  // '' (auto) => Coder-Modell; sonst Light-Modell fuer die Zusammenfassung.
  postSettings({duo_compress_model: S.duoCompressModel});
}

function onFlagToggle(key, checked) {
  var patch = {};
  patch[key] = !!checked;
  S['_flag_' + key] = !!checked;
  postSettings(patch);
}


// MCP CLIENT (beta, 2026-10-27): the client lived only in settings.json. The
// card keeps the server list as JSON on purpose - the shape (command, args,
// env, cwd) is the server's contract, not something a form should hide.
function mcpSetStatus(text, isError) {
  var el = document.getElementById('mcp-status');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--red)' : 'var(--muted)';
}

function mcpLoadStatus() {
  var el = document.getElementById('mcp-servers-json');
  fetch('/mcp/servers').then(function (r) { return r.json(); }).then(function (d) {
    if (el && !el.value.trim() && Array.isArray(d.configured)) {
      el.value = d.configured.length ? JSON.stringify(d.configured, null, 2) : '';
    }
    var running = (d.running || []).map(function (s) {
      return s.name + (s.ok ? ' (' + s.tools + ' tools)' : ' - ' + (s.error || 'down'));
    });
    var txt = 'Enabled: ' + (d.enabled ? 'yes' : 'no');
    if (d.enabled) txt += ' | ' + (running.length ? running.join(', ') : 'no server running');
    mcpSetStatus(txt, false);
  }).catch(function () { mcpSetStatus('status unavailable', true); });
}

function saveMcpServers() {
  var el = document.getElementById('mcp-servers-json');
  if (!el) return;
  var raw = el.value.trim();
  var list = [];
  if (raw) {
    try {
      list = JSON.parse(raw);
    } catch (e) {
      mcpSetStatus('invalid JSON: ' + e.message, true);
      return;
    }
    if (!Array.isArray(list)) {
      mcpSetStatus('expected a JSON list of servers', true);
      return;
    }
  }
  postSettings({mcp_servers: list});
  mcpSetStatus('saved - press Reload to start the servers', false);
}

function reloadMcpServers() {
  mcpSetStatus('reloading...', false);
  fetch('/mcp/reload', {method: 'POST'}).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { mcpSetStatus('reload failed: ' + (d.error || 'unknown'), true); return; }
    var names = (d.servers || []).map(function (s) {
      return s.name + (s.ok ? ':' + s.tools : ':down');
    });
    mcpSetStatus('reloaded - ' + (names.length ? names.join(', ') : 'no servers'), false);
    mcpLoadStatus();
  }).catch(function () { mcpSetStatus('reload failed', true); });
}

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('mcp-client-toggle')) mcpLoadStatus();
  });
}
// MODELS CHIP (2026-10-23): the header shows which model(s) the selected mode
// will actually call — one muted chip, full names on hover. Deliberately not
// a second row of selects; the pickers stay in the Agents tab.
function _shortModel(m) {
  if (!m) return '';
  m = String(m);
  var slash = m.lastIndexOf('/');
  if (slash >= 0) m = m.slice(slash + 1);
  return m.length > 26 ? m.slice(0, 24) + '\u2026' : m;
}
function updateModelsChip() {
  var el = document.getElementById('h-models');
  if (!el) return;
  var mode = S.mode || 'auto';
  var parts = [], title = '';
  if (mode === 'code_duo') {
    var c = _shortModel(S.duoCoderModel);
    if (c) parts.push('C: ' + c);
    var p = (S.duoPlannerModel && S.duoPlannerModel !== S.duoCoderModel)
      ? _shortModel(S.duoPlannerModel) : '';
    if (p) parts.push('P: ' + p);
    var crName = !S.duoAgenticMode ? (S.duoCriticModel || ((S.currentAssignments && S.currentAssignments.duo_critic && S.currentAssignments.duo_critic.model) || '') || S.duoCoderModel) : '';
    var cr = crName ? _shortModel(crName) : '';
    if (cr) parts.push('Cr: ' + cr);
    title = 'Coder: ' + (S.duoCoderModel || '?')
      + (p ? ' \u00b7 Planner: ' + S.duoPlannerModel : ' \u00b7 Planner: coder ctx')
      + (!S.duoAgenticMode ? ' \u00b7 Critic: ' + (crName || '?') : '');
  } else if (mode === 'pipeline') {
    var a = S.agentsCfg && S.agentsCfg.analyst && S.agentsCfg.analyst.model;
    var sy = S.agentsCfg && S.agentsCfg.synthesizer && S.agentsCfg.synthesizer.model;
    if (a) parts.push('A: ' + _shortModel(a));
    if (sy) parts.push('S: ' + _shortModel(sy));
    title = 'Pipeline \u00b7 analyst: ' + (a || '?') + ' \u00b7 synthesizer: ' + (sy || '?');
  } else if (mode === 'automap') {
    parts.push('auto-fit');
    title = 'Automap assigns models per run (Models panel)';
  } else {
    var d = S._lastDirectRunModel
      || (S.currentAssignments && S.currentAssignments.direct && S.currentAssignments.direct.model)
      || (S.agentsCfg && S.agentsCfg.direct && S.agentsCfg.direct.model) || '';
    if (d) { parts.push(_shortModel(d)); title = 'Direct model: ' + d; }
  }
  el.textContent = parts.join(' \u00b7 ');
  el.style.display = parts.length ? '' : 'none';
  if (title) el.title = title;
}

function setMode(mode, el) {
  // MODE-SWITCH FIX: abort a running run when the mode is changed.
  // Without this fix: the old SSE stream keeps running and e.g. shows the refiner badge
  // even though the UI already shows "duo" → misleading visual race-condition artifact.
  if (S.streaming && S.currentRunId) {
    fetch('/abort/' + S.currentRunId, {method: 'POST'}).catch(function(){});
  }
  // SKIP-RESET FIX: reset pipeline-agent skips on mode switch.
  // If e.g. "analyst" was OFF and the user switches to code_duo and back,
  // the analyst skip stays active → the pipeline only runs with refiner.
  // Mode switch = new context → clean state.
  if (Object.keys(S.skippedAgents).length > 0) {
    S.skippedAgents = {};
    buildAgentForceButtons();
    updateForceHeaderBadge();
  }
  setModeUI(mode);
  if (el) el.classList.add('on');
  postSettings({mode: mode});
}

function setModeUI(mode) {
  S.mode = mode;
  S._lastDirectRunModel = '';
  updateDuoImageUI();
  // FIX: scope auf #mode-btns-row
  document.querySelectorAll('#mode-btns-row .mbtn').forEach(function(b) {
    b.classList.toggle('on', b.dataset.mode === mode);
  });
  const hm = document.getElementById('h-mode');
  hm.textContent = mode === 'code_duo' ? ('\u21C4 ' + (S.duoAgenticMode ? 'coder' : 'duo')) : mode;
  if (mode === 'automap') {
    hm.style.color = '#4878c0'; hm.style.borderColor = 'rgba(72,120,192,.4)';
  } else if (mode === 'code_duo') {
    hm.style.color = '#20b0a0'; hm.style.borderColor = 'rgba(32,176,160,.35)';
  } else {
    hm.style.color = ''; hm.style.borderColor = '';
  }
  const dr = document.getElementById('duo-pair-row');
  if (dr) dr.classList.toggle('on', mode === 'code_duo');
  var _ccMode = document.querySelector('.acard[data-agent="duo_critic"]');
  if (_ccMode) _ccMode.style.display = (mode === 'code_duo' && S.duoAgenticMode) ? 'none' : '';
  // FIX: sync the select value with S.duoPair
  syncDuoPairSelectWithState();
  updateDuoPairHint();
  var _duoAcc2 = document.getElementById('duo-mode-accordion');
  if (_duoAcc2) _duoAcc2.style.display = mode === 'code_duo' ? 'block' : 'none';
  _setDuoModeAccordion(mode === 'code_duo');
  updateAutomapAdvancedVisibility();
  updateWebsearchHint();
  updateChatToolsBadge();
  updateChatToolsSectionVisibility();
  updateReasoningCardVisibility();
  updateModeDesc();
  updateComplexityVisibility();
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
}

function setIters(val) {
  const n = Math.max(1, Math.min(5, parseInt(val) || 2));
  S.iters = n;
  document.getElementById('iters-in').value = n;
  document.getElementById('h-iters-val').textContent = n;
  postSettings({max_iterations: n});
}

// Known non-TC model bases (synced with model_automap.py NO-TC list)
// BUG-FIX: was only a hardcoded set → new models without an entry got no NO-TC badge.
// S.modelProfiles (from the /models endpoint, from model_automap.MODEL_PROFILES) now takes priority.
// The hardcoded set remains as fallback while profiles are not yet loaded.
var _NO_TC_BASES = new Set(['olmo-3','ministral-3','granite3.2-vision']);

function _isNoTc(modelName) {
  if (!modelName) return false;
  // 1. profile check (priority): server delivers tool_call:bool from model_automap.MODEL_PROFILES
  var prof = S.modelProfiles[modelName] || S.modelProfiles[modelName.split(':')[0]] || null;
  if (prof && typeof prof.tool_call === 'boolean') return !prof.tool_call;
  // 2. fallback: hardcoded base names (applies when profiles are not yet loaded)
  var base = modelName.split(':')[0].toLowerCase();
  return _NO_TC_BASES.has(base);
}


function _setDuoModeAccordion(open) {
  var body = document.getElementById('duo-mode-body');
  if (!body) return;
  body.style.display = open ? 'block' : 'none';
}

function setPassFiles(val) {
  S.duoPassFiles = val;
  postSettings({duo_pass_explore_files: val});
  updatePassFilesButtons();
}
function updatePassFilesButtons() {
  ['all','touched','none'].forEach(function(v) {
    var btn = document.getElementById('dpf-' + v);
    if (!btn) return;
    var active = S.duoPassFiles === v;
    btn.style.background = active ? 'rgba(32,176,160,.18)' : '';
    btn.style.color = active ? 'var(--accent)' : '';
    btn.style.borderColor = active ? 'var(--accent)' : '';
  });
}
function setDuoSubMode(mode, opts) {
  var persist = !opts || opts.persist !== false;
  var isCritic = mode === 'critic';
  // Badge
  var badge = document.getElementById('duo-mode-badge');
  if (badge) { badge.textContent = isCritic ? 'CRITIC' : 'AGENTIC'; }
  // Buttons
  var cb = document.getElementById('dmo-critic-btn');
  var ab = document.getElementById('dmo-agent-btn');
  if (cb) { cb.style.borderColor = isCritic ? 'rgba(32,176,160,.6)' : ''; cb.style.color = isCritic ? '#20b0a0' : ''; }
  if (ab) { ab.style.borderColor = !isCritic ? 'rgba(32,176,160,.6)' : ''; ab.style.color = !isCritic ? '#20b0a0' : ''; }
  // Panels
  var co = document.getElementById('dmo-critic-opts');
  var ao = document.getElementById('dmo-agent-opts');
  if (co) co.style.display = isCritic ? 'block' : 'none';
  if (ao) ao.style.display = isCritic ? 'none' : 'block';
  // Desc
  var desc = document.getElementById('duo-mode-desc');
  if (desc) {
    var _ctxVal = S.duoCtxAgentic || _ctxNum('duo-ctx-agentic', 16384);
    var _ctxK = Math.round(_ctxVal / 1024) + 'k';
    desc.textContent = isCritic
      ? 'Coder writes → Critic reviews JSON → Coder fixes. Tool calls optional.'
      : 'Coder runs autonom: one model, solo in VRAM (ctx=' + _ctxK + '), no critic. Explore → Implement → Test → Fix.';
  }
  // Settings anpassen
  S.duoAgenticMode = !isCritic;
  var critRow = document.getElementById('duo-critic-model-row');
  if (critRow) critRow.style.display = isCritic ? 'flex' : 'none';
  var critCard = document.querySelector('.acard[data-agent="duo_critic"]');
  if (critCard) critCard.style.display = (S.mode === 'code_duo' && !isCritic) ? 'none' : '';
  // h-mode Tag oben links: "⇄ duo" im Critic-Modus, "⇄ coder" im Agentic-Modus
  if (S.mode === 'code_duo') {
    var _hm = document.getElementById('h-mode');
    if (_hm) _hm.textContent = '\u21C4 ' + (isCritic ? 'duo' : 'coder');
  }
  var dtrSel = document.getElementById('duo-tool-rounds-sel');
  if (isCritic) {
    if (persist) {
      S.duoToolRounds = 0;
      postSettings({duo_tool_rounds: 0, max_iterations: S.iters || 2, duo_agentic_mode: false});
    }
    if (dtrSel) dtrSel.value = String(persist ? 0 : (S.duoToolRounds || 0));
  } else {
    if (persist) {
      S.duoToolRounds = 1;
      if (!S._thinkingBeforeChunking) S._thinkingBeforeChunking = S.duoAgenticThinking;
      postSettings({duo_tool_rounds: 1, max_iterations: 1, duo_agentic_mode: true});
      S.iters = 1;
    }
    if (dtrSel) dtrSel.value = String(S.duoToolRounds != null ? S.duoToolRounds : 1);
    var _itersIn = document.getElementById('iters-in');
    if (_itersIn) _itersIn.value = S.iters || 1;
    var _hItersVal = document.getElementById('h-iters-val');
    if (_hItersVal) _hItersVal.textContent = S.iters || 1;
    updateAgenticHint();
  }
  updateDuoPairHint();
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  updateCtxScopeHint();
  updateComplexityVisibility();
  updateChatToolsSectionVisibility();
  updateReasoningCardVisibility();
}

function updateAgenticHint() {
  var hint = document.getElementById('duo-agentic-hint');
  if (!hint) return;
  var toolActive = S.duoToolRounds > 0;
  if (toolActive) {
    hint.className = 'ui-hint';
    hint.innerHTML = '✓ <b>Agentic:</b> Coder explores, implements and tests on its own '
      + '(explore → patch_file → run_bash → fix).'
      + '<br><span style="color:#7a8fa8">In Auto mode the routing card decides whether a run '
      + 'goes agentic; this switch only shapes the loop once it does.</span>';
    hint.style.display = 'block';
  } else {
    hint.style.display = 'none';
  }
}

