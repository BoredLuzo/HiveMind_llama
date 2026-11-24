// HiveMind UI module: Voice STT, Push-to-Talk, TTS & Read-Aloud queue.
// ── VOICE CARD (1.4) ─────────────────────────────────────────────────────────
// Opt-in by design (owner decision): while stt_enabled is off this module only
// renders nothing - no download, no microphone, no process. When it is on, the
// card shows one honest status line (engine / model / ready / error) and a
// Download button for whatever is missing, plus a 3 s microphone test that
// posts to /stt and shows the transcript in place (never starts a run).
var _voicePollTimer = null;

function _voiceStatusText(st) {
  if (!st) return 'status unavailable';
  if (st.error) return 'status error: ' + st.error;
  if (!st.engine_installed) {
    return 'engine not installed - use Download here, deploy\\setup_voice.bat, '
           + 'or the installer';
  }
  if (!st.model_installed) {
    return 'model missing: ' + (st.model_label || st.model)
           + ' - use Download here or deploy\\setup_voice.bat';
  }
  var bits = ['ready', st.model_label || st.model];
  if (st.threads) bits.push(st.threads + ' threads');
  if (st.warm) bits.push('warm');
  return bits.join(' \u00b7 ');
}

function _voiceErrorText(j) {
  if (!j) return 'no answer from the engine';
  var map = {
    STT_DISABLED: 'speech to text is off in the settings',
    STT_NO_ENGINE: 'engine not installed - use Download',
    STT_NO_MODEL: 'model not installed - use Download',
    STT_NO_AUDIO_LIB: 'audio support missing: pip install -r requirements-voice.txt',
    STT_DECODE_FAILED: 'the audio could not be decoded',
    STT_TIMEOUT: 'the engine took too long',
    STT_EMPTY: 'no speech detected',
    STT_FAILED: 'the engine failed - see logs/',
    STT_TRANSLATE_UNSUPPORTED: 'this model cannot translate'
  };
  var code = (j && j.code) || '';
  return map[code] || (j.error || code || 'unknown error');
}

async function _voiceCardRefresh() {
  var box = document.getElementById('stt-options');
  var on = !!(S && S.sttEnabled);
  if (typeof _syncMicBtn === 'function') _syncMicBtn();
  if (box) box.style.display = on ? 'block' : 'none';
  if (!on) {
    if (_voicePollTimer) { clearInterval(_voicePollTimer); _voicePollTimer = null; }
    return;
  }
  if (_voicePollTimer && typeof document !== 'undefined' && document.hidden) return;
  var line = document.getElementById('stt-status-line');
  var dlRow = document.getElementById('stt-download-row');
  var btn = document.getElementById('stt-download-btn');
  var prog = document.getElementById('stt-progress');
  var modelSel = document.getElementById('stt-model-sel');
  var langSel = document.getElementById('stt-language-sel');
  var thrSel = document.getElementById('stt-threads-sel');
  var maxEl = document.getElementById('stt-max-seconds');
  try {
    var r = await fetch('/stt/status', {cache: 'no-store'});
    var st = await r.json();
    if (modelSel && st.available_models) {
      var want = S.sttModel || st.model;
      if (modelSel.options.length !== st.available_models.length) {
        modelSel.innerHTML = '';
        st.available_models.forEach(function (m) {
          var o = document.createElement('option');
          o.value = m.key;
          o.textContent = m.label + ' - ' + Math.round(m.size_mb) + ' MB' +
            (m.installed ? '' : ' (not installed)');
          modelSel.appendChild(o);
        });
      }
      if (want) modelSel.value = want;
      // MODEL-HINT (2026-10-18): the dropdown lists the whole tier table, which
      // reads as "why are these not downloaded?". Say it: one default is picked
      // by RAM, the rest are optional and only fetched when you press Download.
      var _mh = document.getElementById('stt-model-hint');
      if (_mh) {
        var _def = (st.available_models || []).filter(function (m) {
          return m.key === st.model;
        })[0] || {};
        var _miss = (st.available_models || []).filter(function (m) {
          return !m.installed;
        }).length;
        var _wer = _def.wer ? ' (' + (_def.wer * 100).toFixed(1) + ' % micro-WER)' : '';
        _mh.textContent = 'Default for this machine: ' + (st.model_label || st.model)
          + ' (' + Math.round(_def.size_mb || st.model_size_mb || 0) + ' MB' + _wer + ')'
          + (st.ram_gb ? ', picked for ' + Math.round(st.ram_gb) + ' GB RAM' : '')
          + ' - best English accuracy per byte, CPU only.'
          + (_miss ? ' The other ' + _miss + ' tier' + (_miss > 1 ? 's are' : ' is')
                     + ' optional: pick one and press Download; nothing is '
                     + 'fetched before that.' : '');
      }
    }
    if (langSel) langSel.value = st.language || 'en';
    if (thrSel) thrSel.value = String(st.threads || 0);
    if (maxEl && !maxEl.value) maxEl.value = 300;
    var ready = !!(st.engine_installed && st.model_installed);
    var fetching = !!(st.fetch && st.fetch.state === 'running');
    if (line) {
      line.textContent = fetching
        ? ('downloading ' + Math.round(st.fetch.percent || 0) + ' %')
        : _voiceStatusText(st);
      line.style.color = ready ? '#6ee7a8' : '#f5c451';
    }
    if (dlRow) dlRow.style.display = (ready || fetching) ? 'none' : 'block';
    if (btn) btn.textContent = st.engine_installed ? 'Download model' : 'Download engine + model';
    if (prog) prog.textContent = '';
    if (fetching && !_voicePollTimer) {
      _voicePollTimer = setInterval(function () { _voiceCardRefresh(); }, 1500);
    }
    if (!fetching && _voicePollTimer) {
      clearInterval(_voicePollTimer);
      _voicePollTimer = null;
    }
  } catch (e) {
    if (line) {
      line.textContent = 'status unavailable (' + e.message + ')';
      line.style.color = '#f5c451';
    }
  }
}

async function voiceFetchModel() {
  var sel = document.getElementById('stt-model-sel');
  var prog = document.getElementById('stt-progress');
  var key = (sel && sel.value) || (S && S.sttModel) || '';
  if (prog) prog.textContent = 'starting...';
  try {
    var r = await fetch('/stt/fetch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: key})
    });
    var j = await r.json();
    if (j && j.code) {
      if (prog) prog.textContent = _voiceErrorText(j);
    }
  } catch (e) {
    if (prog) prog.textContent = 'download failed: ' + e.message;
  }
  _voiceCardRefresh();
}

// The browser records webm/opus (Chromium) or ogg/opus (Firefox); neither is
// readable by the STT binaries, so decode + resample to 16 kHz mono WAV here.
// That keeps python-multipart, ffmpeg and a webm decoder out of the stack.
async function _voiceBlobToWav16k(blob) {
  var buf = await blob.arrayBuffer();
  var AC = window.AudioContext || window.webkitAudioContext;
  var ctx = new AC();
  var decoded;
  try {
    decoded = await ctx.decodeAudioData(buf.slice(0));
  } finally {
    try { ctx.close(); } catch (e) { /* older browsers */ }
  }
  var frames = Math.max(1, Math.round(decoded.duration * 16000));
  var OffCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  var off = new OffCtx(1, frames, 16000);
  var src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start(0);
  var rendered = await off.startRendering();
  var pcm = rendered.getChannelData(0);
  var out = new ArrayBuffer(44 + pcm.length * 2);
  var v = new DataView(out);
  function wstr(off0, s) { for (var i = 0; i < s.length; i++) v.setUint8(off0 + i, s.charCodeAt(i)); }
  wstr(0, 'RIFF');
  v.setUint32(4, 36 + pcm.length * 2, true);
  wstr(8, 'WAVE');
  wstr(12, 'fmt ');
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true);
  v.setUint32(28, 32000, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  wstr(36, 'data');
  v.setUint32(40, pcm.length * 2, true);
  for (var i = 0; i < pcm.length; i++) {
    var s = Math.max(-1, Math.min(1, pcm[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return new Blob([out], {type: 'audio/wav'});
}

async function voiceTestMic() {
  var out = document.getElementById('stt-test-out');
  if (!out) return;
  out.style.color = '#8f9bb3';
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    out.textContent = 'this browser has no microphone API';
    return;
  }
  var stream = null;
  try {
    out.textContent = 'requesting microphone...';
    stream = await navigator.mediaDevices.getUserMedia({audio: true});
    var rec = new MediaRecorder(stream);
    var chunks = [];
    rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    var stopped = new Promise(function (res) { rec.onstop = function () { res(); }; });
    rec.start();
    out.textContent = 'recording 3 s - speak now...';
    await new Promise(function (res) { setTimeout(res, 3000); });
    rec.stop();
    await stopped;
    var blob = new Blob(chunks, {type: (chunks[0] && chunks[0].type) || 'audio/webm'});
    out.textContent = 'transcribing...';
    var wav = await _voiceBlobToWav16k(blob);
    var r = await fetch('/stt', {
      method: 'POST',
      headers: {'Content-Type': 'audio/wav'},
      body: wav
    });
    var j = await r.json();
    if (j && j.text) {
      out.textContent = '\u201c' + j.text + '\u201d';
      out.style.color = '#6ee7a8';
    } else {
      out.textContent = _voiceErrorText(j);
      out.style.color = '#f5c451';
    }
  } catch (e) {
    out.textContent = 'microphone test failed: ' + (e && e.message ? e.message : e);
    out.style.color = '#f5c451';
  } finally {
    if (stream) stream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* gone */ } });
  }
}


// ── V2: push-to-talk in the chat (1.4) ──────────────────────────────────────
// The mic button lives left of send and only exists while Speech to text is on.
// Click once to record, click again to stop: the transcript is APPENDED to the
// message box (never sent automatically), so the user can still edit it.
var _micRec = null;
var _micStream = null;
var _micChunks = [];


// ── V4: text to speech (1.4) ────────────────────────────────────────────────
// Opt-in like STT: the speaker button and the TTS card only act while
// tts_enabled is on. Audio is fetched as WAV, converted to speed 1.08 by the
// engine (WSOLA), and played once - never autoplayed.
var _ttsAudio = null;

function _revokeAudioUrl(el) {
  if (el && el._blobUrl) {
    try { URL.revokeObjectURL(el._blobUrl); } catch (e) {}
    el._blobUrl = null;
  }
}

function _voiceStopAudio() {
  if (_ttsAudio) {
    try { _ttsAudio.pause(); } catch (e) { /* already stopped */ }
    _revokeAudioUrl(_ttsAudio);
    _ttsAudio = null;
  }
  try {
    document.querySelectorAll('.msg-speak.playing').forEach(function (el) {
      el.classList.remove('playing');
    });
  } catch (e) { /* non-DOM test env */ }
}

async function _ttsFetchBlob(text, opts) {
  var body = {text: text};
  if (opts && opts.voice) body.voice = opts.voice;
  if (opts && opts.speed) body.speed = opts.speed;
  var r = await fetch('/tts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    var j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    throw new Error(_ttsErrorText(j));
  }
  return r.blob();
}

function _ttsErrorText(j) {
  if (!j) return 'the TTS engine did not answer';
  var map = {
    TTS_DISABLED: 'text to speech is off in the settings',
    TTS_NO_UV: 'uv is not installed - Pocket TTS needs uvx',
    TTS_NO_NUMPY: 'numpy is missing in the Python venv',
    TTS_BUSY: 'voice model download is already running',
    TTS_EMPTY: 'nothing to speak',
    TTS_TIMEOUT: 'the engine took too long',
    TTS_ENGINE_BROKEN: 'the voice engine produced no audio (voice config or sidecar log)',
    TTS_FAILED: 'the engine failed - see logs/'
  };
  var code = (j && j.code) || '';
  // TTS_ENGINE_BROKEN carries a precise reason from the server (log hint
  // included), so show that instead of the generic mapping.
  if (code === 'TTS_ENGINE_BROKEN' && j && j.error) return j.error;
  return map[code] || (j.error || code || 'unknown TTS error');
}

async function voiceSpeakText(text, btn) {
  // manual playback takes over the audio element: stop the read-aloud pump,
  // otherwise its current clip is killed without an 'ended' event and the queue
  // stops forever.
  if (typeof _readAloudStop === 'function') _readAloudStop();
  text = String(text || '').trim();
  if (!text) { showStatus('Nothing to speak'); return; }
  if (!(S && S.ttsEnabled)) { showStatus('Text to speech is off in the settings'); return; }
  if (_ttsAudio && btn && btn.classList && btn.classList.contains('playing')) {
    _voiceStopAudio();
    btn.classList.remove('playing');
    return;
  }
  if (btn && btn.classList) btn.classList.add('playing');
  showStatus('Speaking...');
  try {
    var blob = await _ttsFetchBlob(text.slice(0, (S && S.ttsMaxChars) || 600), {});
    _voiceStopAudio();
    _ttsAudio = _ttsAudioFromBlob(blob);
    _ttsAudio.onended = function () { if (btn && btn.classList) btn.classList.remove('playing'); };
    _ttsAudio.onerror = function () { if (btn && btn.classList) btn.classList.remove('playing'); };
    await _ttsAudio.play();
  } catch (e) {
    if (btn && btn.classList) btn.classList.remove('playing');
    showStatus('TTS failed: ' + (e && e.message ? e.message : e));
  }
}

async function voicePlaySample() {
  if (typeof _readAloudStop === 'function') _readAloudStop();
  var sel = document.getElementById('tts-voice-sel');
  var out = document.getElementById('tts-sample-out');
  var voice = (sel && sel.value) || (S && S.ttsVoice) || '';
  if (!(S && S.ttsEnabled)) {
    if (out) out.textContent = 'Enable "Text to speech" first.';
    return;
  }
  if (out) out.textContent = 'Rendering sample...';
  try {
    var blob = await _ttsFetchBlob(
      'Hi, I am the HiveMind voice. This is how I sound when I read your results out loud.',
      {voice: voice});
    _voiceStopAudio();
    _ttsAudio = _ttsAudioFromBlob(blob);
    await _ttsAudio.play();
    if (out) out.textContent = 'Playing ' + voice + ' at speed ' + _clampTtsSpeed(S.ttsSpeed) + 'x';
  } catch (e) {
    if (out) out.textContent = 'Sample failed: ' + (e && e.message ? e.message : e);
  }
}


// ── Telegram voice switches (1.4) ───────────────────────────────────────────
// The switches live in the Gateway card but depend on the Voice card: a voice
// note needs STT, a voice answer needs TTS. Both are greyed out while their
// feature is off, and the hint says why.

// ── READ ALOUD (1.4) ────────────────────────────────────────────────────────
// The UI can speak things on its own: the whole answer in direct mode, the plan
// and then the coder's answer in agentic runs, ask-user questions and the
// approval/defensive-gate cards. Every surface has its own switch; nothing is
// spoken while TTS is off, and speech never queues up behind older speech.
var _readAloudQueue = [];
var _readAloudBusy = false;

// VOICE-FLAG-SYNC (2026-11-02): the read-aloud switches are client state, and
// it drifted from the engine - a preset load, a second tab or a reload during a
// run left S.ttsRead* true while the card showed them off, so answers were
// still spoken. Re-read them from the server and repaint the switches.
async function refreshVoiceFlags() {
  try {
    var s = await (await fetch('/settings')).json();
    S.ttsEnabled = s.tts_enabled === true;
    S.ttsReadDirect = s.tts_read_direct === true;
    S.ttsReadAgentic = s.tts_read_agentic === true;
    S.ttsReadAsk = s.tts_read_ask === true;
    S.ttsReadGate = s.tts_read_gate === true;
    [['tts-enabled-toggle', S.ttsEnabled],
     ['tts-read-direct-toggle', S.ttsReadDirect],
     ['tts-read-agentic-toggle', S.ttsReadAgentic],
     ['tts-read-ask-toggle', S.ttsReadAsk],
     ['tts-read-gate-toggle', S.ttsReadGate]].forEach(function (pair) {
      var el = document.getElementById(pair[0]);
      if (el) el.checked = pair[1];
    });
    return S.ttsEnabled;
  } catch (e) { return S.ttsEnabled; }
}

function _readAloudWanted(kind) {
  if (!(S && S.ttsEnabled)) return false;
  if (kind === 'direct') return !!S.ttsReadDirect;
  if (kind === 'plan' || kind === 'fazit') return !!S.ttsReadAgentic;
  if (kind === 'ask') return !!S.ttsReadAsk;
  if (kind === 'gate') return !!S.ttsReadGate;
  return false;
}

function _readAloud(kind, text) {
  if (!_readAloudWanted(kind)) return;
  text = String(text || '').replace(/\s+/g, ' ').trim();
  if (!text) return;
  // cap before sending: the engine caps at 600 chars, a long answer should not
  // be truncated mid-word by the server, so cut here at a sentence/word edge
  var _cap = (S && S.ttsMaxChars) || 600;   // server truth: tts_audio.MAX_CHARS
  if (text.length > _cap) {
    var cut = text.slice(0, _cap);
    var lastStop = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('! '), cut.lastIndexOf('? '));
    text = (lastStop > _cap * 0.4 ? cut.slice(0, lastStop + 1)
                                  : cut.slice(0, cut.lastIndexOf(' ') || _cap)).trim();
  }
  // Keep the kind with the text: a switch flipped off while items are queued
  // must drop exactly those items; a mid-run flip ON takes the next event.
  // PREFIX-FIRST + PREFETCH (2026-10-18): the very first chunk is deliberately
  // small (a clause, <=90 chars) - it is the only one the listener waits for.
  // Everything after it may be longer, because the next chunk is fetched while
  // the previous one plays.
  var _parts = text.match(/[^.!?;,\n]+[.!?;,]?[\s]*/g) || [text];
  var _chunks = [];
  var _buf = '';
  // MEASURED (2026-10-18, warm sidecar): a 71-character chunk needs ~1.9 s before
  // it is audible, 146 chars ~3.9 s. The first chunk is therefore kept to ~60
  // chars (~1.3 s); the model runs at ~1.3-1.9x realtime on CPU, so this is the
  // floor without streaming the model output.
  var _limit = 60;                     // first chunk: short, so audio starts fast
  _parts.forEach(function (p) {
    if ((_buf + p).length > _limit && _buf.trim()) {
      _chunks.push(_buf.trim());
      _buf = p;
      _limit = 240;                    // after the first, chunks may be bigger
    } else {
      _buf += p;
    }
  });
  if (_buf.trim()) _chunks.push(_buf.trim());
  // a clause can be longer than the first-chunk limit (no punctuation at all, a
  // pasted URL): cut it at the last space so the fast start still holds
  if (_chunks.length && _chunks[0].length > 90) {
    var _long = _chunks.shift();
    var _cut = _long.lastIndexOf(' ', 90);
    if (_cut < 30) _cut = 90;
    _chunks.unshift(_long.slice(0, _cut).trim());
    var _rest = _long.slice(_cut).trim();
    if (_rest) _chunks.splice(1, 0, _rest);
  }
  _chunks.forEach(function (c) { _readAloudQueue.push({kind: kind, text: c}); });
  if (!_readAloudBusy && !_raPending) {
    var _first = _raTakeNext();
    if (_first) { _raPending = _raFetch(_first); _readAloudNext(); }
  }
}

var _raPending = null;          // {kind, text, promise} already being fetched

function _raFetch(item) {
  var p = _ttsFetchBlob(item.text, {});
  // A prefetch that is superseded (stop, new message, queue reset) is never
  // consumed by _readAloudNext. Without this no-op catch its rejection
  // surfaced as "Uncaught (in promise)" in the console.
  p.catch(function () {});
  return {kind: item.kind, text: item.text, promise: p};
}

function _raTakeNext() {
  while (_readAloudQueue.length) {
    var it = _readAloudQueue.shift();
    if (_readAloudWanted(it.kind)) return it;
  }
  return null;
}

// The bubble body also carries tool chips, websearch result blocks, tool call
// rows, the think block and code panels. Reading innerText of the whole body made
// the assistant announce every search result before the answer (owner report,
// direct mode). Clone, strip the chrome, read what is left - that is exactly the
// model's final text.
var _ANSWER_CHROME_SEL = [
  '.tool-call-chip', '.tool-call-row', '.tool-result-block', '.tool-result-pre',
  '.tool-act', '.tc-extra', '.tc-extra-pre', '.tc-pruned-notice',
  '.think-block', '.think-body', '.planner-think-block',
  '.amsg-foot', '.msg-speak', '.duo-verdict', '.critic-box', '.img-desc-box',
  '.budget-bar', '.ctx-cut', '.files-summary', '.status-txt', '.pload',
  '.steer-note', '.fc-preview', '.repair-badge',
  'details', 'pre', 'code', '.pre-call-reasoning'
].join(',');

function _answerTextOnly(bodyEl) {
  if (!bodyEl) return '';
  var clone = bodyEl.cloneNode(true);
  try {
    clone.querySelectorAll(_ANSWER_CHROME_SEL).forEach(function (n) { n.remove(); });
  } catch (e) { /* older builds: fall back to the plain text below */ }
  // innerText needs layout, and a detached clone has none (Chrome/Firefox fall
  // back to textContent, which loses paragraph breaks). Mount it off-screen for
  // the read, then throw it away - no reflow the user can see.
  var host = document.createElement('div');
  host.style.cssText = 'position:absolute;left:-99999px;top:0;width:1px;' +
                       'height:1px;overflow:hidden;visibility:hidden';
  host.appendChild(clone);
  document.body.appendChild(host);
  var txt = '';
  try {
    txt = clone.innerText || clone.textContent || '';
  } finally {
    host.remove();
  }
  return String(txt).replace(/\n{3,}/g, '\n\n').trim();
}

// Non-answer UI chrome living inside .abody (tool chips, tool results, inline
// reasoning, steer cards, status/pruned notices). Excluded from persisted
// chat history (`content`) so follow-up turns never feed DOM chrome back into
// the LLM as few-shot assistant text. Code blocks (<pre>/<code>) stay intact
// here (unlike TTS _ANSWER_CHROME_SEL) because code is real answer content.
var _HISTORY_CHROME_SEL = [
  '.tool-call-chip', '.tool-call-row', '.tool-result-block', '.tool-result-pre',
  '.tool-act', '.tc-extra', '.tc-extra-pre', '.tc-pruned-notice', '.tg-card',
  '.think-block', '.think-body', '.planner-think-block', '.pre-call-reasoning',
  '.amsg-foot', '.msg-speak', '.duo-verdict', '.critic-box', '.img-desc-box',
  '.budget-bar', '.ctx-cut', '.files-summary', '.status-txt', '.pload',
  '.steer-note', '.fc-preview', '.repair-badge', 'details'
].join(',');

function _historyTextOnly(bodyEl) {
  if (!bodyEl) return '';
  var clone = bodyEl.cloneNode(true);
  try {
    clone.querySelectorAll(_HISTORY_CHROME_SEL).forEach(function (n) { n.remove(); });
  } catch (e) { /* fall through with untouched clone */ }
  var out = [];
  var BLOCK = {
    P: 1, DIV: 1, LI: 1, BLOCKQUOTE: 1,
    H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1,
    UL: 1, OL: 1, TABLE: 1, SECTION: 1, ARTICLE: 1
  };
  function pushNl() {
    if (!out.length) return;
    var last = out[out.length - 1];
    if (last && last.charAt(last.length - 1) !== '\n') out.push('\n');
  }
  function walk(node) {
    if (!node) return;
    if (node.nodeType === 3) {
      out.push(node.nodeValue || '');
      return;
    }
    if (node.nodeType !== 1) return;
    var tag = String(node.tagName || '').toUpperCase();
    if (tag === 'BR') { out.push('\n'); return; }
    if (tag === 'HR') { pushNl(); out.push('---\n'); return; }
    if (tag === 'PRE') {
      var cls = String(node.className || '');
      var m = cls.match(/\blang-([a-zA-Z0-9_+#.-]+)\b/);
      var lang = m ? m[1] : '';
      var code = String(node.textContent || '').replace(/\r\n/g, '\n').replace(/\n$/, '');
      pushNl();
      out.push('```' + lang + '\n' + code + '\n```\n');
      return;
    }
    if (tag === 'TR') {
      var cells = [];
      for (var c = 0; c < node.childNodes.length; c++) {
        var ch = node.childNodes[c];
        if (ch.nodeType === 1) {
          var ct = String(ch.tagName || '').toUpperCase();
          if (ct === 'TD' || ct === 'TH') {
            cells.push(String(ch.textContent || '').replace(/\s+/g, ' ').trim());
          }
        }
      }
      if (cells.length) { pushNl(); out.push(cells.join(' | ') + '\n'); }
      return;
    }
    var isBlock = !!BLOCK[tag];
    if (isBlock) pushNl();
    for (var i = 0; i < node.childNodes.length; i++) {
      walk(node.childNodes[i]);
    }
    if (isBlock) pushNl();
  }
  walk(clone);
  return out.join('')
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}


// Clamp TTS speed to backend bounds [0.8, 1.5] (1.08 default).
function _clampTtsSpeed(raw) {
  var v = parseFloat(raw);
  if (isNaN(v) || v <= 0) return 1.08;
  return Math.round(Math.max(0.8, Math.min(1.5, v)) * 100) / 100;
}

// Update UI volume state, label, and active audio element live (preserves 0%).
function _ttsApplyVolume(raw) {
  var parsed = parseFloat(raw);
  var v = isNaN(parsed) ? 1.0 : Math.max(0, Math.min(1, parsed));
  S.ttsVolume = v;
  var out = document.getElementById('tts-volume-out');
  if (out) out.textContent = Math.round(v * 100) + ' %';
  if (_ttsAudio) _ttsAudio.volume = v;
  return v;
}

// One place that creates the audio element, so the volume setting applies to
// every path (read aloud, the per-answer speaker, the sample button).
function _ttsAudioFromBlob(blob) {
  var el = new Audio(URL.createObjectURL(blob));
  el._blobUrl = el.src;
  if (typeof el.addEventListener === 'function') {
    el.addEventListener('ended', function () { _revokeAudioUrl(el); });
    el.addEventListener('error', function () { _revokeAudioUrl(el); });
  }
  var v = (S && typeof S.ttsVolume === 'number') ? S.ttsVolume : 1.0;
  el.volume = Math.max(0, Math.min(1, v));
  return el;
}


function _readAloudNext() {
  var cur = _raPending;
  _raPending = null;
  if (!cur) { _readAloudBusy = false; return; }
  _readAloudBusy = true;
  // PREFETCH: start the following chunk now, so playback continues without a
  // synthesis pause. It runs while this one is audible.
  var follow = _raTakeNext();
  if (follow) _raPending = _raFetch(follow);
  cur.promise.then(function (blob) {
    _voiceStopAudio();
    _ttsAudio = _ttsAudioFromBlob(blob);
    _ttsAudio.onended = function () { _readAloudNext(); };
    _ttsAudio.onerror = function () { _readAloudNext(); };
    return _ttsAudio.play();
  }).catch(function (e) {
    var msg = (e && e.message ? e.message : String(e));
    showStatus('Read aloud failed: ' + msg);
    // A broken engine fails for every sentence; retrying the rest of the queue
    // just burns a synthesis round trip each time, so drop the queue and say so.
    if (/voice engine|no audio|translate table/i.test(msg)) {
      _readAloudQueue.length = 0;
      _raPending = null;
      _readAloudBusy = false;
      return;
    }
    _readAloudNext();
  });
}

function _readAloudStop() {
  _readAloudQueue = [];
  _raPending = null;
  _readAloudBusy = false;
  _voiceStopAudio();
}


// Starting the sidecar takes 20-30 s on the first request. Doing that while the
// owner is still setting up means the first real answer is already warm; this
// runs in the background and its audio is thrown away.
function voiceWarmup() {
  if (!(S && S.ttsEnabled)) return;
  fetch('/tts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: 'Ready.'})
  }).then(function (r) {
    if (r.ok) showStatus('Voice engine warm');
  }).catch(function () { /* the next request reports the real error */ });
}


// The speak button appears with the first text token (not on an empty bubble)
// and only while TTS is enabled.

// Turning the TTS master on should make the next answer audible without a second
// visit to the card: if none of the read-aloud switches is set, enable the two
// answer paths (direct + agentic). The switches are visible, so this is not a
// hidden mode - and turning them off again is one click.
// Reveal the speaker button as soon as a bubble has text. Called from the token
// flush (throttled to 4/s) and at run end - before this it only ran inside
// loadSettings, so the button never appeared during a live run.
var _speakSyncAt = 0;
function _speakSyncSoon() {
  var now = Date.now();
  if (now - _speakSyncAt < 250) return;
  _speakSyncAt = now;
  // PERF (2026-10-23): sync only the ACTIVE bubble from the token flush —
  // the full-document scan deep-cloned every bubble in the chat 4x per
  // second, with cost growing with conversation length.
  var act = (S.curAgent && S.curAgent.tid) ? document.getElementById('ab-' + S.curAgent.tid) : null;
  _syncSpeakButtons(act ? act.closest('.ablock') : null);
}

// Cheap has-answer check for the reveal decision: strip the chrome in a
// DETACHED clone and count textContent — no mount, no layout (the innerText
// read in _answerTextOnly forces layout per bubble per sync tick).
function _answerLenOnly(bodyEl) {
  if (!bodyEl) return 0;
  var clone = bodyEl.cloneNode(true);
  try { clone.querySelectorAll(_ANSWER_CHROME_SEL).forEach(function (n) { n.remove(); }); } catch (e) {}
  return (clone.textContent || '').trim().length;
}

function _syncSpeakButtons(scope) {
  var blocks = scope ? [scope]
                     : Array.prototype.slice.call(document.querySelectorAll('#chat .ablock'));
  blocks.forEach(function (wrap) {
    var btn = wrap.querySelector('.amsg-foot .msg-speak');
    if (!btn) return;
    var body = wrap.querySelector('.abody');
    // Keep the speaker button hidden while the bubble is still streaming (.live):
    // pre-call text before a tool_call gets collapsed into .pre-call-reasoning,
    // which otherwise flashed the button in and left it stuck under tool chips
    // with nothing to read aloud.
    var isLive = !!(body && body.classList && body.classList.contains('live'));
    var hasText = !isLive && _answerLenOnly(body) > 8;
    btn.style.display = (hasText && S && S.ttsEnabled) ? '' : 'none';
  });
}

function _syncTgVoice() {
  var inEl = document.getElementById('tg-voice-in-toggle');
  var outEl = document.getElementById('tg-voice-out-toggle');
  var sttOn = !!(S && S.sttEnabled);
  var ttsOn = !!(S && S.ttsEnabled);
  function _paint(el, on, checked) {
    if (!el) return;
    el.checked = !!checked;
    el.disabled = !on;
    var lab = el.closest('label');
    if (lab) {
      lab.style.opacity = on ? '' : '.4';
      lab.style.pointerEvents = on ? '' : 'none';
    }
  }
  _paint(inEl, sttOn, S && S.tgVoiceIn);
  _paint(outEl, ttsOn, S && S.tgVoiceOut);
  // RESULT-MODE (2026-10-18, owner): the result format belongs to the voice
  // feature - with TTS off it must be greyed like the toggles, not sit there
  // looking active. Same for the two hint lines.
  var sel = document.getElementById('tg-result-mode-sel');
  if (sel) {
    sel.disabled = !ttsOn;
    var fld = sel.closest('.fld') || sel.parentElement;
    if (fld) {
      fld.style.opacity = ttsOn ? '' : '.4';
      fld.style.pointerEvents = ttsOn ? '' : 'none';
    }
  }
  ['tg-voice-in-hint', 'tg-voice-out-hint', 'tg-result-hint'].forEach(function (id) {
    var el = document.getElementById(id);
    if (!el) return;
    var on = (id === 'tg-voice-in-hint') ? sttOn : ttsOn;
    el.style.opacity = on ? '' : '.45';
  });
  var prog = document.getElementById('tg-progress-toggle');
  if (prog) {
    // progress messages are independent of voice, but the row only makes sense
    // once the phone channel exists at all - keep it usable, just dim the label
    var row = prog.closest('.tgl-row');
    if (row) row.style.opacity = '';
  }
}


// The voice list comes from GET /tts/voices (core/tts_engine.VOICES / tts_woof) -
// one source, so adding a voice never means editing HTML as well.
var _ttsVoicesBackend = '';

function _ttsFillVoices(sel, force, backendOverride) {
  var backend = backendOverride || (S && S.ttsBackend) || 'pocket';
  if (!sel || (!force && _ttsVoicesBackend === backend)) return;
  fetch('/tts/voices?backend=' + encodeURIComponent(backend), {cache: 'no-store'}).then(function (r) {
    return r.json();
  }).then(function (d) {
    if (!d || !d.voices || !d.voices.length) return;
    _ttsVoicesBackend = d.backend || backend;
    if (d.max_chars) S.ttsMaxChars = d.max_chars;
    var attrEl = document.getElementById('tts-attribution');
    if (attrEl && d.attribution) {
      var woofLink = '<a href="https://underdog.ai" target="_blank" rel="noopener noreferrer" style="color:#8ab4f8">Underdog Woof</a>';
      var woofLic = '<a href="https://huggingface.co/ConwayResearch/Underdog-Woof/blob/main/LICENSE" target="_blank" rel="noopener noreferrer" style="color:#8ab4f8">Underdog Non-Commercial License 1.0</a>';
      if ((_ttsVoicesBackend === 'woof') || (String(d.attribution).indexOf('underdog.ai') >= 0)) {
        attrEl.innerHTML = 'Voice by ' + woofLink + ' (<a href="https://underdog.ai" target="_blank" rel="noopener noreferrer" style="color:#8ab4f8">underdog.ai</a>) &middot; ' + woofLic + ' (non-commercial use only) &middot; <code>docs\\voice_licenses.md</code>';
      } else {
        attrEl.innerHTML = esc(d.attribution) + ' &middot; Optional: Voice by ' + woofLink + ' (non-commercial) &middot; <code>docs\\voice_licenses.md</code>';
      }
    }
    var keep = (S && S.ttsVoice) || sel.value || '';
    sel.textContent = '';
    var validKeys = [];
    d.voices.forEach(function (v) {
      validKeys.push(v.key);
      var o = document.createElement('option');
      o.value = v.key;
      o.textContent = v.label + (v.note ? ' - ' + v.note : '');
      sel.appendChild(o);
    });
    var nextVoice = validKeys.indexOf(keep) >= 0 ? keep : (d.default || validKeys[0] || '');
    sel.value = nextVoice;
    if (S && nextVoice && S.ttsVoice !== nextVoice) {
      S.ttsVoice = nextVoice;
      postSettings({tts_voice: nextVoice});
    }
  }).catch(function () { /* keep the placeholder, the card still works */ });
}

function onTtsBackendChange(backend) {
  backend = (backend === 'woof') ? 'woof' : 'pocket';
  if (S) S.ttsBackend = backend;
  _ttsVoicesBackend = '';
  postSettings({tts_backend: backend});
  var sel = document.getElementById('tts-voice-sel');
  _ttsFillVoices(sel, true, backend);
  _ttsStatusRefresh();
}

async function ttsFetchWoof() {
  var prog = document.getElementById('tts-progress');
  var btn = document.getElementById('tts-download-btn');
  if (btn) btn.disabled = true;
  if (prog) prog.textContent = 'starting...';
  try {
    var r = await fetch('/tts/fetch', {method: 'POST'});
    var j = await r.json();
    if (j && j.code && prog) prog.textContent = _ttsErrorText(j);
  } catch (e) {
    if (prog) prog.textContent = 'download failed: ' + e.message;
  }
  _ttsStatusRefresh();
}

function _syncTtsCard() {
  var on = !!(S && S.ttsEnabled);
  var box = document.getElementById('tts-options');
  if (box) box.style.display = on ? 'block' : 'none';
  if (!on) {
    // a mid-speech switch-off stops the clip, the queue AND the prefetched
    // request. Leaving _raPending behind used to wedge the pump: after turning
    // TTS back on, nothing ever started because the guard saw a pending fetch.
    if (typeof _readAloudStop === 'function') _readAloudStop();
    else { _voiceStopAudio(); _readAloudQueue = []; _readAloudBusy = false; }
  }
  var bSel = document.getElementById('tts-backend-sel');
  if (bSel && S && S.ttsBackend) bSel.value = S.ttsBackend;
  var btn = document.getElementById('tts-speak-all');
  if (btn) btn.style.display = on ? '' : 'none';
  var sel = document.getElementById('tts-voice-sel');
  if (sel) {
    _ttsFillVoices(sel, false);
    if (S.ttsVoice && !sel.value) {
      var matches = Array.prototype.filter.call(sel.options, function (o) {
        return o.value === S.ttsVoice;
      });
      if (matches.length) sel.value = S.ttsVoice;
    }
  }
  var spd = document.getElementById('tts-speed-inp');
  if (spd && document.activeElement !== spd) spd.value = String(_clampTtsSpeed(S.ttsSpeed));
}

var _ttsFetchPoll = null;
async function _ttsStatusRefresh() {
  if (!(S && S.ttsEnabled)) { _syncTtsCard(); return; }
  var line = document.getElementById('tts-status-line');
  var dlRow = document.getElementById('tts-download-row');
  var dlBtn = document.getElementById('tts-download-btn');
  var prog = document.getElementById('tts-progress');
  try {
    var r = await fetch('/tts/status', {cache: 'no-store'});
    var st = await r.json();
    if (st.max_chars) S.ttsMaxChars = st.max_chars;
    var backend = st.backend || (S && S.ttsBackend) || 'pocket';
    if (backend === 'woof') {
      var wf = st.woof_fetch || {};
      var runningFetch = wf.state === 'running';
      if (dlRow) dlRow.style.display = (!st.woof_installed || runningFetch) ? 'block' : 'none';
      if (dlBtn) dlBtn.disabled = runningFetch;
      if (prog) prog.textContent = runningFetch ? (Math.round(wf.percent || 0) + ' %') : (wf.state === 'error' ? (wf.message || 'error') : '');
      if (line) {
        if (runningFetch) {
          line.textContent = 'downloading Underdog Woof (~41 MB)...';
          line.style.color = '#f5c451';
          if (!_ttsFetchPoll) _ttsFetchPoll = setTimeout(function () { _ttsFetchPoll = null; _ttsStatusRefresh(); }, 800);
        } else if (st.woof_installed) {
          line.textContent = (st.running ? 'ready' : 'installed') + ' (Underdog Woof CPU) \u00b7 ' + st.voice + ' \u00b7 ' + st.speed + 'x';
          line.style.color = '#6ee7a8';
        } else {
          line.textContent = 'not downloaded yet (~41 MB, no HF token needed) - click Download or Play sample';
          line.style.color = '#f5c451';
        }
      }
    } else {
      if (dlRow) dlRow.style.display = 'none';
      if (line) {
        if (!st.uv) {
          line.textContent = 'uv is missing - install it, then Pocket TTS works';
          line.style.color = '#f5c451';
        } else if (st.running) {
          line.textContent = 'ready \u00b7 ' + st.voice + ' \u00b7 ' + st.speed + 'x';
          line.style.color = '#6ee7a8';
        } else {
          line.textContent = 'engine idle - warm start ~2 s, cold start installs dependencies (can take up to ~1 min, later requests succeed)';
          line.style.color = '#f5c451';
        }
      }
    }
  } catch (e) {
    if (line) { line.textContent = 'status unavailable (' + e.message + ')'; }
  }
  _syncTtsCard();
}

function _syncMicBtn() {
  var btn = document.getElementById('mic-btn');
  if (!btn) return;
  btn.style.display = (S && S.sttEnabled) ? 'flex' : 'none';
}

function _micInsertTranscript(text) {
  var inp = document.getElementById('input');
  if (!inp) return;
  var cur = (inp.value || '').replace(/\s+$/, '');
  inp.value = (cur ? cur + ' ' : '') + String(text || '').trim();
  inp.focus();
  try {
    inp.dispatchEvent(new Event('input', {bubbles: true}));
  } catch (e) { /* older browsers */ }
}

async function voiceToggleMic() {
  var btn = document.getElementById('mic-btn');
  if (!btn) return;
  if (_micRec && _micRec.state === 'recording') {
    try { _micRec.stop(); } catch (e) { showStatus('Could not stop the recording'); }
    return;
  }
  if (!(S && S.sttEnabled)) {
    showStatus('Voice is off - enable "Speech to text" in the settings first');
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showStatus('This browser has no microphone API');
    return;
  }
  try {
    _micStream = await navigator.mediaDevices.getUserMedia({audio: true});
    _micChunks = [];
    _micRec = new MediaRecorder(_micStream);
    _micRec.ondataavailable = function (e) {
      if (e.data && e.data.size) _micChunks.push(e.data);
    };
    _micRec.onstop = async function () {
      btn.classList.remove('rec');
      try {
        _micStream.getTracks().forEach(function (tr) { tr.stop(); });
      } catch (e) { /* tracks already gone */ }
      var blob = new Blob(_micChunks,
        {type: (_micChunks[0] && _micChunks[0].type) || 'audio/webm'});
      if (!blob.size) { showStatus('Nothing was recorded'); return; }
      showStatus('Transcribing...');
      try {
        var wav = await _voiceBlobToWav16k(blob);
        var r = await fetch('/stt', {
          method: 'POST',
          headers: {'Content-Type': 'audio/wav'},
          body: wav
        });
        var j = await r.json();
        if (j && j.text) {
          _micInsertTranscript(j.text);
          showStatus('Transcript added to the message');
        } else {
          showStatus(_voiceErrorText(j));
        }
      } catch (e) {
        showStatus('Transcription failed: ' + (e && e.message ? e.message : e));
      }
    };
    _micRec.start();
    btn.classList.add('rec');
    showStatus('Recording - click the mic again to stop');
  } catch (e) {
    showStatus('Microphone unavailable: ' + (e && e.message ? e.message : e));
  }
}

// Escape cancels a running recording without transcribing.
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape') return;
    if (_micRec && _micRec.state === 'recording') {
      try {
        _micRec.onstop = null;
        _micRec.stop();
      } catch (e) { /* nothing to stop */ }
      var btn = document.getElementById('mic-btn');
      if (btn) btn.classList.remove('rec');
      try {
        if (_micStream) _micStream.getTracks().forEach(function (tr) { tr.stop(); });
      } catch (e) { /* already stopped */ }
      showStatus('Recording cancelled');
    }
  });
}
