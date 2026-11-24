// HiveMind UI module: Model Configs/Eval, Soul/Skills/Insights/Stats, Automations & Special Agents.
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
  postSettings({startup_preload_enabled: enabled});
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
  var tabs = ['base','learned','defaults','log'];
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
  if (cfgState.currentTab === 'base')     renderBaseConfigs();
  if (cfgState.currentTab === 'learned')  await renderLearnedConfigs(model);
  if (cfgState.currentTab === 'defaults') await loadModelDefaults(model);
  if (cfgState.currentTab === 'log')      await renderLog(model);
}

function _mdefVal(id) {
  var el = document.getElementById(id);
  return el ? String(el.value || '').trim() : '';
}

function _mdefInt(id) {
  var s = _mdefVal(id);
  if (!s) return null;
  var n = parseInt(s, 10);
  return isFinite(n) ? n : null;
}

function _mdefFloat(id) {
  var s = _mdefVal(id);
  if (!s) return null;
  var n = parseFloat(s);
  return isFinite(n) ? n : null;
}

function _mdefSet(id, v) {
  var el = document.getElementById(id);
  if (!el) return;
  if (el.type === 'checkbox') el.checked = !!v;
  else el.value = (v === null || v === undefined) ? '' : String(v);
}

function populateModelDefaultsForm(prof, gguf, onlyEmpty) {
  prof = prof || {};
  gguf = gguf || {};
  cfgState._lastGgufMeta = gguf;
  var caps = prof.capabilities || {};
  var samp = prof.sampling || {};
  var sthk = samp.thinking || {};
  var snth = samp.non_thinking || {};
  var setIf = function(id, v) {
    if (onlyEmpty) {
      var el = document.getElementById(id);
      if (el && el.type !== 'checkbox' && String(el.value || '').trim() !== '') return;
    }
    _mdefSet(id, v);
  };
  if (!onlyEmpty) {
    _mdefSet('mdef-cap-thinking', !!caps.thinking);
    _mdefSet('mdef-cap-vision', !!caps.vision);
    _mdefSet('mdef-cap-toolcall', caps.tool_call !== undefined ? !!caps.tool_call : false);
    _mdefSet('mdef-jinja', !!prof.jinja);
    _mdefSet('mdef-distilled', !!prof.distilled);
    _mdefSet('mdef-mtp', !!prof.mtp);
    _mdefSet('mdef-s-thk-cache', sthk.cache_prompt !== false);
    _mdefSet('mdef-s-nth-cache', snth.cache_prompt !== false);
  }
  setIf('mdef-reasoning', prof.reasoning || '');
  setIf('mdef-thinking-budget', prof.thinking_budget);
  setIf('mdef-num-ctx', prof.num_ctx);
  setIf('mdef-num-ctx-coder', prof.num_ctx_duo_coder);
  setIf('mdef-num-ctx-analyst', prof.num_ctx_analyst);
  setIf('mdef-num-ctx-vision', prof.num_ctx_vision);
  setIf('mdef-moe', prof.moe === true ? 'true' : prof.moe === false ? 'false' : '');
  setIf('mdef-moe-cpu', prof.moe_cpu_experts);
  setIf('mdef-gpu-layers', prof.gpu_layers);
  setIf('mdef-kv-type', prof.kv_cache_type || '');
  setIf('mdef-load-mode', prof.load_mode || '');
  setIf('mdef-vram-ov', prof.vram_gb_override);
  setIf('mdef-kv-bpt', prof.kv_bytes_per_token);
  setIf('mdef-mmproj', prof.mmproj_filename || '');
  setIf('mdef-chat-template', prof.chat_template || '');
  setIf('mdef-extra-args', Array.isArray(prof.extra_args) ? prof.extra_args.join(' ') : (prof.extra_args || ''));
  var mapSampling = function(prefix, blk) {
    setIf(prefix + '-temp', blk.temperature);
    setIf(prefix + '-top-p', blk.top_p);
    setIf(prefix + '-top-k', blk.top_k);
    setIf(prefix + '-min-p', blk.min_p);
    setIf(prefix + '-pres', blk.presence_penalty);
    setIf(prefix + '-rep', blk.repetition_penalty);
  };
  mapSampling('mdef-s-thk', sthk);
  mapSampling('mdef-s-nth', snth);
}

async function loadModelDefaults(model) {
  var infoEl = document.getElementById('mdef-gguf-info');
  var stEl = document.getElementById('mdef-status');
  if (!model) return;
  if (stEl) stEl.textContent = 'Loading...';
  try {
    var d = await (await fetch('/model-profile?model=' + encodeURIComponent(model))).json();
    var prof = d.profile || {};
    var gguf = d.gguf || {};
    populateModelDefaultsForm(prof, gguf, false);
    if (infoEl) {
      var parts = [];
      if (d.file) parts.push('File: ' + d.file);
      else parts.push('No JSON file yet (auto/GGUF fallback active)');
      if (gguf.architecture) parts.push('Arch: ' + gguf.architecture);
      if (gguf.context_length) parts.push('GGUF ctx: ' + gguf.context_length);
      if (gguf.expert_count) parts.push('Experts: ' + gguf.expert_count + (gguf.expert_used_count ? '/' + gguf.expert_used_count : ''));
      if (gguf.kv_bytes_per_token_q4) parts.push('KV q4: ' + Math.round(gguf.kv_bytes_per_token_q4) + ' B/tok');
      if (gguf.file_size_gb) parts.push('Size: ' + gguf.file_size_gb + ' GB');
      infoEl.textContent = parts.join(' · ');
    }
    if (stEl) stEl.textContent = '';
  } catch (e) {
    if (stEl) stEl.textContent = 'Load failed: ' + e.message;
  }
}

async function seedModelDefaultsFromGguf() {
  var model = cfgState.currentModel;
  var stEl = document.getElementById('mdef-status');
  if (!model) return;
  try {
    var res = await fetch('/gguf-meta?model=' + encodeURIComponent(model));
    var d = await res.json();
    var meta = (d && d.meta) || {};
    if (!meta || !Object.keys(meta).length) {
      if (stEl) stEl.textContent = 'No GGUF metadata found on disk for ' + model;
      return;
    }
    _mdefSet('mdef-cap-thinking', !!meta.has_thinking);
    _mdefSet('mdef-cap-vision', !!meta.has_vision);
    _mdefSet('mdef-cap-toolcall', !!meta.has_chat_template);
    _mdefSet('mdef-jinja', !!meta.has_chat_template);
    _mdefSet('mdef-reasoning', meta.has_thinking ? 'on' : 'off');
    _mdefSet('mdef-moe', meta.is_moe ? 'true' : 'false');
    if (meta.context_length) _mdefSet('mdef-num-ctx', Math.min(meta.context_length, 32768));
    if (meta.kv_bytes_per_token_q4) _mdefSet('mdef-kv-bpt', Math.round(meta.kv_bytes_per_token_q4));
    if (stEl) stEl.textContent = 'Populated from GGUF header (click Save Model Defaults to persist).';
  } catch (e) {
    if (stEl) stEl.textContent = 'GGUF probe failed: ' + e.message;
  }
}

async function saveModelDefaults() {
  var model = cfgState.currentModel;
  var stEl = document.getElementById('mdef-status');
  if (!model) return;
  var readSampling = function(prefix, cacheId) {
    var blk = { cache_prompt: !!document.getElementById(cacheId).checked };
    blk.temperature = _mdefFloat(prefix + '-temp');
    blk.top_p = _mdefFloat(prefix + '-top-p');
    blk.top_k = _mdefInt(prefix + '-top-k');
    blk.min_p = _mdefFloat(prefix + '-min-p');
    blk.presence_penalty = _mdefFloat(prefix + '-pres');
    blk.repetition_penalty = _mdefFloat(prefix + '-rep');
    return blk;
  };
  var moeVal = _mdefVal('mdef-moe');
  var extraRaw = _mdefVal('mdef-extra-args');
  var patch = {
    capabilities: {
      thinking: !!document.getElementById('mdef-cap-thinking').checked,
      vision: !!document.getElementById('mdef-cap-vision').checked,
      tool_call: !!document.getElementById('mdef-cap-toolcall').checked,
    },
    jinja: !!document.getElementById('mdef-jinja').checked,
    distilled: !!document.getElementById('mdef-distilled').checked,
    mtp: !!document.getElementById('mdef-mtp').checked,
    reasoning: _mdefVal('mdef-reasoning') || null,
    thinking_budget: _mdefInt('mdef-thinking-budget'),
    num_ctx: _mdefInt('mdef-num-ctx'),
    num_ctx_duo_coder: _mdefInt('mdef-num-ctx-coder'),
    num_ctx_analyst: _mdefInt('mdef-num-ctx-analyst'),
    num_ctx_vision: _mdefInt('mdef-num-ctx-vision'),
    moe: moeVal === 'true' ? true : moeVal === 'false' ? false : null,
    moe_cpu_experts: _mdefInt('mdef-moe-cpu'),
    gpu_layers: _mdefInt('mdef-gpu-layers'),
    kv_cache_type: _mdefVal('mdef-kv-type') || null,
    load_mode: _mdefVal('mdef-load-mode') || null,
    vram_gb_override: _mdefFloat('mdef-vram-ov'),
    kv_bytes_per_token: _mdefInt('mdef-kv-bpt'),
    mmproj_filename: _mdefVal('mdef-mmproj') || null,
    chat_template: _mdefVal('mdef-chat-template') || null,
    extra_args: extraRaw ? extraRaw.split(/\s+/).filter(Boolean) : [],
    sampling: {
      thinking: readSampling('mdef-s-thk', 'mdef-s-thk-cache'),
      non_thinking: readSampling('mdef-s-nth', 'mdef-s-nth-cache'),
    },
  };
  if (stEl) stEl.textContent = 'Saving...';
  try {
    var res = await fetch('/model-profile', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model, patch: patch})
    });
    var d = await res.json();
    if (res.ok && d.ok) {
      if (stEl) stEl.textContent = 'Saved defaults (' + (d.file || model) + ').';
      if (typeof loadModels === 'function') await loadModels();
      if (typeof loadSettings === 'function') await loadSettings();
      await loadModelDefaults(model);
    } else {
      if (stEl) stEl.textContent = 'Save failed: ' + ((d && d.error) || res.status);
    }
  } catch (e) {
    if (stEl) stEl.textContent = 'Save error: ' + e.message;
  }
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
      showEvalResult('Recommendations for ' + model + ':', suggestions, model);
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
      '<button type="button" class="eval-apply-btn" ' +
        'data-model="' + esc(model) + '" ' +
        'data-agent="' + esc(s.agent) + '" ' +
        'data-field="' + esc(s.field) + '" ' +
        'data-suggested="' + esc(String(s.suggested)) + '" ' +
        'style="background:none;border:1px solid #3a9960;border-radius:4px;padding:3px 10px;' +
        'color:#3a9960;font-family:\'IBM Plex Mono\',monospace;font-size:10px;cursor:pointer;transition:all .15s">' +
        '\u2713 Apply' +
      '</button>' +
    '</div>';
  });
  c.innerHTML = html;
  if (!c._evalDelegated) {
    c._evalDelegated = true;
    c.addEventListener('click', function(ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest('.eval-apply-btn') : null;
      if (!btn || !c.contains(btn)) return;
      var val = parseFloat(btn.dataset.suggested);
      if (!isFinite(val)) return;
      applyEvalSuggestion(btn.dataset.model || '', btn.dataset.agent || '', btn.dataset.field || '', val, btn);
    });
  }
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
      badge.textContent = '\u25CF ORIGINAL SOUL ACTIVE (default)';
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
    // SOUL tab: own list element. It used to share the id with the Agents
  // card, so whichever loaded last overwrote the other view.
  const container = document.getElementById('skills-list-soul')
    || document.getElementById('skills-list');

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
    const container = document.getElementById('skills-list-soul')
      || document.getElementById('skills-list');
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

function updateLspHint() {
  // The hint text lives in index.html (one place, full language list); this
  // only swaps the on-state for the off-state when LSP is disabled.
  var on = document.getElementById('lsp-hint');
  var off = document.getElementById('lsp-hint-off');
  var enabled = S.lspEnabled !== false;
  if (on) on.style.display = enabled ? '' : 'none';
  if (off) off.style.display = enabled ? 'none' : '';
  if (enabled) loadLspStatus();
}

function loadLspStatus() {
  // LSP-STATUS (2026-11-03): show which servers this machine actually has.
  var box = document.getElementById('lsp-status-list');
  if (!box || box.dataset.loading === '1') return;
  box.dataset.loading = '1';
  fetch('/lsp/status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      box.textContent = '';
      box.dataset.loading = '0';
      if (!d || !d.ok) {
        box.textContent = 'Status unavailable - engine not reachable.';
        box.style.color = '#c08040';
        return;
      }
      box.style.color = '';
      var rows = d.rows || [];
      var found = rows.filter(function(r) { return r.found; }).length;
      var sum = document.getElementById('lsp-status-summary');
      if (sum) sum.textContent = 'Language servers on PATH — ' + found + '/' +
        rows.length + ' installed (click to expand)';
      var head = document.createElement('div');
      head.style.cssText = 'color:#9aa7b8;margin-bottom:3px';
      head.textContent = found + '/' + rows.length + ' servers installed on PATH';
      box.appendChild(head);
      rows.forEach(function(r) {
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;gap:8px;align-items:center;padding:1px 0';
        var dot = document.createElement('span');
        dot.textContent = '\u25CF';
        dot.style.color = r.found ? '#60c080' : '#c08040';
        var lang = document.createElement('span');
        lang.textContent = r.language;
        lang.style.cssText = 'min-width:118px;color:#c8d4e4';
        var cmd = document.createElement('code');
        cmd.textContent = r.command;
        cmd.style.cssText = 'font-size:10px;color:#8fa0b8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:250px';
        var badge = document.createElement('span');
        badge.textContent = r.found ? 'installed' : 'missing';
        badge.style.cssText = 'margin-left:auto;font-size:9px;color:' +
          (r.found ? '#60c080' : '#c08040');
        row.appendChild(dot); row.appendChild(lang);
        row.appendChild(cmd); row.appendChild(badge);
        box.appendChild(row);
      });
    })
    .catch(function() {
      box.dataset.loading = '0';
      box.textContent = 'Status unavailable - engine not reachable.';
      box.style.color = '#c08040';
    });
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
  var raw = String(m || '').trim();
  m = raw.toLowerCase();
  if (!m) return false;
  var pk = _moeKey(raw).toLowerCase();
  if (S.moePins) {
    if (pk && S.moePins[pk] != null) return !!S.moePins[pk];
    if (S.moePins[m] != null) return !!S.moePins[m];
    if (S.moePins[raw] != null) return !!S.moePins[raw];
  }
  if (S.moeExpertDefaults && (S.moeExpertDefaults[m] || (pk && S.moeExpertDefaults[pk]))) return true;
  if (/moe/.test(m)) return true;
  return /(^|[-._:])(a\d+(?:\.\d+)?b|\d+x\d+(?:\.\d+)?b)([-._:]|$)/.test(m);
}

// Backend-compatible per-model key — mirrors manager_load._moe_model_key
// (slot-alias "#N" and trailing "-ud" suffix are stripped before the override /
// table lookup). Storing overrides under this key guarantees they match at
// model-load time, also for -ud variants of the same base model.
function _moeKey(model) {
  return String(model || '').trim().replace(/#\d+$/, '').replace(/-ud$/i, '');
}

function _moeOverride(model) {
  var m = S.moeCpuExpertsMap || {};
  var k = _moeKey(model);
  return parseInt((k && m[k] != null) ? m[k] : m[model], 10) || 0;
}

function _moeDefaultCount(model) {
  var defaults = S.moeExpertDefaults || {};
  var k = _moeKey(model);
  return (k && defaults[k] != null) ? defaults[k] : defaults[model];
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
  // The override only matters for models the user actually runs, so the
  // dropdown must offer the INSTALLED MoE models — not every entry of the
  // defaults table (many of those are not present on this machine and only
  // confused the list). Active duo models come first, then the rest.
  var list = [];
  var seen = {};
  function add(m) {
    m = String(m || '').trim();
    if (!m || !_isMoeModel(m)) return;
    var k = _moeKey(m);
    if (seen[k]) return;   // one entry per effective key (e.g. -ud variants)
    seen[k] = true;
    list.push(m);
  }
  [S.duoCoderModel, S.duoPlannerModel].forEach(add);
  (S.models || []).forEach(add);
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
      return '<option value="' + esc(m) + '">' + esc(m) + '</option>';
    }).join('');
    sel.value = S.moeSelectedModel;
  });
  _syncMoeInputs();
}

function _syncMoeInputs() {
  var v = _moeOverride(S.moeSelectedModel);
  ['moe-cpu-experts', 'config-moe-cpu-experts'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.value = v;
  });
  var _pinSel = document.getElementById('config-moe-pin-select');
  if (_pinSel) {
    var _pk = _moeKey(S.moeSelectedModel);
    var _pv = (_pk && S.moePins && S.moePins[_pk] != null) ? S.moePins[_pk]
            : (S.moePins && S.moePins[S.moeSelectedModel] != null) ? S.moePins[S.moeSelectedModel] : null;
    _pinSel.value = _pv === true ? 'true' : _pv === false ? 'false' : '';
  }
}

// MOE-PIN (2026-10-14): the explicit "is this a MoE?" answer — written to the
// model profile so the load path skips the 30B+ name heuristic entirely.
function onMoePinChange(val) {
  var model = S.moeSelectedModel;
  if (!model) return;
  var moe = val === 'true' ? true : val === 'false' ? false : null;
  var key = _moeKey(model);
  if (S.moePins) {
    if (moe === null) { if (key) delete S.moePins[key]; delete S.moePins[model]; }
    else S.moePins[key || model] = moe;
  }
  fetch('/moe-pin', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({model: model, moe: moe})
  }).then(function(r) { return r.json(); }).then(function(j) {
    if (!j || j.ok !== true) {
      var _msg = (j && j.error) ? j.error : 'unknown error';
      updateMoeExpertDefaultHint('MoE type NOT saved: ' + _msg);
    }
  }).catch(function(e) {
    updateMoeExpertDefaultHint('MoE type NOT saved: ' + e);
  });
  updateMoeExpertDefaultHint();
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
  var model = S.moeSelectedModel;
  if (!model) return;
  var key = _moeKey(model);
  if (key && key !== model) delete S.moeCpuExpertsMap[model];  // stale spelling
  if (v > 0) S.moeCpuExpertsMap[key || model] = v;
  else if (key) delete S.moeCpuExpertsMap[key];
  _syncMoeInputs();
  postSettings({moe_cpu_experts: S.moeCpuExpertsMap});
  updateMoeExpertDefaultHint();
}

function updateMoeExpertDefaultHint(overrideMsg) {
  var autodetect = S.moeAutodetect || {};
  var model = S.moeSelectedModel || S.duoCoderModel || S.duoPlannerModel || '';
  var key = _moeKey(model);
  var _ovr = _moeOverride(model);
  var _modelShort = (key || model).split(':')[0];
  var _def = _moeDefaultCount(model);
  var _auto = !!(autodetect[key] || (key && key !== model && autodetect[model]));
  var _pin = (key && S.moePins && S.moePins[key] != null) ? S.moePins[key]
           : (S.moePins && S.moePins[model] != null) ? S.moePins[model] : null;
  var _defTxt = '';
  if (_pin === false) _defTxt = 'pinned DENSE (name heuristics ignored)';
  else if (_pin === true) _defTxt = 'pinned MoE (name heuristics ignored)';
  else if (_def) _defTxt = _def + ' experts' + (_auto ? ' (autodetected from model name)' : ' (table)');
  else if (model) _defTxt = 'no default (experts stay on GPU)';
  var text = '';
  if (overrideMsg) {
    text = overrideMsg;
  } else if (_ovr > 0) {
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
      '<div class="soul-stat-row"><span class="soul-stat-label">LLM requests</span><span class="soul-stat-val" style="color:#8fa4b8">' + (totalRequests || '–') + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 cached/request</span><span class="soul-stat-val" style="color:#8fa4b8">' + (avgCachedPerReq ? _fmtTokens(avgCachedPerReq) : '–') + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 input/run</span><span class="soul-stat-val" style="color:#8fa4b8">' + _fmtTokens(avgInPerRun) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">Total runs</span><span class="soul-stat-val" style="color:#8fa4b8">' + totalRuns + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 output/run</span><span class="soul-stat-val" style="color:#8fa4b8">' + _fmtTokens(avgTokens) + '</span></div>' +
      '<div style="font-size:9px;color:#5a6a7a;padding-top:6px;line-height:1.5">Input/Cached = sum over ALL LLM requests: every tool run and every phase resends the context (agentic loop). "Cached" = KV-cache hits \u2014 a high share is good (fast, cheap), no consumption.</div>';

    const daily = data.daily || {};
    const days = Object.keys(daily).sort().reverse();

    // METRICS-EXPAND (2026-10-19): comparison per model, computed by the backend
    // (routers/soul.py -> infra.token_stats.aggregate_by_model). Rendered as
    // compact cards: a fixed-width table was unreadable at panel width.
    // By model = Bewertung der Modelle ueber das ganze Fenster. Eine Tages-Sicht
    // braucht es nicht: die Tagesliste darunter zeigt chronologisch, welches
    // Modell welchen Run beruehrt hat.
    const byModel = data.by_model || [];
    const _filterTag = S.tokenStatsModelFilter || '';
    let byModelHtml = '';
    if (byModel.length) {
      // Nur das Noetige: welches Modell laeuft (crash-frei) und wie schnell.
      // Rollen-Tokens/-Zeiten werden pro Modell zusammengefasst; die Rolle selbst
      // ist fuer die Frage "laeuft es stabil und wie schnell" nicht relevant.
      var _flat = {};
      byModel.forEach(function (m) {
        var f = _flat[m.model];
        if (!f) {
          f = _flat[m.model] = {model: m.model, runs: 0, aborts: 0, completed: 0,
                                timed: 0, secs: 0, tokens: 0, estimated: false};
        }
        f.runs += (m.runs || 0);
        // alles, was DU gestoppt hast (abort + graceful_stop) zaehlt nicht als Fail
        f.aborts += (typeof m.excluded === 'number' ? m.excluded : (m.aborted || 0));
        f.completed += ((m.stop || {})['completed'] || 0);
        f.timed += (m.timed_tokens || 0);
        f.secs += (m.seconds || 0);
        f.tokens += (m.tokens || 0);
        f.estimated = f.estimated || !!m.estimated;
        // Backend-Rate (Phasenmessung oder Log-Median) als Rueckfall merken:
        // ohne das rechnete die Liste nur aus timed/secs und zeigte "–"
        if ((m.tps || 0) > (f.apiTps || 0)) {
          f.apiTps = m.tps;
        }
        if (m.speed_source === 'log') {
          f.speedSource = 'log';
          f.speedSamples = Math.max(f.speedSamples || 0, m.speed_samples || 0);
        }
      });
      S.tokenStatsModels = Object.keys(_flat);
      var _flatRows = Object.keys(_flat).map(function (k, _i) {
        var f = _flat[k];
        f.idx = _i;
        var judged = f.runs - f.aborts;            // dein Abbruch ist kein Crash
        f.tps = (f.timed && f.secs) ? f.timed / f.secs : (f.apiTps || 0);
        f.crashFree = judged > 0 ? f.completed / judged : 0;
        f.judged = judged;
        return f;
      });
      // schnell oben; ohne Messwerte nach unten, dort nach Stabilitaet
      _flatRows.sort(function (a, b) {
        if ((b.tps || 0) !== (a.tps || 0)) return (b.tps || 0) - (a.tps || 0);
        return (b.crashFree || 0) - (a.crashFree || 0);
      });
      var _cards = _flatRows.map(function (f) {
        var _tpsColor = f.tps >= 20 ? '#3a9960' : (f.tps >= 10 ? '#c0a020' : '#a06060');
        var _logSpeed = f.speedSource === 'log';
        var _cfColor = f.crashFree >= 0.8 ? '#3a9960' : (f.crashFree >= 0.6 ? '#c0a020' : '#a06060');
        var _isActive = !!_filterTag && _filterTag === f.model;
        return '<div onclick="setTokenStatsModelFilter(' + f.idx + ')" title="click to filter the runs below" ' +
          'style="display:flex;align-items:baseline;gap:8px;padding:3px 0;border-bottom:1px solid #131c26;font-size:9px;cursor:pointer' +
          (_isActive ? ';background:#141c26' : '') + '">' +
          '<span title="' + esc(f.model) + '" style="flex:1 1 auto;min-width:0;color:#c8d4e0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
            esc(f.model) + '</span>' +
          '<span style="flex:0 0 70px;text-align:right;color:' + _tpsColor + ';font-weight:600" title="' +
            (_logSpeed ? 'median decode speed from the llama-server log (' + (f.speedSamples || 0) + ' samples)'
                       : (f.estimated ? 'no phase timing in part of these records - rate uses the available seconds'
                                      : 'output tokens per second')) + '">' +
            (f.tps ? f.tps.toFixed(1) + ' tok/s' : '\u2013') +
            (f.tps && (_logSpeed || f.estimated)
              ? '<sup style="font-size:6px;color:#5a6a7a">' + (_logSpeed ? '~' : '*') + '</sup>' : '') +
            '</span>' +
          '<span style="flex:0 0 92px;text-align:right;color:' + _cfColor + '" title="completed runs / runs without your own stops (abort + graceful)">' +
            Math.round(f.crashFree * 100) + '% <span style="color:#4a5a6a">(' + f.completed + '/' + f.judged + ')</span></span>' +
        '</div>';
      }).join('');
      var _head = '<div style="display:flex;gap:8px;padding:0 0 4px;margin-bottom:1px;border-bottom:1px solid #16202a;' +
        'font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#55657a">' +
        '<span style="flex:1 1 auto"></span>' +
        '<span style="flex:0 0 70px;text-align:right">speed</span>' +
        '<span style="flex:0 0 92px;text-align:right">crash-free</span>' +
      '</div>';
      byModelHtml =
        '<div style="margin:6px 0 10px;border:1px solid #1e2a38;border-radius:3px;padding:8px 10px">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline;padding:0 0 5px" ' +
            'title="tok/s = output tokens per coder-phase second. Runs before 2026-10-19 carry no phase timing and fall back to the whole run time.">' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:var(--tx2)">By model' +
              (_filterTag ? ' \u00b7 filtered' : '') + '</span>' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#3f4e5c;letter-spacing:.4px">14 days</span>' +
          '</div>' +
          _head + _cards +
          '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#4a5a6a;' +
            'margin-top:6px;padding:6px 0 0;border-top:1px solid #1a2430;line-height:1.7">' +
            '<span style="color:#5a6a7a">crash-free</span> completed \u00f7 runs without your own stops (abort, graceful)' +
            '<br><span style="color:#5a6a7a">~</span> median decode speed from the llama-server log ' +
            '<span style="color:#5a6a7a">*</span> estimated from run time ' +
            '<br><span style="color:#5a6a7a">click a model</span> filters the runs below to that model' +
          '</div>' +
        '</div>';
    }
    // MODE-TRACKING (2026-10-20): welche Route lief wie oft/schnell/erfolgreich
    const byMode = data.by_mode || [];
    let byModeHtml = '';
    if (byMode.length) {
      var _modeOrder = {code_duo: 0, pipeline: 1, direct: 2};
      var _mRows = byMode.slice().sort(function (a, b) {
        var oa = _modeOrder[a.mode] === undefined ? 9 : _modeOrder[a.mode];
        var ob = _modeOrder[b.mode] === undefined ? 9 : _modeOrder[b.mode];
        return oa !== ob ? oa - ob : (b.runs || 0) - (a.runs || 0);
      }).map(function (m) {
        var _ok = Math.round((m.completed_share || 0) * 100);
        var _okColor = _ok >= 80 ? '#3a9960' : (_ok >= 60 ? '#c0a020' : '#a06060');
        return '<div style="display:flex;align-items:baseline;gap:8px;padding:3px 0;' +
          'border-bottom:1px solid #131c26;font-size:9px">' +
          '<span style="flex:1 1 auto;min-width:0;color:#c8d4e0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
            esc(m.mode || '\u2013') + '</span>' +
          '<span style="flex:0 0 52px;text-align:right;color:#7a8fa8">' + (m.runs || 0) + ' runs</span>' +
          '<span style="flex:0 0 66px;text-align:right;color:#60a0e0">' + _fmtTokens(m.tokens || 0) + ' out</span>' +
          '<span style="flex:0 0 62px;text-align:right;color:' + _okColor + '">' + _ok + '% ok</span>' +
          '<span style="flex:0 0 62px;text-align:right;color:#6a7a8a">\u00d8 ' + _fmtDuration(m.avg_elapsed_s || 0) + '</span>' +
        '</div>';
      }).join('');
      byModeHtml =
        '<div style="margin:0 0 10px;border:1px solid #1e2a38;border-radius:3px;padding:8px 10px">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline;padding:0 0 5px">' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:var(--tx2)">By mode</span>' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#3f4e5c" ' +
              'title="route = what actually ran: code_duo (planner+coder loop), pipeline (analyst+synthesizer), direct (chat)">' +
              'route that ran</span>' +
          '</div>' +
          '<div style="display:flex;gap:8px;padding:0 0 4px;margin-bottom:1px;border-bottom:1px solid #16202a;' +
            'font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#55657a">' +
            '<span style="flex:1 1 auto"></span>' +
            '<span style="flex:0 0 52px;text-align:right">runs</span>' +
            '<span style="flex:0 0 66px;text-align:right">out</span>' +
            '<span style="flex:0 0 62px;text-align:right">ok</span>' +
            '<span style="flex:0 0 62px;text-align:right">\u00d8 time</span>' +
          '</div>' +
          _mRows +
        '</div>';
    }

    // Leer-Guard + Balkenskalierung (beim Karten-Umbau versehentlich entfernt,
    // Fehler war "maxDayTokens is not defined")
    if (!days.length) {
      dailyEl.innerHTML = byModelHtml + byModeHtml +
        '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8;padding:8px">' +
        'No token data yet. Captured automatically per run.</div>';
      return;
    }
    let maxDayTokens = 0;
    days.forEach(function (d) { maxDayTokens = Math.max(maxDayTokens, daily[d].tokens || 0); });

    var _filterBar = '';
    if (_filterTag) {
      _filterBar = '<div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin:0 0 4px;' +
        'padding:2px 4px;border:1px solid #2a3a4a;border-radius:3px;font-family:\'IBM Plex Mono\',monospace;font-size:8px">' +
        '<span style="color:#8fa4b8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">filter: ' + esc(_filterTag) + '</span>' +
        '<span onclick="setTokenStatsModelFilter(-1)" style="cursor:pointer;color:#a06060;white-space:nowrap">clear \u2715</span>' +
      '</div>';
    }
    dailyEl.innerHTML = byModelHtml + byModeHtml + _filterBar + days.map(function(day, di) {
      const entry = daily[day];
      const pct = maxDayTokens > 0 ? Math.round((entry.tokens / maxDayTokens) * 100) : 0;
      const avgPerRun = entry.runs > 0 ? Math.round(entry.tokens / entry.runs) : 0;
      const elapsed = entry.elapsed_s || 0;
      var runList = (entry.run_list || []).filter(function (r) {
        return !(r.smoke_test || ((r.requests || 0) === 0 && (r.prompt_tokens || 0) === 0
          && (!r.phases || !Object.keys(r.phases).length)
          && (r.tokens || 0) === 45 && (r.elapsed_s || 0) >= 20));
      });
      if (_filterTag) {
        // "welche Chats hat dieses Modell beruehrt" - Rolle egal, Tag muss passen
        runList = runList.filter(function (r) {
          var mm = r.models || {};
          return Object.keys(mm).some(function (k) {
            var v = mm[k];
            if (Array.isArray(v)) return v.indexOf(_filterTag) !== -1;
            return v === _filterTag;
          });
        });
      }
      const uid = 'tsd-' + di;
      let runsHtml = '';
      if (runList.length) {
        runsHtml = '<div id="' + uid + '" style="display:none;margin:4px 0 2px;padding-left:6px;border-left:1px solid #1e2a38">' +
          runList.map(function(r) {
            const sr = r.stop_reason || 'completed';
            const srColor = sr === 'completed' ? '#3a9960' : (sr === 'graceful_stop' ? '#8a8a8a' : '#c08020');
            // Ein Metrik-Stil fuer alle Zeilen (2026-10-20): eine Kopfzeile mit
            // Zeit/Stop/Modell + eine Zahlenzeile - vorher drei gestapelte
            // Mini-Zeilen mit unterschiedlicher Typografie.
            const models = r.models || {};
            var _tag = function (t) {
              var s = String(t || '').replace(/^.*[\\/]/, '').replace(/\.gguf$/i, '');
              return s.length > 30 ? s.slice(0, 29) + '\u2026' : s;
            };
            // Welche Rollen waren WIRKLICH aktiv? `models` enthaelt auch Rollen,
            // die nur konfiguriert waren (z.B. critic in 70 Runs, aber nie eine
            // Runde). Aktiv heisst: Tokens oder Zeit in phases/phases_ms.
            var _ph0 = r.phases || {};
            var _pm0 = r.phases_ms || {};
            var _act = [];
            [['planner', 'P', '#9a74dc'], ['coder', 'C', '#20b0a0'], ['critic', 'K', '#e09030'],
             ['analyst', 'A', '#4f9ad0'], ['refiner', 'R', '#7fa0c0'],
             ['synthesizer', 'S', '#4fc0a0'], ['vision', 'V', '#c09040']]
              .forEach(function (d) {
                if ((_ph0[d[0]] || 0) > 0 || (_pm0[d[0]] || 0) > 0) _act.push(d);
              });
            // Pipeline-Runs erkennt man an ihren Stages (analyst/refiner/synthesizer)
            var _isPipeline = _act.some(function (d) {
              return d[0] === 'analyst' || d[0] === 'refiner' || d[0] === 'synthesizer';
            });
            // Run-Typ aus der Aktivitaet ableiten (mode ist bei Alt-Runs leer)
            var _kind = '\u2013';
            var _kindColor = '#4a5a6a';
            if (_isPipeline) {
              _kind = 'pipeline'; _kindColor = '#4f9ad0';
            } else if (_act.some(function (d) { return d[0] === 'critic'; })) {
              _kind = 'critic'; _kindColor = '#e09030';
            } else if (_act.some(function (d) { return d[0] === 'coder'; })) {
              _kind = 'agentic'; _kindColor = '#20b0a0';
            } else if (_act.some(function (d) { return d[0] === 'planner'; })) {
              _kind = 'plan'; _kindColor = '#9a74dc';
            } else if (/direct|chat/i.test(String(r.mode || ''))) {
              _kind = 'chat'; _kindColor = '#60a0e0';
            }
            var _tag = function (t) {
              var s = String(t || '').replace(/^.*[\\/]/, '').replace(/\.gguf$/i, '');
              return s.length > 26 ? s.slice(0, 25) + '\u2026' : s;
            };
            var _present = _act.map(function (d) {
              var v = models[d[0]];
              if (Array.isArray(v)) v = v[0];
              return v ? [d[0], v, d[1], d[2]] : null;
            }).filter(Boolean);
            var _unique = {};
            _present.forEach(function (p) { _unique[p[1]] = 1; });
            var _modelLine = '';
            if (_present.length && Object.keys(_unique).length === 1) {
              _modelLine = '<div style="font-size:8px;color:#8fa4b8;margin-top:1px" title="all roles: ' + esc(_present[0][1]) + '">' +
                'all roles: ' + esc(_tag(_present[0][1])) + '</div>';
            } else if (_present.length) {
              _modelLine = '<div style="font-size:8px;margin-top:1px;display:flex;flex-wrap:wrap;gap:8px">' +
                _present.map(function (p) {
                  return '<span title="' + esc(p[0]) + ': ' + esc(p[1]) + '" style="white-space:nowrap">' +
                    '<span style="color:' + p[3] + ';font-weight:600">' + p[2] + '</span> ' +
                    '<span style="color:#9ab0c4">' + esc(_tag(p[1])) + '</span></span>';
                }).join('') + '</div>';
            }
            var _meta = [];
            var _rIn = r.prompt_tokens || 0, _rCa = r.cached_tokens || 0;
            if (_rIn > 0) _meta.push('<span style="color:#6a7a8a">in</span> <span style="color:#9a74dc">' + _fmtTokens(_rIn) + '</span>');
            if (_rCa > 0) {
              var _rHit = _rIn > 0 ? Math.round((_rCa / _rIn) * 100) : 0;
              _meta.push('<span style="color:#6a7a8a">cached</span> <span style="color:#3a9960">' + _fmtTokens(_rCa) + ' (' + _rHit + '%)</span>');
              var _rNew = Math.max(0, _rIn - _rCa);
              if (_rNew > 0) _meta.push('<span style="color:#6a7a8a">recomp</span> <span style="color:#60c080">' + _fmtTokens(_rNew) + '</span>');
            }
            if (r.requests) _meta.push('<span style="color:#6a7a8a">' + r.requests + ' reqs</span>');
            var _pm = r.phases_ms || {};
            var _cSec = (_pm.coder || 0) / 1000;
            if (r.tokens && _cSec > 0) {
              var _rtps = r.tokens / _cSec;
              _meta.push('<span style="color:' + (_rtps >= 20 ? '#3a9960' : (_rtps >= 10 ? '#c0a020' : '#a06060')) +
                         ';font-weight:600">' + _rtps.toFixed(1) + ' tok/s</span>');
            }
            var _ph = r.phases || {};
            if (_ph.planner) _meta.push('<span style="color:#6a7a8a">P</span> <span style="color:#9ab0c4">' + _fmtTokens(_ph.planner) + 't</span>');
            if (_ph.coder) _meta.push('<span style="color:#6a7a8a">C</span> <span style="color:#9ab0c4">' + _fmtTokens(_ph.coder) + 't</span>');
            if (_ph.critic) _meta.push('<span style="color:#6a7a8a">K</span> <span style="color:#9ab0c4">' + _fmtTokens(_ph.critic) + 't</span>');
            if (_pm.planner || _cSec) {
              var _dur = [];
              if (_pm.planner) _dur.push(_fmtDuration(_pm.planner / 1000));
              if (_cSec) _dur.push(_fmtDuration(_cSec));
              _meta.push('<span style="color:#4a6a8a">' + _dur.join(' / ') + '</span>');
            }
            return '<div style="padding:3px 0;border-bottom:1px solid #141c26">' +
              '<div style="display:flex;justify-content:space-between;gap:6px;font-size:9px">' +
                '<span style="color:var(--tx2)">' + (r.t || '') + ' \u00b7 <span style="color:' + srColor + '">' + esc(sr) + '</span>' +
                  ' \u00b7 <span style="color:' + _kindColor + '" title="run type derived from the phases that actually ran' +
                  (r.mode ? ' (mode=' + esc(r.mode) + ')' : '') + '">' + _kind + '</span>' +
                '</span>' +
                '<span style="color:#60a0e0;font-weight:600;white-space:nowrap;flex:0 0 auto">' + _fmtTokens(r.tokens) + ' \u00b7 ' + _fmtDuration(r.elapsed_s || 0) + '</span>' +
              '</div>' +
              _modelLine +
              (_meta.length ? '<div style="font-size:8px;color:#7a8fa8;display:flex;flex-wrap:wrap;gap:6px;margin-top:1px">' + _meta.join('') + '</div>' : '') +
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

function setTokenStatsModelFilter(idx) {
  // Klick auf ein Modell in der By-model-Liste filtert die Runs darunter
  if (idx === null || idx === undefined || idx === -1) {
    S.tokenStatsModelFilter = '';
  } else {
    const list = S.tokenStatsModels || [];
    const tag = list[idx];
    S.tokenStatsModelFilter = (S.tokenStatsModelFilter === tag) ? '' : (tag || '');
  }
  loadTokenStats();
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
          '<input type="checkbox" ' + (sk.enabled ? 'checked' : '') + '>' +
          '<span class="sl"></span>' +
        '</label>';
      const _cb = row.querySelector('input[type="checkbox"]');
      if (_cb) _cb.addEventListener('change', function() { toggleSkill(this, sk.name); });
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
    if (!data.ok) { el.checked = !el.checked; showInfo('⚠ Skill toggle failed'); }
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
      body: JSON.stringify({answer: answer, decision_id: (S.activeDecisionId || '')})
    });
    S.activeDecisionId = '';
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


// -- Automations ------------------------------------------------
var _autoJobs = [];
var _autoEditingId = null;
var _autoCapabilities = {};

function _autoScheduleLabel(s) {
  s = s || {};
  if (s.kind === 'daily') {
    var ds = Array.isArray(s.days) && s.days.length ? ' (' + s.days.map(function(d){ return ['Mo','Tu','We','Th','Fr','Sa','Su'][d] || d; }).join(',') + ')' : '';
    return 'daily ' + (s.at_hm || '09:00') + ds;
  }
  if (s.kind === 'once') return 'once in ' + Math.round((s.interval_s || 0) / 60) + 'm';
  return 'every ' + Math.round((s.interval_s || 0) / 60) + 'm';
}

function _autoCountdown(ts) {
  if (!ts) return 'never';
  var diff = Math.round(Number(ts) - Date.now() / 1000);
  if (diff <= 0) return 'due now';
  if (diff < 60) return 'in ' + diff + 's';
  if (diff < 3600) return 'in ' + Math.round(diff / 60) + 'm';
  var h = Math.floor(diff / 3600);
  var m = Math.round((diff % 3600) / 60);
  return 'in ' + h + 'h' + (m > 0 ? ' ' + m + 'm' : '');
}

function _autoNextLabel(ts) {
  if (!ts) return 'never';
  try {
    return new Date(Number(ts) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) + ' (' + _autoCountdown(ts) + ')';
  } catch (e) { return '?'; }
}

function _autoModeBadgeInfo(mode) {
  var m = String(mode || 'simple').toLowerCase();
  if (m === 'agentic' || m === 'code_duo') return {label: '\u26A1 Agentic', cls: 'mode-agentic'};
  if (m === 'code_critic') return {label: '\u21C4 Code+Critic', cls: 'mode-critic'};
  if (m === 'pipeline') return {label: '\uD83D\uDD17 Pipeline', cls: ''};
  if (m === 'auto') return {label: '\uD83E\uDDED Auto', cls: ''};
  return {label: '\uD83D\uDCAC Direct', cls: 'mode-direct'};
}

function _autoTierBadgeInfo(tier) {
  var t = String(tier || 'readonly').toLowerCase();
  if (t === 'mcp') return {label: '\uD83D\uDD0C MCP + Web', cls: 'tier-mcp'};
  if (t === 'full') return {label: '\uD83D\uDD27 Full + MCP', cls: 'tier-full'};
  if (t === 'python') return {label: '\uD83D\uDC0D Python', cls: ''};
  if (t === 'research') return {label: '\uD83D\uDD0D Research', cls: ''};
  if (t === 'off') return {label: '\uD83D\uDEAB No Tools', cls: ''};
  return {label: '\uD83C\uDF10 Web Only', cls: ''};
}

function _renderAutoCapBar(caps) {
  caps = caps || {};
  var mcpEl = document.getElementById('auto-cap-mcp');
  var tgEl = document.getElementById('auto-cap-tg');
  if (mcpEl) {
    var srvs = Array.isArray(caps.mcp_servers) ? caps.mcp_servers : [];
    if (caps.mcp_enabled && srvs.length) {
      mcpEl.className = 'auto-cap-pill ok';
      mcpEl.textContent = '\uD83D\uDD0C MCP: ' + srvs.length + ' (' + srvs.slice(0, 2).join(', ') + (srvs.length > 2 ? '…' : '') + ')';
    } else if (caps.mcp_enabled) {
      mcpEl.className = 'auto-cap-pill warn';
      mcpEl.textContent = '\uD83D\uDD0C MCP: ON (no ready servers)';
    } else {
      mcpEl.className = 'auto-cap-pill';
      mcpEl.textContent = '\uD83D\uDD0C MCP: OFF';
    }
  }
  if (tgEl) {
    if (caps.telegram_configured && caps.telegram_paired) {
      tgEl.className = 'auto-cap-pill ok';
      tgEl.textContent = '\uD83D\uDCF1 Telegram: Ready';
    } else if (caps.telegram_configured) {
      tgEl.className = 'auto-cap-pill warn';
      tgEl.textContent = '\uD83D\uDCF1 Telegram: Not paired';
    } else {
      tgEl.className = 'auto-cap-pill';
      tgEl.textContent = '\uD83D\uDCF1 Telegram: Not configured';
    }
  }
}

function automationSyncModeHint() {
  var box = document.getElementById('auto-mode-hint');
  if (!box) return;
  var mode = (_autoField('auto-mode') || 'simple').toLowerCase();
  var tier = (_autoField('auto-tier') || 'mcp').toLowerCase();
  var modeDesc = mode === 'agentic'
    ? '\u26A1 Agentic: Autonomous multi-step loop for research, scripts, files & complex MCP workflows.'
    : mode === 'code_critic'
      ? '\u21C4 Code+Critic: Coder + Reviewer pair (experimental for automations).'
      : mode === 'pipeline'
        ? '\uD83D\uDD17 Pipeline: Multi-model analyst \u2192 refiner \u2192 critic \u2192 synthesizer.'
        : mode === 'auto'
          ? '\uD83E\uDDED Auto: Router picks Direct vs Agentic dynamically.'
          : '\uD83D\uDCAC Direct: Fast single-model turn — ideal for recurring MCP summaries & notifications.';
  var tierDesc = tier === 'mcp'
    ? '\uD83D\uDD0C MCP + Web + Read enabled (Mail, Calendar, connected MCP APIs; no shell/write).'
    : tier === 'full'
      ? '\uD83D\uDD27 Full access: MCP tools + file edits + Python + bash.'
      : tier === 'off'
        ? '\uD83D\uDEAB Tools OFF: pure text generation only.'
        : tier === 'python'
          ? '\uD83D\uDC0D Python + workspace read + web (no MCP).'
          : tier === 'research'
            ? '\uD83D\uDD0D Read workspace + web search (no MCP).'
            : '\uD83C\uDF10 Websearch + dialog only (no MCP — switch to MCP/Full for Mail/API tools).';
  box.textContent = modeDesc + ' ' + tierDesc;
}

function automationApplyTemplate(tplKey) {
  automationNew();
  var tpls = {
    mail_mcp_tg: {
      name: 'MCP Mail Summary',
      kind: 'every', interval: '30',
      mode: 'simple', tier: 'mcp', notify_tg: true,
      prompt: 'Check my unread emails from the last 30 minutes using the connected MCP mail tools. Summarize important messages with sender, subject, and concrete action items concisely.'
    },
    agentic_mcp_tg: {
      name: 'Agentic MCP Triage',
      kind: 'every', interval: '60',
      mode: 'agentic', tier: 'full', notify_tg: true,
      prompt: 'Inspect incoming tasks/messages via connected MCP tools, cross-check relevant workspace notes/files, execute any safe follow-up checks, and send a concise status briefing.'
    },
    daily_digest_tg: {
      name: 'Morning Briefing',
      kind: 'daily', time: '08:00',
      mode: 'simple', tier: 'mcp', notify_tg: true,
      prompt: 'Create my morning briefing using MCP calendar/mail tools and web search for today. Highlight urgent deadlines, meetings, and key updates.'
    },
    repo_health: {
      name: 'Daily Repo Health',
      kind: 'daily', time: '09:00',
      mode: 'agentic', tier: 'full', notify_tg: false,
      prompt: 'Run the test suite and check git status/recent logs in the workspace. Summarize any failing tests, regressions, or uncommitted changes.'
    }
  };
  var t = tpls[tplKey];
  if (!t) return;
  _autoField('auto-name', t.name);
  _autoField('auto-prompt', t.prompt);
  _autoField('auto-kind', t.kind);
  if (t.interval) _autoField('auto-interval', t.interval);
  if (t.time) _autoField('auto-time', t.time);
  _autoField('auto-mode', t.mode);
  _autoField('auto-tier', t.tier);
  var tg = document.getElementById('auto-notify-tg');
  if (tg) tg.checked = !!t.notify_tg;
  automationScheduleChanged();
  automationSyncModeHint();
}

async function loadAutomations() {
  const list = document.getElementById('automation-list');
  if (!list) return;
  try {
    const d = await (await fetch('/automations')).json();
    _autoJobs = d.automations || [];
    _autoCapabilities = d.capabilities || {};
    _renderAutoCapBar(_autoCapabilities);
    if (!_autoJobs.length) {
      list.innerHTML = '<div class="empty">No automations yet. Pick a template above or create one below.</div>';
      return;
    }
    list.innerHTML = '';
    _autoJobs.forEach(function(job) {
      const card = document.createElement('div');
      card.className = 'auto-card' + (job.enabled ? '' : ' paused');

      const head = document.createElement('div');
      head.className = 'auto-card-head';
      const dot = document.createElement('span');
      dot.className = 'auto-status-dot';
      dot.style.background = !job.enabled ? 'var(--tx3)'
        : (job.last_status === 'ok' ? 'var(--green)'
        : (job.last_status === 'needs_input' || job.last_status === 'partial' || job.last_status === 'queued' ? '#e0a030'
        : (job.last_status === 'failed' || job.last_status === 'failed_chat' ? '#c05050' : 'var(--tx2)')));
      const title = document.createElement('div');
      title.className = 'auto-card-title';
      title.textContent = job.name || 'Automation';
      head.appendChild(dot);
      head.appendChild(title);

      const prev = document.createElement('div');
      prev.className = 'auto-card-prompt';
      prev.textContent = job.prompt || '';

      const badges = document.createElement('div');
      badges.className = 'auto-badges';
      function addBadge(text, extraCls) {
        const b = document.createElement('span');
        b.className = 'auto-badge' + (extraCls ? ' ' + extraCls : '');
        b.textContent = text;
        badges.appendChild(b);
      }
      var mb = _autoModeBadgeInfo(job.mode);
      addBadge(mb.label, mb.cls);
      var tb = _autoTierBadgeInfo(job.tools_tier);
      addBadge(tb.label, tb.cls);
      addBadge(job.notify_telegram ? '\uD83D\uDCF1 Telegram' : '\uD83D\uDCAC Local Chat', job.notify_telegram ? 'tg-on' : '');
      addBadge('\u23F1 ' + _autoScheduleLabel(job.schedule), '');
      if (job.workspace) {
        var wsName = String(job.workspace).replace(/\\/g, '/').split('/').filter(Boolean).pop() || job.workspace;
        addBadge('\uD83D\uDCC1 ' + wsName, '');
      }

      const meta = document.createElement('div');
      meta.className = 'auto-meta-row';
      var durTxt = (job.last_duration_s !== null && job.last_duration_s !== undefined) ? (' (' + job.last_duration_s + 's)') : '';
      var lastTxt = job.last_status ? (' \u00B7 last: ' + job.last_status + durTxt) : '';
      var firesTxt = job.fires ? (' \u00B7 ' + job.fires + 'x') : '';
      meta.textContent = (job.enabled ? ('Next: ' + _autoNextLabel(job.next_ts)) : 'Paused') + lastTxt + firesTxt;

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:4px;margin-top:4px;flex-wrap:wrap';
      function mk(label, fn, cls, titleTxt) {
        const b = document.createElement('button');
        b.textContent = label;
        b.className = cls || 'pload';
        b.style.marginRight = '0';
        if (titleTxt) b.title = titleTxt;
        b.onclick = function(e) { e.stopPropagation(); fn(); };
        actions.appendChild(b);
      }
      mk('\u25B6 Run', function() { automationRun(job.id); }, null, 'Run now');
      if (job.chat_id) {
        mk('\uD83D\uDCAC Chat', function() { loadChat(job.chat_id); }, null, 'Open automation chat transcript');
      }
      mk('\u270E Edit', function() { automationEdit(job.id); });
      mk('Runs', function() { automationShowRuns(job.id); });
      mk(job.enabled ? '\u23F8 Pause' : '\u25B6 Enable', function() { automationToggle(job.id); });
      mk('\u00D7', function() { automationDelete(job.id); }, 'pdel', 'Delete automation');

      card.appendChild(head);
      card.appendChild(prev);
      card.appendChild(badges);
      card.appendChild(meta);
      card.appendChild(actions);
      list.appendChild(card);
    });
  } catch (e) {
    list.innerHTML = '<div class="empty">Could not load automations.</div>';
  }
}

var _autoDays = [];

function _buildDayChips() {
  const row = document.getElementById('auto-days-row');
  if (!row) return;
  row.innerHTML = '';
  ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].forEach(function(label, i) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.textContent = label;
    chip.className = 'pload';
    chip.style.marginRight = '0';
    chip.style.color = _autoDays.indexOf(i) >= 0 ? 'var(--amber)' : '';
    chip.onclick = function() {
      const at = _autoDays.indexOf(i);
      if (at >= 0) _autoDays.splice(at, 1); else _autoDays.push(i);
      chip.style.color = _autoDays.indexOf(i) >= 0 ? 'var(--amber)' : '';
      automationPreview();
    };
    row.appendChild(chip);
  });
}

function automationScheduleChanged() {
  const kind = (document.getElementById('auto-kind') || {}).value;
  const ir = document.getElementById('auto-interval-row');
  const tr = document.getElementById('auto-time-row');
  if (ir) ir.style.display = kind === 'daily' ? 'none' : 'flex';
  if (tr) tr.style.display = kind === 'daily' ? 'block' : 'none';
  if (kind === 'daily') { _buildDayChips(); }
  automationPreview();
}

function _readAutoSchedule() {
  const kind = _autoField('auto-kind') || 'every';
  if (kind === 'daily') {
    return {kind: 'daily', at_hm: _autoField('auto-time') || '09:00', days: _autoDays.slice()};
  }
  const minutes = Math.max(1, parseInt(_autoField('auto-interval'), 10) || 60);
  return {kind: kind, interval_s: minutes * 60};
}

async function automationPreview() {
  const box = document.getElementById('auto-preview');
  if (!box) return;
  const schedule = _readAutoSchedule();
  try {
    const r = await fetch('/automations/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({schedule: schedule}),
    });
    if (!r.ok) { box.textContent = ''; return; }
    const d = await r.json().catch(function() { return {}; });
    const times = (d.next || []).map(function(ts) {
      return new Date(Number(ts) * 1000).toLocaleString();
    });
    box.textContent = times.length
      ? ('next: ' + times.join(' · ')) : '';
  } catch (e) { box.textContent = ''; }
}

function _autoShowEditor(show) {
  const el = document.getElementById('automation-editor');
  if (!el) return;
  el.style.display = show ? 'block' : 'none';
  // EDITOR-BELOW-FOLD (audit 2026-11-05): with a long job list the editor
  // sits under the fold - "+ New automation" looked like a no-op.
  if (show) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function _autoField(id, value) {
  const e = document.getElementById(id);
  if (e && arguments.length > 1) e.value = value;
  return e ? e.value : '';
}

function automationNew() {
  _autoEditingId = null;
  _autoField('auto-name', '');
  _autoField('auto-prompt', '');
  _autoField('auto-kind', 'every');
  _autoField('auto-interval', '30');
  _autoField('auto-time', '09:00');
  _autoDays = [];
  _autoField('auto-mode', 'simple');
  _autoField('auto-tier', 'mcp');
  var tg = document.getElementById('auto-notify-tg'); if (tg) tg.checked = false;
  _autoField('auto-workspace', '');
  _autoField('auto-policy', 'skip');
  const en = document.getElementById('auto-enabled'); if (en) en.checked = true;
  const title = document.getElementById('automation-editor-title');
  if (title) title.textContent = 'New automation';
  automationScheduleChanged();
  automationSyncModeHint();
  _autoShowEditor(true);
}

function automationEdit(jobId) {
  const job = _autoJobs.find(function(j) { return j.id === jobId; });
  if (!job) return;
  _autoEditingId = jobId;
  const s = job.schedule || {};
  _autoField('auto-name', job.name || '');
  _autoField('auto-prompt', job.prompt || '');
  _autoField('auto-kind', s.kind === 'daily' ? 'daily' : (s.kind === 'once' ? 'once' : 'every'));
  _autoField('auto-interval', String(Math.max(1, Math.round((s.interval_s || 3600) / 60))));
  _autoField('auto-time', s.at_hm || '09:00');
  _autoDays = Array.isArray(s.days) ? s.days.slice() : [];
  var m = String(job.mode || 'simple').toLowerCase();
  if (m === 'code_duo') m = 'agentic';
  if (m === 'direct') m = 'simple';
  _autoField('auto-mode', m);
  _autoField('auto-tier', job.tools_tier || 'readonly');
  var tg = document.getElementById('auto-notify-tg'); if (tg) tg.checked = !!job.notify_telegram;
  _autoField('auto-workspace', job.workspace || '');
  _autoField('auto-policy', job.policy || 'skip');
  const en = document.getElementById('auto-enabled'); if (en) en.checked = !!job.enabled;
  const title = document.getElementById('automation-editor-title');
  if (title) title.textContent = 'Edit automation';
  automationScheduleChanged();
  automationSyncModeHint();
  _autoShowEditor(true);
}

function automationCancel() {
  _autoEditingId = null;
  _autoShowEditor(false);
}

async function automationSave() {
  const schedule = _readAutoSchedule();
  const prompt = _autoField('auto-prompt').trim();
  if (!prompt) { showStatus('⚠ A prompt is required.'); return; }
  const payload = {
    name: _autoField('auto-name') || 'Automation',
    prompt: prompt,
    schedule: schedule,
    mode: _autoField('auto-mode') || 'simple',
    tools_tier: _autoField('auto-tier') || 'mcp',
    notify_telegram: !!(document.getElementById('auto-notify-tg') || {}).checked,
    workspace: _autoField('auto-workspace') || '',
    policy: _autoField('auto-policy') || 'skip',
    enabled: !!(document.getElementById('auto-enabled') || {}).checked,
  };
  try {
    const url = _autoEditingId ? '/automations/' + _autoEditingId : '/automations';
    const r = await fetch(url, {
      method: _autoEditingId ? 'PUT' : 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(function() { return {}; });
    if (!r.ok) { showStatus('⚠ ' + (d.error || ('HTTP ' + r.status))); return; }
    _autoEditingId = null;
    _autoShowEditor(false);
    showStatus('Automation saved.');
    loadAutomations();
  } catch (e) {
    showStatus('Saving automation failed: ' + e.message);
  }
}

async function automationRun(jobId) {
  try {
    const r = await fetch('/automations/' + jobId + '/run', {method: 'POST'});
    const d = await r.json().catch(function() { return {}; });
    showStatus(d.ok ? '▶ Automation started.' : ('⚠ ' + (d.note || 'not started')));
    loadAutomations();
  } catch (e) { showStatus('Run failed: ' + e.message); }
}

async function automationToggle(jobId) {
  try {
    const r = await fetch('/automations/' + jobId + '/toggle', {method: 'POST'});
    if (!r.ok) {
      const d = await r.json().catch(function() { return {}; });
      showStatus('⚠ ' + (d.error || ('HTTP ' + r.status)));
      return;
    }
    loadAutomations();
  } catch (e) { showStatus('Toggle failed: ' + e.message); }
}

async function automationDelete(jobId) {
  if (!confirm('Delete this automation? Its chat history stays.')) return;
  try {
    const r = await fetch('/automations/' + jobId, {method: 'DELETE'});
    if (!r.ok) {
      const d = await r.json().catch(function() { return {}; });
      showStatus('⚠ ' + (d.error || ('HTTP ' + r.status)));
      return;
    }
    showStatus('Automation deleted.');
    const _runsBox = document.getElementById('automation-runs');
    if (_runsBox) _runsBox.innerHTML = '';
    loadAutomations();
  } catch (e) { showStatus('Delete failed: ' + e.message); }
}

async function automationShowRuns(jobId) {
  const box = document.getElementById('automation-runs');
  if (!box) return;
  try {
    const d = await (await fetch('/automations/' + jobId + '/runs')).json();
    const runs = d.runs || [];
    if (!runs.length) { box.innerHTML = '<div class="empty">No runs yet.</div>'; return; }
    box.innerHTML = '<div class="sec">Recent runs</div>';
    runs.slice().reverse().forEach(function(run) {
      const row = document.createElement('div');
      row.className = 'chat-hist-meta';
      row.style.marginBottom = '4px';
      var icon = run.status === 'ok' ? '\u2705' : (run.status === 'needs_input' ? '\u2753' : '\u26A0');
      var dur = (run.duration_s !== undefined && run.duration_s !== null)
        ? (' (' + run.duration_s + 's)')
        : (run.finished && run.started ? (' (' + (Number(run.finished) - Number(run.started)).toFixed(1) + 's)') : '');
      row.textContent = icon + ' ' + (run.status || '?') + dur + ' \u00B7 '
        + (run.stop_reason || '') + ' \u00B7 '
        + new Date(Number(run.started || run.ts || 0) * 1000).toLocaleString();
      box.appendChild(row);
    });
  } catch (e) { box.innerHTML = '<div class="empty">Could not load runs.</div>'; }
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

