"""TTS endpoints + Voice card wiring (source-level, like the STT server suite).

Pins:
  (a) GET /tts/status, GET /tts/voices, POST /tts exist and refuse while off;
  (b) POST /tts returns real audio (Response with audio/* media type) and the
      X-HiveMind-* headers, and supports format=opus;
  (c) the settings keys exist with the owner's defaults (cosette, 1.08, off);
  (d) the Voice card has the TTS block with every control wired to a real key,
      and the block is hidden while tts_enabled is off;
  (e) the per-answer speaker button exists, is hidden unless TTS is on, and never
      autoplays (it is bound to a click handler).

Run: python tests/test_tts_server.py
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
        print(f"  FAIL {name}{extra}")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


server = read("server.py")
html = read("index.html")
app = read("static/app.js")
css = read("static/style.css")
settings = read("settings.py")
docs = read("docs/settings.md")

# ── (a) endpoints ──────────────────────────────────────────────────────────
check("GET /tts/status exists", '@app.get("/tts/status")' in server)
check("GET /tts/voices exists", '@app.get("/tts/voices")' in server)
check("POST /tts exists", '@app.post("/tts")' in server)
tseg = server.split('@app.post("/tts")', 1)[1].split('@app.post("/stt/fetch")', 1)[0] \
    if '@app.post("/tts")' in server else ""
check("disabled -> 409", "status_code=409" in tseg)
check("empty text -> 400", "TTS_EMPTY" in tseg and "status_code=400" in tseg)
check("engine errors -> 400", "tts_engine.TtsError" in tseg)
check("post-processing errors -> 500", "status_code=500" in tseg)

# ── (b) audio response ─────────────────────────────────────────────────────
check("returns audio via Response", "Response(content=payload" in tseg)
check("wav is the default format", '"wav"' in tseg and "audio/wav" in tseg)
check("opus is supported", "to_ogg_opus" in tseg and "audio/ogg" in tseg)
check("headers carry duration/voice/speed",
      "X-HiveMind-Duration" in tseg and "X-HiveMind-Voice" in tseg
      and "X-HiveMind-Speed" in tseg)
check("the sidecar is not started while disabled",
      tseg.index("tts_enabled") < tseg.index("tts_engine.synthesize"))
check("the blocking work runs in a thread (no event-loop freeze)",
      "asyncio.to_thread(" in tseg)

# ── (c) settings ───────────────────────────────────────────────────────────
for key, want in (("tts_enabled", "False"), ("tts_voice", '"anna"'),
                  ("tts_speed", "1.08"), ("tts_timeout_s", "120"),
                  ("tts_idle_stop_s", "900")):
    line = next((l for l in settings.splitlines() if f'"{key}"' in l), "")
    check(f"setting {key} defaults to {want}", want in line, f" ({line.strip()[:50]})")

# ── (d) the card ───────────────────────────────────────────────────────────
card = html.split('id="voice-card"', 1)[1].split("<!-- Smart Preload -->", 1)[0] \
    if 'id="voice-card"' in html else ""
for el in ("tts-enabled-toggle", "tts-options", "tts-status-line",
           "tts-voice-sel", "tts-speed-inp", "tts-sample-out"):
    check(f"card element {el}", f'id="{el}"' in card)
_vsel = card.split('id="tts-voice-sel"', 1)[1].split('</select>', 1)[0] \
    if 'id="tts-voice-sel"' in card else ''
check("the select is filled from /tts/voices (no duplicated list)",
      'function _ttsFillVoices' in app and '(loading...)' in _vsel
      and 'value="alba"' not in _vsel)
check("speed is wired", "postSettings({tts_speed:" in card)
check("voice is wired", "postSettings({tts_voice:this.value})" in card)
check("the block is hidden by default", 'id="tts-options" class="tgl-sub" style="display:none"' in card)
check("sample button calls the sample helper", "voicePlaySample()" in card)
check("docs describe the keys", "tts_voice" in docs and "tts_speed" in docs)

# ── (e) speaker button ─────────────────────────────────────────────────────
check("speak button markup exists", "msg-speak'" in app or "msg-speak'" in html)
check("it is hidden unless TTS is on",
      "S.ttsEnabled) ? '' : 'none'" in app or "S.ttsEnabled) ? '' : 'none'" in card)
check("it is bound to a click, never autoplay",
      "voiceSpeakText(" in app
      and ".autoplay" not in app and "autoplay:" not in app)
check("the helper fetches /tts", "'/tts'" in app)
check("audio is created on demand", "new Audio(URL.createObjectURL(blob))" in app)
check("stop helper exists", "function _voiceStopAudio" in app)
check("error codes are translated", "TTS_DISABLED" in app and "TTS_NO_UV" in app)
check("the button has a style", ".msg-speak{" in css)

print()
print(f"SUMMARY tts_server: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
