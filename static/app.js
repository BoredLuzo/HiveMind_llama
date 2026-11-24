// app.js — HiveMind Web UI bootstrap (all subsystems live in static/js/*.js)

// -- Init -------------------------------------------------------
// FIX: init is now sequential and more robust.
// loadModels must complete before renderAgentCards.
// BOOT-HARDENING (2026-09-15): every step wrapped individually — one failed
// fetch (backend momentarily down) used to abort the WHOLE boot chain
// silently, leaving a broken UI with no explanation.
async function init() {
  const cardsEl = document.getElementById('agent-cards');
  if (cardsEl) cardsEl.innerHTML = '<div class="loading-msg">&#9679; Loading models...</div>';

  async function _step(name, fn) {
    try { await fn(); }
    catch (e) {
      console.error('[boot] ' + name + ' failed:', e);
      window._showErrorToast && window._showErrorToast('Boot step "' + name + '" failed: ' + (e && e.message || e));
    }
  }

  _step('loadChatHistory', loadChatHistory);  // non-blocking: populate Saved Chats immediately alongside model/settings boot
  await _step('loadModels', loadModels);
  await _step('clear_session', () => fetch('/memory/clear_session', {method:'POST'}).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); }));  // Seitenreload = neuer Chat
  await _step('loadAgentMap', loadAgentMap);
  await _step('loadSettings', loadSettings);    // renderAgentCards now uses S.currentAssignments
  if (S.duoWebsearch || S.pipelineWebsearch || S.automapPipelineWebsearch) checkWebsearchStatus();
  await _step('loadPresets', loadPresets);
  await _step('loadMemory', loadMemory);
  await _step('loadVisionConfig', loadVisionConfig);
  await _step('loadSpecialAgentsConfig', loadSpecialAgentsConfig);
  _step('loadSoulStatus', loadSoulStatus);
  _step('buildAgentForceButtons', buildAgentForceButtons);
  try {
    document.getElementById('h-dot').className = 'on';
  } catch (e) { /* cosmetic */ }
}


function _wireAppEvents() {
  // -- Input Events -----------------------------------------------
  document.getElementById('input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });
  document.getElementById('input').addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 140) + 'px';
    _updateSendState();
  });
  _updateSendState();
  document.getElementById('input').addEventListener('paste', function(e) {
    Array.from(e.clipboardData.items).forEach(function(item) {
      if (!item.type.startsWith('image/')) return;
      const r = new FileReader();
      r.onload = function(ev) {
        // PASTE-DOWNSCALE (2026-10-03): same 1568px path as drag&drop:
        // screenshots pasted full-res used to skip it entirely.
        _downscaleImage(ev.target.result, function(_url, _meta) {
          if (!_url) { showStatus('⚠ Pasted GIF/WebP could not be converted to JPEG — image dropped.'); return; }
          _imgConvertNotice(_meta, '');
          S.pendingImgs.push({b64: _url.split(',')[1], preview: _url});
          renderImgPreview();
        });
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
        _downscaleImage(ev.target.result, function(_url, _meta) {
          if (!_url) { showStatus('⚠ ' + file.name + ': this GIF/WebP could not be converted to JPEG — image dropped.'); return; }
          _imgConvertNotice(_meta, file.name);
          S.pendingImgs.push({b64: _url.split(',')[1], preview: _url});
          renderImgPreview();
        });
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
      if (btn.dataset.p === 'automations') loadAutomations();
    });
  });

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
      workspace: S.currentChatWorkspace || S.workspace || '',
      title: S.currentChatId ? undefined : title   // don't overwrite existing titles
    };
    try {
      navigator.sendBeacon('/chats/persist', new Blob([JSON.stringify(payload)], {type: 'application/json'}));
    } catch(e) {}
  });

  // Reattach shortly after page load (script sits at end of body, #chat exists).
  setTimeout(_journalInitAttach, 1500);
  // LIVE MIRROR (2026-10-06, owner): a run started on the PHONE (or another
  // tab) while this UI sits idle now appears here too - poll the journal
  // every 10 s and attach to any new active run.
  setInterval(function() { if (!document.hidden) _journalInitAttach(); }, 10000);
}

// -- Start ------------------------------------------------------
if (typeof document !== 'undefined') {
  _wireAppEvents();
  init();
  _initAdvancedSettingsDrawer();
  _initModelProfilesModal();
}
