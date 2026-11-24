// HiveMind UI module: C3 Advanced Settings drawer & Model Profiles modal.
// ── Advanced / Engine Settings Drawer (C3) ──────────────────────────────────
var _advSettingsSchema = null;

function _advDeepEqual(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return a === b;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (!_advDeepEqual(a[i], b[i])) return false;
    }
    return true;
  }
  if (typeof a === 'object') {
    var ka = Object.keys(a).sort();
    var kb = Object.keys(b).sort();
    if (ka.length !== kb.length) return false;
    for (var j = 0; j < ka.length; j++) {
      if (ka[j] !== kb[j] || !_advDeepEqual(a[ka[j]], b[kb[j]])) return false;
    }
    return true;
  }
  return false;
}

function _advSetDrawerStatus(msg, isErr) {
  var st = document.getElementById('adv-settings-status');
  if (!st) return;
  st.textContent = msg || '';
  st.style.color = isErr ? '#c06060' : 'var(--tx2, #7a8fa8)';
}

function _advScopeBadgeStyle(mode) {
  if (mode === 'restart') {
    return 'color:#c06060;border:1px solid rgba(192,96,96,.4);background:rgba(192,96,96,.08)';
  }
  if (mode === 'next_load') {
    return 'color:#e09030;border:1px solid rgba(224,144,48,.4);background:rgba(224,144,48,.08)';
  }
  return 'color:#3a9960;border:1px solid rgba(58,153,96,.4);background:rgba(58,153,96,.08)';
}

async function _advSaveEntry(entry, newVal, rowStatusEl, modBadgeEl, resetBtnEl) {
  if (rowStatusEl) {
    rowStatusEl.textContent = 'Saving...';
    rowStatusEl.style.color = 'var(--tx2, #7a8fa8)';
  }
  try {
    var patch = {};
    patch[entry.key] = newVal;
    await postSettings(patch);
    entry.value = newVal;
    var modified = !_advDeepEqual(entry.value, entry.default);
    if (modBadgeEl) modBadgeEl.style.display = modified ? 'inline-block' : 'none';
    if (resetBtnEl) resetBtnEl.disabled = !modified;
    var scopeNote = entry.apply_mode === 'restart'
      ? 'Saved (restart required)'
      : (entry.apply_mode === 'next_load' ? 'Saved (next load)' : 'Saved');
    if (rowStatusEl) {
      rowStatusEl.textContent = scopeNote;
      rowStatusEl.style.color = '#3a9960';
    }
    _advSetDrawerStatus(entry.key + ': ' + scopeNote, false);
  } catch (e) {
    console.warn('[adv-settings] save failed for ' + entry.key + ':', e);
    if (rowStatusEl) {
      rowStatusEl.textContent = 'Save failed: ' + (e && e.message || e);
      rowStatusEl.style.color = '#c06060';
    }
    _advSetDrawerStatus('Save failed for ' + entry.key, true);
  }
}

function _advBuildInput(entry, rowStatusEl, modBadgeEl, resetBtnEl) {
  var wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;gap:6px;align-items:center;flex-wrap:wrap';
  var hasChoices = Array.isArray(entry.choices) && entry.choices.length > 0;

  if (hasChoices) {
    var sel = document.createElement('select');
    sel.className = 'fld-input';
    sel.style.cssText = 'flex:1;min-width:140px;padding:4px 6px;font-size:10.5px';
    if (entry.nullable) {
      var nullOpt = document.createElement('option');
      nullOpt.value = '__null__';
      nullOpt.textContent = '(null / auto)';
      sel.appendChild(nullOpt);
    }
    var seenVals = new Set();
    entry.choices.forEach(function(ch) {
      var sv = String(ch);
      seenVals.add(sv);
      var opt = document.createElement('option');
      opt.value = sv;
      opt.textContent = sv;
      sel.appendChild(opt);
    });
    if (entry.value != null && !seenVals.has(String(entry.value))) {
      var curOpt = document.createElement('option');
      curOpt.value = String(entry.value);
      curOpt.textContent = String(entry.value);
      sel.appendChild(curOpt);
    }
    sel.value = (entry.value == null && entry.nullable) ? '__null__' : String(entry.value != null ? entry.value : '');
    sel.addEventListener('change', function() {
      var v = (entry.nullable && sel.value === '__null__') ? null : sel.value;
      _advSaveEntry(entry, v, rowStatusEl, modBadgeEl, resetBtnEl);
    });
    wrap.appendChild(sel);
    return { wrap: wrap, syncValue: function(v) { sel.value = (v == null && entry.nullable) ? '__null__' : String(v != null ? v : ''); } };
  }

  if (entry.type === 'bool') {
    if (entry.nullable) {
      var bsel = document.createElement('select');
      bsel.className = 'fld-input';
      bsel.style.cssText = 'width:auto;min-width:110px;padding:4px 6px;font-size:10.5px';
      [['__null__', 'auto (null)'], ['true', 'true'], ['false', 'false']].forEach(function(pair) {
        var o = document.createElement('option');
        o.value = pair[0];
        o.textContent = pair[1];
        bsel.appendChild(o);
      });
      bsel.value = entry.value === true ? 'true' : (entry.value === false ? 'false' : '__null__');
      bsel.addEventListener('change', function() {
        var v = bsel.value === '__null__' ? null : (bsel.value === 'true');
        _advSaveEntry(entry, v, rowStatusEl, modBadgeEl, resetBtnEl);
      });
      wrap.appendChild(bsel);
      return { wrap: wrap, syncValue: function(v) { bsel.value = v === true ? 'true' : (v === false ? 'false' : '__null__'); } };
    }
    var lbl = document.createElement('label');
    lbl.className = 'cfl-sw';
    lbl.style.margin = '0';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!entry.value;
    var sl = document.createElement('span');
    sl.className = 'sl';
    lbl.appendChild(cb);
    lbl.appendChild(sl);
    cb.addEventListener('change', function() {
      _advSaveEntry(entry, !!cb.checked, rowStatusEl, modBadgeEl, resetBtnEl);
    });
    wrap.appendChild(lbl);
    return { wrap: wrap, syncValue: function(v) { cb.checked = !!v; } };
  }

  if (entry.type === 'int' || entry.type === 'float') {
    var num = document.createElement('input');
    num.type = 'number';
    num.className = 'fld-input';
    num.style.cssText = 'flex:1;min-width:110px;padding:4px 6px;font-size:10.5px';
    if (entry.min != null) num.min = String(entry.min);
    if (entry.max != null) num.max = String(entry.max);
    if (entry.step != null) num.step = String(entry.step);
    else num.step = entry.type === 'int' ? '1' : 'any';
    if (entry.nullable) num.placeholder = 'null (auto)';
    num.value = (entry.value === null || entry.value === undefined) ? '' : String(entry.value);
    num.addEventListener('change', function() {
      var raw = num.value.trim();
      if (raw === '') {
        if (entry.nullable) {
          _advSaveEntry(entry, null, rowStatusEl, modBadgeEl, resetBtnEl);
        } else {
          if (rowStatusEl) {
            rowStatusEl.textContent = 'Value required';
            rowStatusEl.style.color = '#c06060';
          }
        }
        return;
      }
      var numVal = Number(raw);
      var parsed = entry.type === 'int' ? parseInt(raw, 10) : numVal;
      if (!isFinite(numVal) || (entry.type === 'int' && !Number.isInteger(numVal))) {
        if (rowStatusEl) {
          rowStatusEl.textContent = 'Invalid ' + entry.type;
          rowStatusEl.style.color = '#c06060';
        }
        return;
      }
      if (entry.min != null && parsed < entry.min) {
        if (rowStatusEl) { rowStatusEl.textContent = 'Min ' + entry.min; rowStatusEl.style.color = '#c06060'; }
        return;
      }
      if (entry.max != null && parsed > entry.max) {
        if (rowStatusEl) { rowStatusEl.textContent = 'Max ' + entry.max; rowStatusEl.style.color = '#c06060'; }
        return;
      }
      _advSaveEntry(entry, parsed, rowStatusEl, modBadgeEl, resetBtnEl);
    });
    wrap.appendChild(num);
    if (entry.nullable) {
      var clrBtn = document.createElement('button');
      clrBtn.type = 'button';
      clrBtn.className = 'btn';
      clrBtn.style.cssText = 'padding:2px 6px;font-size:9px';
      clrBtn.textContent = 'Null';
      clrBtn.addEventListener('click', function() {
        num.value = '';
        _advSaveEntry(entry, null, rowStatusEl, modBadgeEl, resetBtnEl);
      });
      wrap.appendChild(clrBtn);
    }
    return { wrap: wrap, syncValue: function(v) { num.value = (v === null || v === undefined) ? '' : String(v); } };
  }

  if (entry.type === 'object' || entry.type === 'list') {
    var ta = document.createElement('textarea');
    ta.className = 'fld-input';
    ta.rows = 3;
    ta.style.cssText = 'width:100%;padding:5px 7px;font-size:10px';
    ta.value = JSON.stringify(entry.value != null ? entry.value : (entry.type === 'list' ? [] : {}), null, 2);
    var applyJson = function() {
      try {
        var parsed = JSON.parse(ta.value);
        if (entry.type === 'list' && !Array.isArray(parsed)) {
          throw new Error('Expected JSON array');
        }
        if (entry.type === 'object' && (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))) {
          throw new Error('Expected JSON object');
        }
        _advSaveEntry(entry, parsed, rowStatusEl, modBadgeEl, resetBtnEl);
      } catch (e) {
        if (rowStatusEl) {
          rowStatusEl.textContent = 'Invalid JSON: ' + (e && e.message || e);
          rowStatusEl.style.color = '#c06060';
        }
      }
    };
    ta.addEventListener('change', applyJson);
    var saveJsonBtn = document.createElement('button');
    saveJsonBtn.type = 'button';
    saveJsonBtn.className = 'btn';
    saveJsonBtn.style.cssText = 'padding:3px 8px;font-size:9.5px';
    saveJsonBtn.textContent = 'Apply JSON';
    saveJsonBtn.addEventListener('click', applyJson);
    wrap.appendChild(ta);
    wrap.appendChild(saveJsonBtn);
    return {
      wrap: wrap,
      syncValue: function(v) {
        ta.value = JSON.stringify(v != null ? v : (entry.type === 'list' ? [] : {}), null, 2);
      }
    };
  }

  var txt = document.createElement('input');
  txt.type = 'text';
  txt.className = 'fld-input';
  txt.style.cssText = 'flex:1;min-width:140px;padding:4px 6px;font-size:10.5px';
  if (entry.nullable) txt.placeholder = 'null (auto)';
  txt.value = (entry.value === null || entry.value === undefined) ? '' : String(entry.value);
  txt.addEventListener('change', function() {
    var v = (entry.nullable && txt.value === '') ? null : txt.value;
    _advSaveEntry(entry, v, rowStatusEl, modBadgeEl, resetBtnEl);
  });
  wrap.appendChild(txt);
  return { wrap: wrap, syncValue: function(v) { txt.value = (v === null || v === undefined) ? '' : String(v); } };
}

function renderAdvancedSettingsList() {
  var listEl = document.getElementById('adv-settings-list');
  var countEl = document.getElementById('adv-settings-count');
  if (!listEl) return;
  if (!_advSettingsSchema || !Array.isArray(_advSettingsSchema.entries)) {
    listEl.innerHTML = '';
    if (countEl) countEl.textContent = 'not loaded';
    return;
  }
  var searchEl = document.getElementById('adv-settings-search');
  var groupEl = document.getElementById('adv-settings-group');
  var modOnlyEl = document.getElementById('adv-settings-modified-only');
  var q = searchEl ? searchEl.value.toLowerCase().trim() : '';
  var tokens = q ? q.split(/\s+/).filter(Boolean) : [];
  var grp = groupEl ? groupEl.value : '';
  var modOnly = !!(modOnlyEl && modOnlyEl.checked);

  var allEntries = _advSettingsSchema.entries;
  var filtered = allEntries.filter(function(entry) {
    if (grp && entry.group !== grp) return false;
    var isMod = !_advDeepEqual(entry.value, entry.default);
    if (modOnly && !isMod) return false;
    if (tokens.length) {
      var hay = ((entry.key || '') + ' ' + (entry.group || '') + ' ' + (entry.description || '')).toLowerCase();
      if (!tokens.every(function(tok) { return hay.indexOf(tok) >= 0; })) return false;
    }
    return true;
  });

  if (countEl) {
    countEl.textContent = filtered.length + ' / ' + allEntries.length + ' keys';
  }
  listEl.innerHTML = '';
  if (!filtered.length) {
    var empty = document.createElement('div');
    empty.className = 'fld-hint';
    empty.textContent = 'No matching settings.';
    listEl.appendChild(empty);
    return;
  }

  var frag = document.createDocumentFragment();
  filtered.forEach(function(entry) {
    var isMod = !_advDeepEqual(entry.value, entry.default);
    var row = document.createElement('div');
    row.className = 'adv-setting-row';
    row.style.cssText = 'border:1px solid var(--b,#1e2435);border-radius:5px;padding:7px 9px;background:var(--bg2,#10131a)';
    if (entry.description) row.title = entry.description;

    var head = document.createElement('div');
    head.style.cssText = 'display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px';

    var keyLbl = document.createElement('span');
    keyLbl.style.cssText = "font-family:'IBM Plex Mono',monospace;font-size:10.5px;font-weight:700;color:var(--txh,#e5ecf4);word-break:break-all";
    keyLbl.textContent = entry.key;
    head.appendChild(keyLbl);

    var modBadge = document.createElement('span');
    modBadge.className = 'adv-mod-badge';
    modBadge.textContent = 'Modified';
    modBadge.style.cssText = 'font-size:8.5px;padding:1px 5px;border-radius:3px;border:1px solid rgba(224,144,48,.45);color:#e09030;background:rgba(224,144,48,.1);display:' + (isMod ? 'inline-block' : 'none');
    head.appendChild(modBadge);

    var scopePill = document.createElement('span');
    scopePill.className = 'adv-scope-pill scope-' + (entry.apply_mode || 'live');
    scopePill.textContent = entry.apply_mode || 'live';
    scopePill.style.cssText = 'font-size:8.5px;padding:1px 5px;border-radius:3px;' + _advScopeBadgeStyle(entry.apply_mode || 'live');
    head.appendChild(scopePill);

    var grpSpan = document.createElement('span');
    grpSpan.style.cssText = 'font-size:8.5px;color:var(--tx3,#6b7f94);margin-left:auto';
    grpSpan.textContent = entry.group || 'general';
    head.appendChild(grpSpan);

    var resetBtn = document.createElement('button');
    resetBtn.type = 'button';
    resetBtn.className = 'btn adv-reset-btn';
    resetBtn.textContent = 'Reset';
    resetBtn.disabled = !isMod;
    resetBtn.style.cssText = 'padding:2px 6px;font-size:9px';
    head.appendChild(resetBtn);
    row.appendChild(head);

    if (entry.description) {
      var desc = document.createElement('div');
      desc.className = 'fld-hint';
      desc.style.cssText = 'margin:0 0 6px;font-size:9.5px;line-height:1.4';
      desc.textContent = entry.description;
      row.appendChild(desc);
    }

    var rowStatus = document.createElement('div');
    rowStatus.style.cssText = 'font-size:9px;margin-top:4px;min-height:12px;color:var(--tx2,#7a8fa8)';
    var ctrl = _advBuildInput(entry, rowStatus, modBadge, resetBtn);
    resetBtn.addEventListener('click', function() {
      var defVal = entry.default !== undefined ? JSON.parse(JSON.stringify(entry.default)) : null;
      ctrl.syncValue(defVal);
      _advSaveEntry(entry, defVal, rowStatus, modBadge, resetBtn);
    });

    row.appendChild(ctrl.wrap);
    row.appendChild(rowStatus);
    frag.appendChild(row);
  });
  listEl.appendChild(frag);
}

async function loadAdvancedSettingsSchema() {
  var listEl = document.getElementById('adv-settings-list');
  var countEl = document.getElementById('adv-settings-count');
  if (countEl) countEl.textContent = 'loading...';
  _advSetDrawerStatus('Loading /settings/schema...', false);
  try {
    var resp = await fetch('/settings/schema');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var data = await resp.json();
    if (!data || !data.ok) throw new Error((data && data.error) || 'Invalid schema response');
    _advSettingsSchema = data;
    if (data.settings_rev) S.settingsRev = parseInt(data.settings_rev, 10) || S.settingsRev;
    var groupEl = document.getElementById('adv-settings-group');
    if (groupEl) {
      var prevGrp = groupEl.value;
      groupEl.innerHTML = '<option value="">All groups</option>';
      (data.groups || []).forEach(function(g) {
        var opt = document.createElement('option');
        opt.value = g;
        opt.textContent = g;
        groupEl.appendChild(opt);
      });
      if (prevGrp) groupEl.value = prevGrp;
    }
    _advSetDrawerStatus('', false);
    renderAdvancedSettingsList();
  } catch (e) {
    console.warn('[adv-settings] schema fetch failed:', e);
    _advSetDrawerStatus('Failed to load schema: ' + (e && e.message || e), true);
    if (countEl) countEl.textContent = 'error';
    if (listEl) listEl.innerHTML = '<div class="fld-hint" style="color:#c06060">Failed to load /settings/schema.</div>';
  }
}

function _initAdvancedSettingsDrawer() {
  var card = document.getElementById('adv-engine-settings-card');
  if (!card || card._advWired) return;
  card._advWired = true;
  card.addEventListener('toggle', function() {
    if (card.open) loadAdvancedSettingsSchema();
  });
  var searchEl = document.getElementById('adv-settings-search');
  if (searchEl) searchEl.addEventListener('input', renderAdvancedSettingsList);
  var groupEl = document.getElementById('adv-settings-group');
  if (groupEl) groupEl.addEventListener('change', renderAdvancedSettingsList);
  var modEl = document.getElementById('adv-settings-modified-only');
  if (modEl) modEl.addEventListener('change', renderAdvancedSettingsList);
  var reloadBtn = document.getElementById('adv-settings-reload-btn');
  if (reloadBtn) reloadBtn.addEventListener('click', loadAdvancedSettingsSchema);
}

// ── Model Profiles Modal (C3) ───────────────────────────────────────────────
var _mpState = { currentModel: '', loading: false };

function _mpSetStatus(msg, isErr) {
  var st = document.getElementById('mp-status');
  if (!st) return;
  st.textContent = msg || '';
  st.style.color = isErr ? '#c06060' : 'var(--tx2, #7a8fa8)';
}

function _mpApplySourceBadge(badgeEl, source) {
  if (!badgeEl) return;
  var src = String(source || 'generic');
  badgeEl.textContent = src;
  badgeEl.className = 'cfg-card-badge';
  if (src === 'user') {
    badgeEl.style.cssText = 'font-size:9px;color:#c7a8ff;border:1px solid rgba(168,126,242,.5);background:rgba(168,126,242,.12)';
  } else if (src === 'shipped') {
    badgeEl.style.cssText = 'font-size:9px;color:#70d898;border:1px solid rgba(58,153,96,.45);background:rgba(58,153,96,.1)';
  } else if (src === 'gguf_auto') {
    badgeEl.style.cssText = 'font-size:9px;color:#7cc0ff;border:1px solid rgba(96,160,224,.45);background:rgba(96,160,224,.1)';
  } else {
    badgeEl.style.cssText = 'font-size:9px;color:var(--tx2,#7a8fa8);border:1px solid var(--b2,#2a3644);background:transparent';
  }
}

function _mpRenderModelList() {
  var listEl = document.getElementById('mp-model-list');
  var selEl = document.getElementById('mp-model-sel');
  var searchEl = document.getElementById('mp-model-search');
  var filter = searchEl ? searchEl.value : '';
  var models = Array.isArray(S.models) ? S.models.slice() : [];
  if (selEl) {
    selEl.innerHTML = '';
    models.forEach(function(m) {
      var opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      selEl.appendChild(opt);
    });
    if (_mpState.currentModel) selEl.value = _mpState.currentModel;
  }
  if (!listEl) return;
  var filtered = filter ? models.filter(function(m) { return _modelMatchesFilter(m, filter); }) : models;
  listEl.innerHTML = '';
  if (!filtered.length) {
    var emp = document.createElement('div');
    emp.className = 'fld-hint';
    emp.textContent = models.length ? 'No matching models.' : 'No models found.';
    listEl.appendChild(emp);
    return;
  }
  var frag = document.createDocumentFragment();
  filtered.forEach(function(m) {
    var profInfo = (S.modelProfiles && S.modelProfiles[m]) || {};
    var src = profInfo.source || 'generic';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mp-model-item';
    btn.dataset.model = m;
    var active = m === _mpState.currentModel;
    btn.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:6px;padding:5px 7px;border-radius:4px;cursor:pointer;text-align:left;font-family:inherit;font-size:10px;border:1px solid '
      + (active ? 'var(--amber,#e09030)' : 'transparent') + ';background:'
      + (active ? 'rgba(224,144,48,.12)' : 'transparent') + ';color:var(--tx,#c8d4e0)';
    var nm = document.createElement('span');
    nm.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    nm.textContent = m;
    nm.title = m;
    var bdg = document.createElement('span');
    _mpApplySourceBadge(bdg, src);
    btn.appendChild(nm);
    btn.appendChild(bdg);
    frag.appendChild(btn);
  });
  listEl.appendChild(frag);
}

function _mpTriStateSet(id, val) {
  var el = document.getElementById(id);
  if (!el) return;
  el.value = (val === true) ? 'true' : (val === false ? 'false' : '');
}

function _mpTriStateGet(id) {
  var el = document.getElementById(id);
  if (!el) return null;
  if (el.value === 'true') return true;
  if (el.value === 'false') return false;
  return null;
}

function _mpPopulateForm(data) {
  var prof = (data && data.profile) || {};
  var gguf = (data && data.gguf) || {};
  var src = (data && data.source) || 'generic';
  _mpApplySourceBadge(document.getElementById('mp-source-badge'), src);
  var fileEl = document.getElementById('mp-file-badge');
  if (fileEl) fileEl.textContent = data && data.file ? ('(' + data.file + ')') : '';

  var sumEl = document.getElementById('mp-gguf-summary');
  if (sumEl) {
    if (!gguf || !Object.keys(gguf).length) {
      sumEl.textContent = 'No GGUF metadata detected on disk for ' + (data && data.model || _mpState.currentModel) + '.';
    } else {
      var parts = [];
      if (gguf.architecture) parts.push('arch: ' + gguf.architecture);
      if (gguf.context_length) parts.push('ctx: ' + gguf.context_length);
      parts.push('is_moe: ' + (gguf.is_moe ? 'yes' : 'no'));
      parts.push('thinking: ' + (gguf.has_thinking ? 'yes' : 'no'));
      parts.push('vision: ' + (gguf.has_vision ? 'yes' : 'no'));
      parts.push('tool_call: ' + (gguf.has_tool_call ? 'yes' : 'no'));
      var sibs = gguf.siblings || {};
      var sibList = [];
      ['mmproj', 'mtp', 'dflash', 'dspark'].forEach(function(sk) {
        if (sibs[sk]) sibList.push(sk + '=' + sibs[sk]);
      });
      if (sibList.length) parts.push('siblings: ' + sibList.join(', '));
      sumEl.textContent = parts.join(' | ');
    }
  }

  var setVal = function(id, v) {
    var el = document.getElementById(id);
    if (el) el.value = (v === null || v === undefined) ? '' : String(v);
  };
  setVal('mp-kv-cache-type', prof.kv_cache_type || '');
  _mpTriStateSet('mp-flash-attn', prof.flash_attn);
  setVal('mp-load-mode', prof.load_mode || '');
  setVal('mp-num-ctx', prof.num_ctx);
  setVal('mp-moe-cpu-experts', prof.moe_cpu_experts);
  setVal('mp-gpu-layers', prof.gpu_layers);
  setVal('mp-ubatch', prof.ubatch);
  _mpTriStateSet('mp-mtp', prof.mtp);
  setVal('mp-mtp-draft', prof.mtp_draft_filename || '');
  setVal('mp-dflash-draft', prof.dflash_draft_filename || '');
  setVal('mp-dspark-draft', prof.dspark_draft_filename || '');
  setVal('mp-mmproj', prof.mmproj_filename || '');
  var caps = prof.capabilities || {};
  _mpTriStateSet('mp-cap-thinking', caps.thinking);
  _mpTriStateSet('mp-cap-vision', caps.vision);
  _mpTriStateSet('mp-cap-toolcall', caps.tool_call);
  setVal('mp-extra-args', Array.isArray(prof.extra_args) ? prof.extra_args.join(' ') : (prof.extra_args || ''));
  setVal('mp-sampling', (prof.sampling && Object.keys(prof.sampling).length) ? JSON.stringify(prof.sampling, null, 2) : '');
}

async function loadModelProfileInModal(modelName) {
  if (!modelName) return;
  _mpState.currentModel = modelName;
  _mpRenderModelList();
  _mpSetStatus('Loading profile for ' + modelName + '...', false);
  try {
    var resp = await fetch('/models/profile/' + encodeURIComponent(modelName));
    var data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error((data && data.error) || ('HTTP ' + resp.status));
    }
    if (S.modelProfiles && S.modelProfiles[modelName]) {
      S.modelProfiles[modelName].source = data.source || 'generic';
    }
    _mpPopulateForm(data);
    _mpRenderModelList();
    _mpSetStatus('', false);
  } catch (e) {
    console.warn('[model-profiles] load failed for ' + modelName + ':', e);
    _mpSetStatus('Load failed: ' + (e && e.message || e), true);
  }
}

async function openModelProfilesModal(preferredModel) {
  var modal = document.getElementById('model-profiles-modal');
  if (!modal) return;
  modal.style.display = 'flex';
  if (!Array.isArray(S.models) || !S.models.length) {
    try { await loadModels(); } catch (e) { console.warn('[model-profiles] loadModels failed:', e); }
  }
  var target = preferredModel || _mpState.currentModel || (cfgState && cfgState.currentModel) || (S.models && S.models[0]) || '';
  _mpState.currentModel = target;
  _mpRenderModelList();
  if (target) await loadModelProfileInModal(target);
}

function closeModelProfilesModal() {
  var modal = document.getElementById('model-profiles-modal');
  if (modal) modal.style.display = 'none';
}

async function saveModelProfileOverride() {
  var model = _mpState.currentModel;
  if (!model) { _mpSetStatus('Select a model first.', true); return; }
  var intOrNull = function(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    var s = el.value.trim();
    if (!s) return null;
    var n = parseInt(s, 10);
    return isFinite(n) ? n : NaN;
  };
  var strOrNull = function(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    var s = el.value.trim();
    return s || null;
  };
  var numCtx = intOrNull('mp-num-ctx');
  var moeCpu = intOrNull('mp-moe-cpu-experts');
  var gpuLayers = intOrNull('mp-gpu-layers');
  var ubatch = intOrNull('mp-ubatch');
  if (Number.isNaN(numCtx) || Number.isNaN(moeCpu) || Number.isNaN(gpuLayers) || Number.isNaN(ubatch)) {
    _mpSetStatus('Numeric fields must be valid integers.', true);
    return;
  }
  var samplingVal = null;
  var sampEl = document.getElementById('mp-sampling');
  var sampRaw = sampEl ? sampEl.value.trim() : '';
  if (sampRaw) {
    try {
      samplingVal = JSON.parse(sampRaw);
      if (!samplingVal || typeof samplingVal !== 'object' || Array.isArray(samplingVal)) {
        throw new Error('sampling must be a JSON object');
      }
    } catch (e) {
      _mpSetStatus('Invalid sampling JSON: ' + (e && e.message || e), true);
      return;
    }
  }
  var extraRaw = strOrNull('mp-extra-args');
  var payload = {};
  var putIfSet = function(k, v) { if (v !== null && v !== undefined) payload[k] = v; };
  putIfSet('kv_cache_type', strOrNull('mp-kv-cache-type'));
  putIfSet('flash_attn', _mpTriStateGet('mp-flash-attn'));
  putIfSet('load_mode', strOrNull('mp-load-mode'));
  putIfSet('num_ctx', numCtx);
  putIfSet('moe_cpu_experts', moeCpu);
  putIfSet('gpu_layers', gpuLayers);
  putIfSet('ubatch', ubatch);
  putIfSet('mtp', _mpTriStateGet('mp-mtp'));
  putIfSet('mtp_draft_filename', strOrNull('mp-mtp-draft'));
  putIfSet('dflash_draft_filename', strOrNull('mp-dflash-draft'));
  putIfSet('dspark_draft_filename', strOrNull('mp-dspark-draft'));
  putIfSet('mmproj_filename', strOrNull('mp-mmproj'));
  var capsPatch = {};
  var cThink = _mpTriStateGet('mp-cap-thinking');
  var cVis = _mpTriStateGet('mp-cap-vision');
  var cTool = _mpTriStateGet('mp-cap-toolcall');
  if (cThink !== null) capsPatch.thinking = cThink;
  if (cVis !== null) capsPatch.vision = cVis;
  if (cTool !== null) capsPatch.tool_call = cTool;
  if (Object.keys(capsPatch).length) payload.capabilities = capsPatch;
  if (extraRaw) payload.extra_args = extraRaw.split(/\s+/).filter(Boolean);
  if (samplingVal) payload.sampling = samplingVal;
  _mpSetStatus('Saving override...', false);
  try {
    var resp = await fetch('/models/profile/' + encodeURIComponent(model), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error((data && data.error) || ('HTTP ' + resp.status));
    }
    if (S.modelProfiles && S.modelProfiles[model]) {
      S.modelProfiles[model].source = data.source || 'user';
    }
    await loadModelProfileInModal(model);
    if (typeof loadModels === 'function') await loadModels();
    if (cfgState && cfgState.currentTab === 'defaults' && cfgState.currentModel === model && typeof loadModelDefaults === 'function') {
      await loadModelDefaults(model);
    }
    _mpSetStatus('Saved user override (' + (data.file || model) + ').', false);
  } catch (e) {
    console.warn('[model-profiles] save failed for ' + model + ':', e);
    _mpSetStatus('Save failed: ' + (e && e.message || e), true);
  }
}

async function resetModelProfileOverride() {
  var model = _mpState.currentModel;
  if (!model) return;
  _mpSetStatus('Resetting override...', false);
  try {
    var resp = await fetch('/models/profile/' + encodeURIComponent(model), { method: 'DELETE' });
    var data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error((data && data.error) || ('HTTP ' + resp.status));
    }
    await loadModelProfileInModal(model);
    if (typeof loadModels === 'function') await loadModels();
    if (cfgState && cfgState.currentTab === 'defaults' && cfgState.currentModel === model && typeof loadModelDefaults === 'function') {
      await loadModelDefaults(model);
    }
    _mpSetStatus(data.deleted ? ('Reset to ' + (data.source || 'default') + '.') : 'No user override to remove.', false);
  } catch (e) {
    console.warn('[model-profiles] reset failed for ' + model + ':', e);
    _mpSetStatus('Reset failed: ' + (e && e.message || e), true);
  }
}

async function rescanModelProfiles() {
  var btn = document.getElementById('mp-rescan-btn');
  if (btn) btn.disabled = true;
  _mpSetStatus('Rescanning GGUFs on disk...', false);
  try {
    var resp = await fetch('/models/rescan', { method: 'POST' });
    var data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error((data && data.error) || ('HTTP ' + resp.status));
    }
    S.models = Array.isArray(data.models) ? data.models : [];
    if (typeof loadModels === 'function') await loadModels();
    if (typeof refreshCfgModelList === 'function') await refreshCfgModelList();
    if (typeof refreshAvailableModels === 'function') await refreshAvailableModels();
    var target = (_mpState.currentModel && S.models.indexOf(_mpState.currentModel) >= 0)
      ? _mpState.currentModel
      : (S.models[0] || '');
    _mpState.currentModel = target;
    _mpRenderModelList();
    if (target) await loadModelProfileInModal(target);
    _mpSetStatus('Rescanned ' + (data.count != null ? data.count : S.models.length) + ' models.', false);
  } catch (e) {
    console.warn('[model-profiles] rescan failed:', e);
    _mpSetStatus('Rescan failed: ' + (e && e.message || e), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _initModelProfilesModal() {
  var modal = document.getElementById('model-profiles-modal');
  if (!modal || modal._mpWired) return;
  modal._mpWired = true;
  modal.addEventListener('click', function(e) {
    if (e.target === modal) closeModelProfilesModal();
  });
  var closeBtn = document.getElementById('mp-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', closeModelProfilesModal);
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') {
      closeModelProfilesModal();
    }
  });
  var searchEl = document.getElementById('mp-model-search');
  if (searchEl) searchEl.addEventListener('input', _mpRenderModelList);
  var selEl = document.getElementById('mp-model-sel');
  if (selEl) {
    selEl.addEventListener('change', function() {
      if (selEl.value) loadModelProfileInModal(selEl.value);
    });
  }
  var listEl = document.getElementById('mp-model-list');
  if (listEl) {
    listEl.addEventListener('click', function(ev) {
      var item = ev.target && ev.target.closest ? ev.target.closest('[data-model]') : null;
      if (!item || !listEl.contains(item)) return;
      var m = item.dataset.model || '';
      if (m) loadModelProfileInModal(m);
    });
  }
  var saveBtn = document.getElementById('mp-save-btn');
  if (saveBtn) saveBtn.addEventListener('click', saveModelProfileOverride);
  var resetBtn = document.getElementById('mp-reset-btn');
  if (resetBtn) resetBtn.addEventListener('click', resetModelProfileOverride);
  var rescanBtn = document.getElementById('mp-rescan-btn');
  if (rescanBtn) rescanBtn.addEventListener('click', rescanModelProfiles);
}
