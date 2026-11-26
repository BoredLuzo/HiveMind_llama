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
