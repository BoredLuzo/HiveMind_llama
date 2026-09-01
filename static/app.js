
// -- State ------------------------------------------------------
let S = {
  models: [],
  mode: 'auto',
  iters: 2,
  activePreset: null,
  pendingImgs: [],
  streaming: false,
  curAgent: null,
  _coderElapsed: null,
  editingPreset: null,
  constraintMode: true,
  currentAssignments: {},
  forcedComplexity: 'auto',
  skippedAgents:   {},
  judgeBias: 50,
  // Vision
  visionEnabled: false,
  visionModel: '',
  imageMode: 'direct',       // 'direct' | 'preprocess' | 'pipeline'
  pipelineVisionRoles: {},   // {analyst/refiner/critic/synthesizer: bool}
  modelProfiles: {},        // name → {vision, thinking, tool_call, ...}
  visionAllowlist: new Set(), // models that can do real preprocessing
  // Intent Agent
  intentEnabled: false,
  intentModel: '',
  // Soul-Evolve Agent
  soulEvolveEnabled: false,
  soulEvolveModel: '',
  // Chat history
  currentChatId: null,
  currentChatMessages: [],
  chatAutosave: true,
   // Abort
   currentRunId: null,
   // Pause / Ask-User
   agentPaused: false,
   agentQuestion: null,
    _pauseBtnState: 'idle',
    _stopBtnState: 'idle',
    // Code
  duoPair: 'focused',
  duoToolRounds: 0,
  duoUsePipeline: false,
  duoRuntimeProfile: 'auto',
  duoRuntimeProfileLockOverride: false,
  duoUsePresetModels: false,
  duoUsePresets: true,
  duoProfileSpeedModel: 'qwen3.5:4b',
  duoProfileQualityModel: 'qwen3.5:9b-ud',
  duoCriticTools: false,
  duoChunking:    false,
  duoTestFeedback: false,
  duoPlannerEnabled: false,
  duoCodingMode:  true,   // True=Code-Review, False=General-Review
  duoPreExplore:  false,  // True=Coder erkundet Codebase vor erstem Run
  duoPreExploreMaxTools: 20,
  duoPassFiles: true,
  duoAgenticMode: false,
  duoAgenticThinking: false,  // true=no critic interrupt, coder runs autonomously

  duoCoderToolThinking: false,
  duoCoderToolThinkingAutoMode: 'on_fail',
  duoUntilFinished: false,    // true=coder runs until tests pass (budget 300)
  _askUserCountdownInterval: null,
  _runAbortCtrl: null,
  askUserTimeoutSeconds: 300,
  askUserMaxPer10min: 5,
  askUserAutoAnswer: 'Use best judgment, document decision in commit message.',
  perfRunStartedAt: 0,
  perfFirstTokenAt: 0,
  perfChars: 0,
  perfEstTokens: 0,
  perfRealTokens: 0,
  perfCtxLimit: 0,
  perfCtxPct: 0,
  perfTokRate: 0,
  perfTokRateEma: 0,
  perfCompressing: false,
  duoCtxAgentic: 16384,
  duoCtxUntilFinished: 16384,
  duoCtxNormal: 8192,
  duoCtxPlanner: 16384,
  duoCtxCritic: 8192,
  duoPlannerUseCoderCtx: true,
  duoPlannerModel: '',
  duoCoderModel: '',
  llamaMlock: true,
  llamaCacheReuse: 256,
  moeExpertDefaults: {},
  moeAutodetect: {},
  ctxDefaults: {},
  moeCpuExpertsMap: {},
  moeSelectedModel: '',
  duoPlannerTtl: 0,
  duoCoderTtl: 0,
  duoPartitionMaxFiles: 30,
  prexPoolState: null,
  // AutoMap advanced
  automapCodeDuoEnabled: false,
  automapDuoPreExplore: false,
  automapDuoParallelPreexplore: false,
  automapDuoParallelPartitions: 2,
  automapPipelineWebsearch: false,
  // Git Integration
  duoGitAutocommit: false,
  gitRepoUrl: '',
  gitUsername: '',
  gitToken: '',
  gitBranch: 'main',
};

const AGENT_META = {
  analyst:     {label:'Analyst',    role:'Breaks the problem into core components and assumptions', cls:'ca', color:'#4878c0'},
  refiner:     {label:'Refiner',    role:'Fixes constraints from the critic output', cls:'cr', color:'#3a9960'},
  critic:      {label:'Critic',     role:'Returns tune constraints (ERR/MISS/FIX/CONTRA)', cls:'cc', color:'#b04040'},
  synthesizer: {label:'Synthesizer',role:'Integrates all perspectives into the final answer', cls:'cs', color:'#e09030'},
  direct:      {label:'Direct',     role:'Answers without pipeline (simple mode)', cls:'cd', color:'#8858c0'},
  judge:       {label:'Judge',      role:'Assesses complexity internally (not shown)', cls:'',  color:'#7a8fa8'},
  intent:      {label:'Intent',     role:'Detects memory / evolve / tool-calls from natural language', cls:'', color:'#20b0a0'},
  duo_coder:   {label:'Duo-Coder',  role:'Writes code / agentic coding (tool-calls + thinking)', cls:'', color:'#20b0a0'},
  duo_critic:  {label:'Duo-Critic', role:'Reviews code in duo mode (TUNE format)', cls:'', color:'#b04040'},
};

function agentColor(name) {
  const n = name.toLowerCase();
  if (n.includes('analyst'))   return '#4878c0';
  if (n.includes('refiner'))   return '#3a9960';
  if (n.includes('critic') || n.includes('kritiker')) return '#b04040';
  if (n.includes('synth'))     return '#e09030';
  if (n.includes('hivemind'))  return '#e09030';
  return '#8858c0';
}

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
  updateDuoRuntimeProfileHint();
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

function onDuoRuntimeProfileChange(val) {
  S.duoRuntimeProfile = val || 'auto';
  postSettings({duo_runtime_profile: S.duoRuntimeProfile});
  updateDuoRuntimeProfileHint();
  if (typeof updateCtxScopeHint === 'function') updateCtxScopeHint();
}

function onDuoRuntimeProfileLockOverrideChange(v) {
  S.duoRuntimeProfileLockOverride = !!v;
  postSettings({duo_runtime_profile_lock_override: S.duoRuntimeProfileLockOverride});
  updateDuoRuntimeProfileHint();
  if (typeof updateCtxScopeHint === 'function') updateCtxScopeHint();
}

function onDuoUsePresetModelsChange(v) {
  S.duoUsePresetModels = !!v;
  postSettings({duo_use_preset_models: S.duoUsePresetModels});
  updateDuoRuntimeProfileHint();
}

function resolveDuoRuntimeProfileForRequest() {
  var p = (S.duoRuntimeProfile || 'auto').toLowerCase();
  if (p !== 'auto') return p;
  // auto-mapping onto the existing complexity-override logic
  if (S.forcedComplexity === 'simple') return 'fast';
  if (S.forcedComplexity === 'complex') return 'critical';
  return 'on_fail';
}

function resolveImportantTaskForRequest() {
  // important-task toggle deliberately removed; profile control runs via dropdown.
  return false;
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
}

// removed: updateToolThinkingModeVis — segmented control is always visible

function updateDuoRuntimeProfileHint() {
  var el = document.getElementById('duo-runtime-profile-hint');
  if (!el) return;
  var eff = resolveDuoRuntimeProfileForRequest();
  var speedModel = S.duoProfileSpeedModel || 'qwen3.5:4b';
  var qualityModel = S.duoProfileQualityModel || 'qwen3.5:9b-ud';
  var isDropdownOverride = (S.duoPair === 'free');
  var overrideModel = ((S.currentAssignments || {}).duo_coder || {}).model || '';
  var src = (S.duoRuntimeProfile === 'auto')
    ? ('Auto source: ' + (S.forcedComplexity === 'auto' ? 'neutral' : S.forcedComplexity))
    : 'Manual override';
  var lockTxt = S.duoRuntimeProfileLockOverride
    ? 'Lock: ON (profile stays fixed, no auto-escalation)'
    : 'Lock: OFF (can escalate to CRITICAL on until-finished/important)';
  var presetModelTxt = S.duoUsePresetModels
    ? 'Preset models: ON (dropdown model is replaced)'
    : 'Preset models: OFF (dropdown model stays active)';
  var detail = 'Balanced: quality routing + self-review (ctx-clamped).';
  if (eff === 'fast') detail = 'Fast: speed-first routing, no post-review.';
  if (eff === 'critical') detail = 'Quality: strongest routing + self-review + context injection.';
  var route = 'Routing: preset picks model family + ctx.';
  if (isDropdownOverride && S.duoUsePresetModels) {
    route = 'Routing: preset models active - dropdown model is ignored, preset model is forced.';
  } else if (isDropdownOverride) {
    route = 'Routing: dropdown override active'
      + (overrideModel ? (' (' + overrideModel + ')') : '')
      + ' - preset controls only ctx/timeout/review.';
  }
  el.innerHTML = 'Effective: <span style="color:var(--txh)">' + eff.toUpperCase() + '</span>'
    + '<br><span style="color:var(--tx2)">' + src + '</span>'
    + '<br><span style="color:var(--tx2)">' + lockTxt + '</span>'
    + '<br><span style="color:var(--tx2)">' + presetModelTxt + '</span>'
    + '<br><span style="color:var(--tx2)">Preset models:</span>'
    + '<br><span style="color:var(--tx2)">FAST → <b>' + esc(speedModel) + '</b></span>'
    + '<br><span style="color:var(--tx2)">BALANCED → <b>' + esc(qualityModel) + '</b></span>'
    + '<br><span style="color:var(--tx2)">CRITICAL → <b>' + esc(qualityModel) + '</b></span>'
    + '<br><span style="color:var(--tx2)">' + detail + '</span>'
    + '<br><span style="color:var(--tx2)">' + route + '</span>';
}

function updateWebsearchHint() {
  var h = document.getElementById('duo-websearch-hint');
  var _wsActive = S.duoWebsearch || S.pipelineWebsearch;
  if (h) h.style.display = _wsActive ? 'block' : 'none';
  var to = document.getElementById('ws-timeout-opts');
  if (to) to.style.display = _wsActive ? 'block' : 'none';
  var ah = document.getElementById('automap-websearch-hint');
  if (ah) ah.style.display = (S.mode === 'automap' && S.automapPipelineWebsearch) ? 'block' : 'none';
}

function updateDirectToolsHint() {
  var opts = document.getElementById('direct-tools-opts');
  if (opts) opts.style.display = S.directToolsEnabled ? 'block' : 'none';
}

// CHAT-TOOLS-SECTION (2026-09-01): section (enabled toggle + tool tier) only in
// simple/auto — in pipeline/automap/code-duo the agent tools apply.
function updateChatToolsSectionVisibility() {
  var sec = document.getElementById('chat-tools-section');
  if (!sec) return;
  sec.style.display = (S.mode === 'simple' || S.mode === 'auto') ? 'block' : 'none';
}

// DIRECT-TOOLS-TIER (2026-08-31): segmented control instead of dropdown. sets the
// state, persists and updates badge + tier hint + composer chip.
function setDirectToolsTier(v) {
  S.directToolsTier = v;
  postSettings({direct_tools_tier: v});
  updateDirectToolsTierHint();
  updateChatToolsBadge();
  updateComposerToolStatus();
}

var _DIRECT_TIER_HINTS = {
  off:      'Off \u2014 pure chat, no tools.',
  readonly: 'Read \u2014 read (read_file/list_dir/find_files/search_code) + web search.',
  python:   'Python \u2014 Read + run_python (run code snippets).',
  full:     'Full \u2014 Read/Write/Exec incl. edit_file + run_bash. Needed for real coding requests.',
};

function updateDirectToolsTierHint() {
  var h = document.getElementById('direct-tools-tier-hint');
  if (!h) return;
  var tier = (S.directToolsTier || 'readonly').toString();
  h.textContent = _DIRECT_TIER_HINTS[tier] || '';
  if (tier === 'full') h.style.color = 'var(--amber)';
  else if (tier === 'off') h.style.color = 'var(--tx3)';
  else h.style.color = 'var(--tx3)';
}

// COMPOSER-TOOL-STATUS (2026-08-31): visible tool status right at the input —
// shows which tools are actually active in the current mode (instead of the
// subtle header badge). Click opens the chat-tools section.
function updateComposerToolStatus() {
  var el = document.getElementById('composer-tool-status');
  if (!el) return;
  var tier = (S.directToolsTier || 'readonly').toString();
  var rounds = S.directToolsRounds || 3;
  var html = '', color = 'var(--tx3)';
  if (S.mode === 'simple' || S.mode === 'auto') {
    if (S.directToolsEnabled && tier !== 'off') {
      html = '\u2699 Chat Tools: ' + tier + ' \u00b7 ' + rounds + ' rounds';
      color = 'var(--green)';
    } else {
      html = '\u2699 Chat Tools: off (pure chat)';
      color = 'var(--tx3)';
    }
  } else if (S.mode === 'code_duo') {
    var _crt = (S.duoToolRounds > 0) ? (S.duoToolRounds + ' rounds') : 'off';
    html = '\u21C4 Code-Duo: coder tools ' + _crt;
    color = '#20b0a0';
  } else {
    html = '\u2699 ' + (S.mode === 'automap' ? 'AutoMap' : 'Pipeline') + ' \u2014 chat tools inactive';
    color = 'var(--tx3)';
  }
  el.innerHTML = html;
  el.style.color = color;
  el.style.display = 'block';
}

// COMPOSER-TOOL-STATUS click → open the sidebar + Agents tab + scroll to the chat-tools section.
function openChatToolsSection() {
  document.body.classList.remove('sidebar-collapsed');
  var btn = document.getElementById('h-sidebar-btn');
  if (btn) btn.classList.add('active');
  var agentsTab = document.querySelector('.tab[data-p="agents"]');
  if (agentsTab) agentsTab.click();
  setTimeout(function() {
    var opts = document.getElementById('direct-tools-opts');
    if (opts) opts.scrollIntoView({behavior: 'smooth', block: 'center'});
  }, 60);
}

function updateChatToolsBadge() {
  var b = document.getElementById('h-tools');
  if (!b) return;
  var tier = (S.directToolsTier || 'readonly').toString();
  var active = (S.mode === 'simple' || S.mode === 'auto')
    && S.directToolsEnabled && tier !== 'off';
  b.classList.toggle('off', !active);
  if (!active) { b.style.display = 'none'; b.textContent = ''; }
  else { b.textContent = '\u2699 Tools: ' + tier; b.style.display = ''; }
}

// MODUS-BESCHREIBUNG (2026-08-31): Ein-Zeilen-Text unter den Mode-Buttons.
var _MODE_DESC = {
  auto:      'Auto \u2014 Judge routes by complexity. Tool requests run in the Direct chat loop (when Chat Tools on).',
  simple:    'Chat (Direct) \u2014 fast single-model chat. Chat Tools (read/python/web) available.',
  pipeline:  'Pipeline \u2014 multi-agent: Analyst \u2192 Refiner \u2192 Critic \u2192 Synthesizer.',
  automap:   'AutoMap \u2014 Judge picks a model per task type.',
  code_duo:  'Code-Duo \u2014 iterative coder+critic loop, 2 models in parallel in VRAM.',
};
function updateModeDesc() {
  var el = document.getElementById('mode-desc');
  if (el) el.textContent = _MODE_DESC[S.mode] || '';
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
  S.automapCodeDuoEnabled = !!on;
  postSettings({automap_code_duo_enabled: S.automapCodeDuoEnabled});
  if (!S.automapCodeDuoEnabled) {
    postSettings({
      automap_duo_pre_explore: false,
      automap_duo_parallel_preexplore: false,
    });
  }
  updateAutomapAdvancedInterlocks();
  updateWebsearchHint();
}

function setAutomapDuoPreExplore(on) {
  S.automapDuoPreExplore = !!on;
  postSettings({automap_duo_pre_explore: S.automapDuoPreExplore});
  if (!S.automapDuoPreExplore) {
    S.automapDuoParallelPreexplore = false;
    postSettings({
      automap_duo_parallel_preexplore: false,
    });
  }
  updateAutomapAdvancedInterlocks();
  updateWebsearchHint();
}

function setAutomapDuoParallel(on) {
  S.automapDuoParallelPreexplore = !!on;
  postSettings({automap_duo_parallel_preexplore: S.automapDuoParallelPreexplore});
  updateAutomapAdvancedInterlocks();
}

function setAutomapPipelineWebsearch(on) {
  S.automapPipelineWebsearch = !!on;
  postSettings({automap_pipeline_websearch_enabled: S.automapPipelineWebsearch});
  updateWebsearchHint();
}

function updateCriticToolsHint() {
  var h = document.getElementById('duo-critic-tools-hint');
  if (h) h.style.display = S.duoCriticTools ? 'block' : 'none';
}
function checkWebsearchStatus() {
  var dot = document.getElementById('ws-status-dot');
  var ad = document.getElementById('automap-ws-status-dot');
  var _set = function(d, txt, color) {
    if (!d) return;
    d.textContent = txt;
    d.style.textDecoration = 'none';
    d.style.color = color || '';
  };
  _set(dot, 'Pruefe...', '');
  _set(ad, 'Pruefe...', '');
  fetch('/websearch/status').then(r=>r.json()).then(d=>{
    // STATUS-FIX (2026-08-12): the real search-engine config is checked too —
    // the display now distinguishes disabled/unreachable/HTTP-error/ok.
    if (d.ok) {
      _set(dot, 'OK — ' + (d.engines || 'SearXNG'), '#60c080');
      _set(ad, 'Reachable OK', '#60c080');
    } else if (d.reason === 'disabled') {
      _set(dot, 'Disabled — turn on websearch toggle!', '#c0a060');
      _set(ad, 'Disabled', '#c0a060');
    } else if (d.reason === 'search_http' && d.status === 400) {
      _set(dot, 'ERROR: SearXNG replies 400 (engines?) — engines: ' + (d.engines || '(empty)'), '#d05050');
      _set(ad, 'SearXNG 400', '#d05050');
    } else if (d.reason === 'search_error') {
      _set(dot, 'Search error: ' + (d.detail || d.reason), '#d05050');
      _set(ad, 'Search error', '#d05050');
    } else {
      _set(dot, 'Unreachable (' + (S.searxngHost || 'localhost:8888') + ')', '#d05050');
      _set(ad, 'Unreachable', '#d05050');
    }
  }).catch(()=>{
    _set(dot, 'Error — server unreachable?', '#d05050');
    _set(ad, 'Error', '#d05050');
  });
}

function updateForceHeaderBadge() {
  const badge = document.getElementById('force-badge');
  if (!badge) return;
  const hasSkips   = Object.keys(S.skippedAgents).length > 0;
  const hasForce   = S.forcedComplexity !== 'auto';
  badge.classList.toggle('on', hasSkips || hasForce);
}

// -- Init -------------------------------------------------------
// FIX: init is now sequential and more robust.
// loadModels must complete before renderAgentCards.
async function init() {
  const cardsEl = document.getElementById('agent-cards');
  if (cardsEl) cardsEl.innerHTML = '<div class="loading-msg">&#9679; Loading models...</div>';

 await loadModels();
  await fetch('/memory/clear_session', {method:'POST'});  // Seitenreload = neuer Chat
  await loadAgentMap();
  await loadSettings();    // renderAgentCards now uses S.currentAssignments
  if (S.duoWebsearch || S.pipelineWebsearch || S.automapPipelineWebsearch) checkWebsearchStatus();
  await loadPresets();
  await loadMemory();
  await loadVisionConfig();
  await loadSpecialAgentsConfig();
  loadSoulStatus();
  loadChatHistory();
  buildAgentForceButtons();
  document.getElementById('h-dot').className = 'on';
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
  renderAgentMap(changed);
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
      if (d.profiles && d.profiles.length) {
        S.modelProfiles = {};
        d.profiles.forEach(function(p) { if (p.name) S.modelProfiles[p.name] = p; });
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
    if (reg && reg === cur) {
      // automap active: keep the label on top, rebuild the remaining options
      let opts = '<option value="' + esc(reg) + '" style="color:#4878c0;font-weight:600" selected>'
               + '⟳ ' + esc(reg) + ' (active)</option>';
      if (S.models.filter(m => m !== reg).length)
        opts += '<option disabled>────────────</option>';
      S.models.forEach(function(m) {
        if (m !== reg) opts += '<option value="' + esc(m) + '">' + esc(m) + '</option>';
      });
      sel.innerHTML = opts;
      sel.value = reg;
    } else {
      sel.innerHTML = modelOpts(cur);
      if (cur) sel.value = cur;
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
}

async function loadSettings() {
  try {
    const s = await (await fetch('/settings')).json();

    S.iters          = s.max_iterations || 2;
    // NOTE: S.mode = s.mode is correct — server.py now preserves mode on preset load,
    // so it always returns the correct (user-chosen) mode.
    S.mode           = s.mode || 'automap';
    S.activePreset   = s.active_preset || '';
    S.constraintMode = s.constraint_mode !== false;
    S.smartPreload   = s.smart_preload_enabled !== false;  // FIX: default true (was false)

    document.getElementById('iters-in').value = S.iters;
    document.getElementById('h-iters-val').textContent = S.iters;
    document.getElementById('h-preset-label').textContent = S.activePreset || 'no preset';
    document.getElementById('cfl-toggle').checked = S.constraintMode;

    // Auto-save chats (on by default, can be turned off)
    S.chatAutosave = s.chat_autosave_enabled !== false;
    var _chatAutosaveEl = document.getElementById('chat-autosave-toggle');
    if (_chatAutosaveEl) _chatAutosaveEl.checked = S.chatAutosave;

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
      S.duoToolRounds = parseInt(s.duo_tool_rounds || 0);
      dtrSel.value = String(S.duoToolRounds);
    }
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
    S.duoUntilFinished = s.until_finished || false;
    var _dufTog = document.getElementById('duo-until-finished-toggle');
    if (_dufTog) _dufTog.checked = S.duoUntilFinished;
    S.askUserTimeoutSeconds = s.ask_user_timeout_until_finished_seconds || 300;
    S.askUserMaxPer10min = s.ask_user_max_per_10min || 5;
    S.askUserAutoAnswer = s.ask_user_auto_answer || 'Use best judgment, document decision in commit message.';
    // Ensure chunk-dependent toggles are correctly shown/hidden
    onChunkingChange();
    // Until-finished is always ∞ — no cap field anymore
        // restore chunking state
        onChunkingChange();
    updateAgenticCombinedWarn();
    setDuoSubMode(S.duoAgenticMode ? 'agentic' : 'critic');
    updateComplexityVisibility();
    var dmtrEl = document.getElementById('duo-max-tool-rounds');
    if (dmtrEl) dmtrEl.value = s.duo_max_tool_rounds || 64;
    var dmtrCapEl = document.getElementById('duo-max-tool-rounds-runtime-cap');
    if (dmtrCapEl) dmtrCapEl.value = s.duo_max_tool_rounds_runtime_cap || 300;
    var dctEl = document.getElementById('duo-compress-threshold');
    if (dctEl) dctEl.value = typeof s.duo_compress_threshold === 'number' ? s.duo_compress_threshold : 0;
    var dpmsEl = document.getElementById('duo-planner-max-steps');
    if (dpmsEl) dpmsEl.value = typeof s.duo_planner_max_steps === 'number' ? s.duo_planner_max_steps : 0;
    S.duoRuntimeProfile = (s.duo_runtime_profile || 'auto');
    var drpEl = document.getElementById('duo-runtime-profile');
    if (drpEl) drpEl.value = S.duoRuntimeProfile;
    S.duoProfileSpeedModel = (s.duo_profile_speed_model || 'qwen3.5:4b');
    S.duoProfileQualityModel = (s.duo_profile_quality_model || 'qwen3.5:9b-ud');
    S.duoRuntimeProfileLockOverride = !!s.duo_runtime_profile_lock_override;
    var drpLockEl = document.getElementById('duo-runtime-profile-lock-override');
    if (drpLockEl) drpLockEl.checked = S.duoRuntimeProfileLockOverride;
    S.duoUsePresetModels = !!s.duo_use_preset_models;
    var dpmEl = document.getElementById('duo-use-preset-models-toggle');
    if (dpmEl) dpmEl.checked = S.duoUsePresetModels;
    S.duoUsePresets = (s.duo_use_presets !== false);
    var dupsEl = document.getElementById('duo-use-presets-toggle');
    if (dupsEl) dupsEl.checked = !!S.duoUsePresets;
    var dupsPanel = document.getElementById('duo-presets-panel');
    if (dupsPanel) dupsPanel.style.display = S.duoUsePresets ? 'block' : 'none';
    updateDuoRuntimeProfileHint();
    updateDuoPairHint();
    S.duoCtxAgentic = parseInputAsOptionalInt(s.duo_coder_ctx_agentic, 16384);
    var dctxAgEl = document.getElementById('duo-ctx-agentic');
    if (dctxAgEl) dctxAgEl.value = S.duoCtxAgentic != null ? S.duoCtxAgentic : 16384;
    // Agentic ctx applies to Solo + Until-Finished — one value, both modes
    S.duoCtxUntilFinished = S.duoCtxAgentic;
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
    // sync the planner input status
    if (S.duoPlannerUseCoderCtx && dctxPlEl) { dctxPlEl.disabled = true; dctxPlEl.value = S.duoCtxAgentic; }
    // Planner / coder model
    S.duoPlannerModel = s.duo_planner_model || '';
    // PLANNER-MAX-TOKENS (0.99.2): visible output budget (0 = context as limit)
    S.duoPlannerMaxTokens = parseInt(s.duo_planner_max_tokens) || 8000;
    var dpmtEl = document.getElementById('duo-planner-maxtokens-inp');
    if (dpmtEl) dpmtEl.value = S.duoPlannerMaxTokens;
    S.duoCoderModel = s.duo_coder_model || (s.agents && s.agents.duo_coder && s.agents.duo_coder.model) || '';
    var _moeRaw = s.moe_cpu_experts;
    S.moeCpuExpertsMap = (typeof _moeRaw === 'object' && _moeRaw !== null && !Array.isArray(_moeRaw)) ? _moeRaw : {};
    S.moeExpertDefaults = s.moe_expert_defaults || {};
    S.moeAutodetect = s.moe_autodetect || {};
    S.ctxDefaults = s.ctx_defaults || {};
    var configMoeWrap = document.getElementById('config-moe-experts');
    if (configMoeWrap) configMoeWrap.style.display = 'block';
    rebuildMoeModelDropdowns();
    updateMoeVisibility();
    S.llamaMlock = s.llama_mlock !== false;
    var mlockEl = document.getElementById('llama-mlock-toggle');
    if (mlockEl) mlockEl.checked = S.llamaMlock;
    var configMlockEl = document.getElementById('config-mlock-toggle');
    if (configMlockEl) configMlockEl.checked = S.llamaMlock;
    updateMlockHint();
    S.llamaCacheReuse = parseInt(s.llama_cache_reuse, 10);
    if (isNaN(S.llamaCacheReuse) || S.llamaCacheReuse < 0) S.llamaCacheReuse = 0;
    var cacheReuseEl = document.getElementById('llama-cache-reuse-input');
    if (cacheReuseEl) cacheReuseEl.value = S.llamaCacheReuse;
    updateMoeExpertDefaultHint();
    var dplmEl = document.getElementById('duo-planner-model-sel');
    if (dplmEl && S.duoPlannerModel) dplmEl.value = S.duoPlannerModel;
    var dcmEl = document.getElementById('duo-coder-model-sel');
    if (dcmEl && S.duoCoderModel) dcmEl.value = S.duoCoderModel;
    // Planner / Coder TTL
    S.duoPlannerTtl = parseInt(s.duo_planner_ttl_seconds || 0) || 0;
    S.duoCoderTtl = parseInt(s.duo_coder_ttl_seconds || 0) || 0;
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
    var _dtTierRadio = document.getElementById('dt-tier-' + ((S.directToolsTier === 'python' || S.directToolsTier === 'full' || S.directToolsTier === 'off') ? S.directToolsTier : 'readonly'));
    if (_dtTierRadio) _dtTierRadio.checked = true;
    var _dtR = document.getElementById('direct-tools-rounds');
    var _dtRV = document.getElementById('direct-tools-rounds-v');
    if (_dtR) _dtR.value = S.directToolsRounds;
    if (_dtRV) _dtRV.textContent = S.directToolsRounds;
    updateDirectToolsHint();
    updateDirectToolsTierHint();
    updateChatToolsBadge();
    updateComposerToolStatus();
    updateChatToolsSectionVisibility();
    updatePassFilesButtons();

  // AutoMap Advanced
  S.automapCodeDuoEnabled = s.automap_code_duo_enabled || false;
  S.automapDuoPreExplore = s.automap_duo_pre_explore || false;
  S.automapDuoParallelPreexplore = s.automap_duo_parallel_preexplore || false;
  S.automapPipelineWebsearch = s.automap_pipeline_websearch_enabled || false;
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
    S.subagentLite = s.subagent_lite_enabled !== false;  // default an
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
    if (suEl) suEl.checked = s.startup_preload_enabled !== false;
    var jkaEl = document.getElementById('judge-keepalive-toggle');
    if (jkaEl) jkaEl.checked = s.judge_keepalive_enabled !== false;
    // Analyst Sub-Toggle
    var saEl = document.getElementById('startup-preload-analyst-toggle');
    if (saEl) saEl.checked = s.startup_preload_analyst === true;
    var sjaEl = document.getElementById('startup-preload-judge-agentic-toggle');
    if (sjaEl) sjaEl.checked = s.startup_preload_judge_in_agentic !== false;
    var scEl = document.getElementById('startup-preload-coder-toggle');
    if (scEl) scEl.checked = s.startup_preload_coder === true;
    var pdEl = document.getElementById('pin-direct-toggle');
    if (pdEl) pdEl.checked = s.pin_direct_after_response === true;
    // Vision-Agent state
    S.visionAgentEnabled = s.vision_agent_enabled || false;
    S.visionAgentModel   = s.vision_agent_model   || '';
    S.visionAgentMode    = s.vision_agent_mode     || 'sequential';
    // PIPELINE-VISION: pass images directly to multimodal pipeline agents?
    S.pipelineVisionDirect = s.pipeline_vision_direct !== false;
    // PIPELINE-VISION per role: which roles get raw images directly.
    S.pipelineVisionRoles = s.pipeline_vision_roles || {};
    _applyPipelineVisionRolesUI();
    // AGENT-CTX: per-agent context-size overrides from settings.ctx_overrides
    S.ctxOverrides = (s.ctx_overrides && s.ctx_overrides.roles) ? s.ctx_overrides.roles : {};
    var vaEl = document.getElementById('va-enabled-toggle');
    if (vaEl) vaEl.checked = S.visionAgentEnabled;
    // IMAGE-PROCESSING-MODE: central selector (direct | preprocess | pipeline).
    // Persisted key wins; fall back to legacy toggles so existing setups map
    // onto the new selector without reconfiguration.
    S.imageMode = s.image_processing_mode
      || (S.visionAgentEnabled ? 'pipeline'
          : (S.pipelineVisionDirect ? 'direct' : ''));
    if (S.imageMode) _applyImageModeUI(S.imageMode);
    populateVisionAgentModelSel();
    _updateVisionAgentModeUI(S.visionAgentMode);
    var ar = document.getElementById('preload-analyst-row');
    if (ar) ar.style.opacity = (s.startup_preload_enabled !== false) ? '1' : '0.4';
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

    // call renderAgentCards - S.models is already filled now
    const agents = s.agents || {};
    console.log('[loadSettings] agents keys:', Object.keys(agents), 'S.models:', S.models.length);
    if (Object.keys(agents).length > 0) {
      try { renderAgentCards(agents); } catch(e) { console.error('[loadSettings] renderAgentCards error:', e); }
    } else {
      const cardsEl = document.getElementById('agent-cards');
      if (cardsEl) cardsEl.innerHTML = '<div class="empty">No agent configuration from server.</div>';
    }

    // initialize g-model with the first agent
    const g = document.getElementById('g-model');
    const firstAgent = Object.values(agents)[0];
    if (g && firstAgent && firstAgent.model) {
      g.innerHTML = modelOpts(firstAgent.model);
      g.value = firstAgent.model;
    }

    // Adopt registry data into currentAssignments
    const reg = s._registry || {};
    Object.keys(reg).forEach(function(agent) {
      if (!S.currentAssignments[agent]) S.currentAssignments[agent] = {};
      S.currentAssignments[agent].model = reg[agent];
    });
    if (Object.keys(reg).length) renderAgentMap();

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
  } catch(e) {
    console.error('loadSettings error:', e);
    const cardsEl = document.getElementById('agent-cards');
    if (cardsEl) cardsEl.innerHTML = '<div class="empty">&#9888; Server unreachable.<br><span style="font-size:9px">Is FastAPI started?</span></div>';
  }
}

let _settingsPatchQueue = {};
let _settingsPostTimer = null;
let _settingsPostWaiters = [];

async function _flushQueuedSettings() {
  var _payload = _settingsPatchQueue;
  _settingsPatchQueue = {};
  if (!_payload || !Object.keys(_payload).length) {
    var _emptyWaiters = _settingsPostWaiters.splice(0);
    _emptyWaiters.forEach(function(_resolve){ try { _resolve(); } catch(e) {} });
    return;
  }
  try {
    await fetch('/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(_payload)
    });
  } catch(e) {
    console.warn('postSettings flush failed:', e);
  }
  var _waiters = _settingsPostWaiters.splice(0);
  _waiters.forEach(function(_resolve){ try { _resolve(); } catch(e) {} });
}

function postSettings(patch) {
  if (!patch || typeof patch !== 'object') return Promise.resolve();
  _settingsPatchQueue = Object.assign(_settingsPatchQueue || {}, patch);
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
  el.innerHTML = '<span style="color:#9a74dc">P:</span> ' + esc(planner)
    + ' &nbsp;|&nbsp; <span style="color:#20b0a0">C:</span> ' + esc(coder);
}

function populateDuoModelGroup() {
  // fill both new selects (planner + coder) with all available models
  var plannerSel = document.getElementById('duo-planner-model-sel');
  var coderSel = document.getElementById('duo-coder-model-sel');
  var allModels = (S.models || []).filter(function(m) {
    var base = m.split(':')[0].toLowerCase();
    if (base === 'granite4' || base === 'granite3.2-vision') return false;
    return true;
  });

  function buildOptions(sel, includeNone) {
    if (!sel) return;
    var current = sel.value;
    sel.innerHTML = '';
    if (includeNone) {
      var noneOpt = document.createElement('option');
      noneOpt.value = ''; noneOpt.textContent = '– no planner –';
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
  buildOptions(plannerSel, true);
  buildOptions(coderSel, false);
  // apply the stored state
  if (S.duoPlannerModel && plannerSel) plannerSel.value = S.duoPlannerModel;
  if (S.duoCoderModel && coderSel) coderSel.value = S.duoCoderModel;
  updateDuoPairHint();
}

function onDuoPlannerModelChange(model) {
  S.duoPlannerModel = model || '';
  postSettings({duo_planner_model: S.duoPlannerModel});
  updateDuoPairHint();
}

function onDuoCoderModelChange(model) {
  S.duoCoderModel = model || '';
  // write the coder model directly into agents.duo_coder.model (backend compatible)
  postSettings({duo_pair: 'free', agents: {duo_coder: {model: S.duoCoderModel}}, duo_coder_model: S.duoCoderModel});
  if (S.currentAssignments.duo_coder) S.currentAssignments.duo_coder.model = model;
  updateDuoPairHint();
  updateMoeVisibility();
}

function onDuoPairChange(val) {
  if (val.startsWith('model:')) {
    var model = val.slice(6);
    S.duoPair = 'free';
    postSettings({duo_pair: 'free', agents: {duo_coder: {model: model}}});
    // NOTE: server POST /settings now performs a deep merge for agents
    // (BUG-AGENT-MERGE FIX) — other agent keys are no longer deleted.
    if (S.currentAssignments.duo_coder) S.currentAssignments.duo_coder.model = model;
  } else {
    S.duoPair = val;
    postSettings({duo_pair: val});
  }
  syncDuoPairSelectWithState();
  updateDuoPairHint();
  updateDuoRuntimeProfileHint();
  updateDuoPairVramHint(parseFloat(document.getElementById('vram-budget-inp') && document.getElementById('vram-budget-inp').value || 7.5));
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
  S.mode = mode;
  // FIX: nur Buttons innerhalb #mode-btns-row, nicht cfgtab-Buttons (teilen .mbtn Klasse)
  document.querySelectorAll('#mode-btns-row .mbtn').forEach(function(b) { b.classList.remove('on'); });
  el.classList.add('on');
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
  var _duoAcc = document.getElementById('duo-mode-accordion');
  if (_duoAcc) _duoAcc.style.display = mode === 'code_duo' ? 'block' : 'none';
  updateAutomapAdvancedVisibility();
  updateWebsearchHint();
  updateChatToolsBadge();
  updateComposerToolStatus();
  updateChatToolsSectionVisibility();
  updateModeDesc();
  updateComplexityVisibility();
  postSettings({mode: mode});
}

function setModeUI(mode) {
  S.mode = mode;
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
  // FIX: sync the select value with S.duoPair
  syncDuoPairSelectWithState();
  updateDuoPairHint();
  var _duoAcc2 = document.getElementById('duo-mode-accordion');
  if (_duoAcc2) _duoAcc2.style.display = mode === 'code_duo' ? 'block' : 'none';
  updateAutomapAdvancedVisibility();
  updateWebsearchHint();
  updateChatToolsBadge();
  updateComposerToolStatus();
  updateChatToolsSectionVisibility();
  updateModeDesc();
  updateComplexityVisibility();
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


function toggleDuoModeAccordion() {
  var body = document.getElementById('duo-mode-body');
  var chev = document.getElementById('duo-mode-chevron');
  if (!body) return;
  var open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  if (chev) chev.style.transform = open ? '' : 'rotate(180deg)';
}

function toggleDuoPresetsUI(checked) {
  var pnl = document.getElementById('duo-presets-panel');
  if(pnl) pnl.style.display = checked ? 'block' : 'none';
  S.duoUsePresets = !!checked;
  postSettings({duo_use_presets: S.duoUsePresets});
  updateDuoRuntimeProfileHint();
  if(typeof updateCtxScopeHint === 'function') updateCtxScopeHint();
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
function setDuoSubMode(mode) {
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
  // h-mode Tag oben links: "⇄ duo" im Critic-Modus, "⇄ coder" im Agentic-Modus
  if (S.mode === 'code_duo') {
    var _hm = document.getElementById('h-mode');
    if (_hm) _hm.textContent = '\u21C4 ' + (isCritic ? 'duo' : 'coder');
  }
  if (isCritic) {
    S.duoToolRounds = 0;
    var _dtrSelC = document.getElementById('duo-tool-rounds-sel');
    if (_dtrSelC) _dtrSelC.value = '0';
    postSettings({duo_tool_rounds: 0, max_iterations: S.iters || 2, duo_agentic_mode: false});
  } else {
    S.duoToolRounds = 1;
    var _dtrSelA = document.getElementById('duo-tool-rounds-sel');
    if (_dtrSelA) _dtrSelA.value = '1';
    if (!S._thinkingBeforeChunking) S._thinkingBeforeChunking = S.duoAgenticThinking;
    postSettings({duo_tool_rounds: 1, max_iterations: 1, duo_agentic_mode: true});
    S.iters = 1;
    document.getElementById('iters-in').value = 1;
    document.getElementById('h-iters-val').textContent = 1;
    updateAgenticHint();
  }
  updateCtxScopeHint();
  updateComplexityVisibility();
}

function updateAgenticHint() {
  var hint = document.getElementById('duo-agentic-hint');
  if (!hint) return;
  var toolActive = S.duoToolRounds > 0;
  if (toolActive) {
    hint.className = 'ui-hint';
    hint.innerHTML = '✓ <b>Agentic:</b> Coder explores, implements and tests on its own '
      + '(explore → patch_file → run_bash → fix).';
    hint.style.display = 'block';
  } else {
    hint.style.display = 'none';
  }
}

// -- Agent Cards ------------------------------------------------
function renderAgentCards(agents) {
  const c = document.getElementById('agent-cards');
  if (!c) { console.error('[renderAgentCards] #agent-cards nicht gefunden!'); return; }
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
    // Duo agents belong to the duo panel, not the agent cards
    if (key === 'duo_coder' || key === 'duo_critic') continue;
    const color = m.color;
    const temp = parseFloat(cfg.temperature || 0.3);
    const toks = parseInt(cfg.max_tokens || 400);

    const div = document.createElement('div');
    div.className = 'acard';

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
      if (prof.vision || ['vl','llava','vision','moondream','minicpm','qwen3.5','qwen3.6','hermes3.6','hermes','gemma-4','tiel-coder'].some(function(v){return (activeModel||'').toLowerCase().includes(v);}))
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
      const prof2 = S.modelProfiles[activeModel] || S.modelProfiles[(activeModel||'').split(':')[0]] || {};
      const _isVis = !!(prof2.vision);
      const _isThk = !!(prof2.thinking);
      var _expl = [];
      if (_isVis) _expl.push('<span style="color:#8858c0">🖼 processes images directly</span>');
      else _expl.push('<span style="color:var(--tx3)">no vision — images via vision-agent/prepro</span>');
      if (_isThk) _expl.push('<span style="color:#4878c0">🧠 can think (Thinking toggle)</span>');
      else _expl.push('<span style="color:var(--tx3)">without thinking</span>');
      const expl = document.createElement('div');
      expl.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:8.5px;color:var(--tx3);margin:2px 0 4px;line-height:1.6';
      expl.innerHTML = _expl.join(' · ');
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
    tempLbl.innerHTML = 'Temperature <span id="' + tvId + '">' + temp.toFixed(2) + '</span>';
    tempCell.appendChild(tempLbl);
    const tempRow = document.createElement('div');
    tempRow.className = 'sl-row';
    const tempSlider = document.createElement('input');
    tempSlider.type = 'range'; tempSlider.min = '0'; tempSlider.max = '1'; tempSlider.step = '0.05';
    tempSlider.value = temp;
    tempSlider.dataset.temp = key;
    tempSlider.addEventListener('input', (function(id) {
      return function() {
        const el = document.getElementById(id);
        if (el) el.textContent = parseFloat(this.value).toFixed(2);
        const v = document.getElementById(id + '-val');
        if (v) v.textContent = parseFloat(this.value).toFixed(2);
      };
    })(tvId));
    const tempVal = document.createElement('div');
    tempVal.className = 'sl-val';
    tempVal.id = tvId + '-val';
    tempVal.textContent = temp.toFixed(2);
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
    ctxLbl.innerHTML = 'Context <span id="' + ctxId + '">' + (ctxOverride ? ctxOverride : 'Auto') + '</span>'
      + '<span class="ctx-eff">' + (ctxOverride ? '' : '= ' + effCtxDefault) + '</span>'
      + '<span style="font-size:8px;color:var(--tx3);margin-left:auto">KV: ~' + (ctxOverride ? (ctxOverride/8192).toFixed(1) : (effCtxDefault/8192).toFixed(1)) + 'GB</span>';
    ctxCell.appendChild(ctxLbl);
    const ctxRow = document.createElement('div');
    ctxRow.className = 'sl-row';
    const ctxSlider = document.createElement('input');
    ctxSlider.type = 'range'; ctxSlider.min = '0'; ctxSlider.max = '32768'; ctxSlider.step = '1024';
    ctxSlider.value = ctxOverride;
    ctxSlider.dataset.ctx = key;
    ctxSlider.addEventListener('input', (function(id, refresh) {
      return function() {
        const el = document.getElementById(id);
        const v = parseInt(this.value);
        if (el) el.textContent = v ? v : 'Auto';
        const kv = document.getElementById(id + '-kv');
        if (kv) kv.textContent = 'KV: ~' + (v ? (v/8192).toFixed(1) : ((effCtxDefault/8192).toFixed(1))) + 'GB';
        refresh();
      };
    })(ctxId, refreshBudget));
    const ctxVal = document.createElement('div');
    ctxVal.className = 'sl-val';
    ctxVal.id = ctxId + '-kv';
    ctxVal.textContent = 'KV: ~' + (ctxOverride ? (ctxOverride/8192).toFixed(1) : (effCtxDefault/8192).toFixed(1)) + 'GB';
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
    const thinkRow = document.createElement('div');
    thinkRow.className = 'fl';
    thinkRow.innerHTML = 'Thinking <input type="checkbox" data-agent="' + key + '"'
      + (think ? ' checked' : '') + ' style="margin-left:6px;accent-color:#8858c0">'
      + '<span class="thk-badge" style="font-size:8px;padding:1px 4px;border-radius:2px;'
      + 'background:rgba(136,88,192,.15);color:#8858c0;border:1px solid rgba(136,88,192,.3);'
      + 'margin-left:8px">\uD83E\uDDE0</span>';
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
  // fill all selects with the current models (if S.models is already filled)
  refreshAllSelects();
  // update the agent map after rendering
  setTimeout(buildAgentMapFromSettings, 50);
  // VISION-HINT: update the image path (multimodal direct / prepro / vision agent)
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
}

// AGENT-DROPDOWN-SAVE (2026-09-01): save the model in the agent-card dropdown directly
// (only model — sliders/toggles stay with the Apply button).
async function saveAgentModelDirect(key, sel) {
  if (!sel || !sel.value) return;
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
      sel.dataset.cur = model;
      if (!S.currentAssignments[key]) S.currentAssignments[key] = {};
      S.currentAssignments[key].model = model;
      sel.style.borderColor = '#22c55e';
      setTimeout(function() { sel.style.borderColor = ''; }, 1200);
      // VISION-HINT: Bild-Pfad nach Modellwechsel aktualisieren
      if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
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
  const temperature = tempSlider ? parseFloat(tempSlider.value) : 0.3;
  const max_tokens  = tokSlider ? parseInt(tokSlider.value) : 400;
  // AGENT-THINKING: read the toggle + budget (3rd slider = thinking budget)
  const thkCb = card.querySelector('input[type=checkbox][data-agent="' + key + '"]');
  const thinking = thkCb ? thkCb.checked : false;
  const budgetSlider = card.querySelector('input[type=range][data-budget="' + key + '"]');
  const thinking_budget = budgetSlider ? parseInt(budgetSlider.value || '0') : 0;
  // AGENT-CTX: read the context size (0 = auto / model default)
  const ctxSlider = card.querySelector('input[type=range][data-ctx="' + key + '"]');
  const ctx = ctxSlider ? parseInt(ctxSlider.value || '0') : 0;

  if (!model) { btn.textContent = 'No model!'; setTimeout(function(){btn.textContent='Apply';},1800); return; }

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
      btn.textContent = 'OK - ' + model.slice(0,12);
      btn.classList.add('ok');
      if (!S.currentAssignments[key]) S.currentAssignments[key] = {};
      S.currentAssignments[key].model = model;
      if (!S.ctxOverrides) S.ctxOverrides = {};
      if (ctx > 0) S.ctxOverrides[key] = ctx; else delete S.ctxOverrides[key];
      renderAgentMap();
      // VISION-HINT: update the image path after a model switch
      if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
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
async function loadPresets() {
  try {
    const d = await (await fetch('/presets')).json();
    renderPresets(d);
    const sel = document.getElementById('prompt-agent-sel');
    sel.innerHTML = '';
    Object.entries(AGENT_META).forEach(function(e) {
      if (e[0] === 'judge') return;
      const opt = document.createElement('option');
      opt.value = e[0]; opt.textContent = e[1].label;
      sel.appendChild(opt);
    });
  } catch(e) {}
}

function renderPresets(presets) {
  const c = document.getElementById('preset-list');
  const keys = Object.keys(presets);
  if (!keys.length) { c.innerHTML = '<div class="empty">No presets saved</div>'; return; }
  c.innerHTML = '';
  keys.forEach(function(name) {
    const isActive = name === S.activePreset;
    const item = document.createElement('div');
    item.className = 'preset-item';
    const nd = document.createElement('div');
    nd.className = 'preset-name';
    nd.textContent = name;
    if (isActive) {
      const dot = document.createElement('span');
      dot.className = 'preset-active'; dot.textContent = ' \u25CF active';
      nd.appendChild(dot);
    }
    const bp = document.createElement('button'); bp.className = 'ghost'; bp.textContent = 'Prompts';
    const bs = document.createElement('button'); bs.className = 'pload'; bs.textContent = 'Save';
    bs.title = 'Overwrite this preset with the current config';
    const bl = document.createElement('button'); bl.className = 'pload'; bl.textContent = 'Load';
    const bd = document.createElement('button'); bd.className = 'pdel'; bd.textContent = '\u00D7';
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
  const name = document.getElementById('preset-name-in').value.trim();
  if (!name) return;
  await savePresetAs(name);
  document.getElementById('preset-name-in').value = '';
  await loadPresets();
}

async function savePresetAs(name) {
  if (!name) return;
  await fetch('/presets/' + encodeURIComponent(name), {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({max_iterations: S.iters, constraint_mode: S.constraintMode})
  });
  await loadPresets();
}

async function loadPreset(name) {
  const r = await fetch('/presets/' + encodeURIComponent(name) + '/load', {method:'POST'});
  const d = await r.json();
  if (d.ok) {
    S.activePreset = name;
    document.getElementById('h-preset-label').textContent = name;
    if (d.settings) {
      S.iters = d.settings.max_iterations || S.iters;
      // NOTE: S.mode is NOT set here — the server does not return mode,
      // and even if it did, code_duo / whatever the user chose stays.
      document.getElementById('iters-in').value = S.iters;
      document.getElementById('h-iters-val').textContent = S.iters;
      setModeUI(S.mode);
    }
    await loadSettings();
    await loadPresets();
  }
}

async function delPreset(name) {
  if (!confirm('Delete preset "' + name + '"?')) return;
  await fetch('/presets/' + encodeURIComponent(name), {method:'DELETE'});
  if (S.activePreset === name) {
    S.activePreset = null;
    document.getElementById('h-preset-label').textContent = 'no preset';
  }
  document.getElementById('prompt-editor-section').style.display = 'none';
  await loadPresets();
}

// -- Prompt Editor ----------------------------------------------
async function openPromptEditor(name) {
  S.editingPreset = name;
  document.getElementById('prompt-editor-section').style.display = 'block';
  const p = document.querySelector('#p-presets');
  p.scrollTo(0, p.scrollHeight);
  await loadPrompt();
}

async function loadPrompt() {
  if (!S.editingPreset) return;
  const agent = document.getElementById('prompt-agent-sel').value;
  try {
    const d = await (await fetch('/presets/' + encodeURIComponent(S.editingPreset) + '/prompts/' + agent)).json();
    document.getElementById('prompt-textarea').value = d.content || '';
    document.getElementById('custom-badge').style.display = d.is_custom ? 'inline' : 'none';
  } catch(e) {}
}

function promptChanged() {
  document.getElementById('custom-badge').style.display = 'inline';
}

async function savePrompt() {
  if (!S.editingPreset) return;
  const agent   = document.getElementById('prompt-agent-sel').value;
  const content = document.getElementById('prompt-textarea').value;
  await fetch('/presets/' + encodeURIComponent(S.editingPreset) + '/prompts/' + agent, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({content: content})
  });
  document.getElementById('custom-badge').style.display = 'inline';
}

async function resetPrompt() {
  if (!S.editingPreset) return;
  const agent = document.getElementById('prompt-agent-sel').value;
  const d = await (await fetch('/presets/' + encodeURIComponent(S.editingPreset) + '/prompts/' + agent)).json();
  document.getElementById('prompt-textarea').value = d.content || '';
  document.getElementById('custom-badge').style.display = d.is_custom ? 'inline' : 'none';
}

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
    if (c) c.innerHTML = '<div style="font-family:monospace;font-size:10px;color:#b04040;padding:8px">Error: ' + e.message + '</div>';
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

function _cleanupLoadTimers() {
  if (S.curAgent && S.curAgent.lt) {
    clearTimeout(S.curAgent.lt); S.curAgent.lt = null;
  }
  document.querySelectorAll('[id^="lh-"]').forEach(function(el) {
    if (el._loadTimer) { clearInterval(el._loadTimer); el._loadTimer = null; }
  });
  if (S._runAbortCtrl) { S._runAbortCtrl.abort(); S._runAbortCtrl = null; }
}

function _pruneToolCallRows(body, maxRows) {
  if (!body) return;
  var rows = body.querySelectorAll('.tool-call-row');
  if (rows.length <= maxRows) return;
  var toRemove = rows.length - maxRows;
  var notice = body.querySelector('.tc-pruned-notice');
  var existingCount = notice ? parseInt(notice.dataset.count || '0', 10) : 0;
  for (var i = 0; i < toRemove; i++) {
    var next = rows[i].nextElementSibling;
    while (next && !next.classList.contains('tool-call-row') && !next.classList.contains('tc-pruned-notice')) {
      var tmp = next.nextElementSibling;
      next.remove();
      next = tmp;
    }
    rows[i].remove();
  }
  var totalPruned = existingCount + toRemove;
  if (!notice) {
    notice = document.createElement('div');
    notice.className = 'tc-pruned-notice';
    body.insertBefore(notice, body.firstChild);
  }
  notice.dataset.count = totalPruned;
  notice.textContent = '\u2026 ' + totalPruned + ' older tool calls hidden';
}

// -- New Chat ---------------------------------------------------
async function newChat() {
  await fetch('/memory/clear_session', {method:'POST'});
  _cleanupLoadTimers();
  if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
  if (_tokenRafId) { cancelAnimationFrame(_tokenRafId); _tokenRafId = null; }
  _tokenQueue = [];
  document.getElementById('chat').innerHTML = '';
  S.pendingImgs = [];
  document.getElementById('img-preview').innerHTML = '';
  S.currentChatId = null;
  S.currentChatMessages = [];
  setPauseBtnState('idle');
  setStopBtnState('idle');
  stopAskUserCountdown();
  document.getElementById('h-complexity').style.display = 'none';
  document.getElementById('h-preset-label').textContent = S.activePreset || 'no preset';
  showInfo('New chat started.');
}

function showInfo(txt) {
  const c = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg divider';
  div.textContent = txt;
  c.appendChild(div);
  scrollBtmIfNearBottom(120);
}

function startAskUserCountdown(seconds) {
  stopAskUserCountdown();
  var _remaining = seconds;
  var _badge = document.getElementById('ask-user-countdown');
  if (!_badge) {
    _badge = document.createElement('span');
    _badge.id = 'ask-user-countdown';
    _badge.className = 'ask-user-countdown-badge';
    var _qDiv = document.getElementById('ask-user-question');
    if (_qDiv) _qDiv.appendChild(_badge);
  }
  function _tick() {
    if (_remaining <= 0) {
      _badge.textContent = 'Auto-continue triggered';
      _badge.classList.add('critical');
      clearInterval(S._askUserCountdownInterval);
      S._askUserCountdownInterval = null;
      return;
    }
    var m = Math.floor(_remaining / 60);
    var s = _remaining % 60;
    _badge.textContent = 'Auto-continue in ' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    if (_remaining <= 30) _badge.classList.add('critical');
    _remaining--;
  }
  _tick();
  S._askUserCountdownInterval = setInterval(_tick, 1000);
}

function stopAskUserCountdown() {
  if (S._askUserCountdownInterval) {
    clearInterval(S._askUserCountdownInterval);
    S._askUserCountdownInterval = null;
  }
  var _badge = document.getElementById('ask-user-countdown');
  if (_badge) _badge.remove();
}

// -- Images -----------------------------------------------------
function handleImgs(input) {
  Array.from(input.files).forEach(function(file) {
    const r = new FileReader();
    r.onload = function(e) {
      S.pendingImgs.push({b64: e.target.result.split(',')[1], preview: e.target.result});
      renderImgPreview();
    };
    r.readAsDataURL(file);
  });
  input.value = '';
}

function renderImgPreview() {
  const c = document.getElementById('img-preview');
  c.innerHTML = '';
  S.pendingImgs.forEach(function(img, i) {
    const wrap = document.createElement('div'); wrap.className = 'pitem';
    const im = document.createElement('img'); im.src = img.preview; im.alt = '';
    const rm = document.createElement('button'); rm.className = 'rm'; rm.textContent = '\u00D7';
    rm.addEventListener('click', (function(idx) {
      return function() { S.pendingImgs.splice(idx, 1); renderImgPreview(); };
    })(i));
    wrap.appendChild(im); wrap.appendChild(rm);
    c.appendChild(wrap);
  });
  // VISION-HINT (2026-08-19): Shows clearly which image path will be used.
  if (S.pendingImgs.length) {
    const hint = document.createElement('div');
    hint.id = 'img-vision-hint';
    hint.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:10px;margin-top:4px;padding:3px 8px;border-radius:3px;line-height:1.5';
    const dModel = (S.currentAssignments && S.currentAssignments.direct && S.currentAssignments.direct.model) || '';
    const dProf = S.modelProfiles && (S.modelProfiles[dModel] || S.modelProfiles[(dModel||'').split(':')[0]]);
    const dVision = !!(dProf && dProf.vision);
    if (dVision) {
      hint.style.background = 'rgba(72,120,192,.12)'; hint.style.color = '#80b0e0';
      hint.style.border = '1px solid rgba(72,120,192,.3)';
      hint.textContent = '🖼 Multimodal (' + dModel + ') processes the image directly.';
    } else if (S.visionAgentEnabled && S.visionAgentModel) {
      hint.style.background = 'rgba(136,88,192,.12)'; hint.style.color = '#b090d0';
      hint.style.border = '1px solid rgba(136,88,192,.3)';
      hint.textContent = '👁 Direct model not multimodal — vision-agent (' + S.visionAgentModel + ') describes the image as text.';
    } else {
      hint.style.background = 'rgba(176,64,64,.12)'; hint.style.color = '#d09090';
      hint.style.border = '1px solid rgba(176,64,64,.3)';
      hint.textContent = '⚠ No multimodal direct model + no vision-agent — the image will be ignored.';
    }
    c.appendChild(hint);
  }
}

// -- Chat Rendering ---------------------------------------------
function addUserMsg(text, imgs) {
  const c = document.getElementById('chat');
  const d = document.createElement('div');
  d.className = 'msg msg-user';
  if (imgs.length) {
    const thumbs = document.createElement('div');
    thumbs.className = 'img-thumbs';
    imgs.forEach(function(i) {
      const im = document.createElement('img'); im.src = i.preview; im.alt = '';
      thumbs.appendChild(im);
    });
    d.appendChild(thumbs);
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  d.appendChild(bubble);
  c.appendChild(d);
  scrollBtmIfNearBottom(120);
}

function showStatus(txt) {
  rmEl('status-el');
  const c = document.getElementById('chat');
  const d = document.createElement('div');
  d.className = 'msg status-txt'; d.id = 'status-el';
  d.textContent = txt;
  c.appendChild(d);
  scrollBtmIfNearBottom(120);
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

function _isPlanningStatus(txt) {
  var _t = String(txt || '').toLowerCase();
  return _t.indexOf('planung') >= 0 || _t.indexOf('planner') >= 0 || _t.indexOf('planning') >= 0;
}

function _plannerContextPreview(raw, maxChars) {
  var txt = String(raw || '').replace(/\r\n?/g, '\n');
  if (!txt.trim()) return '—';
  var noiseRe = /^\s*(?:time="[^"]+"\s+level=\w+|#\d+\s|npm\s+error\b|dockerfile:\d+|------|\[\+\]\s+up\s+\d+\/\d+|starting\s+.*services|waiting\s+for\s+services)/i;
  var omitted = 0;
  var lines = txt.split('\n').filter(function(line) {
    if (noiseRe.test(line || '')) {
      omitted += 1;
      return false;
    }
    return true;
  });
  txt = lines.join(' ').replace(/\s+/g, ' ').trim();
  var lim = Math.max(80, parseInt(maxChars || 220, 10) || 220);
  if (txt.length > lim) txt = txt.slice(0, lim - 1).trimEnd() + '…';
  if (omitted > 0) txt += ' [+' + omitted + ' log lines]';
  return txt;
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
  if (S.perfRealTokens > 0 && S.perfFirstTokenAt) {
    _realRate = S.perfRealTokens / Math.max(0.25, (Date.now() - S.perfFirstTokenAt) / 1000);
  }
  var _t = S.perfRealTokens > 0 ? S.perfRealTokens : (S.perfEstTokens || 0);
  var _r = _realRate > 0 ? _realRate.toFixed(1) + ' t/s' : (S.perfTokRate > 0 ? S.perfTokRate.toFixed(1) + ' t/s~' : '');
  var _pm = document.getElementById('planner-tokrate');
  if (_pm) _pm.textContent = (_r || _t ? _t + ' tok' + (_r ? ' \u00b7 ' + _r : '') : 'l\u00e4uft\u2026');
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
      + '<span class="amodel"><span id="planner-tokrate">l\u00e4uft\u2026</span> <span id="planner-elapsed">0s</span></span>';
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

  // Loading indicator: after 2s without tokens "Model loading..." appears
  const _lh = document.createElement('span');
  _lh.id = 'lh-' + tid;
  _lh.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:9px;color:#20b0a0;margin-left:8px;display:none';
  _lh.textContent = '⟳ Loading... 0s';
  const _lt = setTimeout(function() {
  const h = document.getElementById('lh-' + tid);
    if (!h) return;
    h.style.display = 'inline';
    var _sec = 0;
    h._loadTimer = setInterval(function() {
        _sec++;
        if (!document.getElementById('lh-' + tid)) return;
        h.textContent = '⟳ Loading... ' + _sec + 's';
        // color flips to amber after 20s → warning
        h.style.color = _sec > 20 ? '#e09030' : '#20b0a0';
    }, 1000);
}, 2000);  // show earlier: after 2s instead of 8s

  const body = document.createElement('div'); body.className = 'abody live'; body.id = 'ab-' + tid;
  // Think-block: collapsible, injected before body; shown live while model thinks
  const thinkBlock = document.createElement('div'); thinkBlock.className = 'think-block live'; thinkBlock.id = 'tk-' + tid; thinkBlock.style.display = 'none';
  const thinkHdr = document.createElement('div'); thinkHdr.className = 'think-hdr';
  thinkHdr.innerHTML = '<span class="th-label">🧠 Thinking</span><span class="th-tokens" id="tkt-' + tid + '"></span><span class="th-chevron">▲</span>';
  thinkHdr.onclick = function() { thinkBlock.classList.toggle('open'); };
  const thinkBody = document.createElement('div'); thinkBody.className = 'think-body'; thinkBody.id = 'thb-' + tid;
  thinkBlock.appendChild(thinkHdr); thinkBlock.appendChild(thinkBody);
  wrap.appendChild(hdr); wrap.appendChild(thinkBlock); wrap.appendChild(body);
  c.appendChild(wrap);
  S.curAgent = {body: body, tid: tid, name: name, lt: _lt};
  // THINK-BLOCK-FIX (2026-08-19): new agent → discard the old think-block reference.
  // Otherwise the next agent with thinking (e.g. Direct after Vision-Agent) streams into
  // the think block of the PREVIOUS agent at the top of the chat.
  S._thinkBlockId = null;
  scrollBtmIfNearBottom(120);
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

  const _lh = document.createElement('span');
  _lh.id = 'lh-' + tid; _lh.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:9px;color:#7a8fa8;margin-left:8px;display:none';
  _lh.textContent = '⟳ Loading... 0s';
  const _lt = setTimeout(function() {
    const h = document.getElementById('lh-' + tid);
    if (!h) return;
    h.style.display = 'inline';
    var _sec = 0;
    h._loadTimer = setInterval(function() {
      _sec++;
      if (!document.getElementById('lh-' + tid)) return;
      h.textContent = '⟳ Loading... ' + _sec + 's';
      h.style.color = _sec > 20 ? '#e09030' : '#7a8fa8';
    }, 1000);
  }, 2000);

  hdr.appendChild(dot); hdr.appendChild(aname); hdr.appendChild(amodel);
  hdr.appendChild(arole); hdr.appendChild(arnd); hdr.appendChild(atimer); hdr.appendChild(_lh);
  const body = document.createElement('div'); body.className = 'abody live'; body.id = 'ab-' + tid;
  // Think-block for duo agents
  const thinkBlock = document.createElement('div'); thinkBlock.className = 'think-block live'; thinkBlock.id = 'tk-' + tid; thinkBlock.style.display = 'none';
  const thinkHdr = document.createElement('div'); thinkHdr.className = 'think-hdr';
  thinkHdr.innerHTML = '<span class="th-label">🧠 Thinking</span><span class="th-tokens" id="tkt-' + tid + '"></span><span class="th-chevron">▲</span>';
  thinkHdr.onclick = function() { thinkBlock.classList.toggle('open'); };
  const thinkBody = document.createElement('div'); thinkBody.className = 'think-body'; thinkBody.id = 'thb-' + tid;
  thinkBlock.appendChild(thinkHdr); thinkBlock.appendChild(thinkBody);
  wrap.appendChild(hdr); wrap.appendChild(thinkBlock); wrap.appendChild(body);
  c.appendChild(wrap);
  S.curAgent = {body: body, tid: tid, name: label, lt: _lt};
  S._thinkBlockId = null;
  scrollBtmIfNearBottom(120);
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
  _tb.innerHTML = '<div class="think-hdr" onclick="this.parentElement.classList.toggle(\'open\')">'
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
  const summary = verdict.verdict || (approved ? 'Code korrekt' : 'Probleme gefunden');

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
  txt = txt.replace(/(?:^|\n)\[Tool-Loop: [^\]]+\](?=\n|$)/g, '');
  txt = txt.replace(/\n[ \t]+\n/g, '\n\n');
  txt = txt.replace(/(?:\n\s*){3,}/g, '\n\n');
  return txt;
}

// ── Token Render Queue ─────────────────────────────────────────────────────
// Tokens arrive in bursts from the server. The queue buffers them and releases
// them evenly per rAF — the same feel as the planner animation.
// ── Chat Markdown Renderer ─────────────────────────────────────────────────
// CHAT-MD-FIX (2026-08-27): normal (direct) responses are rendered as markdown
// like LM Studio / llama.cpp UIs: real block elements (p, ul/ol, h1-h3,
// pre>code, blockquote, hr, tables) instead of a span flood with <br>. Important:
// the result lands in a <div> — block elements inside a <span> get broken by
// the browser during innerHTML parsing (adoption agency) and the formatting breaks.
function _escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _mdInline(s) {
  // Inline-Code, Bold, Italic, Links (nur http/https, target=_blank)
  s = s.replace(/`([^`\n]+)`/g, '<code class="coder-inline-code">$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
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

    // Restore code block placeholder
    m = ln.match(/^\u0000B(\d+)\u0000$/);
    if (m) { closeList(); closeTable(); out.push(blocks[+m[1]]); continue; }

    if (!ln.trim()) { closeList(); closeTable(); continue; }

    // Headings  # ## ###
    m = ln.match(/^\s{0,3}(#{1,3})\s+(.+)$/);
    if (m) { closeList(); closeTable(); out.push('<div class="coder-h coder-h' + m[1].length + '">' + _mdInline(_escHtml(m[2])) + '</div>'); continue; }

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

    // Numbered list
    m = ln.match(/^\s*\d+[.)]\s+(.+)$/);
    if (m) {
      if (list !== 'ol') { closeList(); closeTable(); out.push('<ol class="coder-ol">'); list = 'ol'; }
      out.push('<li>' + _mdInline(_escHtml(m[1])) + '</li>');
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

    // Plain paragraph (per line — chat style, single \n = line break)
    closeList(); closeTable();
    out.push('<p>' + _mdInline(_escHtml(ln)) + '</p>');
  }
  closeList();
  closeTable();
  return out.join('');
}

function _mdInlinePlan(s) {
  s = _escHtml(s);
  s = s.replace(/`([^`\n]+)`/g, '<code class="coder-inline-code">$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');
  return s;
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
    if (m) { flushP(); var chk = (m[1] === 'x' || m[1] === 'X') ? '\u2611' : '\u2610'; out.push('<div class="pm-li pm-task"><span class="pm-chk">' + chk + '</span>' + _mdInlinePlan(m[2]) + '</div>'); continue; }
    m = ln.match(/^\s*\d+[.)]\s+(.+)$/);
    if (m) { flushP(); out.push('<div class="pm-li"><span class="pm-bul">\u2022</span>' + _mdInlinePlan(m[1]) + '</div>'); continue; }
    m = ln.match(/^\s*[-*]\s+(.+)$/);
    if (m) { flushP(); out.push('<div class="pm-li"><span class="pm-bul">\u2022</span>' + _mdInlinePlan(m[1]) + '</div>'); continue; }
    if (!ln.trim()) { flushP(); continue; }
    buf.push(ln);
  }
  flushP();
  return out.join('');
}

function _renderPlanMarkdown() {
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

function _liveMdRawOf(body) {
  if (body._liveRaw) return body._liveRaw;
  if (body._textRun && body._textRun.nodeType === Node.TEXT_NODE) return body._textRun.textContent;
  return '';
}

function _liveMdReset(body) {
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
      var before = combined.split('<think>')[0];
      if (before) _liveMdAppend(body, before);
      S.curAgent._thinkBuf = '<think>' + combined.split('<think>').slice(1).join('<think>');
      if (thinkBlockEl) { thinkBlockEl.style.display = ''; thinkBlockEl.classList.add('live', 'open'); }
    } else if (S.curAgent._inThink) {
      S.curAgent._thinkBuf = combined;
      if (thinkBodyEl) {
        var visibleThink = combined.replace(/^<think>/i, '').split('</think>')[0];
        thinkBodyEl.textContent = visibleThink;
        var tktEl = document.getElementById('tkt-' + S.curAgent.tid);
        if (tktEl) tktEl.textContent = ' · ' + visibleThink.length + ' chars';
        thinkBodyEl.scrollTop = thinkBodyEl.scrollHeight;
      }
      if (combined.indexOf('</think>') >= 0) {
        S.curAgent._inThink = false;
        var after = combined.split('</think>').slice(1).join('</think>');
        S.curAgent._thinkBuf = '';
        if (thinkBlockEl) thinkBlockEl.classList.remove('live', 'open');
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
  if (!t || !String(t).trim()) return;

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
    if (body) {
      var flushed = S.curAgent._thinkBuf.replace(/^<think>/i, '');
      if (flushed.indexOf('</think>') >= 0) {
        var afterClose = flushed.split('</think>').slice(1).join('</think>').trim();
        flushed = afterClose || flushed.split('</think>')[0].trim();
      }
      if (flushed.trim()) {
        _liveMdAppend(body, flushed);
      }
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
      var _tbt = _tb.querySelector('.th-tokens');
      if (_tbt) _tbt.textContent = (_tbt.textContent || '').replace(' \u2713', '') + ' \u2713';
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
  const t = document.getElementById(S.curAgent.tid);
  if (t) {
    var _tokRate = Number(S.perfTokRate || 0);
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

// ── Batched scroll: coalesce multiple scrollBtm() calls per frame ─────────
// STICKY-FIX (2026-08-10): double rAF — rAF callbacks run BEFORE the layout pass,
// freshly appended content (token flush, tool chips) was not yet in the
// scrollHeight when measuring → you did not stick to the bottom.
var _scrollRafId = null;
function scrollBtm() {
  // Batch: instead of forcing layout per call, schedule a single rAF
  if (!_scrollRafId) {
    _scrollRafId = requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        _scrollRafId = null;
        const c = document.getElementById('chat');
        if (c) c.scrollTop = c.scrollHeight;
      });
    });
  }
}

function scrollBtmIfNearBottom(thresholdPx) {
  // FIX: _partRunning guard removed — same reason as scrollBtm().
  // partition events scroll themselves via scrollBtm(), but other events
  // (file_read, file_change, agent bubbles) must still be able to scroll.
  // Batched: coalesce into single rAF per frame to avoid forced layout recalc.
  // STICKY-FIX (2026-08-10): double rAF so the delta check runs on the layout
  // AFTER the content append (otherwise stale scrollHeight → no scroll).
  if (!window._scrollNearRaf) {
    var _th = Math.max(24, parseInt(thresholdPx || 80, 10) || 80);
    window._scrollNearRaf = requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        window._scrollNearRaf = null;
        const c = document.getElementById('chat');
        if (!c) return;
        var delta = c.scrollHeight - (c.scrollTop + c.clientHeight);
        if (delta <= _th) c.scrollTop = c.scrollHeight;
      });
    });
  }
}

function rmEl(id) {
  const e = document.getElementById(id);
  if (e) e.parentNode.removeChild(e);
}

function esc(s) { return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// CHAT-STRUCTURE-FIX (2026-08-07): clean saved chat HTML before it is
// inserted into the DOM on load — strip active script/embed content and event handlers.
function _sanitizeChatHtml(html) {
  return String(html || '')
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<iframe[\s\S]*?<\/iframe>/gi, '')
    .replace(/<object[\s\S]*?<\/object>/gi, '')
    .replace(/<embed[\s\S]*?>/gi, '')
    .replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/href\s*=\s*["']?\s*javascript:[^"'\s>]*["']?/gi, 'href="#"');
}

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
  S.perfChars = 0;
  S.perfEstTokens = 0;
  S.perfRealTokens = 0;
  // TOKEN-TRACKER UI (2026-08-25): Run-scope Akkumulatoren zuruecksetzen.
  S.runPromptTokens = 0;
  S.runCachedTokens = 0;
  S.runRequestCount = 0;
  S.perfCtxLimit = 0;
  S.perfCtxPct = 0;
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
        + '<span class="ctx-perf-item live" id="pbb-ctx">ctx: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-tok">out: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-rate">tok/s: --</span>'
      + '</div>'
      + '<div class="ctx-perf">'
        + '<span class="ctx-perf-item ok" id="pbb-in">in: --</span>'
        + '<span class="ctx-perf-item ok" id="pbb-cached">cached: --</span>'
        + '<span class="ctx-perf-item ok" id="pbb-new">recomputed: --</span>'
        + '<span class="ctx-perf-item live" id="pbb-reqs">reqs: --</span>'
      + '</div></div>';
  try {
    if ((localStorage.getItem('hivemind-pbb-collapsed') || '') === '1')
      _bar.classList.add('collapsed');
  } catch (e) {}
}

function _pbbToggle() {
  var _bar = document.getElementById('perf-bar-bottom');
  if (!_bar) return;
  var _collapsed = _bar.classList.toggle('collapsed');
  try { localStorage.setItem('hivemind-pbb-collapsed', _collapsed ? '1' : '0'); } catch (e) {}
}
window._pbbToggle = _pbbToggle;

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
    _realRate = S.perfRealTokens / Math.max(0.25, (Date.now() - S.perfFirstTokenAt) / 1000);
  }
  // GEN-TIME-FIX: final -> prefer the real generation rate (without tool latency)
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
    if ((_e = _qId('pbb-ctx'))) _e.textContent = 'ctx: ' + (_ctx > 0 ? (_pct > 0 ? _pct + '%' : '--') : '--');
    if ((_e = _qId('pbb-tok'))) _e.textContent = 'out: ' + (_outT > 0 ? _fmtTokens(_outT) : '--');
    if ((_e = _qId('pbb-rate'))) _e.textContent = (_rateVal > 0 ? _rateVal.toFixed(1) : '--') + '/s';
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
    var _sumEl = _qId('pbb-summary');
    if (_sumEl) {
      // SUMMARY-REDESIGN (2026-08-26): collapsed = ctx ABS/LIMIT (P%) · N t/s · M% cached.
      // reqs deliberately ONLY expanded. Color coding on ctx-% (green <70, amber <90, red ≥90).
      var _parts = [];
      if (_ctx > 0 && _pct > 0) {
        var _absTxt = _est > 0 ? (_fmtTokens(_est) + '/' + _fmtTokens(_ctx)) : String(_pct);
        _parts.push('ctx ' + _absTxt + ' (' + _pct + '%)');
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
  if (!S.perfFirstTokenAt) S.perfFirstTokenAt = _now;
  var _chars = String(content).length;
  if (_chars <= 0) return;
  S.perfChars += _chars;
  S.perfEstTokens += Math.max(1, Math.floor(_chars / 3));
  var _elapsed = Math.max(0.25, (_now - S.perfFirstTokenAt) / 1000);
  var _inst = S.perfEstTokens / _elapsed;
  if (!isFinite(S.perfTokRateEma) || S.perfTokRateEma <= 0) S.perfTokRateEma = _inst;
  else S.perfTokRateEma = (S.perfTokRateEma * 0.8) + (_inst * 0.2);
  S.perfTokRate = S.perfTokRateEma;
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

function _perfOnCtxMeter(estTokens, ctxLimit, compressing) {
  var _est = parseInt(estTokens || 0, 10);
  var _ctx = parseInt(ctxLimit || 0, 10);
  if (!isNaN(_est) && _est >= 0) S.perfEstTokens = _est;
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
  await newChat();
  var _inp = document.getElementById('input');
  if (_inp) { _inp.value = _lp; }
  sendMsg();
}

async function sendMsg() {
  if (S.agentPaused) { resumeWithAnswer(); return; }
  const inp = document.getElementById('input');
  const txt = inp.value.trim();
  if ((!txt && !S.pendingImgs.length) || S.streaming) return;

  const imgs = S.pendingImgs.slice();
  S.pendingImgs = []; renderImgPreview();
  inp.value = ''; inp.style.height = 'auto';
  var _helReset = document.getElementById('h-elapsed'); if (_helReset) _helReset.style.display = 'none';
  var _phReset = document.getElementById('h-phases'); if (_phReset) { _phReset.style.display = 'none'; _phReset.innerHTML = ''; }
  // reset the agent-phase badge
  var _papReset = document.getElementById('perf-agent-phase'); if (_papReset) { _papReset.className = ''; _papReset.innerHTML = ''; }
  // historicize the planner bubble: old run stays visible, new run gets a new bubble id.
  document.querySelectorAll('#planner-bubble').forEach(function(el) {
    el.classList.remove('live');
    el.id = '';
  });
  S.lastPrompt = txt;
  addUserMsg(txt, imgs);

  S.streaming = true;
  S.currentRunId = null;
  if (S._runAbortCtrl) { S._runAbortCtrl.abort(); }
  S._runAbortCtrl = new AbortController();
  _perfResetRuntimeState();
  _perfRender(false);
  document.getElementById('send').disabled = true;
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
  const _effDuoRuntimeProfile = resolveDuoRuntimeProfileForRequest();
  if (S.forcedComplexity !== 'auto' || skippedKeys.length || S.judgeBias !== 50) {
    const parts = [];
    if (S.forcedComplexity !== 'auto') parts.push('MODE FORCE: ' + S.forcedComplexity.toUpperCase());
    if (S.judgeBias !== 50) parts.push('JUDGE-BIAS: ' + S.judgeBias + (S.judgeBias > 50 ? ' (complex-bevorzugend)' : ' (simple-bevorzugend)'));
    if (skippedKeys.length) parts.push('SKIP: ' + skippedKeys.join(', '));
    if (S.mode === 'code_duo') {
      parts.push('RUNTIME: ' + _effDuoRuntimeProfile.toUpperCase());
    }
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

  const VISION_MODELS = ['qwen3-vl', 'llava', 'moondream', 'bakllava', 'minicpm', 'granite3.2', 'vision'];
  if (imgs.length && S.mode !== 'automap') {
    // FIX: the vision warning checks BOTH agent assignments AND the image-preprocessing config.
    // Before: only direct/analyst checked → warning always when granite3.2-vision:2b was
    // configured via the preprocessing toggle (because 'granite3.2' was not in VISION_MODELS
    // and it is not an agent assignment but a separate vision_cfg).
    const directModel  = ((S.currentAssignments['direct']  || {}).model || '').toLowerCase();
    const analystModel = ((S.currentAssignments['analyst'] || {}).model || '').toLowerCase();
    const hasVisionAgent = VISION_MODELS.some(function(vm) {
      return directModel.indexOf(vm) >= 0 || analystModel.indexOf(vm) >= 0;
    });
    // preprocessing mode: image-preprocess toggle active + model set = vision active
    const hasVisionPrepro = S.visionEnabled && !!S.visionModel;
    const hasVisionDedicated = S.visionAgentEnabled && !!S.visionAgentModel;
    const hasVision = hasVisionAgent || hasVisionPrepro || hasVisionDedicated;
    if (!hasVision) {
      const hint = document.createElement('div');
      hint.className = 'msg divider';
      hint.style.cssText = 'color:#e09030;border-color:rgba(224,144,48,.3)';
      hint.textContent = '\u26A0 No vision model active \u2014 enable image preprocessing or choose automap';
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

    const res = await fetch('/stream', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        q: txt,
        images: imgs.map(function(i) { return i.b64; }),
        mode: S.mode,
        iterations: S.iters,
        active_preset: S.activePreset,
        constraint_mode: S.constraintMode,
        force_complexity: S.forcedComplexity !== 'auto' ? S.forcedComplexity : undefined,
        skip_agents:      Object.keys(S.skippedAgents).filter(function(k){return S.skippedAgents[k];}),
        judge_bias:       S.judgeBias,
        duo_pair:          S.duoPair,
        duo_tool_rounds:   S.duoToolRounds  || 0,
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
                chat_id: S.currentChatId || undefined,
        until_finished: !!S.duoUntilFinished,
        duo_runtime_profile: _effDuoRuntimeProfile,
        duo_runtime_profile_lock_override: !!S.duoRuntimeProfileLockOverride,
        duo_use_preset_models: !!S.duoUsePresetModels,
      })
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buf += dec.decode(chunk.value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for (let li = 0; li < lines.length; li++) {
        const line = lines[li];
        if (line.indexOf('data:') !== 0) continue;
        try { handleEvent(JSON.parse(line.slice(5))); } catch(e) { console.warn('[SSE] Bad JSON:', line.slice(5,80), e); }
      }
    }
  } catch(e) {
    if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
    _cleanupLoadTimers();
    if (S.curAgent) {
      if (!S.curAgent.body.textContent.trim()) {
        S.curAgent.body.textContent = '[Error: ' + e.message + ']';
        S.curAgent.body.style.color = '#b04040';
      }
      S.curAgent.body.classList.remove('live');
      S.curAgent = null;
    }
    // P1-3 (2026-08-11): show errors even WITHOUT an agent bubble in the chat —
    // previously the error vanished into a transient status line and the
    // UI looked as if no request had ever been sent.
    if (!S.curAgent) {
      const errDiv = document.createElement('div');
      errDiv.className = 'msg status-txt';
      errDiv.style.cssText = 'color:#b04040;font-size:10px;border:1px solid rgba(200,64,64,.25);padding:6px 8px;border-radius:4px';
      errDiv.textContent = '\u26A0 Stream error: ' + (e.message || e);
      const chatEl = document.getElementById('chat');
      if (chatEl) chatEl.appendChild(errDiv);
      scrollBtmIfNearBottom(120);
    }
    document.querySelectorAll('.abody.live').forEach(function(b) {
      if (!b.textContent.trim()) {
        b.textContent = '[No output - stream aborted]';
        b.style.color = '#7a8fa8';
        b.style.fontStyle = 'italic';
      }
      b.classList.remove('live');
    });
    showStatus('\u26A0 Stream error: ' + e.message);
  } // end inner try/catch (fetch)

  } finally { // end outer try — covers automap + intent + fetch
    S.streaming = false;
    _cleanupLoadTimers();
    S.currentRunId = null;
    document.getElementById('send').disabled = false;
    document.getElementById('h-dot').className = 'on';
    const stopBtnEnd = document.getElementById('stop-btn');
    if (stopBtnEnd) stopBtnEnd.classList.remove('visible');
    setPauseBtnState('idle');
    setStopBtnState('idle');
    stopAskUserCountdown();
    S.agentPaused = false;
    S.agentQuestion = null;
    const inpEnd = document.getElementById('input');
    if (inpEnd) inpEnd.placeholder = 'Message...';
        const skipBtnEnd = document.getElementById('skip-btn');
  if (skipBtnEnd) skipBtnEnd.style.display = 'none';
    // refresh VRAM after pipeline/stream — only if the panel is open
    if (!_vramPending && _vramOpen) refreshVram();
    // update prefetch avgs (backend has recalculated the lead)
    if (S.smartPreload) setTimeout(loadPrefetchAvgs, 800);
    // always hide the task-type badge after the stream
    const taskBadgeEnd = document.getElementById('h-tasktype');
    if (taskBadgeEnd) taskBadgeEnd.style.display = 'none';
    rmEl('status-el');
    loadMemory();
    // Auto-save chat message (on by default; covers stream end, errors, stop and abort)
    if (S.chatAutosave && !S._chatPersistBusy) {
      S._chatPersistBusy = true;
      persistCurrentChat(true).then(function() { S._chatPersistBusy = false; })
        .catch(function() { S._chatPersistBusy = false; });
    }
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

function handleEvent(d) {
  if (d.type === 'run_id') {
    S.currentRunId = d.run_id;
  }
  else if (d.type === 'heartbeat') {
    var _hbEl = document.getElementById('status-el');
    if (!_hbEl) {
      _hbEl = document.createElement('div');
      _hbEl.id = 'status-el';
      _hbEl.className = 'msg status-txt';
      _hbEl.style.cssText = 'color:#e0a030;font-style:italic;font-weight:600';
      document.getElementById('chat').appendChild(_hbEl);
    }
    _hbEl.textContent = '\u23F3 Processing context\u2026 ' + (d.elapsed || '') + 's';
    scrollBtmIfNearBottom(120);
  }
  else if (d.type === 'usage_meta') {
    if (d.completion_tokens) S.perfRealTokens = (S.perfRealTokens || 0) + parseInt(d.completion_tokens, 10);
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
    if (d.phase === 'planner' && d.reasoning_tokens > 0) {
      var _tcReal = document.getElementById('planner-think-token-count');
      if (_tcReal) {
        var _realTok = parseInt(d.reasoning_tokens, 10) || 0;
        // real llama.cpp value (without ≈) — replaces the chars/3 estimate.
        _tcReal.textContent = (_realTok > 999 ? (_realTok / 1000).toFixed(1) + 'k' : _realTok) + ' tok';
        _tcReal.style.color = '#4a3060';
      }
    }
  }
  else if (d.type === 'status') {
      // F5: render system hints as colored dividers
      var _sc = String(d.content || '');
      // COMPRESSION-CUT: render as a cut BETWEEN the tool calls in the coder bubble
      if (_sc.indexOf('Context compressed') >= 0 || _sc.indexOf('Compression failed') >= 0) {
        var _cutBody = null;
        if (S.curAgent) { _cutBody = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body; }
        if (_cutBody) {
          var _cutDiv = document.createElement('div');
          _cutDiv.className = 'ctx-cut' + (_sc.indexOf('failed') >= 0 ? ' err' : '');
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
  }
  else if (d.type === 'planner_start') {
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
  }
  else if (d.type === 'planner_thinking') {
    // heartbeat: planner is running — update the pulse indicator while no real tokens are there yet
    var _thinkContent = document.getElementById('planner-think-content');
    if (_thinkContent && !window._plannerFirstRealToken) {
      var _hbSec = d.elapsed || Math.round((Date.now() - (window._plannerTickStart || Date.now())) / 1000);
      _thinkContent.textContent = '\u23f3 Thinking\u2026 ' + _hbSec + 's';
      var _detHb = document.getElementById('planner-think-block');
      if (_detHb && !_detHb.open) _detHb.open = true;
    }
  }
  else if (d.type === 'planner_thinking_progress') {
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
  }
  else if (d.type === 'planner_thinking_token') {
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
        (function(el) { requestAnimationFrame(function() { el.scrollTop = el.scrollHeight; }); })(_thinkEl);
      }
    }
    scrollBtmIfNearBottom(60);
  }
  else if (d.type === 'planner_plan_token') {
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
          var _trTxt2 = _trEl2 ? _trEl2.textContent : '&nbsp;';
          var _elTxt2 = _elEl2 ? _elEl2.textContent : '0s';
          _am2.innerHTML = '<span id="planner-tokrate"></span> Plan \u00b7 ' + (window._plannerModelShort || '') + ' &middot; <span id="planner-elapsed"></span>';
          if (_trEl2) _trEl2.textContent = _trTxt2;
          if (_elEl2) _elEl2.textContent = _elTxt2;
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
  }
  else if (d.type === 'planner_token') {
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
  }
  else if (d.type === 'planner_done') {
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
  }
  else if (d.type === 'worker_slot_failed') {
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
  }
  else if (d.type === 'worker_pool_state') {
    S.prexPoolState = d;
    _applyWorkerPoolStateToUi();
  }
  // UI-BUG-3 FIX: pre_explore_done — dedicated event instead of fragile status-string matching.
  // Before: _prexMarkDone() only on "Exploration complete" in the status text →
  // with empty/broken _explore_ctx this status was never sent → the bar stayed stuck.
  else if (d.type === 'pre_explore_done') {
    _prexMarkDone(d.status === 'timeout' || d.status === 'error');
    if (d.status === 'error' || !d.has_ctx) {
      var _pxWarn = document.getElementById('prex-info');
      if (_pxWarn) _pxWarn.style.opacity = '0.6';
    }
  }
  else if (d.type === 'pre_explore_info') {
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
  }
  else if (d.type === 'pre_explore_partition_shape') {
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
  }
  else if (d.type === 'image_description') {
    // Show vision preprocessing result in chat
    showImageDescription(d.content);
  }
  else if (d.type === 'complexity') {
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
  }
  else if (d.type === 'pipeline_start') {
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
  }
  else if (d.type === 'round')          { addDiv('Round ' + d.n + ' / ' + d.total); }
  else if (d.type === 'agent')          { startAgent(d.content, d.model || '', d.role || '', d.repair || false); }
  else if (d.type === 'token')          {
    appendToken(d.content);
    _perfOnToken(d.content);
  }
  // THINKING-STREAM: live display of reasoning tokens in the 🧠 think block.
  // Thinking tokens live in inline blocks in the body — chronologically between tool calls.
  // Each new think block is created after the previous tool call.
  else if (d.type === 'thinking_token') {
    _perfOnToken(d.content);
    if (S.curAgent) {
      if (!S._thinkBlockId) {
        S._thinkBlockId = _createThinkBlock();
      }
      var _tbEl = S._thinkBlockId ? document.getElementById(S._thinkBlockId) : null;
      if (!_tbEl) return;
      _tbEl.style.display = '';
      _tbEl.classList.add('live', 'open');
      var _thbEl = _tbEl.querySelector('.think-body');
      if (_thbEl) {
        _thbEl.textContent += d.content || '';
        var _tktEl = _tbEl.querySelector('.th-tokens');
        if (_tktEl) _tktEl.textContent = ' \u00b7 ' + _thbEl.textContent.length + ' chars';
        _thbEl.scrollTop = _thbEl.scrollHeight;
      }
      scrollBtmIfNearBottom(120);
    }
  }
  else if (d.type === 'agent_done')     {
    // tune format: critic_tune arrives in the agent_done event
    if (d.critic_tune && S.curAgent) {
      const body = S.curAgent.body;
      if (body) body._criticTune = d.critic_tune;
    }
    doneAgent(d.elapsed);
    if (!_vramPending && _vramOpen) refreshVram();
  }  // FIX: refresh VRAM after an agent
  // BUG-20 FIX: clear_agent empties the current bubble before the retry stream.
  // Prevents double output: [first attempt][retry] in the same bubble.
  else if (d.type === 'clear_agent') {
    if (S.curAgent) {
      const body = document.getElementById('ab-' + S.curAgent.tid) || S.curAgent.body;
      if (body) body.textContent = '';
      S.curAgent._thinkBuf = '';
      S.curAgent._inThink  = false;
      S.curAgent._toolXmlCarry = '';
    }
  }
  else if (d.type === 'automap') {
    showAutomapEvent(d.task_type || d.content, d.assignments || {});
  }
  // ── Code Events ──────────────────────────────────────────
  else if (d.type === 'duo_start') {
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
    var _rtSource = (d.runtime_model_source === 'dropdown')
      ? ('Dropdown-Override' + (d.runtime_model_override ? (': ' + d.runtime_model_override) : ''))
      : (d.runtime_model_source === 'preset'
          ? 'Preset-Routing'
          : (d.runtime_model_source === 'preset_toggle' ? 'Preset-Routing (Toggle)' : 'Pair-Routing'));
    var _ctxText = d.runtime_ctx_target ? String(d.runtime_ctx_target) : '?';
    _runtimeInfo.textContent = 'Runtime: ' + String((d.runtime_profile || 'balanced')).toUpperCase()
      + ' | Source: ' + _rtSource
      + ' | ctx=' + _ctxText;
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
  }
  else if (d.type === 'planner_result') {
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
      ? ('Plan erstellt: ' + _chunkCount + ' Schritt' + (_chunkCount === 1 ? '' : 'e'))
      : 'Briefing erstellt';
    _renderPlanMarkdown();
    finalizePlannerBubble(_planLabel);
    // SUBTASK-CHECKLIST (2026-08-31): chunks merken + Checkliste im UI rendern.
    if (_chunkCount > 0) {
      S._planChunks = (Array.isArray(d.chunks) ? d.chunks : []).map(function(c){ return (c && c.title) ? c.title : String(c || ''); });
      _renderPlanChecklist();
    }
  }
  else if (d.type === 'duo_round') {
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
  }
  else if (d.type === 'agent_asking') {
    S.agentPaused = true;
    S.agentQuestion = d.question;
    S.currentRunId = d.run_id || S.currentRunId;
    setPauseBtnState('paused_by_ask_user');
    // show the question as a system message in the chat
    var _askDiv = document.createElement('div');
    _askDiv.id = 'ask-user-question';
    _askDiv.className = 'msg divider';
    _askDiv.style.cssText = [
      'color:#4878c0',
      'border:1px solid rgba(72,120,192,.4)',
      'border-left:3px solid #4878c0',
      'background:rgba(72,120,192,.07)',
      'border-radius:8px',
      'padding:10px 14px',
      'font-size:13px',
      'font-style:italic',
      'margin:4px 0'
    ].join(';');
    _askDiv.textContent = '\uD83E\uDD14 ' + (d.question || 'Your input is needed.');
    var _existingAsk = document.getElementById('ask-user-question');
    if (_existingAsk) {
      _existingAsk.textContent = _askDiv.textContent;
    } else {
      document.getElementById('chat').appendChild(_askDiv);
    }
    scrollBtmIfNearBottom(120);
    var _inpAsk = document.getElementById('input');
    if (_inpAsk) { _inpAsk.placeholder = 'Your answer\u2026'; _inpAsk.focus(); }
    if (S.duoUntilFinished && S.askUserTimeoutSeconds > 0) {
      startAskUserCountdown(S.askUserTimeoutSeconds);
    }
  }
  else if (d.type === 'ask_user_timeout_reached') {
    stopAskUserCountdown();
    showInfo('Auto-answer sent: "' + (d.auto_answer || S.askUserAutoAnswer) + '"');
    var _qDivTo = document.getElementById('ask-user-question');
    if (_qDivTo) _qDivTo.remove();
    S.agentPaused = false;
    S.agentQuestion = null;
    var _inpTo = document.getElementById('input');
    if (_inpTo) { _inpTo.placeholder = 'Message...'; _inpTo.value = ''; _inpTo.disabled = false; }
    setPauseBtnState('running');
  }
  else if (d.type === 'agent_throttled') {
    stopAskUserCountdown();
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
  }
  else if (d.type === 'agent_resumed') {
    stopAskUserCountdown();
    S.agentPaused = false;
    S.agentQuestion = null;
    setPauseBtnState('running');
    var _inpRes = document.getElementById('input');
    if (_inpRes) { _inpRes.placeholder = 'Message...'; _inpRes.value = ''; _inpRes.disabled = false; }
    var _qDiv = document.getElementById('ask-user-question');
    if (_qDiv) _qDiv.remove();
  }
  else if (d.type === 'run_paused_manual') {
    setPauseBtnState('paused_manual');
    var _pauseDiv = document.createElement('div');
    _pauseDiv.className = 'msg divider';
    _pauseDiv.style.cssText = 'color:#f0ad4e;border-color:rgba(240,173,78,.35);font-size:11px;font-weight:600';
    _pauseDiv.textContent = '\u23F8 Run paused after chunk ' + d.chunks_done + '. ' + d.chunks_remaining + ' chunks remaining. Click Resume to continue.';
    document.getElementById('chat').appendChild(_pauseDiv);
    scrollBtmIfNearBottom(120);
  }
  else if (d.type === 'run_resumed_manual') {
    setPauseBtnState('running');
    var _resDiv = document.createElement('div');
    _resDiv.className = 'msg divider';
    _resDiv.style.cssText = 'color:#20b0a0;border-color:rgba(32,176,160,.35);font-size:11px;font-weight:600';
    _resDiv.textContent = '\u25B6 Run resumed \u2014 chunk ' + (d.chunks_done + 1) + ' starting.';
    document.getElementById('chat').appendChild(_resDiv);
    scrollBtmIfNearBottom(120);
  }
  else if (d.type === 'run_halted_graceful') {
    _cleanupLoadTimers();
    setPauseBtnState('idle');
    setStopBtnState('idle');
    var _haltDiv = document.createElement('div');
    _haltDiv.className = 'msg divider';
    _haltDiv.style.cssText = 'color:#808080;border-color:rgba(128,128,128,.35);font-size:11px;font-weight:600';
    _haltDiv.textContent = '\u23F9 Graceful stop: ' + d.chunks_done + ' chunks completed, ' + d.chunks_remaining + ' remaining.';
    document.getElementById('chat').appendChild(_haltDiv);
    scrollBtmIfNearBottom(120);
  }
  else if (d.type === 'duo_coder') {
    if (S._chunkingActive) {
      // chunking: EVERY chunk gets its own coder block (no reuse)
      startDuoAgent('coder', 'Coder', d.model || '', 'R' + d.round);
    } else {
      // normal: reuse the coder bubble as before
      var _existingCoderBlock = document.querySelector('.ablock.duo-coder');
      if (_existingCoderBlock && S.curAgent && S.curAgent.body && S.curAgent.body.closest('.duo-coder') === _existingCoderBlock) {
        var _arnd = _existingCoderBlock.querySelector('.arole:last-of-type');
        if (_arnd) _arnd.textContent = ' \u00B7 R' + d.round;
        _existingCoderBlock.classList.add('live');
        if (S.curAgent.body) S.curAgent.body.classList.add('live');
        S._thinkBlockId = null;
      } else {
        startDuoAgent('coder', 'Coder', d.model || '', 'R' + d.round);
      }
    }
  }
  else if (d.type === 'duo_coder_done') {
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
  }
  else if (d.type === 'duo_critic') {
    // Critic starts — close coder bubble
    if (S.curAgent && S.curAgent.body && S.curAgent.body.closest('.duo-coder')) {
      doneAgent(S._coderElapsed || '');
    }
    S._coderElapsed = null;
    _addFlowConnector('#d08020', 0.07, 'Critic', 'Review code', 'R' + d.round);
    showStatus('\u23F3 Critic is reviewing the code (R' + d.round + ')...');
  }
  else if (d.type === 'duo_critic_done') {
    // build the critic block now with the final verdict
    rmEl('status-el');
    renderDuoVerdict(d.verdict || {}, d.elapsed, d.approved);
    if (!_vramPending && _vramOpen) refreshVram();
  }
  else if (d.type === 'test_result') {
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
  }
  // ── Partition Events (paralleler Pre-Explore) ─────────────────────────────
  else if (d.type === 'partition_start') {
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
  }
  else if (d.type === 'partition_done') {
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

  updateMoeVisibility();
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
  }
  else if (d.type === 'parallel_cancelled') {
    window._partRunning = false;
    rmEl('part-grid');
    scrollBtmIfNearBottom(120);
  }
  else if (d.type === 'partition_dedup') {
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
  }
  // ── Partition-Token (Tool-Call-Log in Card-Log, nicht in Haupt-Stream) ────
  else if (d.type === 'partition_token') {
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
  }
  // ── File-Read: Live-Counter im Card-Header updaten ────────────────────────
  else if (d.type === 'file_read') {
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
  }
  // ── File-Change (live during tool loop) ──────────────────────────────────
  else if (d.type === 'file_change') {
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
      _tcLine.innerHTML = '<span class="tool-act-op ' + _tcOp + '">' + _tcLabel + '</span>'
        + '<span class="tool-act-path" title="' + esc(d.path) + '">' + esc(_tcShort) + '</span>';
      _tcBody.appendChild(_tcLine);
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
        _tcBody.appendChild(_fcPreview);
      }
      scrollBtmIfNearBottom(80);
    }
    // 2a. live code panel — show content if present
    if (d.content) {
      _cpAddOrUpdateFile(d.path || '', d.content, d.op || 'write');
    }
  }
  // ── Files summary (after the tool loop, shows all touched files) ──────────
  else if (d.type === 'files_summary') {
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
  }
  // ── Context meter (PERF-CONSOLIDATION: thin bar on top removed,
  //     values live in the lower performance bar) ─────────────────────────
  else if (d.type === 'ctx_meter') {
    _perfOnCtxMeter(d.est_tokens, d.ctx_limit, !!d.compressing);
  }
  else if (d.type === 'memory_saved') {
    loadMemory();
  }
  else if (d.type === 'done') {
    _cleanupLoadTimers();
    _perfOnDone(d);
    setPauseBtnState('idle');
    setStopBtnState('idle');
    stopAskUserCountdown();
    var _qDivDone = document.getElementById('ask-user-question');
    if (_qDivDone) _qDivDone.remove();
    // Close any still-open coder bubble (no-critic runs skip duo_critic event)
    if (S.curAgent && S.curAgent.body && S.curAgent.body.closest('.duo-coder')) {
      doneAgent(S._coderElapsed || '');
    }
    S._coderElapsed = null;
    finalizePlannerBubble();
    // F2: run-end marker — all stop reasons
    var _stopMap = {
      'completed':       { icon: '\u2713', color: '#22c55e', border: 'rgba(34,197,94,.35)',  label: 'Run completed' },
      'graceful_stop':   { icon: '\u23F8', color: '#f0ad4e', border: 'rgba(240,173,78,.35)',  label: 'Graceful stop after chunk' },
      'loop_detected':   { icon: '\u21BA', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Loop detected - run stopped' },
      'blocked':         { icon: '\u26A0', color: '#e09030', border: 'rgba(224,144,48,.35)',  label: 'Run blocked' },
      'max_tool_rounds': { icon: '\u23F1', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Max tool rounds reached' },
      'stuck_in_loop':   { icon: '\u21BA', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Stuck loop aborted' },
      'error':           { icon: '\u2716', color: '#c04040', border: 'rgba(200,64,64,.35)',   label: 'Run error' },
      'user_aborted':    { icon: '\u23F9', color: '#808080', border: 'rgba(128,128,128,.35)', label: 'Run aborted (user)' },
      'aborted':         { icon: '\u25A0', color: '#c04040', border: 'rgba(200,64,64,.3)',   label: 'Run aborted' },
      'timeout':         { icon: '\u23F1', color: '#e09030', border: 'rgba(224,144,48,.3)',  label: 'Run timeout' },
      // B11 (2026-08-04): error/stop reasons that previously fell silently on the green
      // 'completed' checkmark (_stopMap fallback) — "silently wrong".
      'hard_stop':         { icon: '\u26D4', color: '#c04040', border: 'rgba(200,64,64,.35)',   label: 'Run hard-stopped - resume available' },
      'tool_round_error':  { icon: '\u26D4', color: '#c04040', border: 'rgba(200,64,64,.35)',   label: 'Tool round error - run stopped' },
      'verification_required': { icon: '\u26A0', color: '#f0ad4e', border: 'rgba(240,173,78,.35)', label: 'Verification pending - resume available' },
      'halted':            { icon: '\u23F9', color: '#d0a020', border: 'rgba(208,160,32,.35)',  label: 'Run halted' },
      'timeout_guard':     { icon: '\u23F1', color: '#e09030', border: 'rgba(224,144,48,.3)',   label: 'Run timeout (guard)' },
    };
    var _sr = _stopMap[d.stop_reason] || _stopMap['completed'];
    if (_sr && d.stop_reason) {
      var _endDiv = document.createElement('div');
      _endDiv.className = 'msg divider';
      _endDiv.style.cssText = 'color:' + _sr.color + ';border-color:' + _sr.border + ';font-size:10px;letter-spacing:.05em;font-weight:600';
      _endDiv.textContent = _sr.icon + ' ' + _sr.label;
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
    var _retryReasons = { error: 1, aborted: 1, hard_stop: 1, tool_round_error: 1, timeout: 1, timeout_guard: 1, stuck_in_loop: 1, loop_detected: 1, blocked: 1, max_tool_rounds: 1, verification_required: 1, halted: 1 };
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
      // but a fresh chat (session/DOM cleared via newChat()).
      var _rtNCBtn = document.createElement('button');
      _rtNCBtn.textContent = '\u21BA Retry in new chat';
      _rtNCBtn.style.cssText = 'background:none;border:1px solid #c09050;border-radius:4px;color:#d0a060;font-family:\'IBM Plex Mono\',monospace;font-size:10px;font-weight:600;padding:4px 12px;cursor:pointer;letter-spacing:.05em';
      _rtNCBtn.onclick = function() { _rtNCBtn.disabled = true; _rtNCBtn.textContent = '\u21BA Retrying\u2026'; _retryLastPromptNewChat(); };
      _rtWrap.appendChild(_rtNCBtn);
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
  }
  else if (d.type === 'tool_call') {
    // model-initiated tool call — as a chip in the current coder bubble
    // Flush pending text tokens first so mid-sentence text is committed before the tool chip
    _flushTokenQueueSync();
    if (!S.curAgent) return;
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
    // chip click (2026-08-25): write/edit chips toggle the live-code panel
    // (with exactly this file); all other chips keep showing the
    // server-delivered extra details.
    _tcChip.addEventListener('click', function(e) {
      e.stopPropagation();
      if (_tcRow.dataset.toolPath) {
        var _cpKey = _cpFindEntry(_tcRow.dataset.toolPath);
        if (_cpKey) _cpShowFile(_cpKey);
        toggleCodePanel();
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
    // Insert BEFORE open think-block: order = [Think] then [Tool], not reversed
    var _tkCurrent = S._thinkBlockId ? document.getElementById(S._thinkBlockId) : null;
    if (_tkCurrent && _tkCurrent.parentNode === _tcBody) {
      _tcBody.insertBefore(_tcRow, _tkCurrent);
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
    scrollBtmIfNearBottom(60);
  }
  else if (d.type === 'tool_result') {
    // Output des Tool-Calls — aufklappbar unter dem letzten Chip
    // Flush pending text tokens first
    _flushTokenQueueSync();
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
    // Short: <120 chars AND text matches full (both stripped server-side now)
    var _trShort = _trFull.length < 120 && _trText === _trFull;
    var _trLines = _trFull.split('\n').length;
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
    var _dsM = _trOk ? _trFull.match(/^\[DIFFSTAT\] added=(\d+) removed=(\d+) hunks=(\d+) truncated=([01])$/m) : null;
    if (/^(write_file|write_file_append|edit_file|patch_file|replace_lines|edit_ast)$/.test(_trToolName) && _dsM) {
      var _dsAdd = parseInt(_dsM[1], 10), _dsRem = parseInt(_dsM[2], 10),
          _dsHunks = parseInt(_dsM[3], 10), _dsTrunc = _dsM[4] === '1';
      var _trSumDs = document.createElement('summary');
      _trSumDs.innerHTML = '<span class="tr-name">' + (_TOOL_ICONS[_trToolName] || '\uD83D\uDD27') + ' ' + esc(_trToolName) + '</span>'
        + '<span style="color:#22c55e;font-weight:700;margin-left:8px">+' + _dsAdd + '</span>'
        + '<span style="color:#e06060;font-weight:700;margin-left:6px">&minus;' + _dsRem + '</span>'
        + '<span style="color:var(--tx2);margin-left:10px">' + _dsHunks + ' hunks</span>'
        + (_dsTrunc ? '<span style="color:#f0ad4e;margin-left:10px;font-size:10px">(diff truncated — numbers complete)</span>' : '');
      var _dsTbl = '<table class="tc-extra-table" style="margin:2px 0 4px">'
        + '<tr><td>added</td><td style="color:#22c55e;font-weight:700">+' + _dsAdd + ' lines</td></tr>'
        + '<tr><td>removed</td><td style="color:#e06060;font-weight:700">&minus;' + _dsRem + ' lines</td></tr>'
        + '<tr><td>Hunks (change blocks)</td><td>' + _dsHunks + '</td></tr>'
        + '</table>';
      _trPre.innerHTML = _dsTbl;
      _trBlock.appendChild(_trSumDs);
      _trBlock.appendChild(_trPre);
      _trBody.appendChild(_trBlock);
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
        var _trRe = /(?:build_status|status)["\s:]+(\w+)/i; var _trM = _trFull.match(_trRe); if (_trM) _trTcStatus = _trM[1].toLowerCase();
      }
      var _trTcColor = /^(passing|completed|success)$/i.test(_trTcStatus) ? '#22c55e' : /^(failing|blocked|error)$/i.test(_trTcStatus) ? '#c04040' : '#686868';
      _trTcBadge = '<span style="display:inline-block;font-size:10px;font-weight:700;background:' + _trTcColor + '1a;color:' + _trTcColor + ';border:1px solid ' + _trTcColor + '40;border-radius:3px;padding:0 5px;margin-right:4px;">' + esc(_trTcStatus) + '</span>';
      _trSum.innerHTML = _trTcBadge + _trNameHtml;
      _trBlock.appendChild(_trSum);
      _trBlock.appendChild(_trPre);
      _trBody.appendChild(_trBlock);
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
        _trInner = esc(_trLines + ' results');
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
    _trBlock.appendChild(_trPre); // BUG-1 FIX: always append, open= controls visibility
    _trBody.appendChild(_trBlock);
    _liveMdReset(_trBody);  // next text token starts a fresh live-md segment after this result
    scrollBtmIfNearBottom(60);
  }
}

// -- Input Events -----------------------------------------------
document.getElementById('input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});
document.getElementById('input').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 140) + 'px';
});
document.getElementById('input').addEventListener('paste', function(e) {
  Array.from(e.clipboardData.items).forEach(function(item) {
    if (!item.type.startsWith('image/')) return;
    const r = new FileReader();
    r.onload = function(ev) {
      S.pendingImgs.push({b64: ev.target.result.split(',')[1], preview: ev.target.result});
      renderImgPreview();
    };
    r.readAsDataURL(item.getAsFile());
  });
});
const chatCol = document.querySelector('.chat-col');
chatCol.addEventListener('dragover', function(e) { e.preventDefault(); });
chatCol.addEventListener('drop', function(e) {
  e.preventDefault();
  Array.from(e.dataTransfer.files).forEach(function(file) {
    if (!file.type.startsWith('image/')) return;
    const r = new FileReader();
    r.onload = function(ev) {
      S.pendingImgs.push({b64: ev.target.result.split(',')[1], preview: ev.target.result});
      renderImgPreview();
    };
    r.readAsDataURL(file);
  });
});

// -- Tabs -------------------------------------------------------
document.querySelectorAll('.tab').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.tab').forEach(function(b) { b.classList.remove('on'); });
    document.querySelectorAll('.panel').forEach(function(p) { p.classList.remove('on'); });
    btn.classList.add('on');
    document.getElementById('p-' + btn.dataset.p).classList.add('on');
    if (btn.dataset.p === 'models') { _vramOpen = true; refreshVram(); refreshModelsAutomap(); loadModels(); refreshAvailableModels(); }
    if (btn.dataset.p === 'agents') { loadModels(); }
    if (btn.dataset.p === 'memory') loadMemory();
    if (btn.dataset.p === 'presets') loadPresets();
    if (btn.dataset.p === 'configs') initConfigsPanel();
    if (btn.dataset.p === 'soul')   loadSoulStatus();
    if (btn.dataset.p === 'chats')  loadChatHistory();
  });
});

// -- Model Configs Panel ----------------------------------------
var cfgState = {
  currentModel: '',
  currentTab: 'base',
  learningMode: false,
};

function setLearningMode(enabled) {
  cfgState.learningMode = enabled;
  S.learningMode = enabled;
  document.getElementById('lpm-info').style.display = enabled ? 'block' : 'none';
  postSettings({learning_preset_mode: enabled});
  loadSoulStatus();
}

function setStartupPreload(enabled) {
  var t1 = document.getElementById('startup-preload-toggle');
  if (t1) t1.checked = enabled;
  // analyst sub-toggle shown/hidden depending on the main toggle
  var ar = document.getElementById('preload-analyst-row');
  if (ar) ar.style.opacity = enabled ? '1' : '0.4';
  postSettings({startup_preload_enabled: enabled});
}

function setStartupPreloadAnalyst(enabled) {
  var t = document.getElementById('startup-preload-analyst-toggle');
  if (t) t.checked = enabled;
  postSettings({startup_preload_analyst: enabled});
}

async function preloadJudgeNow(btn) {
  var orig = btn.textContent;
  btn.disabled = true; btn.textContent = '...';
  try {
    var d = await fetch('/vram/preload_judge', {method:'POST'}).then(function(r){return r.json();});
    btn.textContent = d.ok ? '✓' : '✗';
    btn.style.color = d.ok ? '#3a9960' : '#b04040';
    if (!_vramPending && _vramOpen) refreshVram();
  } catch(e) { btn.textContent = '✗'; }
  btn.disabled = false;
  setTimeout(function(){ btn.textContent = orig; btn.style.color = '#e09030'; }, 2500);
}

function setSmartPreload(enabled) {
  S.smartPreload = enabled;
  document.getElementById('prefetch-cfg').style.display = enabled ? 'block' : 'none';
  postSettings({smart_preload_enabled: enabled});
  if (enabled) loadPrefetchAvgs();
}

function setWorkersAfterRun(enabled) {
  S.workersAfterRun = enabled;
  postSettings({preload_workers_after_run: enabled});
}

function savePrefetchLead() {
  var v = parseFloat(document.getElementById('prefetch-lead-sl').value);
  document.getElementById('pfl-val2').textContent = v.toFixed(1) + 's';
  postSettings({prefetch_lead_seconds: v});
}

async function loadPrefetchAvgs() {
  var el = document.getElementById('prefetch-avg-display');
  if (!el) return;
  try {
    var d = await fetch('/prefetch/stats').then(function(r){return r.json();});
    var lead = d.prefetch_lead_seconds || 8.0;
    var agentLabels = {analyst:'Analyst', refiner:'Refiner', critic:'Kritiker', synthesizer:'Synthesizer'};
    var html = '<div style="font-size:9px;color:#7a8fa8;text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">'
             + 'Avg Laufzeiten · Lead: <b style="color:#e09030">' + lead.toFixed(1) + 's</b>'
             + (d.runs_logged > 0 ? ' · ' + d.runs_logged + ' agents with data' : ' · no data yet')
             + '</div>';
    for (var ag in d.agents) {
      var info = d.agents[ag];
      var avg  = info.avg_seconds;
      var fire = info.prefetch_fires_at;
      var model = (info.model || '').replace(':latest','').split(':')[0];
      html += '<div style="display:flex;align-items:center;gap:4px;padding:3px 0;border-bottom:1px solid #222a38;font-family:\'IBM Plex Mono\',monospace;font-size:10px">'
            + '<span style="width:72px;color:#c8d4e4">' + agentLabels[ag] + '</span>'
            + '<span style="flex:1;color:#7a8fa8;font-size:9px">' + model + '</span>'
            + '<span style="color:#e09030;min-width:44px;text-align:right">' + (avg > 0 ? avg.toFixed(1)+'s' : '—') + '</span>'
            + '<span style="color:#334;margin:0 3px">→</span>'
            + '<span style="color:#3a9960;min-width:44px">' + (fire !== null ? '&#9654; @' + fire.toFixed(1)+'s' : '<span style="color:#445">no avg</span>') + '</span>'
            + '</div>';
    }
    el.innerHTML = html;
    // update the slider value even when the backend changed the lead
    var sl = document.getElementById('prefetch-lead-sl');
    if (sl && Math.abs(parseFloat(sl.value) - lead) > 0.4) {
      sl.value = lead;
      document.getElementById('pfl-val2').textContent   = lead.toFixed(1) + 's';
      document.getElementById('prefetch-lead-val').textContent = lead.toFixed(1);
    }
  } catch(e) {
    if (el) el.innerHTML = '<span style="color:#667;font-size:10px">Error loading the stats</span>';
  }
}

function initConfigsPanel() {
  refreshCfgModelList();
  buildCfgAgentSelector();
}

function buildCfgAgentSelector() {
  var sel = document.getElementById('cfg-agent-sel');
  sel.innerHTML = '';
  var agents = ['analyst','refiner','critic','synthesizer','direct','judge'];
  var labels  = ['Analyst','Refiner','Kritiker','Synthesizer','Direkt','Judge'];
  for (var i = 0; i < agents.length; i++) {
    var opt = document.createElement('option');
    opt.value = agents[i]; opt.textContent = labels[i];
    sel.appendChild(opt);
  }
}

async function refreshCfgModelList() {
  var sel = document.getElementById('cfg-model-sel');
  var prev = sel.value;
  sel.innerHTML = '';
  for (var i = 0; i < S.models.length; i++) {
    var opt = document.createElement('option');
    opt.value = S.models[i]; opt.textContent = S.models[i];
    sel.appendChild(opt);
  }
  try {
    var d = await (await fetch('/model_configs/learned')).json();
    var learnedModels = Object.keys(d.models || {});
    for (var i = 0; i < learnedModels.length; i++) {
      var lm = learnedModels[i];
      var found = false;
      for (var j = 0; j < sel.options.length; j++) {
        if (sel.options[j].value === lm) { found = true; break; }
      }
      if (!found) {
        var opt = document.createElement('option');
        opt.value = lm; opt.textContent = lm + ' (learned only)';
        sel.appendChild(opt);
      }
    }
  } catch(e) {}
  if (prev) sel.value = prev;
  if (!sel.value && sel.options.length) sel.value = sel.options[0].value;
  cfgState.currentModel = sel.value;
  loadModelConfigs();
}

function switchCfgTab(tab, btn) {
  cfgState.currentTab = tab;
  var tabs = ['base','learned','log'];
  for (var i = 0; i < tabs.length; i++) {
    var el = document.getElementById('cfgtab-' + tabs[i]);
    if (el) el.classList.toggle('on', tabs[i] === tab);
    var panel = document.getElementById('cfg-' + tabs[i] + '-panel');
    if (panel) panel.style.display = tabs[i] === tab ? 'block' : 'none';
  }
  loadModelConfigs();
}

async function loadModelConfigs() {
  var model = document.getElementById('cfg-model-sel').value;
  cfgState.currentModel = model;
  if (!model) return;
  if (cfgState.currentTab === 'base')    renderBaseConfigs();
  if (cfgState.currentTab === 'learned') await renderLearnedConfigs(model);
  if (cfgState.currentTab === 'log')     await renderLog(model);
}

async function renderBaseConfigs() {
  var c = document.getElementById('base-cfg-list');
  c.innerHTML = '<div class="status-txt">Loading...</div>';
  try {
    var d = await (await fetch('/model_configs/base')).json();
    c.innerHTML = '';
    var agents = Object.keys(d);
    for (var i = 0; i < agents.length; i++) {
      var key = agents[i];
      var cfg = d[key];
      var card = document.createElement('div');
      card.className = 'cfg-card';
      var hdr = document.createElement('div'); hdr.className = 'cfg-card-hdr';
      var name = document.createElement('div'); name.className = 'cfg-card-name'; name.textContent = key;
      var badge = document.createElement('div'); badge.className = 'cfg-card-badge base'; badge.textContent = 'BASE';
      hdr.appendChild(name); hdr.appendChild(badge);
      card.appendChild(hdr);
      if (cfg.temperature !== undefined) {
        var r = document.createElement('div'); r.className = 'cfg-row';
        r.innerHTML = 'Temperature <span>' + parseFloat(cfg.temperature).toFixed(2) + '</span>';
        card.appendChild(r);
      }
      if (cfg.max_tokens !== undefined) {
        var r2 = document.createElement('div'); r2.className = 'cfg-row';
        r2.innerHTML = 'Max Tokens <span>' + cfg.max_tokens + '</span>';
        card.appendChild(r2);
      }
      if (cfg.notes) {
        var n = document.createElement('div'); n.className = 'cfg-prompt-preview'; n.textContent = cfg.notes;
        card.appendChild(n);
      }
      c.appendChild(card);
    }
  } catch(e) {
    c.innerHTML = '<div class="empty">Error loading base configs.<br>Is model_configs.py in the server?</div>';
  }
}

async function renderLearnedConfigs(model) {
  var c = document.getElementById('learned-cfg-list');
  c.innerHTML = '<div class="status-txt">Loading...</div>';
  try {
    var d = await (await fetch('/model_configs/learned/' + encodeURIComponent(model))).json();
    c.innerHTML = '';
    var configs = d.configs || {};
    var keys = Object.keys(configs);
    if (!keys.length) {
      c.innerHTML = '<div class="empty">No overrides for this model.<br>Create one below.</div>';
      return;
    }
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var cfg = configs[key];
      var card = document.createElement('div'); card.className = 'cfg-card';
      var hdr = document.createElement('div'); hdr.className = 'cfg-card-hdr';
      var name = document.createElement('div'); name.className = 'cfg-card-name'; name.textContent = key;
      var badge = document.createElement('div'); badge.className = 'cfg-card-badge learned'; badge.textContent = 'OVERRIDE';
      var del = document.createElement('button'); del.className = 'cfg-del'; del.textContent = '\u00D7';
      (function(k, m) {
        del.addEventListener('click', function() { deleteLearned(m, k); });
      })(key, model);
      hdr.appendChild(name); hdr.appendChild(badge); hdr.appendChild(del);
      card.appendChild(hdr);
      if (cfg.temperature !== undefined) {
        var r = document.createElement('div'); r.className = 'cfg-row';
        r.innerHTML = 'Temperature <span>' + parseFloat(cfg.temperature).toFixed(2) + '</span>';
        card.appendChild(r);
      }
      if (cfg.max_tokens !== undefined) {
        var r2 = document.createElement('div'); r2.className = 'cfg-row';
        r2.innerHTML = 'Max Tokens <span>' + cfg.max_tokens + '</span>';
        card.appendChild(r2);
      }
      if (cfg.system_prompt_override) {
        var p = document.createElement('div'); p.className = 'cfg-prompt-preview';
        p.textContent = cfg.system_prompt_override.slice(0, 80) + (cfg.system_prompt_override.length > 80 ? '...' : '');
        card.appendChild(p);
      }
      if (cfg.notes) {
        var n = document.createElement('div'); n.className = 'cfg-prompt-preview';
        n.textContent = '\u270E ' + cfg.notes;
        card.appendChild(n);
      }
      c.appendChild(card);
    }
  } catch(e) {
    c.innerHTML = '<div class="empty">Error or no learned configs available.</div>';
  }
}

async function saveLearned() {
  var model = cfgState.currentModel;
  if (!model) { alert('No model selected.'); return; }
  var agent   = document.getElementById('cfg-agent-sel').value;
  var temp    = parseFloat(document.getElementById('lcfg-temp-sl').value);
  var tokens  = parseInt(document.getElementById('lcfg-tok-sl').value);
  var prompt  = document.getElementById('lcfg-prompt').value.trim();
  var notes   = document.getElementById('lcfg-notes').value.trim();
  var payload = {temperature: temp, max_tokens: tokens};
  if (prompt) payload.system_prompt_override = prompt;
  if (notes)  payload.notes = notes;
  try {
    var res = await fetch('/model_configs/learned/' + encodeURIComponent(model) + '/' + agent, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var d = await res.json();
    if (d.ok) {
      document.getElementById('lcfg-prompt').value = '';
      document.getElementById('lcfg-notes').value = '';
      await renderLearnedConfigs(model);
    }
  } catch(e) { alert('Error: ' + e.message); }
}

async function deleteLearned(model, agent) {
  if (!confirm('Delete override for ' + agent + '?')) return;
  await fetch('/model_configs/learned/' + encodeURIComponent(model) + '/' + agent, {method: 'DELETE'});
  await renderLearnedConfigs(model);
}

async function resetLearnedModel() {
  var model = cfgState.currentModel;
  if (!model) return;
  if (!confirm('Delete all overrides for ' + model + '? (log is kept)')) return;
  await fetch('/model_configs/learned/' + encodeURIComponent(model), {method: 'DELETE'});
  await renderLearnedConfigs(model);
}

async function renderLog(model) {
  var c = document.getElementById('log-list');
  c.innerHTML = '<div class="status-txt">Loading...</div>';
  try {
    var d = await (await fetch('/model_configs/log/' + encodeURIComponent(model))).json();
    c.innerHTML = '';
    var entries = d.entries || [];
    if (!entries.length) {
      c.innerHTML = '<div class="empty">No log entries yet.</div>';
      return;
    }
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      var div = document.createElement('div');
      div.className = 'log-entry ' + (e.event === 'pipeline_run' ? 'pipeline' : 'manual');
      var ts = document.createElement('div'); ts.className = 'log-ts'; ts.textContent = e._timestamp || '';
      var evt = document.createElement('div'); evt.className = 'log-evt';
      evt.textContent = (e.event || '?') + (e.agent ? ' / ' + e.agent : '');
      div.appendChild(ts); div.appendChild(evt);
      if (e.event === 'pipeline_run') {
        var det = document.createElement('div'); det.className = 'log-detail';
        det.textContent = 'temp:' + (e.temperature || '?') + '  tokens:' + (e.output_length || '?') + ' chars out';
        div.appendChild(det);
      } else if (e.config) {
        var det2 = document.createElement('div'); det2.className = 'log-detail';
        det2.textContent = JSON.stringify(e.config).slice(0, 80);
        div.appendChild(det2);
      }
      c.appendChild(div);
    }
  } catch(e) {
    c.innerHTML = '<div class="empty">Error loading the log.</div>';
  }
}

async function clearLog() {
  var model = cfgState.currentModel;
  if (!model) return;
  if (!confirm('Clear log for ' + model + '?')) return;
  await fetch('/model_configs/log/' + encodeURIComponent(model), {method: 'DELETE'});
  await renderLog(model);
}

// -- Config Evaluator -------------------------------------------
async function runConfigEval() {
  var model = cfgState.currentModel;
  if (!model) { alert('No model selected.'); return; }

  var btn = document.getElementById('cfg-eval-btn');
  btn.textContent = 'Analysiere...';
  btn.disabled = true;

  try {
    var logData = await (await fetch('/model_configs/log/' + encodeURIComponent(model) + '?limit=50')).json();
    var entries = logData.entries || [];
    if (!entries.length) {
      showEvalResult('No log entries for ' + model + ' yet. Run some pipeline runs first.');
      return;
    }

    var agentStats = {};
    entries.forEach(function(e) {
      if (e.event !== 'pipeline_run' || !e.agent) return;
      if (!agentStats[e.agent]) agentStats[e.agent] = { runs: 0, totalOutLen: 0, temps: [], toks: [] };
      var s = agentStats[e.agent];
      s.runs++;
      s.totalOutLen += (e.output_length || 0);
      s.temps.push(e.temperature || 0);
      s.toks.push(e.max_tokens || 0);
    });

    var suggestions = [];
    Object.keys(agentStats).forEach(function(agent) {
      var s = agentStats[agent];
      if (!s.runs) return;
      var avgOut = Math.round(s.totalOutLen / s.runs);
      var avgTemp = s.temps.reduce(function(a,b){return a+b;},0) / s.temps.length;
      var avgTok = s.toks.reduce(function(a,b){return a+b;},0) / s.toks.length;

      if (avgOut > avgTok * 0.85) {
        suggestions.push({
          agent: agent, field: 'max_tokens',
          current: Math.round(avgTok),
          suggested: Math.min(2000, Math.round(avgTok * 1.3)),
          reason: 'Outputs benutzen ' + Math.round(avgOut/avgTok*100) + '% des Token-Budgets (\u00d8 ' + avgOut + '/' + Math.round(avgTok) + ') \u2014 Budget erhoehen.'
        });
      }
      if (avgOut < avgTok * 0.25 && avgTok > 200) {
        suggestions.push({
          agent: agent, field: 'max_tokens',
          current: Math.round(avgTok),
          suggested: Math.max(100, Math.round(avgOut * 2)),
          reason: 'Outputs only use ' + Math.round(avgOut/avgTok*100) + '% of the token budget \u2014 reduce budget for efficiency.'
        });
      }
    });

    if (!suggestions.length) {
      showEvalResult('\u2713 Configs look good for ' + model + ' (' + entries.length + ' log entries analyzed). No automatic adjustments recommended.');
    } else {
      showEvalResult('Empfehlungen fuer ' + model + ':', suggestions, model);
    }
  } catch(e) {
    showEvalResult('Error during analysis: ' + e.message);
  }

  btn.textContent = '\u26a1 Config-Eval';
  btn.disabled = false;
}

function showEvalResult(msg, suggestions, model) {
  var c = document.getElementById('cfg-eval-result');
  c.style.display = 'block';
  if (!suggestions || !suggestions.length) {
    c.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:11px;color:#7a8fa8;line-height:1.6">' + esc(msg) + '</div>';
    return;
  }
  var html = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8;margin-bottom:8px">' + esc(msg) + '</div>';
  suggestions.forEach(function(s) {
    html += '<div class="cfg-card" style="margin-bottom:6px">' +
      '<div class="cfg-card-hdr">' +
        '<span class="cfg-card-name" style="color:#e09030">' + esc(s.agent) + '</span>' +
        '<span class="cfg-card-badge base">' + esc(s.field) + '</span>' +
      '</div>' +
      '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8;margin-bottom:6px;line-height:1.5">' + esc(s.reason) + '</div>' +
      '<div style="display:flex;gap:8px;align-items:center;font-family:\'IBM Plex Mono\',monospace;font-size:12px;margin-bottom:6px">' +
        '<span style="color:#7a8fa8">' + s.current + '</span>' +
        '<span style="color:#7a8fa8">\u2192</span>' +
        '<span style="color:#3a9960;font-weight:600">' + s.suggested + '</span>' +
      '</div>' +
      '<button onclick="applyEvalSuggestion(\'' + esc(model) + '\',\'' + esc(s.agent) + '\',\'' + esc(s.field) + '\',' + s.suggested + ',this)" ' +
        'style="background:none;border:1px solid #3a9960;border-radius:4px;padding:3px 10px;' +
        'color:#3a9960;font-family:\'IBM Plex Mono\',monospace;font-size:10px;cursor:pointer;transition:all .15s">' +
        '\u2713 Apply' +
      '</button>' +
    '</div>';
  });
  c.innerHTML = html;
}

async function applyEvalSuggestion(model, agent, field, value, btn) {
  btn.textContent = '...';
  btn.disabled = true;
  try {
    var existing = {};
    try {
      var r = await (await fetch('/model_configs/learned/' + encodeURIComponent(model) + '/' + agent)).json();
      if (r.is_learned) existing = r.config;
    } catch(e) {}
    existing[field] = value;
    await fetch('/model_configs/learned/' + encodeURIComponent(model) + '/' + agent, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(existing)
    });
    await fetch('/model_configs/log/' + encodeURIComponent(model), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event: 'eval_suggestion_applied', agent: agent, field: field, value: value})
    });
    btn.textContent = '\u2713 Saved';
    btn.style.borderColor = '#e09030';
    btn.style.color = '#e09030';
    setTimeout(function() { renderLearnedConfigs(model); }, 500);
  } catch(e) {
    btn.textContent = 'Error';
    btn.disabled = false;
  }
}

// -- Soul Panel ------------------------------------------------
let soulCurrentTab = 'view';
let soulIsLearned  = false;

async function loadSoulStatus() {
  try {
    const [statusRes, currentRes] = await Promise.all([
      fetch('/soul/status').then(r => r.json()),
      fetch('/soul/current').then(r => r.json()),
    ]);

    soulIsLearned = statusRes.use_learned || false;
    const learned = statusRes.learned || {};

    const badge = document.getElementById('soul-source-badge');
    if (soulIsLearned) {
      badge.textContent = '\u25CF LEARNED SOUL ACTIVE (Learning Mode)';
      badge.className   = 'soul-source-badge learned';
    } else {
      badge.textContent = '\u25CF ORIGINAL SOUL ACTIVE (Standard)';
      badge.className   = 'soul-source-badge original';
    }

    document.getElementById('soul-ver').textContent    = soulIsLearned ? ('v' + (learned.version || 0)) : 'Original (immutable)';
    document.getElementById('soul-runs').textContent   = soulIsLearned ? (learned.run_count || 0) : '-';
    document.getElementById('soul-evos').textContent   = soulIsLearned ? (learned.evolutions || 0) : '-';
    document.getElementById('soul-reason').textContent = soulIsLearned ? (learned.last_reason || 'none yet') : 'Never changed';

    document.getElementById('soul-text-display').textContent = currentRes.text || '(empty)';

    const editBtn = document.getElementById('soul-edit-btn');
    editBtn.disabled  = !soulIsLearned;
    editBtn.style.opacity = soulIsLearned ? '1' : '0.35';
    editBtn.title = soulIsLearned ? '' : 'Editable only in Learning Mode';

    if (soulCurrentTab === 'edit') {
      const learnedRes = await fetch('/soul/learned').then(r => r.json());
      document.getElementById('soul-edit-area').value = learnedRes.text || '';
    }
    if (soulCurrentTab === 'history') {
      await loadSoulHistory();
    }
  } catch(e) {
    document.getElementById('soul-text-display').textContent = 'Error: ' + e.message;
  }
}

async function loadSoulHistory() {
  try {
    const data = await fetch('/soul/history?limit=8').then(r => r.json());
    const c    = document.getElementById('soul-history-list');
    if (!data.history || !data.history.length) {
      c.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8">No evolutions saved yet.</div>';
      return;
    }
    c.innerHTML = data.history.map(function(h) {
      return '<div class="soul-history-item">' +
        '<span class="soul-history-ver">v' + h.version + '</span>' +
        '<span class="soul-history-ts">' + (h.timestamp || '').slice(0,16) + '</span>' +
        '<div class="soul-history-reason">' + esc(h.reason || '?') + '</div>' +
        '<div class="soul-history-preview">' + esc(h.soul_preview || '') + '</div>' +
      '</div>';
    }).join('');
  } catch(e) {}
}

// FIX: setSoulTab now uses the correct 'ghost' class
async function setSoulTab(tab, btn) {
  soulCurrentTab = tab;
  // reset all soul sub-tab buttons
  document.querySelectorAll('#soul-sub-tabs button').forEach(function(b) {
    b.classList.remove('on');
  });
  btn.classList.add('on');

  document.getElementById('soul-tab-view').style.display    = tab === 'view'    ? '' : 'none';
  document.getElementById('soul-tab-edit').style.display    = tab === 'edit'    ? '' : 'none';
  document.getElementById('soul-tab-history').style.display = tab === 'history' ? '' : 'none';
  document.getElementById('soul-tab-skills').style.display  = tab === 'skills'  ? '' : 'none';
  document.getElementById('soul-tab-insights').style.display = tab === 'insights' ? '' : 'none';
  document.getElementById('soul-tab-tokens').style.display  = tab === 'tokens'  ? '' : 'none';

  if (tab === 'edit' && soulIsLearned) {
    const data = await fetch('/soul/learned').then(r => r.json());
    document.getElementById('soul-edit-area').value = data.text || '';
  }
  if (tab === 'history') {
    await loadSoulHistory();
  }
  if (tab === 'skills') {
    await loadSkills();
  }
  if (tab === 'insights') {
    await loadSoulInsights();
  }
  if (tab === 'tokens') {
    await loadTokenStats();
  }
}

// ── Skills Viewer ─────────────────────────────────────────────────────────
async function loadSkills() {
  try {
    const res = await fetch('/soul/skills');
    const skills = await res.json();
    const container = document.getElementById('skills-list');

    if (!skills.length) {
      container.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8;padding:8px">No skills available. Enable Skill Writing to generate skills.</div>';
      return;
    }

    container.innerHTML = skills.map(s => {
      const scoreColor = s.relevance_score > 0.7 ? 'var(--green)' : s.relevance_score > 0.4 ? 'var(--amber)' : '#7a8fa8';
      const preview = (s.insight || '').slice(0, 120) + ((s.insight || '').length > 120 ? '\u2026' : '');
      const paths = (s.trigger_paths || []).join(', ');
      return '<div style="border-bottom:1px solid #1e2a38;padding:6px 2px">' +
        '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:' + scoreColor + ';font-weight:600">' + s.relevance_score.toFixed(2) + '</span>' +
        '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:#7a8fa8;margin-left:6px">\u00d7' + s.merge_count + ' \u00b7 ' + esc(s.source) + '</span>' +
        '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:#c8d8e8;margin-top:2px">' + esc(preview) + '</div>' +
        (paths ? '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#5a6a7a;margin-top:1px">' + esc(paths) + '</div>' : '') +
      '</div>';
    }).join('');
  } catch(e) {
    const container = document.getElementById('skills-list');
    if (container) container.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#c05050;padding:8px">Error loading: ' + esc(e.message) + '</div>';
  }
}

// ── Insights Viewer ────────────────────────────────────────────────────────
async function loadSoulInsights() {
  try {
    const res = await fetch('/soul/insights');
    const data = await res.json();
    const totalEl = document.getElementById('insights-total');
    const listEl = document.getElementById('insights-list');

    totalEl.textContent = data.total + ' insights stored';

    if (!data.insights || !data.insights.length) {
      listEl.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8;padding:8px">No insights available. They are generated automatically after agentic loops.</div>';
      return;
    }

    listEl.innerHTML = data.insights.map(function(ins) {
      const scoreColor = ins.relevance_score > 0.7 ? 'var(--green)' : ins.relevance_score > 0.4 ? 'var(--amber)' : '#7a8fa8';
      const preview = esc((ins.insight || '').slice(0, 100));
      return '<div style="border-bottom:1px solid #1e2a38;padding:5px 2px">' +
        '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:' + scoreColor + ';font-weight:600">' + ins.relevance_score.toFixed(2) + '</span>' +
        '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:#7a8fa8;margin-left:4px">\u00d7' + ins.merge_count + ' \u00b7 ' + esc(ins.source) + '</span>' +
        '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:#c8d8e8;margin-top:2px">' + preview + '</div>' +
        (ins.trigger_path ? '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#5a6a7a;margin-top:1px">' + esc(ins.trigger_path) + '</div>' : '') +
      '</div>';
    }).join('');
  } catch(e) {
    const listEl = document.getElementById('insights-list');
    if (listEl) listEl.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#c05050;padding:8px">Error: ' + esc(e.message) + '</div>';
  }
}

async function resetInsights() {
  if (!confirm('Reset all insights? This deletes all learned patterns.')) return;
  try {
    await fetch('/soul/insights', {method: 'DELETE'});
    await loadSoulInsights();
  } catch(e) { alert('Error: ' + e.message); }
}

// ── Token Stats Viewer ─────────────────────────────────────────────────────
function _fmtTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

function _fmtDuration(s) {
  if (s >= 3600) return (s / 3600).toFixed(1) + 'h';
  if (s >= 60) return (s / 60).toFixed(1) + 'm';
  return s.toFixed(0) + 's';
}

function toggleRunDetail(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function updateMlockHint() {
  var mlockEl = document.getElementById('llama-mlock-toggle');
  if (mlockEl) mlockEl.checked = S.llamaMlock;
  var configMlockEl = document.getElementById('config-mlock-toggle');
  if (configMlockEl) configMlockEl.checked = S.llamaMlock;
  ['mlock-hint', 'config-mlock-hint'].forEach(function(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = S.llamaMlock ? '\u25CF on' : '\u25CB off';
    el.style.color = S.llamaMlock ? '#3a9960' : 'var(--tx2)';
  });
}

function _isMoeModel(m) {
  m = String(m || '').trim().toLowerCase();
  if (!m) return false;
  if (S.moeExpertDefaults && S.moeExpertDefaults[m]) return true;
  if (/moe/.test(m)) return true;
  return /(^|[-._:])a\d+b([-._]|$)/.test(m);
}

function updateMoeVisibility() {
  // MOE-AUTODETECT (2026-08-27): panel visibility no longer depends only
  // on "a3b/moe" in the name, but on _isMoeModel (defaults table +
  // generic aXd-b pattern, e.g. 8b-a1b).
  var moeWrap = document.getElementById('wrapper-moe-experts');
  if (moeWrap) {
    moeWrap.style.display = (_isMoeModel(S.duoCoderModel) || _isMoeModel(S.duoPlannerModel)) ? 'block' : 'none';
  }
  updateMoeExpertDefaultHint();
}

function _moeModelList() {
  var list = [];
  var seen = {};
  function add(m) {
    m = String(m || '').trim();
    if (!m || seen[m]) return;
    seen[m] = true;
    list.push(m);
  }
  Object.keys(S.moeExpertDefaults || {}).forEach(add);
  Object.keys(S.moeCpuExpertsMap || {}).forEach(add);
  [S.duoCoderModel, S.duoPlannerModel].forEach(function(m) {
    if (_isMoeModel(m)) add(m);
  });
  return list;
}

function rebuildMoeModelDropdowns() {
  var list = _moeModelList();
  if (!S.moeSelectedModel || list.indexOf(S.moeSelectedModel) === -1) {
    S.moeSelectedModel = list.indexOf(S.duoCoderModel) >= 0 ? S.duoCoderModel : (list[0] || '');
  }
  ['moe-model-select', 'config-moe-model-select'].forEach(function(id) {
    var sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = list.map(function(m) {
      var short = m.split(':')[0];
      return '<option value="' + esc(m) + '">' + esc(short) + '</option>';
    }).join('');
    sel.value = S.moeSelectedModel;
  });
  _syncMoeInputs();
}

function _syncMoeInputs() {
  var v = (S.moeCpuExpertsMap[S.moeSelectedModel] || 0);
  ['moe-cpu-experts', 'config-moe-cpu-experts'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.value = v;
  });
}

function onMoeModelSelect(model) {
  S.moeSelectedModel = model;
  ['moe-model-select', 'config-moe-model-select'].forEach(function(id) {
    var sel = document.getElementById(id);
    if (sel) sel.value = S.moeSelectedModel;
  });
  _syncMoeInputs();
  updateMoeVisibility();
}

function onMoeExpertsChange(val) {
  var v = parseInt(val, 10) || 0;
  if (!S.moeSelectedModel) return;
  if (v > 0) S.moeCpuExpertsMap[S.moeSelectedModel] = v;
  else delete S.moeCpuExpertsMap[S.moeSelectedModel];
  _syncMoeInputs();
  postSettings({moe_cpu_experts: S.moeCpuExpertsMap});
  updateMoeExpertDefaultHint();
}

function updateMoeExpertDefaultHint() {
  var defaults = S.moeExpertDefaults || {};
  var autodetect = S.moeAutodetect || {};
  var model = S.moeSelectedModel || S.duoCoderModel || S.duoPlannerModel || '';
  var _ovr = parseInt((S.moeCpuExpertsMap || {})[model], 10) || 0;
  var _modelShort = model.split(':')[0];
  var _def = defaults[model];
  var _auto = !!autodetect[model];
  var _defTxt = '';
  if (_def) _defTxt = _def + ' experts' + (_auto ? ' (autodetected from model name)' : ' (table)');
  else if (model) _defTxt = 'no default (experts stay on GPU)';
  var text = '';
  if (_ovr > 0) {
    text = model
      ? 'Override: ' + _ovr + ' experts. Default for ' + _modelShort + ': ' + _defTxt + '.'
      : '';
  } else if (model) {
    text = 'Auto \u2192 default for ' + _modelShort + ': ' + _defTxt + '. Number on the right = override for this model.';
  }
  ['moe-default-hint', 'config-moe-default-hint'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  });
}

async function loadTokenStats() {
  try {
    const res = await fetch('/soul/token-stats?days=14');
    const data = await res.json();
    const summaryEl = document.getElementById('token-stats-summary');
    const dailyEl = document.getElementById('token-daily-list');

    const totalTokens = data.total_tokens || 0;
    const totalRuns = data.total_runs || 0;
    const avgTokens = totalRuns > 0 ? Math.round(totalTokens / totalRuns) : 0;
    // TOKEN-TRACKER UI (2026-08-25): Input-/Cache-Dimension + Hit-Quote.
    const totalPrompt = data.total_prompt_tokens || 0;
    const totalCached = data.total_cached_tokens || 0;
    const hitPct = totalPrompt > 0 ? Math.round((totalCached / totalPrompt) * 100) : 0;
    const avgInPerRun = totalRuns > 0 ? Math.round(totalPrompt / totalRuns) : 0;
    // AUDIT-R2+ (2026-08-25): request count — Σ cached ÷ requests ≈ Ø
    // reused context per LLM call (~context size) → makes the
    // large cached sum comprehensible.
    const totalRequests = data.total_requests || 0;
    const avgCachedPerReq = totalRequests > 0 ? Math.round(totalCached / totalRequests) : 0;
    // The actual compute load: prompt minus KV hits (e.g. only 40K of 1.3M)
    const actualComputed = Math.max(0, totalPrompt - totalCached);

    // TOKEN LABELS CLARIFICATION (2026-08-25): total_tokens are only the
    // GENERATED output tokens — next to 1.3M input, "total tokens" looked
    // like a contradiction. New: input+output grand total + precise labels.
    const grandTotalTokens = totalPrompt + totalTokens;

    summaryEl.innerHTML =
      '<div class="soul-stat-row"><span class="soul-stat-label">Input + Output total</span><span class="soul-stat-val" style="color:#c8d4e0">' + _fmtTokens(grandTotalTokens) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">Output tokens (generated)</span><span class="soul-stat-val" style="color:#60a0e0">' + _fmtTokens(totalTokens) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">Input (prompt)</span><span class="soul-stat-val" style="color:#9a74dc">' + _fmtTokens(totalPrompt) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">of which Cached (Σ KV hits all requests)</span><span class="soul-stat-val" style="color:#3a9960">' + _fmtTokens(totalCached) + ' <span style="color:#5a6a7a;font-size:10px">(' + hitPct + '% hit)</span></span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">↳ actually recomputed</span><span class="soul-stat-val" style="color:#60c080">' + _fmtTokens(actualComputed) + ' <span style="color:#5a6a7a;font-size:10px">(' + (100 - hitPct) + '%)</span></span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">LLM requests</span><span class="soul-stat-val">' + (totalRequests || '–') + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 cached/request</span><span class="soul-stat-val">' + (avgCachedPerReq ? _fmtTokens(avgCachedPerReq) : '–') + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 input/run</span><span class="soul-stat-val">' + _fmtTokens(avgInPerRun) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">Total runs</span><span class="soul-stat-val">' + totalRuns + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 output/run</span><span class="soul-stat-val">' + _fmtTokens(avgTokens) + '</span></div>' +
      '<div style="font-size:9px;color:#5a6a7a;padding-top:6px;line-height:1.5">Input/Cached = sum over ALL LLM requests: every tool run and every phase resends the context (agentic loop). "Cached" = KV-cache hits \u2014 a high share is good (fast, cheap), no consumption.</div>';

    const daily = data.daily || {};
    const days = Object.keys(daily).sort().reverse();

    if (!days.length) {
      dailyEl.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8;padding:8px">No token data yet. Captured after each run (including aborted ones).</div>';
      return;
    }

    // Find max for bar scaling
    let maxDayTokens = 0;
    days.forEach(function(d) { maxDayTokens = Math.max(maxDayTokens, daily[d].tokens || 0); });

    dailyEl.innerHTML = days.map(function(day, di) {
      const entry = daily[day];
      const pct = maxDayTokens > 0 ? Math.round((entry.tokens / maxDayTokens) * 100) : 0;
      const avgPerRun = entry.runs > 0 ? Math.round(entry.tokens / entry.runs) : 0;
      const elapsed = entry.elapsed_s || 0;
      const runList = entry.run_list || [];
      const uid = 'tsd-' + di;
      let runsHtml = '';
      if (runList.length) {
        runsHtml = '<div id="' + uid + '" style="display:none;margin:4px 0 2px;padding-left:6px;border-left:1px solid #1e2a38">' +
          runList.map(function(r) {
            const sr = r.stop_reason || 'completed';
            const srColor = sr === 'completed' ? '#3a9960' : (sr === 'graceful_stop' ? '#8a8a8a' : '#c08020');
            const phases = r.phases || {};
            const phaseParts = [];
            ['pre_explore','planner','coder','critic'].forEach(function(p) {
              if (phases[p]) phaseParts.push('<span style="color:#6a7a8a">' + p.replace('_',' ') + ':</span> <span style="color:#9ab0c4">' + _fmtTokens(phases[p]) + '</span>');
            });
            const models = r.models || {};
            const modelParts = [];
            if (models.planner) modelParts.push('<span style="color:#9a74dc">P:</span> ' + esc(String(models.planner).split(':')[0]));
            if (models.coder) modelParts.push('<span style="color:#20b0a0">C:</span> ' + esc(String(models.coder).split(':')[0]));
            if (models.critic) modelParts.push('<span style="color:#e09030">K:</span> ' + esc(String(models.critic).split(':')[0]));
            if (Array.isArray(models.pre_explore) && models.pre_explore.length) modelParts.push('<span style="color:#6a9a8a">X:</span> ' + esc(models.pre_explore.map(function(m){return String(m).split(':')[0];}).join(',')));
            // TOKEN-TRACKER UI (2026-08-25): show input/cached per run.
            var _rIn = r.prompt_tokens || 0, _rCa = r.cached_tokens || 0;
            var _ioParts = [];
            if (_rIn > 0) _ioParts.push('<span style="color:#6a7a8a">in:</span> <span style="color:#9a74dc">' + _fmtTokens(_rIn) + '</span>');
            if (_rCa > 0) {
              var _rHit = _rIn > 0 ? Math.round((_rCa / _rIn) * 100) : 0;
              _ioParts.push('<span style="color:#6a7a8a">cached:</span> <span style="color:#3a9960">' + _fmtTokens(_rCa) + ' (' + _rHit + '%)</span>');
              // AUDIT-R2+ (2026-08-25): make the real compute load visible
              var _rNew = Math.max(0, _rIn - _rCa);
              if (_rNew > 0 || r.requests) _ioParts.push('<span style="color:#6a7a8a">recomputed:</span> <span style="color:#60c080">' + _fmtTokens(_rNew) + '</span>');
            }
            if (r.requests) _ioParts.push('<span style="color:#6a7a8a">' + r.requests + ' reqs</span>');
            return '<div style="padding:3px 0;border-bottom:1px solid #141c26">' +
              '<div style="display:flex;justify-content:space-between;font-size:9px">' +
                '<span style="color:var(--tx2)">' + (r.t || '') + ' · <span style="color:' + srColor + '">' + esc(sr) + '</span></span>' +
                '<span style="color:#60a0e0;font-weight:600">' + _fmtTokens(r.tokens) + ' · ' + _fmtDuration(r.elapsed_s || 0) + '</span>' +
              '</div>' +
              (phaseParts.length ? '<div style="font-size:8px;color:#6a7a8a;margin-top:1px">' + phaseParts.join(' · ') + '</div>' : '') +
              (modelParts.length ? '<div style="font-size:8px;margin-top:1px">' + modelParts.join(' · ') + '</div>' : '') +
              (_ioParts.length ? '<div style="font-size:8px;margin-top:1px">' + _ioParts.join(' · ') + '</div>' : '') +
            '</div>';
          }).join('') + '</div>';
      }
      return '<div style="border-bottom:1px solid #1e2a38;padding:4px 2px">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none" onclick="toggleRunDetail(\'' + uid + '\')">' +
          '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:var(--tx2)">' + day + ' <span style="color:#4a5a6a">&#9656;</span></span>' +
          '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#60a0e0;font-weight:600">' + _fmtTokens(entry.tokens) + '</span>' +
        '</div>' +
        '<div style="height:4px;background:#1a2030;border-radius:2px;margin:3px 0;overflow:hidden">' +
          '<div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#3a6090,#60a0e0);border-radius:2px"></div>' +
        '</div>' +
        '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#5a6a7a">' + entry.runs + ' runs \u00b7 \u00d8 ' + _fmtTokens(avgPerRun) + '/run \u00b7 ' + _fmtDuration(elapsed) + ' total</span>' +
        runsHtml +
      '</div>';
    }).join('');
  } catch(e) {
    const summaryEl = document.getElementById('token-stats-summary');
    if (summaryEl) summaryEl.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#c05050;padding:8px">Error: ' + esc(e.message) + '</div>';
  }
}

async function resetTokenStats() {
  if (!confirm('Reset all token statistics?')) return;
  try {
    await fetch('/soul/token-stats', {method: 'DELETE'});
    await loadTokenStats();
  } catch(e) { alert('Error: ' + e.message); }
}

async function saveLearnesSoul() {
  if (!soulIsLearned) {
    alert('Editable only in Learning Mode. Enable Learning Mode and try again.');
    return;
  }
  const text   = document.getElementById('soul-edit-area').value.trim();
  const reason = document.getElementById('soul-edit-reason').value.trim() || 'Manually edited';
  if (text.length < 50) {
    alert('Soul text too short (min. 50 characters).');
    return;
  }
  try {
    const r = await fetch('/soul/learned', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: text, reason: reason})
    });
    const d = await r.json();
    if (d.ok) {
      document.getElementById('soul-edit-reason').value = '';
      await loadSoulStatus();
      alert('Learned Soul saved. v' + (d.status && d.status.version || '?'));
    }
  } catch(e) { alert('Error: ' + e.message); }
}

async function resetLearnedSoul() {
  if (!confirm('Reset Learned Soul to seed? Current text is saved to history.')) return;
  try {
    await fetch('/soul/learned', {method: 'DELETE'});
    await loadSoulStatus();
  } catch(e) { alert('Error: ' + e.message); }
}


// -- Memory Add ------------------------------------------------
async function addMem() {
  var key = (document.getElementById('mem-add-key').value || '').trim();
  var val = (document.getElementById('mem-add-val').value || '').trim();
  if (!key || !val) return;
  try {
    var res = await fetch('/memory/' + encodeURIComponent(key), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({value: val})
    });
    var d = await res.json();
    if (d.ok) {
      document.getElementById('mem-add-key').value = '';
      document.getElementById('mem-add-val').value = '';
      await loadMemory();
    } else {
      alert('Error: ' + (d.error || 'unknown'));
    }
  } catch(e) { alert('Error: ' + e.message); }
}

// -- Soul Evolution Force ----------------------------------------
async function triggerSoulEvolution(force) {
  var btn = document.getElementById(force ? 'soul-force-btn' : 'soul-evolve-btn');
  var statusEl = document.getElementById('soul-evolve-status');
  if (btn) { btn.disabled = true; btn.textContent = force ? 'Forcing...' : 'Running...'; }
  if (statusEl) statusEl.textContent = force ? 'Forcing evolution...' : 'Starting evolution...';
  try {
    var res = await fetch('/soul/evolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: force})
    });
    var d = await res.json();
    if (d.ok) {
      if (statusEl) statusEl.style.color = '#3a9960';
      if (statusEl) statusEl.textContent = (force ? 'Forced: ' : 'Evolved: ') + 'v' + (d.soul && d.soul.version || '?');
      await loadSoulStatus();
    } else {
      if (statusEl) statusEl.style.color = '#e09030';
      if (statusEl) statusEl.textContent = d.reason || 'Not possible';
      if (!force) {
        // hint: offer the force option
        if (statusEl) statusEl.textContent += ' → use "FORCE"';
      }
    }
  } catch(e) {
    if (statusEl) statusEl.textContent = 'Error: ' + e.message;
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = force ? '⚠ Evolution FORCE' : '◆ Trigger Evolution';
  }
  setTimeout(function() {
    if (statusEl) { statusEl.textContent = ''; statusEl.style.color = '#7a8fa8'; }
  }, 6000);
}

// -- Stop Stream -----------------------------------------------
async function stopStream() {
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
      setStopBtnState('graceful_pending');
      try {
        var resp = await fetch('/abort/graceful/' + S.currentRunId, {method: 'POST'});
        if (!resp.ok) {
          setStopBtnState('idle');
          showInfo('\u26A0 Graceful-Stop fehlgeschlagen');
          return;
        }
        showInfo('\u23F9 Stop requested after chunk end \u2014 runs until the current chunk completes');
      } catch(e) {
        setStopBtnState('idle');
        showInfo('\u26A0 Graceful-Stop fehlgeschlagen: ' + e);
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

// tab/window closes → silent kill, no resume block
window.addEventListener('beforeunload', function() {
  if (S._pauseBtnState === 'paused_manual') return;
  if (S._pauseBtnState === 'paused_by_throttle') return;
  if (S._stopBtnState === 'graceful_pending') return;
  if (S.currentChatId && S.streaming) {
    navigator.sendBeacon('/abort?chat_id=' + S.currentChatId + '&silent=true');
  } else if (S.currentRunId && S.streaming) {
    navigator.sendBeacon('/abort/' + S.currentRunId);
  }
});

// tab/window closes → persist chat (possibly partial) if auto-save is active
window.addEventListener('pagehide', function() {
  if (!S.chatAutosave) return;
  _autosaveChatMessage();
  if (!S.currentChatMessages || !S.currentChatMessages.length) return;
  const firstUser = S.currentChatMessages.find(function(m){return m.role==='user';});
  const title = firstUser ? firstUser.content.slice(0, 48) : 'Chat ' + new Date().toLocaleTimeString();
  const payload = {
    chat_id: S.currentChatId || undefined,
    messages: S.currentChatMessages,
    title: S.currentChatId ? undefined : title   // don't overwrite existing titles
  };
  try {
    navigator.sendBeacon('/chats/persist', new Blob([JSON.stringify(payload)], {type: 'application/json'}));
  } catch(e) {}
});

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

// ── Skills (C-6, Phase 1) ────────────────────────────────────────────────
async function loadSkillsUI() {
  const listEl = document.getElementById('skills-list');
  if (!listEl) return;
  try {
    const r = await fetch('/skills');
    const data = await r.json();
    const skills = (data && data.skills) || [];
    if (!skills.length) {
      listEl.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:#7a8fa8">No skills found — create .hivemind/skills/&lt;name&gt;.md.</div>';
      return;
    }
    listEl.innerHTML = '';
    skills.forEach(function(sk) {
      const row = document.createElement('div');
      row.className = 'cfl-row';
      row.style.cssText = 'margin:0;padding:0 2px;gap:6px';
      const srcBadge = sk.source === 'distilled'
        ? '<span style="font-size:7px;padding:0 4px;border-radius:2px;background:rgba(144,96,240,.2);color:#9060f0">distilled</span>'
        : '';
      row.innerHTML =
        '<div style="flex:1;min-width:0">' +
          '<div style="font-size:10px;color:#c8d6e8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(sk.name) + ' ' + srcBadge + '</div>' +
          '<div style="font-size:8px;color:#7a8fa8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(sk.description || '') + '</div>' +
        '</div>' +
        '<label class="cfl-sw">' +
          '<input type="checkbox" ' + (sk.enabled ? 'checked' : '') + ' onchange="toggleSkill(this,\'' + sk.name.replace(/'/g, "\\'") + '\')">' +
          '<span class="sl"></span>' +
        '</label>';
      listEl.appendChild(row);
    });
  } catch (e) {
    listEl.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:#e08080">Failed to load skills: ' + esc(String(e)) + '</div>';
  }
}

async function toggleSkill(el, name) {
  try {
    const r = await fetch('/skills/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, enabled: el.checked})
    });
    const data = await r.json();
    if (!data.ok) { el.checked = !el.checked; showInfo('⚠ Skill-Toggle fehlgeschlagen'); }
  } catch (e) {
    el.checked = !el.checked;
    showInfo('⚠ Network error while toggling skill');
  }
}

async function resumeWithAnswer() {
  var inp = document.getElementById('input');
  var answer = inp ? inp.value.trim() : '';
  if (!answer) { if (inp) inp.focus(); return; }
  if (S._pauseBtnState === 'paused_by_throttle' && answer.length < 10) {
    showInfo('\u26A0 Please enter at least 10 characters');
    if (inp) inp.focus();
    return;
  }
  stopAskUserCountdown();

  if (inp) inp.disabled = true;

  try {
    var resp = await fetch('/api/run/' + S.currentRunId + '/resume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({answer: answer})
    });
    if (resp.status === 409) {
      if (inp) { inp.value = ''; inp.placeholder = 'Message...'; inp.disabled = false; }
      showInfo('Auto-answer was already sent \u2014 your answer is ignored.');
      var _qDivRace = document.getElementById('ask-user-question');
      if (_qDivRace) _qDivRace.remove();
      S.agentPaused = false;
      setPauseBtnState('running');
      return;
    }
    if (!resp.ok) {
      if (inp) inp.disabled = false;
      var _errDiv = document.createElement('div');
      _errDiv.className = 'msg divider';
      _errDiv.style.cssText = 'color:#b04040;border-color:rgba(176,64,64,.3)';
      _errDiv.textContent = '\u26A0 Error sending the answer';
      document.getElementById('chat').appendChild(_errDiv);
      scrollBtmIfNearBottom(120);
      if (inp) inp.focus();
      return;
    }
  } catch(e) {
    if (inp) inp.disabled = false;
    var _errDiv2 = document.createElement('div');
    _errDiv2.className = 'msg divider';
    _errDiv2.style.cssText = 'color:#b04040;border-color:rgba(176,64,64,.3)';
    _errDiv2.textContent = '\u26A0 Network error: ' + e.message;
    document.getElementById('chat').appendChild(_errDiv2);
    scrollBtmIfNearBottom(120);
    if (inp) inp.focus();
    return;
  }

  if (inp) { inp.value = ''; inp.placeholder = 'Message...'; inp.disabled = false; }
  S.agentPaused = false;
  var pb = document.getElementById('pause-btn');
  if (pb) { pb.textContent = '\u23F8'; pb.classList.remove('paused'); pb.title = 'Pause'; }
}

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
function onChunkingChange() {
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
            postSettings({_thinking_before_chunking: S.duoAgenticThinking}); // persist original preference
        }
        S.duoAgenticThinking = true; // UI-only override, NOT persisted
        if (thinkTog) { thinkTog.checked = true; thinkTog.disabled = true; }
        if (thinkHint) thinkHint.textContent = 'Forced during chunking — planner needs reasoning for smart splitting';
        // do NOT postSettings({duo_agentic_thinking: true}) — that corrupts the user value!
        // the server enforces thinking during chunking automatically (see server.py)
        // show the per-chunk test feedback
        if (tfChunkRow) tfChunkRow.style.display = '';
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
                postSettings({duo_agentic_thinking: S.duoAgenticThinking});
            }
            // cleanup: remove the stored preference
            S._thinkingBeforeChunking = undefined;
            postSettings({_thinking_before_chunking: null}); // clear persisted backup
        }
        if (thinkHint) thinkHint.textContent = 'Planner thinks through the task before output';
        // Test-Feedback pro Chunk ausblenden (nur bei Chunking relevant)
        S.duoTestFeedbackChunk = false;
        if (tfChunkToggle) { tfChunkToggle.checked = false; }
        if (tfChunkRow) tfChunkRow.style.display = 'none';
        postSettings({duo_test_feedback_chunk: false});
    }
}
function updatePlannerHint() {
    var chunkRow = document.getElementById('duo-chunking-row');
    if (chunkRow) chunkRow.style.display = S.duoPlannerEnabled ? '' : 'none';
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

function togglePlannerUseCoderCtx(on) {
  S.duoPlannerUseCoderCtx = on;
  postSettings({duo_planner_use_coder_ctx: on});
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
  
  if (isAgentic) {
    // agentic mode: coder + planner
    agWrap.style.display = 'block';
    if (plTog) plTog.style.display = 'block';
    if (plWrap) plWrap.style.display = 'block';
    
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

function togglePreExploreAdv() {
    var body  = document.getElementById('preexplore-adv-body');
    var arrow = document.getElementById('preexplore-adv-arrow');
    if (!body) return;
    var open = body.style.display === 'none';
    body.style.display = open ? 'block' : 'none';
    if (arrow) arrow.style.transform = open ? 'rotate(90deg)' : '';
}
// -- Vision Accordion -----------------------------------------
function _applyImageModeUI(mode) {
  var bd = document.getElementById('im-direct');
  var bp = document.getElementById('im-preprocess');
  var bl = document.getElementById('im-pipeline');
  if (bd) bd.classList.toggle('on', mode === 'direct');
  if (bp) bp.classList.toggle('on', mode === 'preprocess');
  if (bl) bl.classList.toggle('on', mode === 'pipeline');
  var ds = document.getElementById('im-direct-sec');
  var ps = document.getElementById('im-preprocess-sec');
  var ls = document.getElementById('im-pipeline-sec');
  if (ds) ds.style.display = (mode === 'direct') ? 'block' : 'none';
  if (ps) ps.style.display = (mode === 'preprocess') ? 'block' : 'none';
  if (ls) ls.style.display = (mode === 'pipeline') ? 'block' : 'none';
}

function setImageMode(mode) {
  S.imageMode = mode;
  _applyImageModeUI(mode);
  postSettings({image_processing_mode: mode});
  if (mode === 'preprocess') {
    S.visionEnabled = true;
    fetch('/vision/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: true})});
    S.visionAgentEnabled = false;
    postSettings({vision_agent_enabled: false});
  } else if (mode === 'direct') {
    S.visionEnabled = false;
    fetch('/vision/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: false})});
    S.visionAgentEnabled = false;
    postSettings({vision_agent_enabled: false});
  } else {
    S.visionEnabled = false;
    fetch('/vision/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: false})});
    S.visionAgentEnabled = true;
    if (!S.pipelineVisionRoles || !Object.keys(S.pipelineVisionRoles).length) {
      S.pipelineVisionRoles = {analyst: true};
      postSettings({pipeline_vision_roles: S.pipelineVisionRoles});
    }
    postSettings({vision_agent_enabled: true});
  }
  var vaEl = document.getElementById('va-enabled-toggle');
  if (vaEl) vaEl.checked = S.visionAgentEnabled;
  _applyPipelineVisionRolesUI();
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
}

function setVisionAgentEnabled(enabled) {
  S.visionAgentEnabled = enabled;
  postSettings({vision_agent_enabled: enabled});
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
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
  postSettings({pipeline_vision_roles: S.pipelineVisionRoles});
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
}

function setVisionAgentModel(model) {
  S.visionAgentModel = model;
  postSettings({vision_agent_model: model});
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch(e) {} }
}

function setVisionAgentMode(mode) {
  S.visionAgentMode = mode;
  postSettings({vision_agent_mode: mode});
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
  grpOther.label = 'Andere';
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
  var _dProf = _dModel && S.modelProfiles && (S.modelProfiles[_dModel] || S.modelProfiles[_dModel.split(':')[0]]);
  var _dVision = !!( _dProf && _dProf.vision );
  var _dDisp = _dModel ? _dModel.replace(':latest','').split('/').pop() : '—';
  var mode = S.imageMode || 'direct';
  var _flow = '';
  if (flowCard) {
    if (mode === 'preprocess') {
      flowCard.style.borderColor = 'rgba(224,144,48,.45)';
      flowCard.style.background = 'rgba(224,144,48,.07)';
      var _pm = (S.visionModel || '').replace(':latest','').split('/').pop();
      _flow = '<span style="color:#e0a050">&#9654; Preprocessor:</span> ' + (_pm || 'no model selected')
            + ' describes the image as text before the run.<br>'
            + '<span style="color:var(--tx3)">Every agent stays text-based.</span>';
    } else if (mode === 'pipeline') {
      flowCard.style.borderColor = 'rgba(136,88,192,.45)';
      flowCard.style.background = 'rgba(136,88,192,.07)';
      var _vm = (S.visionAgentModel || '').replace(':latest','').split('/').pop();
      _flow = '<span style="color:#b090d0">&#9654; Pipeline:</span> the vision-agent'
            + (_vm ? ' (' + _vm + ')' : ' (no model selected)')
            + ' describes the image as text.<br>';
      var _roles = [];
      ['analyst','refiner','critic','synthesizer'].forEach(function(r) {
        if (S.pipelineVisionRoles && S.pipelineVisionRoles[r]) _roles.push(r);
      });
      if (_roles.length) {
        _flow += '<span style="color:var(--tx3)">Raw image to: ' + _roles.join(', ') + ' (multimodal only).</span>';
      } else {
        _flow += '<span style="color:var(--tx3)">No role gets the raw image &mdash; all agents receive only the text description.</span>';
      }
    } else {
      flowCard.style.borderColor = 'rgba(72,120,192,.45)';
      flowCard.style.background = 'rgba(72,120,192,.07)';
      if (_dVision) {
        _flow = '<span style="color:#80b0e0">&#9654; Direct:</span> ' + _dDisp
              + ' is multimodal &mdash; raw images go straight to it.<br>'
              + '<span style="color:var(--tx3)">No separate vision model is loaded.</span>';
      } else {
        _flow = '<span style="color:#d09090">&#9654; Direct:</span> ' + _dDisp
              + ' is not multimodal.<br>'
              + '<span style="color:var(--tx3)">Images may be ignored &mdash; switch to Preprocessor/Pipeline or pick a multimodal direct model.</span>';
      }
    }
    flowCard.innerHTML = _flow;
  }

  if (mode === 'preprocess' && S.visionModel) {
    const short = S.visionModel.replace(':latest','').split('/').pop();
    preview.textContent = '(' + short + ' · Prepro)';
    if (badge) { badge.style.display = 'inline-block'; badge.classList.add('on'); badge.textContent = 'PREPRO'; }
    if (inputEl) inputEl.style.borderColor = '#8858c0';
  } else if (mode === 'pipeline' && S.visionAgentModel) {
    const short = S.visionAgentModel.replace(':latest','').split('/').pop();
    preview.textContent = '(' + short + ' · Pipeline)';
    if (badge) { badge.style.display = 'inline-block'; badge.classList.add('on'); badge.textContent = 'PIPELINE'; }
    if (inputEl) inputEl.style.borderColor = '';
  } else {
    preview.textContent = '';
    if (badge) badge.style.display = 'none';
    if (inputEl) inputEl.style.borderColor = '';
  }
}

// -- Vision Config ---------------------------------------------
async function loadVisionConfig() {
  try {
    const d = await (await fetch('/vision/config')).json();
    S.visionEnabled = d.enabled || false;
    S.visionModel   = d.model   || '';
    const sel  = document.getElementById('vision-model-sel');
    const badge = document.getElementById('vision-active-badge');
    // IMAGE-PROCESSING-MODE: derive if not persisted yet (migration from the
    // previous two-accordion UI where the preprocessor was a separate toggle).
    if (!S.imageMode) {
      if (S.visionEnabled && S.visionModel) S.imageMode = 'preprocess';
      else if (S.visionAgentEnabled) S.imageMode = 'pipeline';
      else S.imageMode = 'direct';
    }
    _applyImageModeUI(S.imageMode);
    if (badge) badge.classList.toggle('on',
      (S.imageMode === 'preprocess' && !!S.visionModel) ||
      (S.imageMode === 'pipeline' && !!S.visionAgentModel));
    _updateVisionPreview();
    if (sel) {
      sel.innerHTML = '<option value="">-- No vision model --</option>';
      // optgroup: preprocessing-capable models on top, the rest below
      var grpPrepro = document.createElement('optgroup');
      grpPrepro.label = '✓ Preprocessing-capable (recommended)';
      var grpVision  = document.createElement('optgroup');
      grpVision.label = '👁 Vision (no preprocessing)';
      var grpOther   = document.createElement('optgroup');
      grpOther.label = 'Other models';
      S.models.forEach(function(m) {
        const o = document.createElement('option');
        o.value = m;
        const prof = S.modelProfiles[m] || {};
        const inAllowlist = S.visionAllowlist.has(m);
        // fallback detection when profiles are not yet loaded
        const looksVision = ['vl','llava','vision','moondream','minicpm','glm'].some(function(v){
          return m.toLowerCase().includes(v);
        });
        if (inAllowlist) {
          o.textContent = '✓ ' + m;
          o.style.color = '#4caf82';
          grpPrepro.appendChild(o);
        } else if (prof.vision || looksVision) {
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
      // warning for a stored model that cannot do preprocessing
      const warn = document.getElementById('vision-prepro-warn');
      if (warn) warn.style.display = (S.visionModel && !S.visionAllowlist.has(S.visionModel)) ? 'block' : 'none';
    }
  } catch(e) {}
}

function setVisionModel(model) {
  S.visionModel = model;
  const badge = document.getElementById('vision-active-badge');
  if (badge) badge.classList.toggle('on', S.visionEnabled && !!model);
  _updateVisionPreview();
  // warning when the chosen model cannot do preprocessing
  const warn = document.getElementById('vision-prepro-warn');
  if (warn) warn.style.display = (model && !S.visionAllowlist.has(model)) ? 'block' : 'none';
  fetch('/vision/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({model: model})
  });
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

// -- Chat History ----------------------------------------------
function _autosaveChatMessage() {
  // Collect current chat DOM as messages and store in S.currentChatMessages
  const msgs = [];
  document.querySelectorAll('#chat .msg').forEach(function(el) {
    if (el.classList.contains('msg-user')) {
      const bubble = el.querySelector('.bubble');
      if (bubble) msgs.push({role: 'user', content: bubble.textContent, ts: Date.now()});
    } else if (el.classList.contains('ablock')) {
      const aname = el.querySelector('.aname');
      // abody can be directly or in a duo-coder/duo-critic wrapper
      const abody = el.querySelector('.abody') || el.querySelector('.duo-coder .abody') || el.querySelector('.duo-critic .abody');
      if (aname && abody && abody.textContent.trim()) {
        // CHAT-STRUCTURE-FIX (2026-08-07): also save the rendered structure (markdown blocks,
        // code snippets, tool chips), otherwise the live structure is lost
        // and the chat is shown as flat text on load.
        msgs.push({role: 'assistant', agent: aname.textContent, content: abody.textContent,
                   html: abody.innerHTML, ts: Date.now()});
      }
    }
  });
  S.currentChatMessages = msgs;
}

function chatAutosaveToggle(el) {
  S.chatAutosave = !!el.checked;
  postSettings({chat_autosave_enabled: S.chatAutosave});
  showStatus(S.chatAutosave ? 'Auto-save enabled.' : 'Auto-save disabled.');
}

async function persistCurrentChat(silent) {
  _autosaveChatMessage();
  if (!S.currentChatMessages.length) return false;
  // Generate title from first user message (only on creation; existing titles stay)
  const firstUser = S.currentChatMessages.find(function(m){return m.role==='user';});
  const title = firstUser ? firstUser.content.slice(0, 48) : 'Chat ' + new Date().toLocaleTimeString();
  try {
    if (S.currentChatId) {
      // Update existing — do not overwrite the title (preserve user-named titles)
      await fetch('/chats/' + S.currentChatId, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({messages: S.currentChatMessages})
      });
    } else {
      // Create new
      const r = await (await fetch('/chats', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, messages: S.currentChatMessages})
      })).json();
      if (r && r.id) S.currentChatId = r.id;
    }
  } catch(e) {
    if (!silent) showStatus('Saving chat failed: ' + e.message);
    return false;
  }
  if (!silent) {
    loadChatHistory();
    showStatus('Chat saved.');
  }
  return true;
}

async function saveCurrentChat() {
  await persistCurrentChat(false);
}

function _fmtTokCount(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(Math.round(n));
  if (n < 1000000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return (n / 1000000).toFixed(1) + 'M';
}

async function loadChatHistory() {
  try {
    const d  = await (await fetch('/chats')).json();
    const c  = document.getElementById('chat-history-list');
    if (!c) return;
    const chats = d.chats || [];
    if (!chats.length) {
      c.innerHTML = '<div class="empty">No saved chats yet.</div>';
      return;
    }
    c.innerHTML = '';
    chats.forEach(function(ch) {
      const item = document.createElement('div');
      item.className = 'chat-hist-item' + (ch.id === S.currentChatId ? ' active' : '');
      item.title = 'Click to load';

      const dot  = document.createElement('div'); dot.className = 'chat-hist-dot';
      const info = document.createElement('div'); info.className = 'chat-hist-info';
      const title = document.createElement('div'); title.className = 'chat-hist-title'; title.textContent = ch.title;
      const prev  = document.createElement('div'); prev.className = 'chat-hist-preview'; prev.textContent = ch.preview;
      const meta  = document.createElement('div'); meta.className = 'chat-hist-meta';
      var _msgN = ch.msg_count;
      var _tokN = ch.tokens || 0;
      var _metaParts = [_msgN + ' message' + (_msgN === 1 ? '' : 's')];
      if (_tokN > 0) _metaParts.push('~' + _fmtTokCount(_tokN) + ' tok');
      _metaParts.push((ch.updated_at || '').slice(0,16));
      meta.textContent = _metaParts.join(' \u00B7 ');
      if (ch.interrupted) {
        const bad = document.createElement('span');
        bad.style.cssText = 'color:#e0a030;font-size:10px;font-weight:600;margin-left:6px';
        bad.textContent = '\u26A0 interrupted';
        meta.appendChild(bad);
      }
      const del  = document.createElement('button'); del.className = 'chat-hist-del'; del.textContent = '\u00D7';
      del.title = 'Delete chat';

      info.appendChild(title); info.appendChild(prev); info.appendChild(meta);
      item.appendChild(dot); item.appendChild(info); item.appendChild(del);

      (function(chatId) {
        item.addEventListener('click', function(e) {
          if (e.target === del) return;
          loadChat(chatId);
        });
        del.addEventListener('click', function(e) {
          e.stopPropagation();
          deleteChat(chatId);
        });
      })(ch.id);

      c.appendChild(item);
    });
  } catch(e) {}
}

async function loadChat(chatId) {
  if (S.streaming) { showStatus('Please wait until the current stream has finished.'); return; }
  try {
    const chat = await (await fetch('/chats/' + chatId)).json();
    // Clear session and reload
    await fetch('/memory/clear_session', {method:'POST'});
    document.getElementById('chat').innerHTML = '';

    S.currentChatId = chatId;
    S.currentChatMessages = chat.messages || [];
    setPauseBtnState('idle');
    setStopBtnState('idle');
    stopAskUserCountdown();

    // UI-WORKSPACE (2026-08-07): Feld zeigt den im Chat persistierten Workspace
    // (sonst das globale Setting) — Anzeige nur, das Setting bleibt unveraendert.
    var _wsField = document.getElementById('workspace-input');
    if (_wsField) _wsField.value = (chat.workspace || S.workspace || '');

    // Restore chat DOM from saved messages
    (chat.messages || []).forEach(function(msg) {
      if (msg.role === 'user') {
        addUserMsg(msg.content, []);
      } else if (msg.role === 'assistant') {
        // CHAT-STRUCTUR-FIX (2026-08-07): gespeicherte Live-Struktur wiederherstellen
        const c = document.getElementById('chat');
        const wrap = document.createElement('div');
        wrap.className = 'msg ablock';
        const color = agentColor(msg.agent || 'assistant');
        const hdr = document.createElement('div');
        hdr.className = 'ahdr';
        hdr.innerHTML =
          '<div class="dot" style="background:' + color + '"></div>' +
          '<span class="aname" style="color:' + color + '">' + esc(msg.agent || 'Assistant') + '</span>';
        const abody = document.createElement('div');
        abody.className = 'abody';
        if (msg.html && msg.html.trim()) {
          abody.innerHTML = _sanitizeChatHtml(msg.html);
        } else {
          abody.style.whiteSpace = 'pre-wrap';
          abody.textContent = msg.content || '';
        }
        wrap.appendChild(hdr);
        wrap.appendChild(abody);
        c.appendChild(wrap);
      }
    });
    // INTERRUPTED-BANNER (2026-08-21): last run parked via browser close
    // (stop_reason="disconnect") -> hint that a follow-up message is needed.
    if (chat.last_run && chat.last_run.stop_reason === 'disconnect') {
      const _bn = document.createElement('div');
      _bn.className = 'msg divider';
      _bn.style.cssText = 'color:#e09030;border-color:rgba(224,144,48,.35);font-size:10px';
      _bn.textContent = '\u26A0 Previous run was interrupted (browser closed) \u2014 send a message to continue.';
      document.getElementById('chat').appendChild(_bn);
    }
    scrollBtm();
    document.getElementById('h-preset-label').textContent = chat.title.slice(0,18);
    showInfo('Chat loaded: ' + chat.title.slice(0,40));
    loadChatHistory();  // refresh active state
    try {
      var _psResp = await fetch('/pause-state/' + chatId);
      var _psData = await _psResp.json();
      if (_psData.active && _psData.run_id) {
        S.currentRunId = _psData.run_id;
        S.streaming = true;
        setPauseBtnState('paused_manual');
        showInfo('\u23F8 Run is paused \u2014 click Resume to continue (' + _psData.chunks_done + ' chunks done)');
      }
    } catch(e2) {}
  } catch(e) {
    showStatus('Error loading chat: ' + e.message);
  }
}

async function deleteChat(chatId) {
  if (!confirm('Delete chat?')) return;
  await fetch('/chats/' + chatId, {method: 'DELETE'});
  if (S.currentChatId === chatId) S.currentChatId = null;
  loadChatHistory();
}

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
  if (S.soulEvolveEnabled && S.soulEvolveModel) {
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
        + S.models.map(m => `<option value="${m}"${m===S.intentModel?' selected':''}>${m}</option>`).join('');
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
        const rec = ['gemma-4:e4b-it-obliterated','google-gemma-3:4b-it','qwen3.5:9b-ud'].find(m => S.models.includes(m));
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
    if (sdtog) sdtog.checked = s.soul_skill_distillation !== false;
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
  S.explorationAgentEnabled = enabled;
  _updateSpecialAgentChips();
  const workers = S.explorationWorkers || [];
  fetch('/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({exploration_agent: {enabled, workers}})
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
  _updateSpecialAgentChips();
  fetch('/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({soul_evolve_agent: {enabled, model: S.soulEvolveModel||'',
      temperature: parseFloat(document.getElementById('soul-evolve-temp-sl')?.value||0.65),
      max_tokens: parseInt(document.getElementById('soul-evolve-tok-sl')?.value||800)}})
  });
}

// -- Intent Agent --------------------------------------------------
// Pre-pipeline agent that routes natural language intents to
// memory / soul-evolve / tool calls before the main pipeline runs.

const INTENT_MEMORY_KWS = [
  'remember that', 'forget', 'store this', 'save that', 'keep in mind',
  'merke dir', 'speichere', 'vergiss', 'erinnere dich',
];
const INTENT_EVOLVE_KWS = [
  'soul evolution', 'soul evolve', 'evolve yourself', 'learn from this',
  'evolviere dich', 'entwickle dich', 'lerne daraus',
  'lerne aus unserem', 'passe dich an', 'verbesser dich', 'update deine soul',
  'lerne aus dieser unterhaltung', 'lerne aus unserem gespraech',
];
// Tool keywords kept minimal — full detection lives in server
const INTENT_TOOL_KWS   = ['search for', 'search the web', 'google it', 'calculate', 'compute', 'suche im web', 'google das', 'berechne', 'rechne aus'];

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
  fetch('/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({intent_agent: {
      enabled,
      model: S.intentModel || '',
      temperature: parseFloat(document.getElementById('intent-temp-sl')?.value || 0.1)
    }})
  });
}

function setIntentModel(model) {
  S.intentModel = model;
  _updateIntentCard();
  _updateSpecialAgentChips();
  fetch('/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({intent_agent: {enabled: S.intentEnabled, model}})
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

// -- Start ------------------------------------------------------
init();

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
  fetch('/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({smart_preload_keep_alive: ka, default_keep_alive: ka})}).catch(function(){});
}

function setDefaultKeepalive(ka, el) {
  setKeepAlive(ka, el);
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

async function refreshVram() {
  if (_vramPending) return;  // PERF: no stacking while a fetch is running
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
          return '<div title="'+short+': ~'+m.vram_gb.toFixed(1)+' GB (inkl. KV-Cache)" style="'
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
        var mEsc   = m.name.replace(/'/g,"\\'");
        var approx = m.from_lookup ? '~' : '';
        var safeTitle = esc(m.name).replace(/"/g,'&quot;');

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
          +'<button class="vram-model-unload" onclick="unloadModel(\''+mEsc+'\')" title="Unload">&#215;</button>'
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
          +'<span style="color:#7a8fa8">'+soloModel+' ('+models[0].vram_gb.toFixed(1)+'GB) + '
          +'Judge ('+data.judge_gb+'GB) = '+(models[0].vram_gb+data.judge_gb).toFixed(1)+'GB &gt; '
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
          +'<span style="color:#7a8fa8;flex:1">'+models.map(function(m){return m.name.replace(':latest','');}).join(' + ')+'</span>'
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
    if (listEl) listEl.innerHTML = rows;

    // force kill button: show when the VRAM bar is >10% but no models are listed
    // (= suspected orphan process — e.g. after a server restart)
    var fkBtn = document.getElementById('force-kill-btn');
    if (fkBtn) {
      var hasOrphan = (data.pct > 10 && !models.length);
      fkBtn.style.display = hasOrphan ? '' : 'none';
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
    if (_vramOpen) {
      var _ms = _vramFastMode ? Math.min(_vramRefreshInterval, 2000) : _vramRefreshInterval;
      setTimeout(refreshVram, _ms);
    }
  }
}
async function _vramEstimate(model, ctx, elId) {
  if (!model || !ctx) return;
  var el = document.getElementById(elId);
  if (!el) return;
  el.textContent = '';
  try {
    var r = await fetch('/vram/estimate?model=' + encodeURIComponent(model) + '&ctx=' + ctx);
    if (!r.ok) return;
    var d = await r.json();
    if (d.error) return;
    var txt = '\u2248 ' + d.vram_gb.toFixed(1) + ' GB (KV: ' + d.vram_kv_gb.toFixed(2) + ' GB)';
    if (d.ram_gb > 0) txt += ' + ' + d.ram_gb.toFixed(1) + ' GB RAM';
    el.textContent = txt;
  } catch(e) {}
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
      // no models detected but VRAM full → offer force kill
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
    showStatus('Force Kill fehlgeschlagen: ' + e.message);
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

function preloadPipeline() { preloadPipelineModels(); }

async function manualLoadModel() {
  var sel = document.getElementById('mm-load-sel') || document.getElementById('manual-load-select');
  var ka  = document.getElementById('mm-keepalive-sel') || document.getElementById('manual-keep-alive');
  if (!sel || !sel.value) return;
  try {
    await fetch('/vram/load', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({model: sel.value, keep_alive: ka ? ka.value : '-1'})});
    if (!_vramPending && _vramOpen) refreshVram();
  } catch(e) {}
}


async function refreshModelManager() { refreshVram(); refreshAvailableModels(); }

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

function _renderAvailableModels(models, filter) {
  var list = document.getElementById('available-models-list');
  if (!list) return;
  var f = (filter || '').toLowerCase().trim();
  var filtered = f ? models.filter(function(m){return m.name.toLowerCase().includes(f);}) : models;
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
    var mEsc = m.name.replace(/'/g, "\\'");
    var safeTitle = esc(m.name).replace(/"/g,'&quot;');
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
      + '<button class="' + btnClass + '" title="' + btnTitle + '" onclick="toggleAvailModel(\'' + mEsc + '\', this)">' + btnLabel + '</button>'
      + '</div>'
      + '</div>';
  }).join('');
  list.innerHTML = rows;
}

function filterAvailableModels(val) {
  _renderAvailableModels(_availModels, val);
}

async function toggleAvailModel(name, btn) {
  var isLoaded = btn.classList.contains('loaded');
  btn.classList.add('loading');
  btn.textContent = isLoaded ? 'Unloading...' : 'Loading...';
  btn.onclick = null;
  try {
    if (isLoaded) {
      var r = await fetch('/vram/unload', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model: name})});
      if (!r.ok) throw new Error('Unload failed: ' + r.status);
      showStatus(name.split(':')[0] + ' unloaded');
      await Promise.all([refreshVram(), refreshAvailableModels()]);
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
    }
  } catch(e) {
    btn.classList.remove('loading');
    btn.classList.toggle('loaded', isLoaded);
    btn.innerHTML = isLoaded ? '&#215; Unload' : '&#9654; Load';
    btn.onclick = function(){toggleAvailModel(name,btn);};
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

// ── Live Code Panel ──────────────────────────────────────────────────────────
var _cpFiles = {};          // path → {content, op, tab, pre}
var _cpActive = null;       // currently shown path

function toggleCodePanel(forceOpen) {
  var open = forceOpen !== undefined ? forceOpen : !document.body.classList.contains('code-panel-open');
  document.body.classList.toggle('code-panel-open', open);
  var btn = document.getElementById('h-code-btn');
  if (btn) btn.classList.toggle('active', open);
}

function toggleSidebar() {
  var collapsed = document.body.classList.toggle('sidebar-collapsed');
  var btn = document.getElementById('h-sidebar-btn');
  if (btn) btn.classList.toggle('active', !collapsed);
}

function _cpSyntaxHighlight(code) {
  // Minimal syntax coloring — no external lib needed
  var s = code
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Strings
  s = s.replace(/(&#39;&#39;&#39;|""")([\s\S]*?)\1/g,'<span style="color:#7ea">$1$2$1</span>');
  s = s.replace(/(["'`])((?:\\.|(?!\1)[^\\])*)\1/g,'<span style="color:#9a8">$1$2$1</span>');
  // Comments
  s = s.replace(/(#[^\n]*)/g,'<span style="color:#5a7a5a">$1</span>');
  s = s.replace(/(\/\/[^\n]*)/g,'<span style="color:#5a7a5a">$1</span>');
  // Keywords
  s = s.replace(/\b(def|class|return|import|from|if|else|elif|for|while|try|except|finally|with|as|pass|break|continue|yield|async|await|const|let|var|function|export|default|new|this|null|true|false|None|True|False|and|or|not|in|is)\b/g,
    '<span style="color:#7ab">$1</span>');
  // Numbers
  s = s.replace(/\b(\d+\.?\d*)\b/g,'<span style="color:#c8a">$1</span>');
  // Function calls
  s = s.replace(/\b([a-zA-Z_]\w*)\s*(?=\()/g,'<span style="color:#acb">$1</span>');
  return s;
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

function _cpShowFile(path) {
  var entry = _cpFiles[path];
  if (!entry) return;
  _cpActive = path;
  // Update tab highlights
  Object.keys(_cpFiles).forEach(function(p) {
    var t = _cpFiles[p].tab;
    if (t) t.classList.toggle('active', p === path);
  });
  // Render content
  var body = document.getElementById('code-panel-body');
  if (!body) return;
  var lines = entry.content.split('\n');
  var lineNums = lines.map(function(l, i) {
    return '<span style="color:var(--tx2);user-select:none;margin-right:14px;display:inline-block;min-width:28px;text-align:right;opacity:.45">'+(i+1)+'</span>'+_cpSyntaxHighlight(l);
  }).join('\n');
  body.innerHTML = '<pre>' + lineNums + '</pre>';
}

function _cpAddOrUpdateFile(path, content, op) {
  var hdr = document.getElementById('code-panel-hdr');
  if (!hdr) return;
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
    tab.innerHTML = '<span class="cp-op '+opLabel+'">'+opLabel.toUpperCase()+'</span>'+esc(shortName);
    tab.title = path;
    tab.onclick = (function(p){return function(){_cpShowFile(p)}})(path);
    // Insert before close button
    var closeBtn = document.getElementById('code-panel-close');
    hdr.insertBefore(tab, closeBtn);
    _cpFiles[path] = {content: content, op: opLabel, tab: tab};
  } else {
    _cpFiles[path].content = content;
    _cpFiles[path].op = opLabel;
    // Update op badge
    var opBadge = _cpFiles[path].tab.querySelector('.cp-op');
    if (opBadge) { opBadge.className='cp-op '+opLabel; opBadge.textContent=opLabel.toUpperCase(); }
  }
  // Auto-show this file (panel contents update invisibly along)
  _cpShowFile(path);
  // 2026-08-25: the panel no longer opens by itself — manually via
  // the "⌨ Code" button or a click on a write/edit tool chip.
  var btn = document.getElementById('h-code-btn');
  if (btn) btn.style.display = '';
}

// Reset code panel on new session / new chat
function _cpReset() {
  _cpFiles = {};
  _cpActive = null;
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

// -- Git Integration ------------------------------------------------
function updateGitIntegrationUI() {
  var branchOpts = document.getElementById('git-branch-options');
  var autocommitToggle = document.getElementById('duo-git-autocommit-toggle');
  if (branchOpts) {
    branchOpts.style.display = (S.duoGitAutocommit || (autocommitToggle && autocommitToggle.checked)) ? 'block' : 'none';
  }
  updateGitAuthStatus();
}

function updateGitAuthStatus() {
  var el = document.getElementById('git-auth-status');
  if (!el) return;
  if (S.gitRepoUrl || S.gitUsername) {
    el.innerHTML = '&#x2713; Git konfiguriert';
    el.style.color = 'var(--green)';
  } else {
    el.innerHTML = '&#x26A0; No git configured';
    el.style.color = 'var(--amber)';
  }
}

// SUBAGENT-LITE (2026-08-24): Body ausblenden wenn deaktiviert —
// das Tool ist dann auch serverseitig unsichtbar (definitions-Filter).
function syncSubagentUI() {
  var tgl = document.getElementById('subagent-lite-toggle');
  var body = document.getElementById('subagent-lite-body');
  if (tgl && body) body.style.display = tgl.checked ? '' : 'none';
}

function syncGitToggles() {
  var cfgToggle = document.getElementById('git-autocommit-cfg-toggle');
  var duoToggle = document.getElementById('duo-git-autocommit-toggle');
  if (cfgToggle && duoToggle) {
    duoToggle.checked = cfgToggle.checked;
  }
  updateGitIntegrationUI();
}

async function testGitConfig() {
  var btn = document.getElementById('git-test-btn');
  var result = document.getElementById('git-test-result');
  if (btn) btn.textContent = 'Teste...';
  try {
    var res = await fetch('/git/test', {method:'POST'});
    var data = await res.json();
    if (data.ok) {
      if (result) { result.textContent = '\u2713 ' + (data.message || 'Connection OK'); result.style.color = 'var(--green)'; }
    } else {
      if (result) { result.textContent = '\u2717 ' + (data.error || 'Error'); result.style.color = 'var(--red)'; }
    }
  } catch(e) {
    if (result) { result.textContent = '\u2717 Server error'; result.style.color = 'var(--red)'; }
  }
  if (btn) btn.textContent = 'Test connection';
}

async function initGitRepo() {
  var btn = document.getElementById('git-init-btn');
  var statusEl = document.getElementById('git-init-status');
  if (btn) btn.textContent = 'Erstelle...';
  try {
    var res = await fetch('/git/init', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        clone_url: S.gitRepoUrl || '',
        username: S.gitUsername || '',
        email: '',
        default_branch: S.gitBranch || 'main'
      })
    });
    var data = await res.json();
    if (data.ok) {
      if (data.already_existed) {
        if (statusEl) { statusEl.textContent = '\u2713 Repo existiert bereits (Branch: ' + (data.branch || '?') + ')'; statusEl.style.color = 'var(--green)'; }
      } else if (data.cloned) {
        if (statusEl) { statusEl.textContent = '\u2713 Repo geklont: ' + data.repo; statusEl.style.color = 'var(--green)'; }
      } else if (data.initialized) {
        if (statusEl) { statusEl.textContent = '\u2713 New repo created (branch: ' + (data.branch || 'main') + ')'; statusEl.style.color = 'var(--green)'; }
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
      body: JSON.stringify({action: action})
    });
    var data = await res.json();
    if (statusEl) { statusEl.textContent = data.message || 'OK'; statusEl.style.color = data.ok ? 'var(--green)' : 'var(--red)'; }
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
      body: JSON.stringify({target: 'HEAD', hard: hard})
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
  S.gitToken = s.git_token || '';
  S.gitBranch = s.git_branch || 'main';
  // Sync toggles
  var duoToggle = document.getElementById('duo-git-autocommit-toggle');
  if (duoToggle) duoToggle.checked = S.duoGitAutocommit;
  var cfgToggle = document.getElementById('git-autocommit-cfg-toggle');
  if (cfgToggle) cfgToggle.checked = S.duoGitAutocommit;
  // Fill inputs
  var repoInp = document.getElementById('git-repo-url');
  if (repoInp) repoInp.value = S.gitRepoUrl;
  var userInp = document.getElementById('git-username');
  if (userInp) userInp.value = S.gitUsername;
  var tokInp = document.getElementById('git-token');
  if (tokInp) tokInp.value = S.gitToken;
  var brInp = document.getElementById('git-branch-inp');
  if (brInp) brInp.value = S.gitBranch;
  // v0.96.5: Additional git config fields
  var autoPushTog = document.getElementById('git-auto-push-toggle');
  if (autoPushTog) autoPushTog.checked = s.git_auto_push || false;
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
  var brSel = document.getElementById('git-branch-sel');
  if (brSel) { brSel.value = S.gitBranch || 'main'; }
  updateGitIntegrationUI();
  // load the branch list from the server (for the dropdown in the duo panel)
  try {
    var brRes = await (await fetch('/git/branches')).json();
    if (brRes.branches && brRes.branches.length && brSel) {
      brSel.innerHTML = '';
      brRes.branches.forEach(function(b) {
        var opt = document.createElement('option');
        opt.value = b; opt.textContent = b;
        if (b === S.gitBranch) opt.selected = true;
        brSel.appendChild(opt);
      });
      // custom option in case the branch does not exist yet
      if (!brRes.branches.includes(S.gitBranch)) {
        var customOpt = document.createElement('option');
        customOpt.value = S.gitBranch; customOpt.textContent = S.gitBranch + ' (new)';
        customOpt.selected = true;
        brSel.appendChild(customOpt);
      }
    }
  } catch(e) {}
}

