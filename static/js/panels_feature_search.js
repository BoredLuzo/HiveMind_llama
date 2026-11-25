// HiveMind UI module: Ctrl+K Feature Search palette.
// ── FEATURE SEARCH (2026-10-04, phase 1 in 1.3.0): search the UI itself,
// not chats. A command-palette style overlay (Ctrl+K / the Search button in
// the header) indexes every panel's sections, toggle rows and labeled
// controls; a click switches to the panel, scrolls to the element and
// flashes it. Static DOM index (built on first open) + agent cards.
(function() {
  var _fsOverlay = null, _fsIndex = null, _fsList = null, _fsInput = null, _fsSel = -1;

  var PANEL_LABELS = { agents: 'AGENTS', presets: 'PRESETS', configs: 'CONFIGS',
                       memory: 'MEMORY', soul: 'SOUL', models: 'MODELS',
                       chats: 'CHATS', automations: 'AUTOMATIONS' };

  function _selText(el) {
    var row = el.closest('.tgl-row, .cfl-row, label, .agent-card, .set-row, div');
    if (!row) return (el.title || '').trim();
    var t = row.querySelector('.tgl-text, .cfl-label, .sec, .agent-name, .set-label');
    if (t) return t.textContent.trim();
    var own = row.cloneNode(true);
    own.querySelectorAll('input,select,button,span.sl').forEach(function(e) { e.remove(); });
    return (own.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90);
  }

  function _buildIndex() {
    var idx = [];
    var seen = {};
    // DEAD-BLOCK EXCLUDE (2026-10-05): the sequential/parallel demo has no
    // backend reader - never surface it, visible or not.
    function _dead(el) {
      // precise: only elements INSIDE the hidden demo container are dead.
      // An ancestor walk with querySelector marked the whole AGENTS panel
      // as dead (the panel contains the demo button deep in its tree).
      var demo = document.getElementById('va-mode-seq');
      if (!demo || !demo.parentElement) return false;
      return demo.parentElement.contains(el);
    }
    // DUO CTX SLIDERS (2026-10-05, user: 'ctx settings schwer zu finden'):
    // the VRAM-block lever lives in wrappers hidden by submode - indexed
    // explicitly (once, panel=agents), visible or not.
    [['wrapper-ctx-agentic', 'Context Coder (Agentic)', 'duo coder agentic'],
     ['wrapper-ctx-planner', 'Context Planner', 'duo planner'],
     ['wrapper-planner-coder-toggle', 'Planner = Coder (shared context)', 'planner coder same shared ctx context window'],
     ['wrapper-ctx-normal', 'Context (Coder + Critic Dual)', 'duo dual'],
     ['wrapper-ctx-critic', 'Context Critic', 'duo critic']].forEach(function(d) {
      var w = document.getElementById(d[0]);
      if (!w) return;
      var lbl = w.querySelector('span');
      _push('agents', d[1], lbl || w, 'Duo ctx', null, d[2] + ' ctx context vram');
    });
    // VOICE (under Configs tab -> #voice-card) + ROUTING (under Agents tab).
    [['tts-enabled-toggle', 'Voice output (TTS)', 'voice tts text to speech output enabled'],
     ['tts-backend-sel', 'TTS voice model (Pocket TTS / Underdog Woof)', 'voice tts backend model pocket woof underdog numpy'],
     ['tts-voice-sel', 'TTS voice', 'voice tts voice model speaker anna cosette juno mira'],
     ['tts-read-direct-toggle', 'Read aloud: direct answers (TTS)', 'tts read aloud speak direct chat'],
     ['tts-read-agentic-toggle', 'Read aloud: agentic runs (TTS)', 'tts read aloud speak agentic plan'],
     ['tts-read-ask-toggle', 'Read aloud: agent questions (TTS)', 'tts read aloud speak ask question'],
     ['tts-read-gate-toggle', 'Read aloud: approval cards (TTS)', 'tts read aloud speak gate approval'],
     ['tts-speed-inp', 'TTS speed', 'tts speed voice rate'],
     ['tts-volume-inp', 'TTS volume', 'tts volume loudness voice'],
     ['stt-enabled-toggle', 'Voice input (STT)', 'voice stt speech to text input transcribe'],
     ['stt-model-sel', 'STT model', 'voice stt whisper parakeet model transcribe'],
     ['stt-language-sel', 'STT language', 'voice stt language transcribe'],
     ['stt-threads-sel', 'STT threads', 'voice stt threads cpu transcribe'],
     ['stt-preload-toggle', 'STT preload', 'voice stt preload warm transcribe'],
     ['stt-max-seconds', 'STT max seconds', 'voice stt length limit transcribe']
    ].forEach(function(d) {
      var el = document.getElementById(d[0]);
      if (!el) return;
      _push('configs', d[1], el, 'Voice', null, d[2]);
    });
    [['mode-btns-row', 'Mode: Auto / Chat / Pipeline / Code', 'mode auto chat pipeline code agentic critic_duo code_duo routing'],
     ['duo-planner-model-sel', 'Code: Planner model', 'duo planner model select code agentic critic'],
     ['duo-coder-model-sel', 'Code: Coder model', 'duo coder model select code agentic critic'],
     ['duo-critic-model-sel', 'Code: Critic model (Critic-Duo)', 'duo critic model select critic_duo review'],
     ['routing-card', 'Routing: who decides in auto', 'routing auto decider judge plan step gliner'],
     ['routing-decider-sel', 'Routing: decider', 'routing decider encoder gliner classifier on off shadow'],
     ['routing-judge-sel', 'Routing: judge', 'routing judge llm on off shadow'],
     ['routing-planner-sel', 'Routing: plan step', 'routing plan step planner always decider never'],
     ['routing-status', 'Routing: current choice', 'routing status current who decides'],
     ['vision-accordion-hdr', 'Image Processing (Direct / Describe first)', 'image vision preprocess direct multimodal mmproj steer duo_image_mode vision_preprocessing'],
     ['moe-model-select', 'MoE CPU experts & VRAM fit', 'moe cpu experts offload ram llama_fit_target llama_cache_reuse']
    ].forEach(function(d) {
      var el = document.getElementById(d[0]);
      if (!el) return;
      _push('agents', d[1], el, 'Voice / Routing', null, d[2]);
    });
    // VRAM PRE-FLIGHT GATE (2026-10-11): guaranteed findability - the
    // llama backend panel uses inline styles (no .tgl-row markup), so the
    // DOM scan would miss it.
    var _pfGate = document.getElementById('vram-preflight-gate-toggle');
    if (_pfGate) _push('agents', 'VRAM Pre-Flight Gate', _pfGate, 'Llama backend', null,
      'vram preflight gate refuse loads estimate ctx cascade backend');
    // KEY-SEARCH (2026-10-31, audit): these keys have no DOM field; search
    // must still find them by their setting names.
    _push('agents', 'Llama ubatch / batch size', null, 'Llama backend', null,
      'ubatch batch prefill chunk duo_ubatch llama_ubatch_size 256 512 1024');
    _push('agents', 'Llama cache / checkpoints', null, 'Llama backend', null,
      'cache reuse ram mib ctx checkpoints llama_cache_reuse llama_cache_ram_mib llama_ctx_checkpoints');
    _push('agents', 'Run timeout (duo)', null, 'Duo timeout', null,
      'duo_run_timeout_seconds duo_run_timeout_critical_seconds critical timeout wall budget 420 900');
    _push('agents', 'Compression tail cap', null, 'Compression', null,
      'duo_compress_tail_tool_max_chars tail tool output 8000 compression');
    _push('agents', 'Tools & Optionals', document.getElementById('tools-optionals-card'), 'Tools & Optionals', null,
      'tools optionals direct coder toolset websearch git');
    _push('agents', 'Coder Toolset (lean / core / full)', document.getElementById('duo-coder-tool-tier'), 'Tools & Optionals', null,
      'duo_coder_tool_tier coder toolset lean core full');
    _push('agents', 'Direct Chat Tools', document.getElementById('tools-direct-block'), 'Tools & Optionals', null,
      'direct_tools_enabled direct_tools_tier direct_tools_max_rounds off readonly research python mcp full');
    _push('agents', 'Websearch (SearXNG)', document.getElementById('duo-websearch-toggle'), 'Tools & Optionals', null,
      'duo_websearch_enabled duo_websearch_timeout_seconds searxng_host websearch check host');
    _push('agents', 'Git Tools', document.getElementById('git-tools-toggle'), 'Tools & Optionals', null,
      'git_tools_enabled git_status git_commit credentials');
    _push('agents', 'Research subagent', document.getElementById('subagent-lite-toggle'), 'Tools & Optionals', null,
      'subagent research delegate model ladder ctx subagent_lite_enabled');
    _push('agents', 'Sandbox tool spawns', document.getElementById('tool-sandbox-toggle'), 'Tools & Optionals', null,
      'sandbox tool spawns job objects duo_tool_sandbox run_bash run_python background');
    _push('configs', 'MCP client timeout', null, 'MCP', null,
      'mcp_client_timeout_s timeout seconds server call');
    _push('configs', 'Git default branch', null, 'Git', null,
      'git_default_branch branch main push');

    // NAMING ROUND (2026-10-05, user): generic row texts ("Active") get the
    // enclosing card/section title as prefix; duplicate labels are deduped;
    // dynamic agent cards (acard) are indexed WITH their setting fields so
    // "ctx" finds every agent's Context slider. Index is rebuilt on every
    // open - agent cards are rendered dynamically.
    function _push(panel, label, el, kind, act, kw) {
      label = String(label || '').replace(/\s+/g, ' ').trim().slice(0, 90);
      if (label.length < 3) return;
      var key = (panel || '') + '|' + label.toLowerCase();
      if (seen[key]) return;
      seen[key] = 1;
      idx.push({ panel: panel, label: label, el: el, kind: kind, act: act,
                 kw: ((kw || '') + ' ' + label + ' ' + kind).toLowerCase() });
    }
    function _cardTitle(el) {
      var card = el.closest('.cfg-card');
      if (!card) return '';
      var t = card.querySelector('.cfg-card-title');
      if (!t) return '';
      return (t.textContent || '').replace(/\s*(BETA|BOOT|EXP|AMBER)\s*$/, '').trim();
    }
    document.querySelectorAll('.panel').forEach(function(panel) {
      var pid = (panel.id || '').replace(/^p-/, '');
      if (!PANEL_LABELS[pid]) return;
      // MODE-INDEPENDENT INDEX (2026-10-05, user screenshots): results must
      // not depend on the active mode - hidden features are exactly the ones
      // the search exists to surface. Reveal-on-click clears the wrapper.
      panel.querySelectorAll('.sec').forEach(function(s) {
        if (_dead(s)) return;
        _push(pid, (s.textContent || '').trim(), s, 'Section');
      });
      panel.querySelectorAll('.cfg-card').forEach(function(card) {
        var titleEl = card.querySelector('.cfg-card-title');
        var title = titleEl ? titleEl.textContent.replace(/\s*(BETA|BOOT|EXP)\s*$/, '').trim() : '';
        if (title.length >= 3) _push(pid, title, titleEl, 'Card');
        card.querySelectorAll('.tgl-row .tgl-text').forEach(function(tx) {
          var txt = (tx.textContent || '').trim();
          if (txt.length >= 3) _push(pid, title ? title + ' — ' + txt : txt, tx, 'Toggle');
        });
      });
      panel.querySelectorAll('.tgl-row .tgl-text, .cfl-label').forEach(function(tx) {
        if (tx.closest('.cfg-card')) return; // handled above with title prefix
        if (_dead(tx)) return;
        var txt = (tx.textContent || '').trim();
        if (txt.length < 3) return;
        // CONTEXT PREFIX (2026-10-05, user screenshot): short labels like
        // "Critic" or "Synthesizer" are meaningless alone — find the nearest
        // preceding section header and prefix.
        var label = txt;
        if (txt.length < 15) {
          var _ctxSec = null;
          var _all = panel.querySelectorAll('.sec');
          for (var _si = 0; _si < _all.length; _si++) {
            if (_all[_si].compareDocumentPosition(tx) & Node.DOCUMENT_POSITION_FOLLOWING) {
              _ctxSec = _all[_si];
            } else break;
          }
          if (_ctxSec) label = _ctxSec.textContent.trim() + ' — ' + txt;
        }
        _push(pid, label, tx, 'Toggle');
      });
      panel.querySelectorAll('label input[type="checkbox"]').forEach(function(inp) {
        var row = inp.closest('label');
        if (!row || _dead(row)) return;
        var txt = (row.textContent || '').replace(/\s+/g, ' ').trim();
        if (txt && row.closest('#vision-accordion') && !/^image/i.test(txt)) {
          txt = 'Image Processing: ' + txt;
        }
        if (txt.length >= 3) _push(pid, txt.slice(0, 80), row, 'Checkbox');
      });
      // BARE CHECKBOX ROWS (2026-11-01): several controls are a checkbox plus a
      // plain <span> label without .tgl-row/.cfl-label/label markup - "Lock in
      // RAM (--load-mode mmap+mlock)" was unfindable, so "mlock" searched
      // nothing. Index the parent row's text for exactly those.
      panel.querySelectorAll('input[type="checkbox"]').forEach(function(inp) {
        if (inp.closest('label') || inp.closest('.tgl-row')) return; // handled above
        var row = inp.parentElement;
        if (!row || _dead(row)) return;
        // the live hint ("○ off") sits in the same row - keep it out of the label
        var txt = '';
        Array.prototype.forEach.call(row.childNodes, function(n) {
          if (n.nodeType === 3) { txt += n.textContent; return; }
          if (n.nodeType !== 1) return;
          if (n.tagName === 'INPUT') return;
          if (/(hint|est)$/i.test(n.id || '')) return;
          txt += n.textContent;
        });
        txt = txt.replace(/\s+/g, ' ').trim();
        if (txt.length >= 3 && txt.length <= 120) _push(pid, txt.slice(0, 90), row, 'Option');
      });
      // OPT-NAME ROWS (2026-10-31, audit): the Duo panel uses .opt-name
      // labels without checkbox markup; "Tool rounds", "Chunking",
      // "Until Finished" etc. were not searched at all.
      panel.querySelectorAll('.opt-name').forEach(function(opt) {
        var t = '';
        opt.childNodes.forEach(function(n) {
          if (n.nodeType === 3) { t += n.textContent; return; }
          if (n.nodeType === 1 && n.classList && n.classList.contains('reasoning-override-slot')) return;
          if (n.nodeType === 1) t += n.textContent;
        });
        t = t.replace(/\s+/g, ' ').trim();
        if (t.length >= 3 && !_dead(opt)) _push(pid, t, opt, 'Duo option');
      });
      // VRAM TTL / keep-alive row (2026-11-01): "VRAM TTL" is the label, the
      // setting is documented as keep_alive - neither "ttl" nor "keep alive"
      // found it before.
      panel.querySelectorAll('.keep-alive-lbl').forEach(function(lbl) {
        if (_dead(lbl)) return;
        _push(pid, 'VRAM TTL (keep alive)', lbl, 'Setting',
              null, 'vram ttl keep alive keepalive 10m 30m 1h unload idle');
      });
      // dynamic agent cards: ONE entry per agent with comprehensive
      // keywords. The old 5-entries-per-agent approach produced noise
      // ("Toggle: Critic", "Agent setting: modelvisionthink", stale
      // temperature values) — this collapses to 1 useful entry per agent
      // whose keywords make every field findable.
      panel.querySelectorAll('#agent-cards .acard').forEach(function(card) {
        var nameEl = card.querySelector('.acard-name');
        var nm = nameEl ? nameEl.textContent.trim() : '';
        if (!nm) return;
        // collect field labels for keywords (ctx, temperature, budget…)
        var fields = [];
        card.querySelectorAll('.fl').forEach(function(fl) {
          var t = (fl.textContent || '').trim().split('\n')[0]
                  .replace(/\b(VISION|THINK)\b/gi, '')
                  .replace(/\s+/g, ' ').trim();
          if (t.length >= 3) fields.push(t.toLowerCase().slice(0, 40));
        });
        var kw = 'agent ' + nm + ' ' + fields.join(' ')
               + ' ctx context temperature temp output-budget budget tokens tok model thinking vision';
        _push(pid, nm + ' (model, ctx, temp, budget)', card, 'Agent card', null, kw);
      });
      // SOUL SUB-TABS (2026-10-05, user): the "Tokens" sub-tab in the Soul
      // panel shows Token Statistics — search should find it by name.
      panel.querySelectorAll('#soul-sub-tabs button').forEach(function(btn) {
        var lbl = (btn.textContent || '').trim();
        if (lbl.length < 2) return;
        _push(pid, 'Soul — ' + lbl, btn, 'Soul tab',
              function() {
                _switchPanel('soul');
                setTimeout(function() { btn.click(); }, 150);
              }, 'soul ' + lbl.toLowerCase());
      });
      panel.querySelectorAll('button[title]').forEach(function(b) {
        if (b.classList && b.classList.contains('reasoning-override-badge')) return;
        var t = (b.title || '').trim();
        // SKIP utility buttons (Apply/Save/Load/Close) - noise in a search
        if (t.length < 8 || /^(apply|save|load|close|refresh|test)$/i.test(t)) return;
        _push(pid, t, b, 'Button');
      });
    });
    var _effTrigSearch = document.getElementById('effort-trigger');
    if (_effTrigSearch) {
      _push('chat', 'Composer — Thinking effort', _effTrigSearch, 'Composer',
            function() {
              if (typeof toggleReasoningEffortMenu === 'function') {
                var m = document.getElementById('effort-menu');
                if (m && m.hidden) toggleReasoningEffortMenu();
              }
              _effTrigSearch.focus();
            },
            'thinking effort reasoning_effort reasoning auto off on minimal low medium high max composer dropdown');
    }
    var _rsnCardSearch = document.getElementById('reasoning-card');
    if (_rsnCardSearch) {
      _push('agents', 'Agents — Reasoning (Effort, Planner & Tool Thinking)', _rsnCardSearch, 'Reasoning',
            function() {
              var det = document.getElementById('reasoning-adv-details');
              if (det && !det.hidden) det.open = true;
            },
            'reasoning thinking effort planner_reasoning_effort planner thinking tool use per chunk max tokens auto off minimal low medium high max');
    }
    // VOICE (1.4): the search must find the new features by their everyday
    // names - the card scan only yields English labels, people type "vorlesen",
    // "tts", "diktieren", "microphone", "voice note".
    [['voice-card', 'Voice — Speech to text', null, 'Voice',
      'speech to text stt dictate dictation microphone mic voice note transcribe whisper parakeet model tier download 3 s record test english'],
     ['voice-card', 'Voice — Text to speech', null, 'Voice',
      'text to speech tts speak read aloud vorlesen ausgeben stimme voice anna cosette woof underdog juno mira numpy speed pocket engine sample uvx'],
     ['voice-card', 'Voice — Read aloud', null, 'Voice',
      'read aloud vorlesen auto speak answer plan coder fazit ask question defensive gate approval card toggles'],
     ['tg-voice-in-toggle', 'Telegram — Voice notes in', null, 'Telegram voice',
      'telegram voice note in dictation transcribe bot phone dictate'],
     ['tg-voice-out-toggle', 'Telegram — Answers as voice notes', null, 'Telegram voice',
      'telegram voice out answer vorlesen voice note bot phone opus'],
     // the 1.4.2 controls: the card scan only sees English labels, and these
     // three live in rows a text scan does not reach (a slider, a select, a
     // switch with a long label) - curated keywords keep them findable.
     ['voice-card', 'Voice — Volume', null, 'Voice',
      'volume lautstaerke lautstärke level loud quiet leise slider playback 0-100'],
     ['voice-card', 'Voice — Preload the engine', null, 'Voice',
      'preload vorladen warmup warm-up cold start sidecar engine run start first sentence latency'],
     ['tg-result-mode-sel', 'Telegram — Result format', null, 'Telegram voice',
      'result mode ergebnis format text voice both nur sprache voice only results only answer'],
     ['tg-progress-toggle', 'Telegram — Progress messages', null, 'Telegram voice',
      'progress status tool calls info messages stumm mute silent only result'],
     // MCP has no card of its own (it is a launcher + a settings list), so the
     // DOM scan cannot find it - without this entry "mcp" searched nothing.
     ['mcp-client-toggle', 'MCP server (external tools)', null, 'Backend',
      'mcp model context protocol server external tools 8090 stdio http client hivemind tools'],
     ['p-configs', 'External clients / API access', null, 'Backend',
      'api openai compatible endpoint v1 chat completions external client integration rest'],
     ['adv-engine-settings-card', 'Advanced / Engine Settings', null, 'Settings',
      'advanced engine settings schema kv cache spec decode mtp dflash vram margin duo compress sampling override']].forEach(function(d) {
      var card = document.getElementById(d[0]) ?
        document.querySelector('.cfg-card:has(#' + d[0] + ')') : null;
      var el = document.getElementById(d[0]);
      if (el && el.closest) card = el.closest('.cfg-card') || card;
      _push('configs', d[1], card || el, d[3], null, d[2] ? d[2] + ' ' + d[4] : d[4]);
    });

    // TABS AS ENTRIES (2026-10-05, user: 'soul, tokens, chats etc sollen
    // auch in die suche') - the seven panels are searchable destinations
    // with curated keywords; the click lands on the tab itself.
    [['agents', 'AGENTS panel', 'agents planner coder duo execution settings vision ctx context temperature'],
     ['presets', 'PRESETS panel', 'presets prompts save load'],
     ['configs', 'CONFIGS panel', 'configs settings toggles options rollup voice tts stt mcp telegram'],
     ['memory', 'MEMORY panel', 'memory long-term notes'],
     ['soul', 'SOUL panel', 'soul personality evolution'],
     ['models', 'MODELS panel', 'models vram tokens tok llama download install ctx moe'],
     ['chats', 'CHATS panel', 'chats history search sizes tokens'],
     ['automations', 'AUTOMATIONS panel', 'automations cron schedule recurring timer zeitplan mcp telegram']].forEach(function(d) {
      var btn = null;
      document.querySelectorAll('.tab').forEach(function(b) {
        if (b.dataset && b.dataset.p === d[0]) btn = b;
      });
      _push(d[0], d[1], btn, 'Tab', null, d[2]);
    });
    _push('chats', 'Search saved chats', document.getElementById('chat-search-input'), 'Chats', null,
          'search chats history filter messages title');
    _push(null, 'New Chat', null, 'Action',
         function() { var b = Array.from(document.querySelectorAll('button'))
           .find(function(x) { return /new chat/i.test(x.textContent); }); if (b) b.click(); });
    _push(null, 'Open chats list', null, 'Action',
         function() { var b = Array.from(document.querySelectorAll('button'))
           .find(function(x) { return x.textContent.trim() === 'Chats'; }); if (b) b.click(); });
    return idx;
  }

  function _switchPanel(pid) {
    if (!pid) return;
    var btn = Array.from(document.querySelectorAll('.tab'))
      .find(function(b) { return (b.dataset && b.dataset.p === pid) || b.textContent.trim() === PANEL_LABELS[pid]; });
    if (btn) btn.click();
  }

  function _flash(el) {
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    var old = el.style.boxShadow;
    el.style.transition = 'box-shadow .3s';
    el.style.boxShadow = '0 0 0 2px #f0b040, 0 0 12px rgba(240,176,64,.5)';
    setTimeout(function() { el.style.boxShadow = old || ''; }, 1400);
  }

  function _open() {
    if (!_fsOverlay) _buildOverlay();
    // REBUILD EVERY OPEN (2026-10-05): agent cards are rendered dynamically -
    // a cached index goes stale the moment a model changes.
    _fsIndex = _buildIndex();
    _fsOverlay.style.display = 'flex';
    _fsInput.value = '';
    _render('');
    setTimeout(function() { _fsInput.focus(); }, 30);
  }

  function _close() { if (_fsOverlay) _fsOverlay.style.display = 'none'; }

  // ALIAS MATCHING (2026-10-05): expanded by the tested pure helper in
  // feature_search_core.js (ctx->context, temp->temperature, ...).
  function _render(q) {
    q = (q || '').toLowerCase().trim();
    _fsList.innerHTML = '';
    var terms = window.FSCore ? window.FSCore.expandTerms(q) : [q];
    var hits = (_fsIndex || []).filter(function(i) {
      if (!q) return true;
      var hay = i.kw || ((i.label + ' ' + i.kind).toLowerCase());
      return terms.some(function(t) { return hay.indexOf(t) >= 0; });
    });
    if (!hits.length) {
      _fsList.innerHTML = '<div class="fs-empty">No matching feature.</div>';
      return;
    }
    var countDiv = document.createElement('div');
    countDiv.className = 'fs-count';
    countDiv.style.cssText = 'font-size:9px;color:#6b7f94;padding:4px 14px;border-bottom:1px solid #1a2330';
    countDiv.textContent = hits.length + ' result' + (hits.length === 1 ? '' : 's');
    _fsList.appendChild(countDiv);
    _fsSel = hits.length ? 0 : -1;
    hits.slice(0, 120).forEach(function(h, i) {
      var row = document.createElement('div');
      row.className = 'fs-row' + (i === _fsSel ? ' fs-sel' : '');
      row.style.cssText = 'display:flex;gap:10px;align-items:center;padding:8px 14px;cursor:pointer;border-bottom:1px solid #1a2330';
      var kind = document.createElement('span');
      kind.className = 'fs-kind';
      kind.style.cssText = 'font-size:9px;color:#6b7f94;min-width:70px';
      kind.textContent = h.kind;
      var label = document.createElement('span');
      label.className = 'fs-label';
      label.style.cssText = 'flex:1;font-size:12px;color:#d8dee9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      label.textContent = h.label;
      var panel = document.createElement('span');
      panel.className = 'fs-panel';
      panel.style.cssText = 'font-size:9px;color:#f0b040';
      panel.textContent = h.panel ? (PANEL_LABELS[h.panel] || h.panel) : '';
      row.appendChild(kind); row.appendChild(label); row.appendChild(panel);
      row.addEventListener('mouseenter', function() {
        Array.prototype.forEach.call(_fsList.querySelectorAll('.fs-row'),
          function(r) { r.classList.remove('fs-sel'); });
        row.classList.add('fs-sel');
      });
      row.addEventListener('click', function() {
        _close();
        if (h.act) { h.act(); return; }
        _switchPanel(h.panel);
        // REVEAL (2026-10-05, user): some settings are hidden by the active
        // mode/submode (e.g. the duo ctx wrappers in direct chat). A search
        // hit must still be usable: clear inline display:none up the chain.
        if (h.el) {
          var n = h.el;
          while (n && n !== document.body) {
            if (n.style && n.style.display === 'none') n.style.display = '';
            if (n.tagName === 'DETAILS' && !n.open) n.open = true;
            n = n.parentElement;
          }
          if (h.el.id === 'adv-engine-settings-card' && typeof loadAdvancedSettingsSchema === 'function') {
            loadAdvancedSettingsSchema();
          }
        }
        setTimeout(function() { _flash(h.el); }, 120);
      });
      _fsList.appendChild(row);
    });
  }

  function _moveSel(delta) {
    var rows = _fsList.querySelectorAll('.fs-row');
    if (!rows.length) return;
    _fsSel += delta;
    if (_fsSel < 0) _fsSel = rows.length - 1;
    if (_fsSel >= rows.length) _fsSel = 0;
    rows.forEach(function(r, i) { r.classList.toggle('fs-sel', i === _fsSel); });
    rows[_fsSel].scrollIntoView({ block: 'nearest' });
  }

  function _buildOverlay() {
    _fsOverlay = document.createElement('div');
    _fsOverlay.id = 'fs-overlay';
    _fsOverlay.style.cssText = 'display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.55);' +
      'align-items:flex-start;justify-content:center;padding-top:12vh';
    _fsOverlay.addEventListener('click', function(e) { if (e.target === _fsOverlay) _close(); });
    var box = document.createElement('div');
    box.style.cssText = 'width:min(560px,92vw);background:#141a22;border:1px solid #2a3644;' +
      'border-radius:8px;box-shadow:0 12px 40px rgba(0,0,0,.6);overflow:hidden;' +
      "font-family:'IBM Plex Mono',monospace";
    _fsInput = document.createElement('input');
    _fsInput.placeholder = 'Search features, settings, agents…  (Esc closes)';
    _fsInput.style.cssText = 'width:100%;box-sizing:border-box;padding:12px 14px;font-size:13px;' +
      'background:#0d1117;color:#d8dee9;border:none;outline:none;border-bottom:1px solid #2a3644';
    _fsInput.addEventListener('input', function() { _render(_fsInput.value); });
    _fsInput.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') { _close(); return; }
      var rows = _fsList.querySelectorAll('.fs-row');
      if (e.key === 'ArrowDown') { e.preventDefault(); _moveSel(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); _moveSel(-1); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        var sel = _fsList.querySelector('.fs-sel') || rows[0];
        if (sel) sel.click();
      }
    });
    _fsList = document.createElement('div');
    _fsList.style.cssText = 'max-height:52vh;overflow-y:auto';
    box.appendChild(_fsInput);
    box.appendChild(_fsList);
    _fsOverlay.appendChild(box);
    document.body.appendChild(_fsOverlay);
  }

  // HOTKEYS (2026-10-05, user: 'besserer hotkey'): Ctrl+K/Cmd+K stays,
  // "/" opens as a single key (GitHub/YouTube style) - guarded so typing
  // in inputs never triggers it.
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('keydown', function(e) {
      var typing = e.target && (e.target.tagName === 'INPUT' ||
        e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT' ||
        e.target.isContentEditable);
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        _open();
      } else if (e.key === '/' && !typing) {
        e.preventDefault();
        _open();
      }
    });
  }

  if (typeof window !== 'undefined') {
    if (typeof window.addEventListener === 'function') {
      window.addEventListener('load', function() {
        var host = document.getElementById('h-sidebar-btn');
        if (host && !document.getElementById('fs-open-btn')) {
          var b = document.createElement('button');
          b.id = 'fs-open-btn';
          b.className = 'hbtn';
          b.title = 'Search the UI (Ctrl+K)';
          b.textContent = 'Search';
          b.addEventListener('click', _open);
          host.parentElement.insertBefore(b, host.nextSibling);
        }
      });
    }
    window.hivemindFeatureSearch = { open: _open, rebuild: function() { _fsIndex = _buildIndex(); } };
  }
})();

