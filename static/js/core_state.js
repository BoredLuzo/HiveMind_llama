// core_state.js — global error net, safe storage, shared state S, agent metadata, chat utilities & DOM sanitizer

// ── GLOBAL ERROR NET (2026-09-15) ────────────────────────────────────────────
// Runtime errors used to land only in the devtools console ("F12 errors are
// basically 0 catched"). Everything uncaught now surfaces as a throttled,
// dismissible toast; console.error stays for debugging.
(function _installGlobalErrorNet() {
  if (typeof window === 'undefined') return;
  var _toastEl = null, _lastAt = 0, _suppressed = 0, _hideTimer = null;
  var _BENIGN = [
    'ResizeObserver loop', 'ResizeObserver loop completed with undelivered',
    'Script error.', 'Load failed', 'The network connection was lost'
  ];
  function _benign(msg) {
    if (!msg) return true;
    msg = String(msg);
    for (var i = 0; i < _BENIGN.length; i++) if (msg.indexOf(_BENIGN[i]) !== -1) return true;
    return false;
  }
  window._showErrorToast = function (msg) {
    try {
      if (_benign(msg)) return;
      var now = Date.now();
      if (now - _lastAt < 8000) {
        _suppressed++;
        if (_toastEl) {
          var cnt = _toastEl.querySelector('.f12-count');
          if (cnt) cnt.textContent = '+' + _suppressed + ' more — see console (F12)';
        }
        return;
      }
      _lastAt = now; _suppressed = 0;
      if (!_toastEl) {
        _toastEl = document.createElement('div');
        _toastEl.style.cssText = 'position:fixed;right:14px;bottom:14px;z-index:99999;' +
          'max-width:420px;background:rgba(60,20,24,.96);color:#ffd7d7;border:1px solid #a33;' +
          'border-radius:6px;padding:10px 12px;font:11px/1.5 IBM Plex Mono,monospace;' +
          'box-shadow:0 4px 18px rgba(0,0,0,.5);cursor:pointer;display:none;';
        _toastEl.title = 'click to dismiss';
        _toastEl.onclick = function () { _toastEl.style.display = 'none'; };
        document.body.appendChild(_toastEl);
      }
      _toastEl.innerHTML = '<b style="color:#ff9c9c">⚠ Frontend error</b> <span class="f12-count" style="opacity:.75"></span><br>';
      _toastEl.appendChild(document.createTextNode(String(msg).slice(0, 300)));
      _toastEl.style.display = 'block';
      if (_hideTimer) clearTimeout(_hideTimer);
      _hideTimer = setTimeout(function () { if (_toastEl) _toastEl.style.display = 'none'; }, 12000);
    } catch (e) { /* the net itself must never throw */ }
  };
  window.addEventListener('error', function (ev) {
    var m = ev && (ev.message || (ev.error && ev.error.message)) || 'unknown script error';
    console.error('[F12-net]', ev && ev.error ? ev.error : m);
    window._showErrorToast(m);
  });
  window.addEventListener('unhandledrejection', function (ev) {
    var r = ev && ev.reason;
    var m = (r && (r.message || String(r))) || 'unhandled promise rejection';
    console.error('[F12-net] unhandled rejection:', r);
    window._showErrorToast(m);
  });
})();

// STORAGE-GUARD (2026-11-08): Firefox with blocked cookies/storage throws
// SecurityError on localStorage access ("The operation is insecure"). That
// surfaced as a Frontend-error toast on every load, because the panel-width
// init read localStorage unguarded. All UI persistence goes through here.
function _hmStorageGet(key, fallback) {
  try {
    var v = window.localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch (e) { return fallback; }
}
function _hmStorageSet(key, value) {
  try { window.localStorage.setItem(key, String(value)); } catch (e) {}
}

// -- State ------------------------------------------------------
let S = {
  models: [],
  mode: 'auto',
  iters: 2,
  activePreset: null,
  pendingImgs: [],
  followUpQueue: [],
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
  imagePersist: false,
   // Abort
   currentRunId: null,
   // Pause / Ask-User
   agentPaused: false,
   agentQuestion: null,
    _pauseBtnState: 'idle',
    _stopBtnState: 'idle',
    // TRUE once a duo/agentic run streamed a duo_start event — only then a
    // graceful "stop after chunk" makes sense. Direct/chat runs have no chunks.
    _runIsDuo: false,
    // Code
  duoPair: 'focused',
  duoToolRounds: 0,
  duoCoderToolTier: 'core',
  subagentLite: false,
  duoUsePipeline: false,
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
  duoCriticModel: '',
  duoAgenticThinking: false,  // true=no critic interrupt, coder runs autonomously

  duoCoderToolThinking: false,
  duoCoderToolThinkingAutoMode: 'on_fail',
  reasoningEffort: 'auto',
  plannerReasoningEffort: 'auto',
  duoUntilFinished: false,    // true=coder runs until tests pass (budget 300)
  _askUserCountdownInterval: null,
  _runAbortCtrl: null,
  askUserTimeoutSeconds: 300,
  askUserMaxPer10min: 5,
  askUserAutoAnswer: 'Use best judgment, document decision in commit message.',
  perfRunStartedAt: 0,
  perfFirstTokenAt: 0,
  perfActiveMs: 0,          // FIX 2026-10-07: active decode time only
  perfLastTokAt: 0,
  perfDecodeEma: 0,
  perfToolCalls: 0, perfToolMs: 0,
  perfToolLabel: '', perfToolStart: 0, perfLastToolTxt: '--',
  perfChars: 0,
  perfEstTokens: 0,
  perfRealTokens: 0,
  perfInFlightChars: 0,
  perfInFlightTokens: 0,
  perfCtxTokens: 0,
  perfCtxLimit: 0,
  perfCtxPct: 0,
  perfTokRate: 0,
  perfTokRateEma: 0,
  perfCompressing: false,
  duoCtxAgentic: 16384,
  duoCtxNormal: 8192,
  duoCtxPlanner: 16384,
  duoCtxCritic: 8192,
  duoPlannerUseCoderCtx: true,
  duoPlannerModel: '',
  duoCoderModel: '',
  llamaMlock: true,
  llamaCacheReuse: 256,
  vramPreflightGate: true,
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
  duoActionApproval: false,
  askUserAutoProceed: false,
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


function _cleanupLoadTimers() {
  if (S.curAgent && S.curAgent.lt) {
    clearTimeout(S.curAgent.lt); S.curAgent.lt = null;
  }
  document.querySelectorAll('[id^="lh-"]').forEach(function(el) {
    if (el._loadTimer) { clearInterval(el._loadTimer); el._loadTimer = null; }
  });
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
      _wireImgZoom(im);
      thumbs.appendChild(im);
    });
    d.appendChild(thumbs);
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  d.appendChild(bubble);
  c.appendChild(d);
  _chatFollow = true;
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


function rmEl(id) {
  const e = document.getElementById(id);
  if (e) e.parentNode.removeChild(e);
}

function esc(s) { return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// CHAT-STRUCTURE-FIX (2026-08-07) + DOMParser allowlist (C6): clean saved
// chat HTML before it is inserted into the DOM on load.
var _CHAT_DROP_TAGS = new Set([
  'script', 'iframe', 'object', 'embed', 'style', 'link', 'meta', 'base',
  'form', 'svg', 'math', 'set', 'template', 'noscript', 'noembed', 'xmp', 'textarea'
]);
var _CHAT_ALLOW_TAGS = new Set([
  'div', 'span', 'p', 'br', 'hr', 'pre', 'code', 'details', 'summary',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'ul', 'ol', 'li',
  'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'em', 'i',
  'u', 's', 'del', 'sub', 'sup', 'a', 'img', 'button'
]);
var _CHAT_SAFE_ATTRS = new Set([
  'class', 'title', 'open', 'colspan', 'rowspan', 'alt', 'width', 'height',
  'target', 'rel', 'type', 'value', 'start', 'disabled'
]);

function _isSafeSanitizedUrl(rawUrl, isImgSrc) {
  var s = String(rawUrl || '').trim();
  if (!s) return false;
  var compact = s.replace(/[\u0000-\u0020]+/g, '');
  var lower = compact.toLowerCase();
  if (lower.indexOf('javascript:') === 0 || lower.indexOf('vbscript:') === 0) return false;
  if (isImgSrc) {
    if (/^data:image\/(?:png|jpeg|jpg|gif|webp);base64,[a-z0-9+/=]+$/i.test(compact)) return true;
    if (lower.indexOf('data:') === 0) return false;
    if (lower.indexOf('http://') === 0 || lower.indexOf('https://') === 0) return true;
    if (s[0] === '/' && s[1] !== '/') return true;
    return false;
  }
  if (lower.indexOf('data:') === 0) return false;
  if (lower.indexOf('http://') === 0 || lower.indexOf('https://') === 0 || lower.indexOf('mailto:') === 0) return true;
  if ((s[0] === '/' && s[1] !== '/') || s[0] === '#') return true;
  return false;
}

function _sanitizeStyleAttr(rawStyle) {
  var cleaned = String(rawStyle || '').replace(/\/\*[\s\S]*?\*\//g, '');
  var kept = [];
  cleaned.split(';').forEach(function(part) {
    var decl = part.trim();
    if (!decl) return;
    var norm = decl.replace(/[\u0000-\u0020]+/g, '').toLowerCase();
    if (/url\(|expression\(|@import|behavior:|-moz-binding|\\/.test(norm)) return;
    kept.push(decl);
  });
  return kept.join('; ');
}

function _sanitizeChatNode(node, outDoc) {
  if (!node) return null;
  if (node.nodeType === 3) {
    return outDoc.createTextNode(node.nodeValue || '');
  }
  if (node.nodeType !== 1) return null;
  var tag = String(node.tagName || '').toLowerCase();
  if (_CHAT_DROP_TAGS.has(tag)) return null;
  if (!_CHAT_ALLOW_TAGS.has(tag)) {
    var frag = outDoc.createDocumentFragment();
    Array.prototype.forEach.call(node.childNodes || [], function(ch) {
      var cleanCh = _sanitizeChatNode(ch, outDoc);
      if (cleanCh) frag.appendChild(cleanCh);
    });
    return frag;
  }
  var el = outDoc.createElement(tag);
  var attrs = node.attributes || [];
  var forceRelNoopener = false;
  for (var i = 0; i < attrs.length; i++) {
    var attr = attrs[i];
    if (!attr) continue;
    var aname = String(attr.name || '').toLowerCase();
    var aval = String(attr.value || '');
    if (!aname || aname.indexOf('on') === 0 || aname === 'srcdoc' || aname === 'formaction' || aname === 'xlink:href') {
      continue;
    }
    if (aname === 'href') {
      if (tag === 'a' && _isSafeSanitizedUrl(aval, false)) {
        el.setAttribute('href', aval.trim());
      }
      continue;
    }
    if (aname === 'src') {
      if (tag === 'img' && _isSafeSanitizedUrl(aval, true)) {
        el.setAttribute('src', aval.trim());
      }
      continue;
    }
    if (aname === 'style') {
      var safeCss = _sanitizeStyleAttr(aval);
      if (safeCss) el.setAttribute('style', safeCss);
      continue;
    }
    if (aname === 'target') {
      if (tag === 'a' && (aval === '_blank' || aval === '_self')) {
        el.setAttribute('target', aval);
        if (aval === '_blank') forceRelNoopener = true;
      }
      continue;
    }
    if (_CHAT_SAFE_ATTRS.has(aname) || /^data-[a-z0-9_-]+$/.test(aname)) {
      el.setAttribute(aname, aval);
    }
  }
  if (forceRelNoopener) el.setAttribute('rel', 'noopener noreferrer');
  Array.prototype.forEach.call(node.childNodes || [], function(ch) {
    var cleanCh = _sanitizeChatNode(ch, outDoc);
    if (cleanCh) el.appendChild(cleanCh);
  });
  return el;
}

function _sanitizeChatHtml(html) {
  var raw = String(html || '');
  if (!raw) return '';
  if (typeof DOMParser === 'undefined') return _escHtml(raw);
  try {
    var parsed = new DOMParser().parseFromString(raw, 'text/html');
    var root = document.createElement('div');
    if (parsed && parsed.body) {
      Array.prototype.forEach.call(parsed.body.childNodes || [], function(ch) {
        var cleanCh = _sanitizeChatNode(ch, document);
        if (cleanCh) root.appendChild(cleanCh);
      });
    }
    return root.innerHTML;
  } catch (e) {
    console.warn('[_sanitizeChatHtml] DOMParser fallback:', e);
    return _escHtml(raw);
  }
}

// END_CHAT_SANITIZER
