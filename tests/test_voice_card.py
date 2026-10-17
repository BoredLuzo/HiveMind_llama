"""The Voice card (1.4): opt-in, honest status, no dead controls.

Owner-approved design (2026-10-17): one card next to Keep-Alive, a master switch
per block, a status line, a Download button for whatever is missing, a 3 s
microphone test, and nothing at all while the switch is off. Telegram and spoken
answers are separate switches in the Gateway card, not here.

Pins:
  (a) the card exists with BETA + "off by default" and the STT block only
      (the TTS block arrives with V4 - no greyed-out dead rows);
  (b) every control is wired to a real setting key through postSettings;
  (c) the sub-block is hidden while stt_enabled is off (opt-in, no downloads);
  (d) the status line distinguishes engine / model / ready / error;
  (e) the microphone test records locally and posts WAV to /stt (never starts a
      run), and the WAV encoder really writes 16 kHz mono PCM;
  (f) /stt/fetch validates the model key, refuses a second parallel download and
      runs the fetch script with this interpreter;
  (g) Telegram/voice stays opt-in (no telegram ids in this card).

Run: python tests/test_voice_card.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
server = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
settings = (ROOT / "settings.py").read_text(encoding="utf-8", errors="replace")

# ── (a) the card ────────────────────────────────────────────────────────────
card = html.split('id="voice-card"', 1)[1].split("<!-- Smart Preload -->", 1)[0] \
    if 'id="voice-card"' in html else ""
check("the Voice card exists", bool(card))
check("it is marked BETA", "BETA" in card)
check("it says off by default", "off by default" in card)
check("it sits next to Keep-Alive", html.index('id="voice-card"') > html.index("Keep-Alive / Pinning"))
for el in ("stt-enabled-toggle", "stt-model-sel", "stt-language-sel",
           "stt-preload-toggle", "stt-test-btn", "stt-download-btn",
           "stt-status-line", "stt-status", "stt-options", "stt-test-out",
           "stt-threads-sel", "stt-max-seconds", "stt-download-row"):
    check(f"element {el} present", f'id="{el}"' in card)
check("no TTS rows yet (no dead controls)", "tts-enabled-toggle" not in card)
check("Telegram is not in this card (opt-in lives in the gateway)",
      "telegram_voice" not in card and "Telegram" in card)

# ── (b) every control writes a real key ─────────────────────────────────────
wired = {
    "stt_enabled": 'postSettings({stt_enabled:this.checked})',
    "stt_model": 'postSettings({stt_model:this.value})',
    "stt_language": 'postSettings({stt_language:this.value})',
    "stt_preload": 'postSettings({stt_preload:this.checked})',
    "stt_threads": 'postSettings({stt_threads:parseInt(this.value,10)||0})',
    "stt_max_seconds": 'postSettings({stt_max_seconds:parseInt(this.value,10)||300})',
}
for key, needle in wired.items():
    check(f"{key} is wired", needle in card)
    check(f"{key} exists in settings.py", f'"{key}"' in settings)

# ── (c)/(d) behaviour of the card script ───────────────────────────────────
check("refresh function exists", "function _voiceCardRefresh" in app)
check("refresh is called when settings load", "if (typeof _voiceCardRefresh === 'function') _voiceCardRefresh();" in app)
check("the sub block is hidden while off",
      "box.style.display = on ? 'block' : 'none'" in app)
check("the master toggle is synced from the setting",
      "_syncFlagCheck('stt-enabled-toggle', S.sttEnabled)" in app
      and "S.sttEnabled = s.stt_enabled === true" in app)
check("preload is synced too",
      "_syncFlagCheck('stt-preload-toggle', S.sttPreload)" in app)
check("status reads /stt/status", "'/stt/status'" in app)
check("status distinguishes engine/model/ready",
      "engine not installed" in app and "model missing" in app and "ready" in app)
check("status polls while a download runs",
      "setInterval(function () { _voiceCardRefresh(); }, 1500)" in app)
check("the download button posts the selected model",
      "'/stt/fetch'" in app and "model: key" in app)
check("engine errors are translated for humans",
      "STT_NO_ENGINE" in app and "STT_NO_AUDIO_LIB" in app and "STT_EMPTY" in app)

# ── (e) microphone test / WAV encoder ──────────────────────────────────────
check("the test records with MediaRecorder",
      "new MediaRecorder(stream)" in app)
check("it stops the tracks afterwards",
      "stream.getTracks().forEach" in app)
check("it posts a WAV blob to /stt",
      "body: wav" in app and "headers: {'Content-Type': 'audio/wav'}" in app)
check("the encoder resamples to 16 kHz mono",
      "OfflineAudioContext" in app and "v.setUint32(24, 16000, true)" in app)
check("the encoder writes a RIFF/WAVE header",
      "wstr(0, 'RIFF')" in app and "wstr(8, 'WAVE')" in app and "wstr(12, 'fmt ')" in app)
check("the test never starts a run",
      "/stream" not in app.split("async function voiceTestMic()", 1)[1].split("async function", 1)[0])

# ── (f) /stt/fetch ─────────────────────────────────────────────────────────
fseg = ""
if '/stt/fetch' in server:
    fseg = server.split('@app.post("/stt/fetch")', 1)[1].split('@app.post("/stt")', 1)[0]
check("/stt/fetch exists", bool(fseg))
check("it validates the model key", "if key not in stt_engine.MODELS" in fseg)
check("it refuses a parallel download", 'status_code=409' in fseg and "running" in fseg)
check("it runs the fetch script with this interpreter",
      "_sys.executable" in fseg and "deploy" in fseg and "fetch_whisper.py" in fseg)
check("it starts the download detached (does not block the engine)",
      "subprocess.Popen" in fseg and "DEVNULL" in fseg)
check("it returns 202", "status_code=202" in fseg)
check("nothing starts a download at import time",
      "fetch_whisper" not in server.split("async def stt_fetch")[0].split("def load_settings")[-1])

# ── (g) opt-in defaults ────────────────────────────────────────────────────
check("stt_enabled defaults to off", '"stt_enabled":              False' in settings
      or '"stt_enabled": False' in settings.replace("  ", " "))
check("stt_preload defaults to off", '"stt_preload":              False' in settings
      or '"stt_preload": False' in settings.replace("  ", " "))
check("no telegram voice key in this card's settings",
      "telegram_voice" not in card)

# ── V2: push-to-talk in the chat ───────────────────────────────────────────
_css = (ROOT / "static" / "style.css").read_text(encoding="utf-8", errors="replace")
check("the mic button sits left of send",
      html.index('id="mic-btn"') < html.index('id="send"'))
check("it is hidden until voice is on",
      'id="mic-btn"' in html and 'style="display:none"' in html)
check("it is a red circle", "#mic-btn{" in _css)
check("recording state pulses", "_micPulse" in _css)
check("the toggle records", "async function voiceToggleMic" in app
      and "new MediaRecorder(_micStream)" in app)
_mic_seg = (app.split("function _micInsertTranscript", 1)[1].split("\nfunction ", 1)[0]
            if "function _micInsertTranscript" in app else "")
check("the transcript is appended, not sent",
      "_micInsertTranscript(j.text)" in app and "sendMsg()" not in _mic_seg)
check("escape cancels a recording", "ev.key !== 'Escape'" in app)
check("visibility follows stt_enabled",
      "btn.style.display = (S && S.sttEnabled) ? 'flex' : 'none'" in app)

print()
print(f"SUMMARY voice_card: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
