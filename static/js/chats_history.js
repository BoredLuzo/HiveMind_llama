// HiveMind UI module: New Chat, Images & Vision UI, Chat Autosave/History & Duo Image Plan UI.
// -- New Chat ---------------------------------------------------
async function newChat() {
  var _epoch = (S._chatSwitchEpoch = (S._chatSwitchEpoch || 0) + 1);
  // READ-ALOUD (2026-10-18): starting a new chat silenced nothing - the previous
  // answer kept playing while the new chat was already empty. Stop the clip, the
  // queue and the prefetched request here.
  if (typeof _readAloudStop === 'function') _readAloudStop();
  var _skipPhClear = Boolean(S._skipPlaceholderClear);
  S._skipPlaceholderClear = false;
  if (!_skipPhClear && !S.workspace && !S.currentChatWorkspace) {
    try {
      var _phRes = await fetch('/workspace/placeholder');
      if (S._chatSwitchEpoch !== _epoch) return;
      if (_phRes && _phRes.ok) {
        var _phData = await _phRes.json();
        if (S._chatSwitchEpoch !== _epoch) return;
        if (_phData && _phData.count > 0) {
          var _phFiles = Array.isArray(_phData.files) ? _phData.files : [];
          var _phList = _phFiles.slice(0, 8).map(function(f) { return '• ' + f; }).join('\n');
          if (_phFiles.length > 8) _phList += '\n• … +' + (_phFiles.length - 8) + ' more';
          if (!confirm('Default workspace contains ' + _phData.count + ' file(s):\n' + _phList + '\n\nStart a new chat and delete these files?')) {
            return;
          }
          await fetch('/workspace/placeholder/clear', {method:'POST'});
          if (S._chatSwitchEpoch !== _epoch) return;
        }
      }
    } catch (_phErr) {
      console.warn('[placeholder-ws]', _phErr);
    }
  }
  await fetch('/memory/clear_session', {method:'POST'});
  if (S._chatSwitchEpoch !== _epoch) return;
  if (typeof _journalStopTail === 'function') _journalStopTail();
  if (S._runAbortCtrl) { try { S._runAbortCtrl.abort(); } catch (_e) {} S._runAbortCtrl = null; }
  _cleanupLoadTimers();
  _chatFollow = true;
  if (window._plannerTickInterval) { clearInterval(window._plannerTickInterval); window._plannerTickInterval = null; }
  if (_tokenRafId) { cancelAnimationFrame(_tokenRafId); _tokenRafId = null; }
  _tokenQueue = [];
  document.getElementById('chat').innerHTML = '';
  S.pendingImgs = [];
  document.getElementById('img-preview').innerHTML = '';
  _cpReset();  // PANEL-SCOPE (2026-10-03): a new chat starts with an empty code panel
  S.currentChatId = null;
  S.currentChatWorkspace = '';
  S.currentChatMessages = [];
  S._lastDirectRunModel = '';
  if (typeof updateModelsChip === 'function') updateModelsChip();
  if (typeof _renderReasoningEffortUI === 'function') _renderReasoningEffortUI(S.reasoningEffort);
  setPauseBtnState('idle');
  setStopBtnState('idle');
  stopAskUserCountdown();
  _setPresetHeader();
  document.getElementById('h-complexity').style.display = 'none';
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

// SETTINGS-SAVE-VISIBILITY (2026-09-15): fire-and-forget /settings posts used
// to swallow failures — the UI showed a setting as changed while nothing was
// persisted. Central helper: on failure surface it and let the caller revert.
function _postSettingsFF(payload, onFail) {
  fetch('/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).then(function(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function(d) {
    if (d && d.ok === false) throw new Error(d.error || 'rejected');
    if (d && d.settings_rev) S.settingsRev = parseInt(d.settings_rev, 10) || S.settingsRev;
  }).catch(function(e) {
    console.error('[settings save failed]', e);
    showInfo('⚠ Setting could NOT be saved (' + (e && e.message || e) + ') — toggle reverted.');
    if (typeof onFail === 'function') onFail(e);
  });
}

function startAskUserCountdown(seconds) {
  stopAskUserCountdown();
  var _remaining = seconds;
  var _badge = document.getElementById('ask-user-countdown');
  if (!_badge) {
    _badge = document.createElement('span');
    _badge.id = 'ask-user-countdown';
    _badge.className = 'ask-user-countdown-badge';
    var _qDiv = document.getElementById('ask-user-card');
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
// DOWNSCALE (2026-10-01): longest edge cap keeps the base64 (and therefore
// the prompt tokens) predictable — the raw screenshot/phone photo used to go
// up unscaled. jpeg q0.9; images without a decodable size pass through.
// MIME GUARD (2026-10-04): gif/webp pass the image/* type check but are the
// formats the server-side decoder chokes on (animated frames, exotic webp
// profiles). They are ALWAYS re-encoded to jpeg via the canvas (first
// frame) even when small; if that fails, cb(null) and the caller drops the
// image with a visible notice instead of shipping dead bytes.
// WHITE FILL (2026-10-04): the canvas starts transparent and jpeg
// has no alpha - without the fill every transparent pixel turned BLACK.
// cb(url, meta) where meta = {reencoded, mime} so callers can surface the
// "first frame only" note for animations.
function _downscaleImage(dataUrl, cb) {
  var _mime = ((dataUrl || '').slice(0, 64).match(/^data:(image\/[a-z0-9.+-]+)[;,]/i) || [])[1] || '';
  var _risky = (_mime === 'image/gif' || _mime === 'image/webp');
  var _img = new Image();
  _img.onload = function() {
    var _edge = Math.max(_img.naturalWidth, _img.naturalHeight);
    if (!_edge) { cb(_risky ? null : dataUrl); return; }
    if (!_risky && _edge <= 1568) { cb(dataUrl); return; }
    var _k = 1568 / _edge;
    var _cv = document.createElement('canvas');
    _cv.width = Math.round(_img.naturalWidth * _k);
    _cv.height = Math.round(_img.naturalHeight * _k);
    var _cx = _cv.getContext('2d');
    _cx.fillStyle = '#ffffff';
    _cx.fillRect(0, 0, _cv.width, _cv.height);
    _cx.drawImage(_img, 0, 0, _cv.width, _cv.height);
    try { cb(_cv.toDataURL('image/jpeg', 0.9), {reencoded: true, mime: _mime}); }
    catch (e) { cb(_risky ? null : dataUrl); }
  };
  _img.onerror = function() { cb(_risky ? null : dataUrl); };
  _img.src = dataUrl;
}

function _imgConvertNotice(meta, name) {
  if (meta && meta.reencoded && (meta.mime === 'image/gif' || meta.mime === 'image/webp')) {
    showStatus('ℹ ' + (name ? name + ': ' : '') + meta.mime.replace('image/', '').toUpperCase()
      + ' converted to JPEG (first frame only for animations).');
  }
}

function handleImgs(input) {
  Array.from(input.files).forEach(function(file) {
    const r = new FileReader();
    r.onload = function(e) {
      _downscaleImage(e.target.result, function(_url, _meta) {
        if (!_url) { showStatus('⚠ ' + file.name + ': this GIF/WebP could not be converted to JPEG — image dropped.'); return; }
        _imgConvertNotice(_meta, file.name);
        S.pendingImgs.push({b64: _url.split(',')[1], preview: _url});
        renderImgPreview();
      });
    };
    r.readAsDataURL(file);
  });
  input.value = '';
}

function _isVisionCapableModel(m) {
  if (!m) return false;
  var pr = S.modelProfiles && (S.modelProfiles[m] || S.modelProfiles[(m || '').split(':')[0]]);
  if (pr && pr.vision) return true;
  var lo = String(m).toLowerCase();
  return ['qwen3-vl', 'llava', 'moondream', 'bakllava', 'minicpm-v', 'granite3.2', 'vision'].some(function(vm) {
    return lo.indexOf(vm) >= 0;
  });
}

function _anyInstalledVisionModel() {
  return (S.models || []).some(_isVisionCapableModel);
}

function _duoImageRoleModels() {
  var coder = S.duoCoderModel
    || (S.currentAssignments && S.currentAssignments.duo_coder && S.currentAssignments.duo_coder.model)
    || '';
  var planner = S.duoPlannerModel || '';
  if (!planner) {
    planner = (S.duoPlannerUseCoderCtx !== false && coder)
      ? coder
      : ((S.currentAssignments && S.currentAssignments.duo_planner && S.currentAssignments.duo_planner.model) || '');
  }
  return { planner: planner, coder: coder };
}

function _updateImgVisionHint() {
  var c = document.getElementById('img-preview');
  if (!c) return;
  var old = document.getElementById('img-vision-hint');
  if (old) old.remove();
  if (!S.pendingImgs.length) return;
  var hint = document.createElement('div');
  hint.id = 'img-vision-hint';
  hint.style.cssText = 'font-family:IBM Plex Mono,monospace;font-size:10px;margin-top:4px;padding:3px 8px;border-radius:3px;line-height:1.5';
  function _style(bg, col, brd) {
    hint.style.background = bg; hint.style.color = col; hint.style.border = '1px solid ' + brd;
  }
  if (S.mode === 'pipeline') {
    if (S.pipelineVisionDirect === true) {
      var _pRoles = ['analyst','refiner','critic','synthesizer'].filter(function(r) {
        return S.pipelineVisionRoles && S.pipelineVisionRoles[r];
      });
      if (!_pRoles.length) _pRoles = ['analyst'];
      var _pCapable = _pRoles.filter(function(r) {
        var am = (S.currentAssignments && S.currentAssignments[r] && S.currentAssignments[r].model) || '';
        return _isVisionCapableModel(am);
      });
      if (_pCapable.length) {
        _style('rgba(72,120,192,.12)', '#80b0e0', 'rgba(72,120,192,.3)');
        hint.textContent = '[Pipeline Direct] Raw image -> ' + _pCapable.join(', ')
          + (S.visionAgentEnabled && S.visionAgentModel ? (' (fallback Vision-Agent: ' + S.visionAgentModel + ')') : '') + '.';
      } else if (S.visionAgentModel) {
        _style('rgba(136,88,192,.12)', '#b090d0', 'rgba(136,88,192,.3)');
        hint.textContent = '[Pipeline Fallback] Checked roles are text-only — Vision-Agent (' + S.visionAgentModel + ') describes image.';
      } else {
        _style('rgba(176,64,64,.12)', '#d09090', 'rgba(176,64,64,.3)');
        hint.textContent = 'WARNING: checked Pipeline stages are text-only and no Vision-Agent model is selected.';
      }
    } else if (S.visionAgentModel) {
      _style('rgba(136,88,192,.12)', '#b090d0', 'rgba(136,88,192,.3)');
      hint.textContent = '[Describe first] Vision-Agent (' + S.visionAgentModel + ') describes the image before Analyst.';
    } else {
      _style('rgba(224,144,48,.12)', '#e0a040', 'rgba(224,144,48,.3)');
      hint.textContent = 'Describe mode needs a Vision-Agent model — pick one under Image Processing.';
    }
    c.appendChild(hint);
    return;
  }
  var _isDuo = S.mode === 'code_duo' || S._runIsDuo;
  if (!_isDuo) {
    var dModel = (S.currentAssignments && S.currentAssignments.direct && S.currentAssignments.direct.model) || '';
    if (S.mode === 'auto') {
      var _aDm = S.duoImageMode || ((S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct');
      var _aMdls = _duoImageRoleModels();
      var _aDirectLbl = (S.imageMode === 'preprocess' && S.visionModel)
        ? ('Describe (' + S.visionModel + ')')
        : (_isVisionCapableModel(dModel) ? ('Direct (' + dModel + ')')
          : (_anyInstalledVisionModel() ? 'Direct (auto-switch)' : 'none'));
      var _aDuoLbl = (_aDm === 'preprocess')
        ? (S.visionModel ? ('Describe (' + S.visionModel + ')') : 'needs Describer')
        : (function() {
            var _t = [];
            if (S.duoImageToPlanner) _t.push('Planner' + (_isVisionCapableModel(_aMdls.planner) ? '' : ' [no vision]'));
            if (S.duoImageToCoder && S.coderImagesOff !== true) _t.push('Coder' + (_isVisionCapableModel(_aMdls.coder) ? '' : ' [no vision]'));
            return _t.length ? _t.join('+') : 'no role checked';
          })();
      if (_aDirectLbl === 'none' && (_aDuoLbl === 'needs Describer' || _aDuoLbl === 'no role checked')) {
        _style('rgba(176,64,64,.12)', '#d09090', 'rgba(176,64,64,.3)');
      } else {
        _style('rgba(72,120,192,.12)', '#80b0e0', 'rgba(72,120,192,.3)');
      }
      hint.textContent = '[Auto] Direct: ' + _aDirectLbl + ' · Agentic: ' + _aDuoLbl + '.';
      c.appendChild(hint);
      return;
    }
    if (S.imageMode === 'preprocess' && S.visionModel) {
      _style('rgba(136,88,192,.12)', '#b090d0', 'rgba(136,88,192,.3)');
      hint.textContent = '[Describe first] (' + S.visionModel + ') turns image into text for ' + (dModel || 'Direct') + '.';
    } else if (S.imageMode === 'preprocess') {
      _style('rgba(224,144,48,.12)', '#e0a040', 'rgba(224,144,48,.3)');
      hint.textContent = 'Describe mode needs a Vision Describer model (open Image Processing).';
    } else if (_isVisionCapableModel(dModel)) {
      _style('rgba(72,120,192,.12)', '#80b0e0', 'rgba(72,120,192,.3)');
      hint.textContent = '[Direct multimodal] (' + dModel + ') sees the image directly.';
    } else if (_anyInstalledVisionModel()) {
      _style('rgba(72,120,192,.12)', '#80b0e0', 'rgba(72,120,192,.3)');
      hint.textContent = '[Direct auto-fallback] (' + (dModel || 'direct') + ' is text-only — switches to an installed multimodal model).';
    } else {
      _style('rgba(176,64,64,.12)', '#d09090', 'rgba(176,64,64,.3)');
      hint.textContent = 'WARNING: no multimodal model installed and Describe first is off — the image will be ignored.';
    }
    c.appendChild(hint);
    return;
  }
  // DUO: mirror resolve_image_plan (describe / raw-per-role / none)
  var _m = S.duoImageMode || ((S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct');
  var _duoMdls = _duoImageRoleModels();
  var _pModel = _duoMdls.planner;
  var _cModel = _duoMdls.coder;
  if (_m === 'preprocess') {
    if (S.visionModel) {
      _style('rgba(136,88,192,.12)', '#b090d0', 'rgba(136,88,192,.3)');
      hint.textContent = '[Describe first] (' + S.visionModel + ') describes the image — Planner and Coder get the text.';
    } else {
      _style('rgba(224,144,48,.12)', '#e0a040', 'rgba(224,144,48,.3)');
      hint.textContent = 'Describe mode needs a Vision Describer model — pick one under Image Processing.';
    }
  } else {
    var _okTargets = [];
    var _badTargets = [];
    if (S.duoImageToPlanner) {
      if (!_pModel || _isVisionCapableModel(_pModel)) _okTargets.push(_pModel ? ('Planner (' + _pModel + ')') : 'Planner');
      else _badTargets.push('Planner (' + _pModel + ')');
    }
    if (S.duoImageToCoder && S.coderImagesOff !== true) {
      if (!_cModel || _isVisionCapableModel(_cModel)) _okTargets.push(_cModel ? ('Coder (' + _cModel + ')') : 'Coder');
      else _badTargets.push('Coder (' + _cModel + ')');
    }
    if (!_okTargets.length && !_badTargets.length) {
      _style('rgba(122,143,168,.1)', '#7a8fa8', 'rgba(122,143,168,.25)');
      hint.textContent = 'No target selected: check Planner or Coder under Image Processing.';
    } else if (_okTargets.length && !_badTargets.length) {
      _style('rgba(32,176,160,.12)', '#20b0a0', 'rgba(32,176,160,.3)');
      hint.textContent = 'Raw image goes to: ' + _okTargets.join(' + ') + '.';
    } else if (_okTargets.length) {
      _style('rgba(224,144,48,.12)', '#e0a040', 'rgba(224,144,48,.3)');
      hint.textContent = 'Raw image -> ' + _okTargets.join(' + ') + ' (skipped for non-multimodal ' + _badTargets.join(' + ') + ').';
    } else {
      _style('rgba(224,144,48,.12)', '#e0a040', 'rgba(224,144,48,.3)');
      hint.textContent = 'WARNING: ' + _badTargets.join(' + ') + ' is not multimodal — switch to Describe first or pick a vision model.';
    }
  }
  c.appendChild(hint);
}

function _openImgZoom(src) {
  var _prev = document.getElementById('img-zoom-overlay');
  if (_prev && typeof _prev._imgZoomClose === 'function') {
    _prev._imgZoomClose();  // REOPEN-CLOSE (2026-10-03): plain remove() used to
  }                         // strand the previous overlay's keydown listener
  else if (_prev) _prev.remove();
  var _ov = document.createElement('div');
  _ov.id = 'img-zoom-overlay';
  _ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.86);z-index:99999;display:flex;align-items:center;justify-content:center;cursor:zoom-out';
  var im = document.createElement('img');
  im.src = src;
  im.style.cssText = 'max-width:92vw;max-height:92vh;object-fit:contain;border-radius:6px';
  _ov.appendChild(im);
  // CLOSE (2026-10-03): one close for BOTH paths - the click-close used to
  // leave the document keydown listener attached (one per open, piling up).
  var _close = function() {
    document.removeEventListener('keydown', _onKey);
    _ov.remove();
  };
  var _onKey = function(ev){ if (ev.key === 'Escape') _close(); };
  _ov._imgZoomClose = _close;
  _ov.addEventListener('click', _close);
  document.addEventListener('keydown', _onKey);
  document.body.appendChild(_ov);
}
function _wireImgZoom(imgEl) {
  if (!imgEl || imgEl.dataset.zoomWired) return;
  imgEl.dataset.zoomWired = '1';
  imgEl.style.cursor = 'zoom-in';
  imgEl.addEventListener('click', function(ev) {
    ev.preventDefault(); ev.stopPropagation();
    _openImgZoom(imgEl.currentSrc || imgEl.src);
  });
}

function _cpSnapshot() {
  var out = {};
  Object.keys(_cpFiles || {}).forEach(function(p) {
    var e = _cpFiles[p];
    out[p] = {
      content: e.content || '',
      op: e.op || 'edit',
      diffText: e.diffText || '',
      diffs: (e.diffs || []).map(function(d){ return { id: d.id, name: d.name || '', text: d.text || '' }; }),
      view: e.view === 'diff' ? 'diff' : 'file'
    };
  });
  return out;
}
function _cpRestoreSnapshot(snap) {
  Object.keys(snap || {}).forEach(function(p) {
    var e = snap[p];
    _cpAddOrUpdateFile(p, e.content || '', e.op || 'edit', false);
    var t = _cpFiles[p];
    if (!t) return;
    t.diffText = e.diffText || '';
    t.diffs = (e.diffs || []).slice();
    t.view = e.view || 'file';
  });
}
function _wireSavedChips(scope) {
  (scope || document).querySelectorAll('.tool-call-row').forEach(function(row) {
    if (row.dataset.savedWired) return;
    var pid = row.dataset.cpDiffId, tp = row.dataset.toolPath;
    if (!pid || !tp) return;
    var chip = row.querySelector('.tool-call-chip');
    if (!chip) return;
    row.dataset.savedWired = '1';
    chip.addEventListener('click', function(ev) {
      ev.preventDefault(); ev.stopPropagation();
      var e = _cpFiles[tp];
      if (!e) return;
      var d = (e.diffs || []).filter(function(x){ return x.id === pid; })[0];
      if (d) { e.pinnedCallId = d.id; e.view = 'diff'; }
      _cpShowFile(tp);
      toggleCodePanel(true);
    });
  });
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
    _wireImgZoom(im);
    wrap.appendChild(im); wrap.appendChild(rm);
    c.appendChild(wrap);
  });
  if (S.pendingImgs.length) {
    _updateImgVisionHint();
    return;
  }
}


// -- Chat History ----------------------------------------------
// RUN-STATS LINE (2026-10-23): one muted line under the final answer —
// DeepSeek-style: small by default, the full detail (model, prompt, requests)
// on hover. Persisted with the chat as msg.stats so a reloaded chat shows it.
function _fmtStatsLine(stats) {
  var bits = [];
  var s = parseFloat(stats.elapsed) || 0;
  if (s >= 60) bits.push(Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's');
  else if (s > 0) bits.push(Math.round(s * 10) / 10 + 's');
  if (stats.out > 0) bits.push(_fmtTokens(stats.out) + ' tok');
  if (stats.cached > 0) bits.push(_fmtTokens(stats.cached) + ' cached');
  if (stats.tps > 0) bits.push((Math.round(stats.tps * 10) / 10) + ' t/s');
  return bits.join(' \u00b7 ');
}

function renderRunStatsLine(stats, footEl) {
  if (!stats) return;
  var foot = footEl;
  if (!foot) {
    var blocks = document.querySelectorAll('#chat .ablock');
    if (!blocks.length) return;
    var lastBlock = blocks[blocks.length - 1];
    foot = lastBlock.querySelector(':scope > .amsg-foot') || lastBlock.querySelector('.amsg-foot');
    if (!foot) {
      foot = document.createElement('div');
      foot.className = 'amsg-foot';
      lastBlock.appendChild(foot);
    }
  }
  var old = foot.querySelector('.msg-stats');
  if (old) old.remove();
  var line = _fmtStatsLine(stats);
  if (!line && !stats.model) return;
  var sp = document.createElement('span');
  sp.className = 'msg-stats';
  sp.textContent = line;
  var title = line;
  if (stats.model) title += (title ? ' \u00b7 ' : '') + stats.model;
  if (stats.prompt > 0) title += ' \u00b7 ' + _fmtTokens(stats.prompt) + ' prompt';
  if (stats.requests > 0) title += ' \u00b7 ' + stats.requests + ' requests';
  sp.title = title;
  // AUDIT (2026-10-24): stash the STRUCTURED stats on the footer so every
  // later autosave can re-attach them — the one-shot _saved flag lost the
  // line on any persist after the run (pagehide beacon, next-run flush).
  try { foot.dataset.stats = JSON.stringify(stats); } catch (e) {}
  foot.insertBefore(sp, foot.firstChild);
}

function _autosaveChatMessage() {
  // Collect current chat DOM as messages and store in S.currentChatMessages
  const msgs = [];
  document.querySelectorAll('#chat .msg').forEach(function(el) {
    if (el.classList.contains('msg-user')) {
      const bubble = el.querySelector('.bubble');
      // IMAGE RESTORE (2026-10-03): thumbnails persist only when the
      // uploads toggle is ON (files live in workspace .hive_uploads then).
      var _uims = [];
      if (S.imagePersist) {
        el.querySelectorAll('.img-thumbs img').forEach(function(im) {
          _uims.push({ preview: im.getAttribute('src') || '' });
        });
      }
      if (bubble) msgs.push({role: 'user', content: bubble.textContent, ts: Date.now(), images: _uims});
    } else if (el.classList.contains('ablock')) {
      const aname = el.querySelector('.aname');
      // abody can be directly or in a duo-coder/duo-critic wrapper
      const abody = el.querySelector('.abody') || el.querySelector('.duo-coder .abody') || el.querySelector('.duo-critic .abody');
      var _cleanContent = abody ? _historyTextOnly(abody) : '';
      if (aname && abody && (_cleanContent || abody.textContent.trim())) {
        // CHAT-STRUCTURE-FIX (2026-08-07): also save the rendered structure (markdown blocks,
        // code snippets, tool chips), otherwise the live structure is lost
        // and the chat is shown as flat text on load.
        // HISTORY-CHROME-FIX (2026-11-21): store ONLY the clean answer text in
        // `content` (stripped of tool chips, [UNTRUSTED CONTENT] tool results,
        // and inline think blocks). Saving raw `abody.textContent` poisoned
        // follow-up turns into imitating DOM tool chips as plain text and
        // truncated the actual answer past `assistant_cap`. `html` still keeps
        // the full visual DOM for reload.
        // CP SNAPSHOT (2026-10-03): persist the code-panel state (diffs per
        // path) so tool chips stay clickable in the LOADED chat too.
        var _stMsg = {role: 'assistant', agent: aname.textContent, content: _cleanContent,
                      html: abody.innerHTML, ts: Date.now()};
        // AUDIT (2026-10-24): re-read the structured stats from the footer
        // (dataset stash) — every persist must carry the stats line, not
        // only the first one after the run ended.
        var _stFoot = el.querySelector('.amsg-foot[data-stats]');
        if (_stFoot && _stFoot.dataset.stats) {
          try { _stMsg.stats = JSON.parse(_stFoot.dataset.stats); } catch (e) {}
        }
        msgs.push(_stMsg);
      }
    } else if (el.classList.contains('steer-note')) {
      // STEER CARD PERSISTENCE (2026-10-06): save as an assistant part —
      // the existing restore path renders it as it looked, no new format.
      var _steerTxt = (el.textContent || '').trim();
      if (_steerTxt) {
        var _steerClone = el.cloneNode(true);
        _steerClone.querySelectorAll('[id]').forEach(function(x) { x.removeAttribute('id'); });
        msgs.push({role: 'assistant', agent: 'Steer', part: true,
                   content: _steerTxt, html: _steerClone.innerHTML,
                   ts: Date.now()});
      }
    } else if (el.classList.contains('planner-bubble') ||
               el.classList.contains('planner-think-block') ||
               el.classList.contains('planner-plan-block') ||
               el.classList.contains('plan-chk')) {
      // PLANNER PERSISTENCE, small variant (2026-10-04): planner bubble,
      // thinking block, plan text and the subtask checklist used to be
      // DOM-only and died with the page. They save as plain assistant
      // parts (content + html of the whole block) - no new format, the
      // existing restore path renders them as normal bubbles.
      var _pTxt = (el.textContent || '').trim();
      if (_pTxt) {
        // strip live ids from the saved html: restored blocks must not
        // carry planner-think-block & friends twice - the next planner run
        // does getElementById and would hit the restored ghost first
        var _clone = el.cloneNode(true);
        _clone.querySelectorAll('[id]').forEach(function(x) { x.removeAttribute('id'); });
        msgs.push({role: 'assistant', agent: 'Planner', part: true,
                   content: _pTxt, html: _clone.innerHTML,
                   ts: Date.now()});
      }
    }
  });
  // RUN-STATS (2026-10-23): attach the run's stats to the final answer so a
  // reloaded chat shows the same muted line. The flag makes the attach happen
  // exactly once per run (a stale object can never reach the next run).
  if (S._lastRunStats && !S._lastRunStats._saved) {
    for (var _i = msgs.length - 1; _i >= 0; _i--) {
      if (msgs[_i].role === 'assistant' && !msgs[_i].part) {
        msgs[_i].stats = S._lastRunStats;
        S._lastRunStats._saved = true;
        break;
      }
    }
  }
  // CODE-PANEL-SNAPSHOT: store cumulative code-panel state once on the last
  // assistant turn instead of cloning _cpFiles into every bubble (O(N*M) bloat).
  if (Object.keys(_cpFiles || {}).length > 0) {
    for (var _ci = msgs.length - 1; _ci >= 0; _ci--) {
      if (msgs[_ci].role === 'assistant' && !msgs[_ci].part) {
        msgs[_ci].cp = _cpSnapshot();
        break;
      }
    }
  }
  S.currentChatMessages = msgs;
}

function chatAutosaveToggle(el) {
  S.chatAutosave = !!el.checked;
  postSettings({chat_autosave_enabled: S.chatAutosave});
  showStatus(S.chatAutosave ? 'Auto-save enabled.' : 'Auto-save disabled.');
}

function imagePersistToggle(el) {
  S.imagePersist = !!el.checked;
  postSettings({image_uploads_persistent: S.imagePersist});
  showStatus(S.imagePersist
    ? 'Uploaded images are kept in workspace .hive_uploads.'
    : 'Uploaded images are temporary: available during the run, removed after.');
}

async function persistCurrentChat(silent) {
  // SERIALIZE (2026-11-05): two saves that both ran before the first POST
  // returned saw currentChatId=null and each created a chat - the history
  // filled with identical one-message entries. Wait for the in-flight save,
  // then update the chat it created.
  if (S._chatPersistPromise) {
    try { await S._chatPersistPromise; } catch (e) {}
  }
  const _p = _persistCurrentChatNow(silent);
  S._chatPersistPromise = _p;
  try {
    return await _p;
  } finally {
    if (S._chatPersistPromise === _p) S._chatPersistPromise = null;
  }
}

async function _persistCurrentChatNow(silent) {
  _autosaveChatMessage();
  if (!S.currentChatMessages.length) return false;
  // Generate title from first user message (only on creation; existing titles stay)
  const firstUser = S.currentChatMessages.find(function(m){return m.role==='user';});
  const title = firstUser ? firstUser.content.slice(0, 48) : 'Chat ' + new Date().toLocaleTimeString();
  try {
    if (S.currentChatId) {
      // Update existing — do not overwrite the title (preserve user-named titles)
      var _putRes = await fetch('/chats/' + S.currentChatId, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        // CHAT-WS-PRESERVE: keep the chat's own workspace (global setting
        // must not re-point an existing chat's run chain).
        body: JSON.stringify({messages: S.currentChatMessages, workspace: S.currentChatWorkspace || S.workspace || '', base_rev: S.currentRev || 0})
      });
      if (_putRes.status === 409) {
        // SERVER WINS (2026-10-03): adopt the saved messages, keep only
        // what this tab still holds unsent is the backend's call now.
        var _srv = await _putRes.json().catch(function () { return null; });
        if (_srv && _srv.messages) {
          S.currentChatMessages = _srv.messages;
          S.currentRev = _srv.rev || 0;
          showStatus('Chat changed elsewhere - adopted the saved version.');
        }
      } else if (_putRes.ok) {
        var _pj = await _putRes.json().catch(function () { return {}; });
        if (_pj.rev) S.currentRev = _pj.rev;
      }
    } else {
      // Create new
      const r = await (await fetch('/chats', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, messages: S.currentChatMessages, workspace: S.workspace || ''})
      })).json();
      if (r && r.id) { S.currentChatId = r.id; S.currentRev = r.rev || 0; S.currentChatWorkspace = S.workspace || ''; }
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

function _fmtSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

var _chatSearchTimer = null;
function searchChatHistory(q) {
  if (_chatSearchTimer) clearTimeout(_chatSearchTimer);
  var trimmed = String(q || '').trim();
  if (trimmed.length < 2) {
    loadChatHistory();
    return;
  }
  _chatSearchTimer = setTimeout(async function() {
    try {
      const res = await fetch('/chats/search?q=' + encodeURIComponent(trimmed) + '&limit=30');
      const d = await res.json();
      const c = document.getElementById('chat-history-list');
      if (!c) return;
      const hits = d.results || [];
      if (!hits.length) {
        c.innerHTML = '<div class="empty">No matching chats found.</div>';
        return;
      }
      c.innerHTML = '';
      hits.forEach(function(ch) {
        const item = document.createElement('div');
        item.className = 'chat-hist-item' + (ch.id === S.currentChatId ? ' active' : '');
        item.title = 'Click to load';
        const dot = document.createElement('div'); dot.className = 'chat-hist-dot';
        const info = document.createElement('div'); info.className = 'chat-hist-info';
        const title = document.createElement('div'); title.className = 'chat-hist-title';
        title.textContent = ch.title || 'Untitled';
        const prev = document.createElement('div'); prev.className = 'chat-hist-preview';
        prev.textContent = ch.snippet || '';
        const meta = document.createElement('div'); meta.className = 'chat-hist-meta';
        meta.textContent = (ch.match_in || 'title') + (ch.updated_at ? ' \u00B7 ' + String(ch.updated_at).slice(0, 16) : '');
        info.appendChild(title); info.appendChild(prev); info.appendChild(meta);
        item.appendChild(dot); item.appendChild(info);
        item.addEventListener('click', function() { loadChat(ch.id); });
        c.appendChild(item);
      });
    } catch (e) {}
  }, 160);
}

async function loadChatHistory() {
  var _inp = document.getElementById('chat-search-input');
  if (_inp && _inp.value && _inp.value.trim().length >= 2) {
    searchChatHistory(_inp.value);
    return;
  }
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
      // CHAT SIZE (2026-10-05, user): json + blobs, right after the date
      if (ch.bytes > 0) _metaParts.push(_fmtSize(ch.bytes));
      _metaParts.push((ch.updated_at || '').slice(0, 16));
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
  var _epoch = (S._chatSwitchEpoch = (S._chatSwitchEpoch || 0) + 1);
  try {
    const _chatRes = await fetch('/chats/' + chatId);
    if (S._chatSwitchEpoch !== _epoch) return;
    const chat = await _chatRes.json();
    if (S._chatSwitchEpoch !== _epoch) return;
    // Clear session and reload
    await fetch('/memory/clear_session', {method:'POST'});
    if (S._chatSwitchEpoch !== _epoch) return;
    document.getElementById('chat').innerHTML = '';
    _chatFollow = true;

    S.currentChatId = chatId;
    S.currentRev = chat.rev || 0;
    S.currentChatMessages = chat.messages || [];
    setPauseBtnState('idle');
    setStopBtnState('idle');
    stopAskUserCountdown();

    // UI-WORKSPACE (2026-08-07): Feld zeigt den im Chat persistierten Workspace
    // (sonst das globale Setting) — Anzeige nur, das Setting bleibt unveraendert.
    var _wsField = document.getElementById('workspace-input');
    if (_wsField) _wsField.value = (chat.workspace || S.workspace || '');
    // CHAT-WS-PRESERVE (2026-10-25 audit): remember the chat's OWN workspace;
    // autosave (PUT /chats, pagehide beacon) used to rewrite chat.workspace
    // with the GLOBAL setting, silently re-pointing the chat's run chain.
    S.currentChatWorkspace = String(chat.workspace || '');

    // Restore chat DOM from saved messages
    _cpReset();  // PANEL-SCOPE (2026-10-03): snapshots must not merge across chats
    // CHAT RESTORE: restore cumulative code-panel snapshot once from the last
    // message carrying `.cp` rather than re-replaying every bubble's snapshot.
    var _lastCp = null;
    for (var _mi = (chat.messages || []).length - 1; _mi >= 0; _mi--) {
      var _mm = chat.messages[_mi];
      if (_mm && _mm.cp && typeof _mm.cp === 'object' && Object.keys(_mm.cp).length > 0) { _lastCp = _mm.cp; break; }
    }
    if (_lastCp) _cpRestoreSnapshot(_lastCp);
    (chat.messages || []).forEach(function(msg) {
      if (msg.role === 'user') {
        addUserMsg(msg.content, (msg.images || []).map(function(i) { return { preview: i.preview }; }));
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
        // Wire the restored tool chips to the restored code-panel diffs.
        _wireSavedChips(abody);
        wrap.appendChild(hdr);
        wrap.appendChild(abody);
        // RUN-STATS (2026-10-23): the saved per-answer stats line comes back
        // with the chat — same muted footer as live.
        if (msg.stats && typeof renderRunStatsLine === 'function') {
          var _stFoot = document.createElement('div');
          _stFoot.className = 'amsg-foot';
          wrap.appendChild(_stFoot);
          renderRunStatsLine(msg.stats, _stFoot);
        }
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
    showInfo('Chat loaded: ' + chat.title.slice(0,40));
    loadChatHistory();  // refresh active state
    try {
      var _psResp = await fetch('/pause-state/' + chatId);
      if (S._chatSwitchEpoch !== _epoch) return;
      var _psData = await _psResp.json();
      if (S._chatSwitchEpoch !== _epoch) return;
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


// ── DUO IMAGE PLAN UI (2026-10-01) ─────────────────────────────────────────
// Radio (preprocess | direct) + per-role checkboxes; sentinel null is
// derived the same way the backend does (active vision cfg -> preprocess).
// Role status uses the SAME check as the backend: modelProfiles vision AND
// a resolvable projector file (GET /vision/mmproj).
function setDuoImageMode(m) {
  S.duoImageMode = m;
  S._duoImageModeExplicit = true;
  postSettings({duo_image_mode: m});
  updateDuoImageUI();
}
function setDuoImageRole(role, checked) {
  if (role === 'planner') { S.duoImageToPlanner = checked; postSettings({duo_image_to_planner: checked}); }
  else { S.duoImageToCoder = checked; postSettings({duo_image_to_coder: checked}); }
  updateDuoImageUI();
}
function updateDuoImageUI() {
  var _sec = document.getElementById('duo-image-sec');
  if (!_sec) return;
  var _isDuo = (S.mode === 'code_duo');
  var _isAuto = (S.mode === 'auto');
  var _isPipe = (S.mode === 'pipeline');
  var _showDirect = (!_isDuo && !_isPipe);
  var _showDuo = (_isDuo || _isAuto);
  var _m = S.duoImageMode || ((S.visionEnabled && S.visionModel) ? 'preprocess' : 'direct');
  var _imgMode = (S.imageMode === 'preprocess') ? 'preprocess' : 'direct';
  var _pipeDir = (S.pipelineVisionDirect === true);

  var _directSec = document.getElementById('direct-image-sec');
  var _pipeSec = document.getElementById('im-pipeline-sec');
  var _preproSec = document.getElementById('im-preprocess-sec');
  var _flowEl = document.getElementById('vision-flow-card');
  if (_flowEl) _flowEl.style.display = '';
  _sec.style.display = _showDuo ? '' : 'none';
  _sec.style.borderTop = _isAuto ? '1px dashed #222a38' : '';
  _sec.style.marginTop = _isAuto ? '8px' : '';
  _sec.style.paddingTop = _isAuto ? '10px' : '2px';
  var _duoHdr = document.getElementById('duo-image-sec-hdr');
  if (_duoHdr) {
    _duoHdr.innerHTML = _isAuto
      ? 'Coding / <b style="color:#20b0a0">Agentic</b> routes in <b>Auto</b>:'
      : 'How attached images are handled in <b style="color:#20b0a0">Agentic / Code Duo</b>:';
  }
  if (_directSec) _directSec.style.display = _showDirect ? '' : 'none';
  if (_pipeSec) _pipeSec.style.display = _isPipe ? '' : 'none';
  if (_preproSec) {
    var _showPrepro = (_showDuo && _m === 'preprocess') || (_showDirect && _imgMode === 'preprocess');
    _preproSec.style.display = _showPrepro ? 'block' : 'none';
  }

  // Direct / Auto controls
  var _ghdr = document.getElementById('image-mode-generic-hdr');
  if (_ghdr) {
    _ghdr.innerHTML = _isAuto
      ? '<b>Chat / Direct</b> routes in <b>Auto</b>:'
      : 'How attached images are handled in <b>Chat / Direct</b>:';
  }
  var _dsEl = document.getElementById('im-direct-sec');
  if (_dsEl) _dsEl.style.display = (_showDirect && _imgMode !== 'preprocess') ? 'block' : 'none';
  var _bdBtn = document.getElementById('im-direct');
  var _bpBtn = document.getElementById('im-preprocess');
  if (_bdBtn) _bdBtn.classList.toggle('on', _imgMode !== 'preprocess');
  if (_bpBtn) _bpBtn.classList.toggle('on', _imgMode === 'preprocess');
  if (_showDirect && _imgMode !== 'preprocess') {
    var _dMdl = (S.currentAssignments && S.currentAssignments.direct && S.currentAssignments.direct.model) || '';
    _updateDuoRoleStatus('direct-img-status', 'Direct', _dMdl, {
      nonVisionSuffix: _anyInstalledVisionModel()
        ? " \u2014 auto-switches to installed multimodal model"
        : " \u2014 can't see images"
    });
  }

  // Agentic / Code Duo controls (same 2-button bar, also shown inside Auto)
  var _dbDir = document.getElementById('duo-im-direct');
  var _dbPre = document.getElementById('duo-im-preprocess');
  if (_dbDir) _dbDir.classList.toggle('on', _m !== 'preprocess');
  if (_dbPre) _dbPre.classList.toggle('on', _m === 'preprocess');
  var _duoDirectSec = document.getElementById('duo-direct-sec');
  if (_duoDirectSec) _duoDirectSec.style.display = (_m !== 'preprocess') ? 'block' : 'none';
  var _roles = document.getElementById('duo-image-roles');
  if (_roles) _roles.style.display = (_m !== 'preprocess') ? 'block' : 'none';
  var _pChk = document.getElementById('duo-img-planner');
  var _cChk = document.getElementById('duo-img-coder');
  if (_pChk) _pChk.checked = S.duoImageToPlanner === true;
  if (_cChk) _cChk.checked = S.duoImageToCoder === true;

  var _cio = S.coderImagesOff === true;
  var _cavDuo = document.getElementById('coder-always-vision-toggle-duo');
  var _togglesRow = document.getElementById('duo-coder-img-toggles');
  if (_togglesRow) _togglesRow.style.display = (_showDuo && _m !== 'preprocess') ? '' : 'none';
  if (_cavDuo) { _cavDuo.checked = !_cio && S.coderAlwaysVision === true; _cavDuo.disabled = _cio; }
  var _cioToggle = document.getElementById('coder-images-off-toggle');
  if (_cioToggle) _cioToggle.checked = _cio;
  if (_cChk) { _cChk.disabled = _cio; if (_cio) _cChk.checked = false; }

  if (_showDuo && _m !== 'preprocess') {
    var _duoMdls = _duoImageRoleModels();
    _updateDuoRoleStatus('duo-img-planner-status', 'Planner', _duoMdls.planner);
    _updateDuoRoleStatus('duo-img-coder-status', 'Coder', _duoMdls.coder);
  }
  var _note = document.getElementById('duo-image-note');
  if (_note) {
    if (_m === 'preprocess') { _note.style.display = 'none'; }
    else {
      var _t = [];
      if (S.duoImageToPlanner) _t.push('Planner');
      if (S.duoImageToCoder && S.coderImagesOff !== true) _t.push('Coder');
      if (_t.length) {
        _note.textContent = '→ Raw image goes to: ' + _t.join(' + ')
          + (S.coderImagesOff === true ? ' (coder images OFF)' : '');
      } else {
        _note.textContent = S.coderImagesOff === true
          ? '→ Coder takes NO images (toggle on)'
          : '→ No target selected: pick Planner and/or Coder above (or switch to Describe first)';
      }
      _note.style.display = 'block';
    }
  }
  var _dinfo = document.getElementById('duo-image-info');
  if (_dinfo) _dinfo.style.display = (_showDuo && _m !== 'preprocess') ? 'block' : 'none';
  var _trap = document.getElementById('duo-image-prepro-warn');
  if (_trap) _trap.style.display = (_showDuo && _m === 'preprocess' && !S.visionModel) ? 'block' : 'none';

  // Pipeline controls (same 2-button bar + live role statuses)
  var _pbDir = document.getElementById('pipe-im-direct');
  var _pbPre = document.getElementById('pipe-im-preprocess');
  if (_pbDir) _pbDir.classList.toggle('on', _pipeDir);
  if (_pbPre) _pbPre.classList.toggle('on', !_pipeDir);
  var _pDirSec = document.getElementById('pipe-direct-sec');
  if (_pDirSec) _pDirSec.style.display = (_isPipe && _pipeDir) ? 'block' : 'none';
  var _pVaPicker = document.getElementById('pipe-va-picker');
  if (_pVaPicker) _pVaPicker.style.display = (_isPipe && (!_pipeDir || S.visionAgentEnabled)) ? 'block' : 'none';
  var _pFullRow = document.getElementById('full-imgdesc-row');
  if (_pFullRow) _pFullRow.style.display = (_isPipe && (!_pipeDir || S.visionAgentEnabled)) ? 'block' : 'none';
  var _pWarn = document.getElementById('pipe-image-prepro-warn');
  if (_pWarn) _pWarn.style.display = (_isPipe && !_pipeDir && !S.visionAgentModel) ? 'block' : 'none';
  var _pNote = document.getElementById('pipe-image-note');
  if (_isPipe && _pipeDir) {
    var _pRoleList = [['analyst','Analyst'],['refiner','Refiner'],['critic','Critic'],['synthesizer','Synthesizer']];
    var _pChecked = [];
    _pRoleList.forEach(function(pair) {
      var rk = pair[0], rlbl = pair[1];
      var rm = (S.currentAssignments && S.currentAssignments[rk] && S.currentAssignments[rk].model) || '';
      _updateDuoRoleStatus('pipe-img-' + rk + '-status', rlbl, rm);
      if (S.pipelineVisionRoles && S.pipelineVisionRoles[rk]) _pChecked.push(rlbl);
    });
    if (_pNote) {
      _pNote.textContent = _pChecked.length
        ? ('→ Raw image goes to: ' + _pChecked.join(' + '))
        : '→ Default fallback: Analyst (pick stage checkboxes above)';
      _pNote.style.display = 'block';
    }
  } else if (_pNote) {
    _pNote.style.display = 'none';
  }

  if (S.pendingImgs && S.pendingImgs.length) _updateImgVisionHint();
  if (typeof _updateVisionPreview === 'function') { try { _updateVisionPreview(); } catch (e) {} }
}
function _updateDuoRoleStatus(elId, label, model, opts) {
  var _el = document.getElementById(elId);
  if (!_el) return;
  if (!model) { _el.textContent = label; return; }
  var _prof = S.modelProfiles && (S.modelProfiles[model] || S.modelProfiles[(model||'').split(':')[0]]);
  var _vision = !!(_prof && _prof.vision) || _isVisionCapableModel(model);
  if (!_vision) {
    _el.textContent = label + ' (' + model + ')' + ((opts && opts.nonVisionSuffix) || " — can't see images");
    return;
  }
  function _renderMM(d) {
    if (_el.dataset && _el.dataset.mmModel !== model) return;
    if (d && d.projector) _el.textContent = label + ' (' + model + ') \uD83D\uDC41 sees images';
    else _el.textContent = label + ' (' + model + ') — mmproj missing, image will be skipped';
  }
  if (_el.dataset) _el.dataset.mmModel = model;
  if (S._mmprojCache && S._mmprojCache[model] !== undefined) {
    _renderMM(S._mmprojCache[model]);
    return;
  }
  _el.textContent = label + ' (' + model + ') \uD83D\uDC41 checking projector\u2026';
  fetch('/vision/mmproj?model=' + encodeURIComponent(model))
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (!S._mmprojCache) S._mmprojCache = {};
      S._mmprojCache[model] = d;
      _renderMM(d);
    })
    .catch(function() { _el.textContent = label + ' (' + model + ')'; });
}

