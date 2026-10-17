"""Voice-out: the bridge speaks the final answer (opt-in).

Covers the whole path without a live bot:
  (a) send.py routes voice_bytes to sendVoice (and keeps the owner-only check);
  (b) the messenger exposes send_voice for the bridge;
  (c) the bridge only speaks when cfg.voice_out or settings.telegram_voice_out is
      on, only for real answers (not for error runs), and never lets a TTS
      failure break the run;
  (d) the voice note is the CLEAN answer (no ❌/⏹ decorations).

Run: python tests/test_gateway_voice_out.py
"""
import asyncio
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


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


send_src = read("hivemind_gateway/send.py")
main_src = read("hivemind_gateway/main.py")
bridge_src = read("hivemind_gateway/bridge.py")
config_src = read("hivemind_gateway/config.py")

# ── (a) send.py ───────────────────────────────────────────────────────────
check("send() accepts voice bytes", "voice_bytes: bytes | None = None" in send_src)
check("voice goes to sendVoice", "api.send_voice(" in send_src)
check("the owner check runs before the voice branch",
      send_src.index("not the owner") < send_src.index("api.send_voice("))

# ── (b) messenger ─────────────────────────────────────────────────────────
check("the messenger exposes send_voice", "async def send_voice(self, ogg" in main_src)
check("it goes through the central send()",
      "voice_bytes=ogg" in main_src and "gw_send.send(" in main_src)

# ── (c) the bridge decides ────────────────────────────────────────────────
check("voice-out is read from the config", "self.cfg.voice_out" in bridge_src)
check("and from the engine setting", '.get("telegram_voice_out"' in bridge_src)
check("the helpers exist",
      "async def _voice_out_enabled" in bridge_src
      and "async def _maybe_voice_answer" in bridge_src)
_fin = bridge_src[bridge_src.index("async def _finish"):]
_end = _fin.find("    # -- busy heuristic")
_fin = _fin[:_end] if _end > 0 else _fin[:4000]
# RESULT-MODE (1.4.2): the order depends on the mode. "both" sends the text
# chunks first and the voice note after them; "voice" speaks first and falls back
# to text only when the synthesis failed.
# _fin starts at the "voice" branch, which speaks FIRST by design; the text path
# sits in the else-branch after it. So compare inside the whole _finish body.
_fin_all = bridge_src[bridge_src.index("async def _finish"):]
_fin_all = _fin_all[:_fin_all.find("    # -- busy heuristic")
                    if _fin_all.find("    # -- busy heuristic") > 0 else 4000]
_both_branch = _fin_all[_fin_all.index(
    "else:\n            chunks = render.split_message"):]
check("both: the text goes out before the voice note",
      _both_branch.index("for c in chunks:")
      < _both_branch.index("await self._maybe_voice_answer(answer)"))
check("voice mode speaks first and falls back to text",
      'if _mode == "voice"' in _fin and "if not self._voice_sent" in _fin)
check("text mode never speaks", '_mode == "both"' in _fin)
check("the mode is read from the engine setting",
      "telegram_result_mode" in bridge_src)
check("error runs are not spoken", 'stop_reason != "error" and answer' in bridge_src)
check("a TTS failure is only a log note",
      "voice answer skipped" in bridge_src and "voice answer send failed" in bridge_src)
check("the voice answer is not the decorated text",
      "await self._maybe_voice_answer(answer)" in bridge_src)

# ── behaviour of the helper with a stub hive/ms ───────────────────────────
from hivemind_gateway.bridge import RunBridge  # noqa: E402
from hivemind_gateway.config import GatewayConfig  # noqa: E402


class _StubState:
    data = {}


class _StubHive:
    def __init__(self, settings: dict, ogg: bytes = b"OggSvoice", code: str = ""):
        self._settings = settings
        self._ogg = ogg
        self._code = code
        self.tts_calls = []

    async def settings(self):
        return self._settings

    async def tts(self, text, voice="", speed=None):
        self.tts_calls.append(text)
        return self._ogg, self._code


class _StubMs:
    def __init__(self):
        self.voices = []

    async def send_voice(self, ogg, duration_s=0):
        self.voices.append(ogg)


def build(cfg, settings, ogg=b"OggSvoice", code=""):
    ms = _StubMs()
    hive = _StubHive(settings, ogg, code)
    bridge = RunBridge(hive, _StubState(), cfg, ms)
    return bridge, hive, ms


cfg_off = GatewayConfig()
cfg_on = GatewayConfig(voice_out=True)

b, hive, ms = build(cfg_off, {})
asyncio.run(b._maybe_voice_answer("hello"))
check("off: no synthesis", hive.tts_calls == [] and ms.voices == [])

b, hive, ms = build(cfg_on, {})
asyncio.run(b._maybe_voice_answer("hello there"))
check("cfg on: one synthesis", len(hive.tts_calls) == 1)
check("cfg on: one voice note sent", ms.voices == [b"OggSvoice"])

b, hive, ms = build(cfg_off, {"telegram_voice_out": True})
asyncio.run(b._maybe_voice_answer("from the setting"))
check("setting on: speaks too", len(ms.voices) == 1)

b, hive, ms = build(cfg_off, {})
asyncio.run(b._maybe_voice_answer("   "))
check("empty answer: nothing", hive.tts_calls == [])

b, hive, ms = build(cfg_on, {})
asyncio.run(b._maybe_voice_answer("x"))
check("engine reports a code (no audio) -> nothing sent",
      build(cfg_on, {}, ogg=b"", code="TTS_DISABLED")[2].voices == [])

b, hive, ms = build(cfg_on, {})
asyncio.run(b._maybe_voice_answer("a reasonably long answer " * 60))
check("long answers are passed through (the engine caps at 600 chars)",
      len(hive.tts_calls[0]) > 600)


# ── result mode + progress gate ───────────────────────────────────────────
check("result mode accepts text/voice/both",
      'mode in ("text", "voice", "both")' in bridge_src)
check("progress messages have a gate", "_progress_ok" in bridge_src)
check("the status line is gated, approvals are not",
      "_progress_ok()" in bridge_src.split("async def _edit_status")[1][:400]
      and "_progress_ok" not in bridge_src.split("async def _relay_own_approval")[1][:600])
check("progress setting is read from the engine",
      "telegram_progress_msgs" in bridge_src)


# ── run-start preload (1.4.2) ─────────────────────────────────────────────
check("the bridge warms the engine when a run starts",
      "await self._preload_voice()" in bridge_src)
check("the preload is best effort",
      "voice preload skipped" in bridge_src)
check("the preload skips a pure text result",
      'if mode == "text" and not (s or {}).get("telegram_voice_out", False)' in bridge_src)
client_src = (ROOT / "hivemind_gateway" / "hive_client.py").read_text(
    encoding="utf-8", errors="replace")
check("the client calls the preload endpoint", '"/tts/preload"' in client_src)
server_src = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
check("the engine exposes it", '@app.post("/tts/preload")' in server_src)
check("the endpoint does not block the loop",
      "asyncio.to_thread(tts_engine.preload" in server_src)

print()
print(f"SUMMARY gateway_voice_out: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
