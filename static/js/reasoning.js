// HiveMind UI module: Composer & Sidebar Planner Reasoning Effort controls.
var _REASONING_EFFORT_LABELS = {
  auto: 'Auto',
  none: 'Off',
  on: 'On',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  max: 'Max',
};

var _REASONING_EFFORT_SHORT = {
  auto: 'Auto',
  none: 'Off',
  on: 'On',
  minimal: 'Min',
  low: 'Low',
  medium: 'Med',
  high: 'High',
  max: 'Max',
};

function _normalizeReasoningEffort(v) {
  var eff = String(v || 'auto').trim().toLowerCase();
  if (eff === 'off' || eff === 'disabled' || eff === 'false' || eff === '0') return 'none';
  if (eff === 'true' || eff === '1' || eff === 'enabled') return 'on';
  if (eff === 'med') return 'medium';
  if (eff === 'xhigh' || eff === 'maximum') return 'max';
  return (eff === 'auto' || eff === 'none' || eff === 'on' || eff === 'minimal' || eff === 'low' || eff === 'medium' || eff === 'high' || eff === 'max')
    ? eff
    : 'auto';
}

function _lookupModelProfileUI(modelName) {
  if (!modelName || !S.modelProfiles) return null;
  return S.modelProfiles[modelName] || S.modelProfiles[String(modelName).split(':')[0]] || null;
}

function _getModelReasoningMetaUI(modelName) {
  var prof = _lookupModelProfileUI(modelName);
  if (!prof) {
    return {
      known: false,
      mode: 'effort',
      levels: ['none', 'minimal', 'low', 'medium', 'high', 'max'],
      source: '',
      profEff: '',
    };
  }
  var rMode = prof.reasoning_mode || (prof.thinking ? (prof.reasoning_effort ? 'effort' : 'binary') : 'none');
  var rLevels = Array.isArray(prof.reasoning_levels)
    ? prof.reasoning_levels.slice()
    : (rMode === 'effort' ? ['none', 'low', 'medium', 'high', 'max'] : (rMode === 'binary' ? ['none', 'on'] : []));
  var profEff = (rMode === 'effort')
    ? (prof.reasoning_effort
      || (prof.sampling_thinking_code && prof.sampling_thinking_code.reasoning_effort)
      || (prof.sampling_thinking && prof.sampling_thinking.reasoning_effort)
      || '')
    : '';
  return {
    known: true,
    mode: rMode,
    levels: rLevels,
    source: prof.reasoning_template_source || '',
    profEff: profEff ? _normalizeReasoningEffort(profEff) : '',
  };
}

function _modelSupportsThinkingUI(modelName) {
  return _getModelReasoningMetaUI(modelName).mode !== 'none';
}

function _resolveMappedEffortUI(rawEff, rMode, availLevels, profEff) {
  if (rawEff === 'auto') return 'auto';
  if (rMode === 'none') return '';
  if (rMode === 'binary') return rawEff === 'none' ? 'none' : 'on';
  if (rawEff === 'none') return 'none';
  var scale = { minimal: 0, low: 1, medium: 2, high: 3, max: 4 };
  var pos = ['minimal', 'low', 'medium', 'high', 'max'].filter(function(l) {
    return availLevels.indexOf(l) >= 0;
  });
  if (rawEff === 'on') {
    if (profEff && profEff !== 'auto' && profEff !== 'on' && profEff !== 'none') {
      return _resolveMappedEffortUI(profEff, rMode, availLevels);
    }
    if (pos.indexOf('medium') >= 0) return 'medium';
    return pos.length ? pos[Math.floor(pos.length / 2)] : 'on';
  }
  if (availLevels.indexOf(rawEff) >= 0) return rawEff;
  if (!pos.length) return '';
  var targetRank = scale[rawEff] !== undefined ? scale[rawEff] : 2;
  var best = pos[0];
  var bestDist = Math.abs(scale[best] - targetRank);
  for (var i = 1; i < pos.length; i++) {
    var cand = pos[i];
    var dist = Math.abs(scale[cand] - targetRank);
    if (dist < bestDist || (dist === bestDist && scale[cand] > scale[best])) {
      best = cand;
      bestDist = dist;
    }
  }
  return best;
}

function _targetModelForEffortKey(agentKey) {
  if (agentKey === 'duo') {
    return S.duoCoderModel || ((S.agentsCfg && S.agentsCfg.duo_coder && S.agentsCfg.duo_coder.model) || '');
  }
  if (agentKey === 'duo_planner') {
    var coderM = S.duoCoderModel || ((S.agentsCfg && S.agentsCfg.duo_coder && S.agentsCfg.duo_coder.model) || '');
    if (S.duoPlannerModel) return S.duoPlannerModel;
    if (S.duoPlannerUseCoderCtx !== false && coderM) return coderM;
    return (S.currentAssignments && S.currentAssignments.duo_planner && S.currentAssignments.duo_planner.model)
      || (S.agentsCfg && S.agentsCfg.duo_planner && S.agentsCfg.duo_planner.model)
      || coderM
      || '';
  }
  return (S.currentAssignments && S.currentAssignments[agentKey] && S.currentAssignments[agentKey].model)
    || (S.agentsCfg && S.agentsCfg[agentKey] && S.agentsCfg[agentKey].model)
    || '';
}

function _getActiveEffortState(v) {
  var mode = S.mode || 'auto';
  var m = '';
  if (mode === 'code_duo') {
    m = S.duoCoderModel || ((S.agentsCfg && S.agentsCfg.duo_coder && S.agentsCfg.duo_coder.model) || '');
  } else if (mode === 'pipeline') {
    m = (S.currentAssignments && S.currentAssignments.synthesizer && S.currentAssignments.synthesizer.model)
      || (S.agentsCfg && S.agentsCfg.synthesizer && S.agentsCfg.synthesizer.model)
      || '';
  } else {
    m = S._lastDirectRunModel
      || (S.currentAssignments && S.currentAssignments.direct && S.currentAssignments.direct.model)
      || (S.agentsCfg && S.agentsCfg.direct && S.agentsCfg.direct.model)
      || '';
  }
  var shortM = m ? (typeof _shortModel === 'function' ? _shortModel(m) : m) : '';
  var meta = _getModelReasoningMetaUI(m);
  var rMode = meta.mode;
  var supportsThinking = (rMode !== 'none');
  var availLevels = meta.levels;
  var eff = _normalizeReasoningEffort(v !== undefined ? v : S.reasoningEffort);
  var mappedEff = _resolveMappedEffortUI(eff, rMode, availLevels, meta.profEff);
  var isSkippedOnModel = (!supportsThinking && eff !== 'auto');
  var isOverriding = (supportsThinking && eff !== 'auto');
  var toggleOff = false;
  if (mode === 'code_duo') {
    toggleOff = Boolean(S.duoAgenticMode !== false && !S.duoToolThinkingAlways && S.duoToolThinkingMode === 'off');
  } else if (mode === 'pipeline') {
    toggleOff = Boolean(S.agentsCfg && S.agentsCfg.synthesizer && S.agentsCfg.synthesizer.thinking === false);
  } else {
    toggleOff = Boolean(S.agentsCfg && S.agentsCfg.direct && S.agentsCfg.direct.thinking === false);
  }

  var effectiveAutoLabel = 'Auto';
  if (!supportsThinking) {
    effectiveAutoLabel = 'No reasoning';
  } else if (toggleOff) {
    effectiveAutoLabel = 'Auto \u00b7 Off';
  } else if (rMode === 'binary') {
    effectiveAutoLabel = 'Auto \u00b7 On';
  } else if (meta.profEff && meta.profEff !== 'auto' && meta.profEff !== 'on') {
    var autoMapped = _resolveMappedEffortUI(meta.profEff, rMode, availLevels) || meta.profEff;
    effectiveAutoLabel = 'Auto \u00b7 ' + (_REASONING_EFFORT_SHORT[autoMapped] || autoMapped);
  }

  var trigEff;
  if (!supportsThinking) {
    trigEff = eff;
  } else if (eff === 'auto') {
    trigEff = 'auto';
  } else {
    trigEff = mappedEff || eff;
  }

  var trigLabel;
  if (!supportsThinking) {
    trigLabel = (eff !== 'auto')
      ? ((_REASONING_EFFORT_LABELS[eff] || eff) + ' (n/a)')
      : 'No reasoning';
  } else if (eff === 'auto') {
    trigLabel = effectiveAutoLabel;
  } else if (mappedEff && mappedEff !== eff) {
    trigLabel = (_REASONING_EFFORT_LABELS[mappedEff] || mappedEff) + ' (from ' + (_REASONING_EFFORT_SHORT[eff] || eff) + ')';
  } else {
    trigLabel = _REASONING_EFFORT_LABELS[eff] || 'Auto';
  }

  var srcLabel = '';
  if (meta.source && meta.source.indexOf('external:') === 0) {
    srcLabel = meta.source.slice(9);
  } else if (meta.source === 'gguf') {
    srcLabel = 'GGUF template';
  }

  var ctxText;
  if (!m) {
    ctxText = 'Inherits active agent & model default';
  } else if (!meta.known) {
    ctxText = shortM + ' \u00b7 loading capabilities...';
  } else if (!supportsThinking) {
    ctxText = shortM + ' \u00b7 no native reasoning';
  } else if (rMode === 'binary') {
    ctxText = shortM + ' \u00b7 On/Off only' + (srcLabel ? (' (' + srcLabel + ')') : '');
  } else {
    var defPart = (meta.profEff && meta.profEff !== 'auto' && meta.profEff !== 'on')
      ? ('default: ' + (_REASONING_EFFORT_SHORT[meta.profEff] || meta.profEff))
      : '';
    ctxText = shortM + (defPart ? (' \u00b7 ' + defPart) : '') + (srcLabel ? (' \u00b7 ' + srcLabel) : '');
  }

  var ovrKind = 'none';
  var ovrText = '';
  var showReset = false;
  if (isSkippedOnModel) {
    ovrKind = 'muted';
    ovrText = 'Skipped for ' + (shortM || 'active model') + ' (no reasoning)';
    showReset = true;
  } else if (isOverriding) {
    ovrKind = 'source-composer';
    var mapHint = (mappedEff && mappedEff !== eff)
      ? (' \u2192 ' + (_REASONING_EFFORT_LABELS[mappedEff] || mappedEff))
      : '';
    var budHint = (eff === 'none') ? '' : ' \u00b7 Budget: slider';
    ovrText = '\u26a1 Composer overrides sidebar/card' + mapHint + budHint;
    showReset = true;
  } else if (supportsThinking && meta.known) {
    ovrKind = 'source-sidebar';
    var cardDefault = toggleOff
      ? 'Off'
      : (meta.profEff && meta.profEff !== 'auto' && meta.profEff !== 'on'
        ? (_REASONING_EFFORT_LABELS[meta.profEff] || meta.profEff)
        : (rMode === 'binary' ? 'On' : 'Card/Profile'));
    ovrText = 'Sidebar/Card controls thinking \u00b7 ' + cardDefault;
    showReset = false;
  }

  return {
    eff: eff,
    mappedEff: mappedEff,
    trigEff: trigEff,
    model: m,
    shortModel: shortM || 'active model',
    rMode: rMode,
    availLevels: availLevels,
    supportsThinking: supportsThinking,
    profEff: meta.profEff,
    isSkippedOnModel: isSkippedOnModel,
    isOverriding: isOverriding,
    effectiveAutoLabel: effectiveAutoLabel,
    trigLabel: trigLabel,
    ctxText: ctxText,
    ovrKind: ovrKind,
    ovrText: ovrText,
    showReset: showReset,
  };
}

function _getActiveEffortContext() {
  return _getActiveEffortState().ctxText;
}

function _syncReasoningOverrideBadges() {
  if (typeof document === 'undefined' || !document.querySelectorAll) return;
  var mainEff = _normalizeReasoningEffort(S.reasoningEffort);
  var planEff = _normalizeReasoningEffort(S.plannerReasoningEffort);
  var slots = document.querySelectorAll('.reasoning-override-slot');
  for (var i = 0; i < slots.length; i++) {
    var slot = slots[i];
    var key = (typeof slot.getAttribute === 'function' ? slot.getAttribute('data-agent-override') : '') || 'duo';
    var isPlan = (key === 'duo_planner');
    var eff = isPlan ? planEff : mainEff;
    var active = eff !== 'auto';
    if (!active) {
      slot.innerHTML = '';
      continue;
    }
    var baseLbl = (isPlan ? 'Planner: ' : 'Effort: ') + (_REASONING_EFFORT_LABELS[eff] || eff);
    var tModel = _targetModelForEffortKey(key);
    var tMeta = _getModelReasoningMetaUI(tModel);
    var sup = tMeta.mode !== 'none';
    var tMapped = _resolveMappedEffortUI(eff, tMeta.mode, tMeta.levels, tMeta.profEff);
    var lbl;
    if (!sup && eff !== 'auto') {
      lbl = baseLbl + ' (n/a) \u00d7';
    } else if (sup && tMapped && tMapped !== eff) {
      lbl = baseLbl + ' \u2192 ' + (_REASONING_EFFORT_LABELS[tMapped] || tMapped) + ' \u00d7';
    } else {
      lbl = baseLbl + ' \u00d7';
    }
    var resetCall = isPlan ? "setPlannerReasoningEffort('auto')" : "setReasoningEffort('auto')";
    var btn = slot.querySelector('.reasoning-override-badge');
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'reasoning-override-badge';
      slot.innerHTML = '';
      slot.appendChild(btn);
    }
    btn.setAttribute('onclick', resetCall);
    btn.className = (!sup && eff !== 'auto') ? 'reasoning-override-badge muted' : 'reasoning-override-badge';
    btn.title = (!sup && eff !== 'auto')
      ? ('Skipped on this non-reasoning model \u2014 click to reset ' + (isPlan ? 'planner effort' : 'reasoning effort') + ' to Auto')
      : ('Overridden by ' + (isPlan ? 'Planner Effort' : 'Reasoning Effort') + ' \u2014 click to reset to Auto');
    btn.textContent = lbl;
  }
  var rows = document.querySelectorAll('[data-reasoning-ovr-row]');
  for (var j = 0; j < rows.length; j++) {
    var row = rows[j];
    var rKey = (typeof row.getAttribute === 'function' ? row.getAttribute('data-reasoning-ovr-row') : '') || 'duo';
    var rIsPlan = (rKey === 'duo_planner');
    var rEff = rIsPlan ? planEff : mainEff;
    var rActive = rEff !== 'auto';
    var rSup = _modelSupportsThinkingUI(_targetModelForEffortKey(rKey));
    if (row.classList && typeof row.classList.toggle === 'function') {
      row.classList.toggle('reasoning-overridden', Boolean(rActive && rSup));
      row.classList.toggle('reasoning-overridden-off', Boolean(rActive && rSup && rEff === 'none'));
    }
  }
}

function closeReasoningEffortMenu() {
  if (typeof document === 'undefined') return;
  var menu = document.getElementById('effort-menu');
  var trig = document.getElementById('effort-trigger');
  if (menu) menu.hidden = true;
  if (trig) trig.setAttribute('aria-expanded', 'false');
}

function closeSidebarMainEffortMenu() {
  if (typeof document === 'undefined') return;
  var menu = document.getElementById('sidebar-main-effort-menu');
  var trig = document.getElementById('sidebar-main-effort-trigger');
  if (menu) {
    menu.hidden = true;
    if (menu.classList) menu.classList.remove('open-up');
  }
  if (trig) trig.setAttribute('aria-expanded', 'false');
}

function closePlannerReasoningEffortMenu() {
  if (typeof document === 'undefined') return;
  var menu = document.getElementById('planner-effort-menu');
  var trig = document.getElementById('planner-effort-trigger');
  if (menu) {
    menu.hidden = true;
    if (menu.classList) menu.classList.remove('open-up');
  }
  if (trig) trig.setAttribute('aria-expanded', 'false');
}

function _positionSidebarEffortMenu(wrap, menu) {
  if (!menu || !menu.classList) return;
  menu.classList.remove('open-up');
  if (!wrap || typeof wrap.getBoundingClientRect !== 'function' || typeof menu.getBoundingClientRect !== 'function') return;
  var wr = wrap.getBoundingClientRect();
  var mr = menu.getBoundingClientRect();
  var panel = (typeof wrap.closest === 'function') ? wrap.closest('.panel') : null;
  var bounds = (panel && typeof panel.getBoundingClientRect === 'function')
    ? panel.getBoundingClientRect()
    : { top: 0, bottom: (typeof window !== 'undefined' && window.innerHeight) ? window.innerHeight : 900 };
  var needH = Math.max(mr.height || 0, 220);
  var spaceBelow = bounds.bottom - wr.bottom - 8;
  var spaceAbove = wr.top - bounds.top - 8;
  if (spaceBelow < needH && spaceAbove > spaceBelow) {
    menu.classList.add('open-up');
  }
}

function toggleReasoningEffortMenu(ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  if (typeof document === 'undefined') return;
  closeSidebarMainEffortMenu();
  closePlannerReasoningEffortMenu();
  var menu = document.getElementById('effort-menu');
  var trig = document.getElementById('effort-trigger');
  if (!menu || !trig) return;
  var willOpen = Boolean(menu.hidden);
  menu.hidden = !willOpen;
  trig.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  if (willOpen) {
    _renderReasoningEffortUI(S.reasoningEffort);
  }
}

function toggleSidebarMainEffortMenu(ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  if (typeof document === 'undefined') return;
  closeReasoningEffortMenu();
  closePlannerReasoningEffortMenu();
  var menu = document.getElementById('sidebar-main-effort-menu');
  var trig = document.getElementById('sidebar-main-effort-trigger');
  var wrap = document.getElementById('sidebar-main-effort-wrap');
  if (!menu || !trig) return;
  var willOpen = Boolean(menu.hidden);
  menu.hidden = !willOpen;
  trig.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  if (willOpen) {
    _renderReasoningEffortUI(S.reasoningEffort);
    _positionSidebarEffortMenu(wrap, menu);
  } else if (menu.classList) {
    menu.classList.remove('open-up');
  }
}

function togglePlannerReasoningEffortMenu(ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  if (typeof document === 'undefined') return;
  closeReasoningEffortMenu();
  closeSidebarMainEffortMenu();
  var menu = document.getElementById('planner-effort-menu');
  var trig = document.getElementById('planner-effort-trigger');
  var wrap = document.getElementById('planner-effort-wrap');
  if (!menu || !trig) return;
  var willOpen = Boolean(menu.hidden);
  menu.hidden = !willOpen;
  trig.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  if (willOpen) {
    _renderPlannerReasoningEffortUI(S.plannerReasoningEffort);
    _positionSidebarEffortMenu(wrap, menu);
  } else if (menu.classList) {
    menu.classList.remove('open-up');
  }
}

function _getPlannerEffortState(v) {
  var m = _targetModelForEffortKey('duo_planner');
  var shortM = m ? (typeof _shortModel === 'function' ? _shortModel(m) : m) : '';
  var meta = _getModelReasoningMetaUI(m);
  var rMode = meta.mode;
  var supportsThinking = (rMode !== 'none');
  var availLevels = meta.levels;
  var eff = _normalizeReasoningEffort(v !== undefined ? v : S.plannerReasoningEffort);
  var mappedEff = _resolveMappedEffortUI(eff, rMode, availLevels, meta.profEff);
  var isSkippedOnModel = (!supportsThinking && eff !== 'auto');
  var isOverriding = (supportsThinking && eff !== 'auto');
  var toggleOff = !S.duoAgenticThinking;

  var effectiveAutoLabel = 'Auto';
  if (!supportsThinking) {
    effectiveAutoLabel = 'No reasoning';
  } else if (toggleOff) {
    effectiveAutoLabel = 'Auto \u00b7 Off';
  } else if (rMode === 'binary') {
    effectiveAutoLabel = 'Auto \u00b7 On';
  } else if (meta.profEff && meta.profEff !== 'auto' && meta.profEff !== 'on') {
    var autoMapped = _resolveMappedEffortUI(meta.profEff, rMode, availLevels) || meta.profEff;
    effectiveAutoLabel = 'Auto \u00b7 ' + (_REASONING_EFFORT_SHORT[autoMapped] || autoMapped);
  }

  var trigEff = !supportsThinking ? eff : (eff === 'auto' ? 'auto' : (mappedEff || eff));
  var trigLabel;
  if (!supportsThinking) {
    trigLabel = (eff !== 'auto') ? ((_REASONING_EFFORT_LABELS[eff] || eff) + ' (n/a)') : 'No reasoning';
  } else if (eff === 'auto') {
    trigLabel = effectiveAutoLabel;
  } else if (mappedEff && mappedEff !== eff) {
    trigLabel = (_REASONING_EFFORT_LABELS[mappedEff] || mappedEff) + ' (from ' + (_REASONING_EFFORT_SHORT[eff] || eff) + ')';
  } else {
    trigLabel = _REASONING_EFFORT_LABELS[eff] || 'Auto';
  }

  var srcLabel = '';
  if (meta.source && meta.source.indexOf('external:') === 0) srcLabel = meta.source.slice(9);
  else if (meta.source === 'gguf') srcLabel = 'GGUF template';

  var ctxText;
  if (!m) ctxText = 'Inherits Planner Thinking & model default';
  else if (!meta.known) ctxText = shortM + ' \u00b7 loading capabilities...';
  else if (!supportsThinking) ctxText = shortM + ' \u00b7 no native reasoning';
  else if (rMode === 'binary') ctxText = shortM + ' \u00b7 On/Off only' + (srcLabel ? (' (' + srcLabel + ')') : '');
  else {
    var defPart = (meta.profEff && meta.profEff !== 'auto' && meta.profEff !== 'on')
      ? ('default: ' + (_REASONING_EFFORT_SHORT[meta.profEff] || meta.profEff))
      : '';
    ctxText = shortM + (defPart ? (' \u00b7 ' + defPart) : '') + (srcLabel ? (' \u00b7 ' + srcLabel) : '');
  }

  var ovrKind = 'none', ovrText = '', showReset = false;
  if (isSkippedOnModel) {
    ovrKind = 'muted';
    ovrText = 'Skipped for ' + (shortM || 'planner model') + ' (no reasoning)';
    showReset = true;
  } else if (isOverriding) {
    ovrKind = 'source-composer';
    var mapHint = (mappedEff && mappedEff !== eff) ? (' \u2192 ' + (_REASONING_EFFORT_LABELS[mappedEff] || mappedEff)) : '';
    ovrText = '\u26a1 Overrides Planner Thinking' + mapHint;
    showReset = true;
  } else if (supportsThinking && meta.known) {
    ovrKind = 'source-sidebar';
    var pDef = toggleOff ? 'Off' : (meta.profEff && meta.profEff !== 'auto' && meta.profEff !== 'on'
      ? (_REASONING_EFFORT_LABELS[meta.profEff] || meta.profEff)
      : (rMode === 'binary' ? 'On' : 'Planner Thinking'));
    ovrText = 'Planner Thinking controls \u00b7 ' + pDef;
  }

  return {
    eff: eff, mappedEff: mappedEff, trigEff: trigEff, model: m,
    shortModel: shortM || 'planner model', rMode: rMode, availLevels: availLevels,
    supportsThinking: supportsThinking, isSkippedOnModel: isSkippedOnModel,
    isOverriding: isOverriding, effectiveAutoLabel: effectiveAutoLabel,
    trigLabel: trigLabel, ctxText: ctxText,
    ovrKind: ovrKind, ovrText: ovrText, showReset: showReset,
  };
}

function _syncRoutingPlannerEffortSelect(st) {
  if (typeof document === 'undefined') return;
  var rRow = document.getElementById('routing-planner-effort-row');
  if (rRow && rRow.style) {
    rRow.style.display = ((S.routingPlanner || 'auto') === 'never') ? 'none' : '';
  }
  var sel = document.getElementById('routing-planner-effort-sel');
  if (!sel) return;
  var showLevels = (st.rMode === 'binary')
    ? ['auto', 'none', 'on', 'low', 'medium', 'high', 'max']
    : ['auto', 'none', 'minimal', 'low', 'medium', 'high', 'max'];
  if (st.eff && showLevels.indexOf(st.eff) < 0) {
    showLevels = showLevels.concat([st.eff]);
  }
  var html = '';
  for (var i = 0; i < showLevels.length; i++) {
    var lv = showLevels[i];
    var avail = (lv === 'auto') || (st.supportsThinking && (lv === 'none' || st.availLevels.indexOf(lv) >= 0));
    var txt;
    if (lv === 'auto') {
      txt = st.effectiveAutoLabel;
    } else if (!avail && lv === st.eff && st.supportsThinking && st.mappedEff && st.mappedEff !== lv) {
      txt = (_REASONING_EFFORT_LABELS[st.mappedEff] || st.mappedEff) + ' (from ' + (_REASONING_EFFORT_SHORT[lv] || lv) + ')';
    } else {
      txt = (_REASONING_EFFORT_LABELS[lv] || lv) + (avail ? '' : ' (n/a)');
    }
    html += '<option value="' + lv + '"' + (avail ? '' : ' disabled') + '>' + txt + '</option>';
  }
  if (typeof sel.innerHTML === 'string') sel.innerHTML = html;
  sel.value = st.eff;
  sel.disabled = !st.supportsThinking && st.eff === 'auto';
  sel.title = st.ctxText;
}

function _renderPlannerReasoningEffortUI(v) {
  if (typeof document === 'undefined') return;
  var st = _getPlannerEffortState(v);
  var eff = st.eff;
  var trig = document.getElementById('planner-effort-trigger');
  if (trig) {
    trig.setAttribute('data-effort', st.trigEff);
    trig.setAttribute('data-override', st.isOverriding ? '1' : '0');
    if (!st.supportsThinking) trig.setAttribute('data-no-reasoning', '1');
    else if (typeof trig.removeAttribute === 'function') trig.removeAttribute('data-no-reasoning');
    trig.title = st.isSkippedOnModel
      ? ('Planner effort set to ' + (_REASONING_EFFORT_LABELS[eff] || eff) + ', skipped because ' + st.shortModel + ' has no native reasoning')
      : (st.isOverriding
        ? ('Planner override: ' + st.trigLabel + ' (independent of chat composer)')
        : ('Planner reasoning effort: ' + st.ctxText));
  }
  var lblEl = document.getElementById('planner-effort-trig-lbl');
  if (lblEl) lblEl.textContent = st.trigLabel;
  var hintEl = document.getElementById('planner-effort-hint');
  if (hintEl) {
    if (!st.supportsThinking) hintEl.textContent = st.shortModel + ' has no native reasoning';
    else if (st.isOverriding) hintEl.textContent = 'Overrides Planner Thinking · independent of chat composer';
    else hintEl.textContent = 'Independent of chat composer · ' + st.ctxText;
  }
  var ctxEl = document.getElementById('planner-effort-menu-ctx');
  if (ctxEl) ctxEl.textContent = st.ctxText;
  var ovrEl = document.getElementById('planner-effort-menu-ovr');
  var ovrTxt = document.getElementById('planner-effort-menu-ovr-txt');
  var ovrResetBtn = document.getElementById('planner-effort-ovr-reset-btn');
  if (ovrEl) {
    if (st.ovrKind !== 'none') {
      ovrEl.hidden = false;
      ovrEl.className = 'effort-menu-ovr ' + st.ovrKind;
      if (ovrTxt) ovrTxt.textContent = st.ovrText;
      if (ovrResetBtn) ovrResetBtn.hidden = !st.showReset;
    } else {
      ovrEl.hidden = true;
      if (ovrResetBtn) ovrResetBtn.hidden = true;
    }
  }
  var emptyEl = document.getElementById('planner-effort-menu-empty');
  if (emptyEl) {
    emptyEl.hidden = st.supportsThinking;
    if (!st.supportsThinking) emptyEl.textContent = st.shortModel + ' does not support native reasoning';
  }
  var menu = document.getElementById('planner-effort-menu');
  if (menu && menu.querySelectorAll) {
    var opts = menu.querySelectorAll('.effort-opt');
    for (var i = 0; i < opts.length; i++) {
      var b = opts[i];
      var optEff = b.getAttribute('data-effort');
      // Hide only the binary-only "on" row on graduated effort models (and "minimal" on binary models);
      // gray out (disable) every other unsupported level so availability is visible at a glance.
      var shown = (optEff === 'on')
        ? (st.rMode === 'binary')
        : (optEff === 'minimal' ? (st.rMode !== 'binary') : true);
      var avail = (optEff === 'auto') || (st.supportsThinking && (optEff === 'none' || st.availLevels.indexOf(optEff) >= 0));
      var activeOpt = shown && avail && (optEff === eff || (eff !== 'auto' && optEff === st.mappedEff));
      b.hidden = !shown;
      b.disabled = !shown || !avail;
      if (b.classList) {
        b.classList.toggle('active', activeOpt);
        b.classList.toggle('disabled', shown && !avail);
      }
      b.title = (shown && !avail) ? ('Not supported by ' + st.shortModel) : '';
      b.setAttribute('aria-hidden', shown ? 'false' : 'true');
      b.setAttribute('aria-disabled', (!avail) ? 'true' : 'false');
      b.setAttribute('aria-selected', activeOpt ? 'true' : 'false');
    }
  }
  _syncRoutingPlannerEffortSelect(st);
}

function _renderSidebarMainEffortUI(st) {
  if (typeof document === 'undefined' || !st) return;
  var mode = S.mode || 'auto';
  var roleLbl = document.getElementById('sidebar-main-role-lbl');
  if (roleLbl) {
    roleLbl.textContent = (mode === 'code_duo')
      ? 'Coder'
      : (mode === 'pipeline' ? 'Synthesizer' : (mode === 'simple' ? 'Direct' : 'Direct / Coder'));
  }
  var hintEl = document.getElementById('sidebar-main-effort-hint');
  if (hintEl) {
    if (!st.supportsThinking) {
      hintEl.textContent = st.shortModel + ' has no native reasoning';
    } else if (st.isOverriding) {
      hintEl.textContent = 'Synced with composer \u00b7 overrides card/sidebar';
    } else {
      hintEl.textContent = 'Synced with composer \u00b7 ' + st.ctxText;
    }
  }
  var trig = document.getElementById('sidebar-main-effort-trigger');
  if (trig) {
    trig.setAttribute('data-effort', st.trigEff);
    trig.setAttribute('data-override', st.isOverriding ? '1' : '0');
    if (!st.supportsThinking) trig.setAttribute('data-no-reasoning', '1');
    else if (typeof trig.removeAttribute === 'function') trig.removeAttribute('data-no-reasoning');
    trig.title = st.ctxText;
  }
  var lblEl = document.getElementById('sidebar-main-effort-trig-lbl');
  if (lblEl) lblEl.textContent = st.trigLabel;
  var ctxEl = document.getElementById('sidebar-main-effort-menu-ctx');
  if (ctxEl) ctxEl.textContent = st.ctxText;
  var ovrEl = document.getElementById('sidebar-main-effort-menu-ovr');
  var ovrTxt = document.getElementById('sidebar-main-effort-menu-ovr-txt');
  var ovrResetBtn = document.getElementById('sidebar-main-effort-ovr-reset-btn');
  if (ovrEl) {
    if (st.ovrKind !== 'none') {
      ovrEl.hidden = false;
      ovrEl.className = 'effort-menu-ovr ' + st.ovrKind;
      if (ovrTxt) ovrTxt.textContent = st.ovrText;
      if (ovrResetBtn) ovrResetBtn.hidden = !st.showReset;
    } else {
      ovrEl.hidden = true;
      if (ovrResetBtn) ovrResetBtn.hidden = true;
    }
  }
  var emptyEl = document.getElementById('sidebar-main-effort-menu-empty');
  if (emptyEl) {
    emptyEl.hidden = st.supportsThinking;
    if (!st.supportsThinking) emptyEl.textContent = st.shortModel + ' does not support native reasoning';
  }
  var menu = document.getElementById('sidebar-main-effort-menu');
  if (menu && menu.querySelectorAll) {
    var opts = menu.querySelectorAll('.effort-opt');
    for (var i = 0; i < opts.length; i++) {
      var b = opts[i];
      var optEff = b.getAttribute('data-effort');
      var shown = (optEff === 'on')
        ? (st.rMode === 'binary')
        : (optEff === 'minimal' ? (st.rMode !== 'binary') : true);
      var avail = (optEff === 'auto') || (st.supportsThinking && (optEff === 'none' || st.availLevels.indexOf(optEff) >= 0));
      var activeOpt = shown && avail && (optEff === st.eff || (st.eff !== 'auto' && optEff === st.mappedEff));
      b.hidden = !shown;
      b.disabled = !shown || !avail;
      if (b.classList) {
        b.classList.toggle('active', activeOpt);
        b.classList.toggle('disabled', shown && !avail);
      }
      b.title = (shown && !avail) ? ('Not supported by ' + st.shortModel) : '';
      b.setAttribute('aria-hidden', shown ? 'false' : 'true');
      b.setAttribute('aria-disabled', (!avail) ? 'true' : 'false');
      b.setAttribute('aria-selected', activeOpt ? 'true' : 'false');
    }
  }
}

function _renderReasoningCardChrome(mainSt, planSt) {
  if (typeof document === 'undefined') return;
  var mode = S.mode || 'auto';
  var isAgentic = (mode === 'code_duo' && S.duoAgenticMode !== false);
  var showPlanner = (isAgentic || (mode === 'auto' && (S.routingPlanner || 'decider') !== 'never'));
  var showAdv = (isAgentic || mode === 'auto');
  var plannerOff = (isAgentic && S.duoPlannerEnabled === false);

  var pRow = document.getElementById('planner-effort-row');
  if (pRow) {
    if (pRow.style) pRow.style.display = showPlanner ? '' : 'none';
    if (pRow.classList) pRow.classList.toggle('is-inactive', plannerOff);
  }
  var pPill = document.getElementById('planner-effort-inactive-pill');
  if (pPill) pPill.hidden = !plannerOff;

  var sub = document.getElementById('reasoning-card-scope-sub');
  if (sub) {
    if (mode === 'simple') sub.textContent = 'Direct';
    else if (mode === 'pipeline') sub.textContent = 'Synthesizer';
    else if (mode === 'code_duo') sub.textContent = isAgentic ? (plannerOff ? 'Coder (Planner off)' : 'Coder + Planner') : 'Coder';
    else sub.textContent = showPlanner ? 'Direct + Planner' : 'Direct';
  }

  var resetBtn = document.getElementById('reasoning-card-reset-btn');
  if (resetBtn) {
    var hasOvr = (mainSt && mainSt.eff !== 'auto') || (showPlanner && planSt && planSt.eff !== 'auto');
    resetBtn.hidden = !hasOvr;
  }

  var adv = document.getElementById('reasoning-adv-details');
  if (adv) adv.hidden = !showAdv;
  var advVal = document.getElementById('reasoning-adv-summary-val');
  if (advVal) {
    var pTxt = S.duoChunking ? 'Plan: Forced' : (S.duoAgenticThinking ? 'Plan: On' : 'Plan: Off');
    var tTxt = S.duoToolThinkingAlways ? 'Tools: On' : 'Tools: Off';
    var mt = parseInt(S.duoPlannerMaxTokens, 10) || 0;
    var mtTxt = mt > 0 ? (mt >= 1000 ? ((mt % 1000 === 0 ? (mt / 1000) : (mt / 1000).toFixed(1)) + 'k') : String(mt)) : 'ctx';
    advVal.textContent = pTxt + ' \u00b7 ' + tTxt + ' \u00b7 ' + mtTxt;
  }
}

function resetAllReasoningEffort() {
  if (_normalizeReasoningEffort(S.reasoningEffort) !== 'auto') {
    setReasoningEffort('auto');
  }
  if (_normalizeReasoningEffort(S.plannerReasoningEffort) !== 'auto') {
    setPlannerReasoningEffort('auto');
  }
}

function _renderReasoningEffortUI(v) {
  var st = _getActiveEffortState(v);
  var eff = st.eff;
  if (typeof document !== 'undefined') {
    var trig = document.getElementById('effort-trigger');
    if (trig) {
      trig.setAttribute('data-effort', st.trigEff);
      trig.setAttribute('data-override', st.isOverriding ? '1' : '0');
      if (!st.supportsThinking) {
        trig.setAttribute('data-no-reasoning', '1');
      } else if (typeof trig.removeAttribute === 'function') {
        trig.removeAttribute('data-no-reasoning');
      }
      trig.title = st.isSkippedOnModel
        ? ('Composer set to ' + (_REASONING_EFFORT_LABELS[eff] || eff) + ', skipped because ' + st.shortModel + ' has no native reasoning')
        : (st.isOverriding
          ? ('Composer override: ' + st.trigLabel + ' (overrides sidebar & agent cards; Think-Budget slider stays independent)')
          : ('Thinking effort: ' + st.ctxText));
    }
    var lblEl = document.getElementById('effort-trig-lbl');
    if (lblEl) lblEl.textContent = st.trigLabel;
    var ctxEl = document.getElementById('effort-menu-ctx');
    if (ctxEl) ctxEl.textContent = st.ctxText;
    var ovrEl = document.getElementById('effort-menu-ovr');
    var ovrTxt = document.getElementById('effort-menu-ovr-txt');
    var ovrResetBtn = document.getElementById('effort-ovr-reset-btn');
    if (ovrEl) {
      if (st.ovrKind !== 'none') {
        ovrEl.hidden = false;
        ovrEl.className = 'effort-menu-ovr ' + st.ovrKind;
        if (ovrTxt) ovrTxt.textContent = st.ovrText;
        if (ovrResetBtn) ovrResetBtn.hidden = !st.showReset;
      } else {
        ovrEl.hidden = true;
        if (ovrResetBtn) ovrResetBtn.hidden = true;
      }
    }
    var emptyEl = document.getElementById('effort-menu-empty');
    if (emptyEl) {
      emptyEl.hidden = st.supportsThinking;
      if (!st.supportsThinking) {
        emptyEl.textContent = st.shortModel + ' does not support native reasoning';
      }
    }
    var menu = document.getElementById('effort-menu');
    if (menu && menu.querySelectorAll) {
      var opts = menu.querySelectorAll('.effort-opt');
      for (var i = 0; i < opts.length; i++) {
        var b = opts[i];
        var optEff = b.getAttribute('data-effort');
        var visible = st.supportsThinking && (optEff === 'auto' || optEff === 'none' || st.availLevels.indexOf(optEff) >= 0);
        var activeOpt = visible && (
          optEff === eff
          || (eff !== 'auto' && optEff === st.mappedEff)
        );
        b.hidden = !visible;
        b.disabled = !visible;
        if (b.classList) {
          b.classList.toggle('active', activeOpt);
        }
        b.setAttribute('aria-hidden', visible ? 'false' : 'true');
        b.setAttribute('aria-selected', activeOpt ? 'true' : 'false');
      }
    }
  }
  var pSt = _getPlannerEffortState(S.plannerReasoningEffort);
  _renderPlannerReasoningEffortUI(S.plannerReasoningEffort);
  _renderSidebarMainEffortUI(st);
  _renderReasoningCardChrome(st, pSt);
  _syncReasoningOverrideBadges();
}

function setReasoningEffort(level) {
  var prev = S.reasoningEffort || 'auto';
  var eff = _normalizeReasoningEffort(level);
  S.reasoningEffort = eff;
  closeReasoningEffortMenu();
  closeSidebarMainEffortMenu();
  _renderReasoningEffortUI(eff);
  if (typeof _postSettingsFF === 'function') {
    _postSettingsFF({ reasoning_effort: eff }, function() {
      S.reasoningEffort = prev;
      _renderReasoningEffortUI(prev);
    });
  } else if (typeof postSettings === 'function') {
    postSettings({ reasoning_effort: eff });
  }
}

function setPlannerReasoningEffort(level) {
  var prev = S.plannerReasoningEffort || 'auto';
  var eff = _normalizeReasoningEffort(level);
  S.plannerReasoningEffort = eff;
  closePlannerReasoningEffortMenu();
  _renderPlannerReasoningEffortUI(eff);
  _renderReasoningCardChrome(_getActiveEffortState(S.reasoningEffort), _getPlannerEffortState(eff));
  _syncReasoningOverrideBadges();
  if (typeof _postSettingsFF === 'function') {
    _postSettingsFF({ planner_reasoning_effort: eff }, function() {
      S.plannerReasoningEffort = prev;
      _renderPlannerReasoningEffortUI(prev);
      _renderReasoningCardChrome(_getActiveEffortState(S.reasoningEffort), _getPlannerEffortState(prev));
      _syncReasoningOverrideBadges();
    });
  } else if (typeof postSettings === 'function') {
    postSettings({ planner_reasoning_effort: eff });
  }
}

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('click', function(ev) {
    var wrap = document.getElementById('effort-wrap');
    if (wrap && (!ev || !ev.target || (typeof wrap.contains === 'function' && !wrap.contains(ev.target)))) {
      closeReasoningEffortMenu();
    }
    var smWrap = document.getElementById('sidebar-main-effort-wrap');
    if (smWrap && (!ev || !ev.target || (typeof smWrap.contains === 'function' && !smWrap.contains(ev.target)))) {
      closeSidebarMainEffortMenu();
    }
    var pwrap = document.getElementById('planner-effort-wrap');
    if (pwrap && (!ev || !ev.target || (typeof pwrap.contains === 'function' && !pwrap.contains(ev.target)))) {
      closePlannerReasoningEffortMenu();
    }
  });
  document.addEventListener('keydown', function(ev) {
    if (ev && ev.key === 'Escape') {
      closeReasoningEffortMenu();
      closeSidebarMainEffortMenu();
      closePlannerReasoningEffortMenu();
    }
  });
}

// removed: updateToolThinkingModeVis — segmented control is always visible
