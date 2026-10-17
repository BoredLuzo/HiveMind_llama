"""Telegram voice notes: parse, download, transcribe, run (voice-in).

Owner decision (2026-10-17): voice stays opt-in. This suite pins the whole
voice-in path without a live bot:

  (a) the update parser recognises `voice` and `audio` payloads and keeps the
      text path untouched (`ParsedUpdate` defaults stay backwards compatible);
  (b) download goes through getFile + the file URL with a size cap;
  (c) HiveClient.stt posts the raw audio to /stt and maps every failure to a
      code the gateway can translate;
  (d) the handler refuses over-long notes, reports download/STT failures with a
      readable hint, keeps the text length cap, and re-dispatches the transcript
      through the SAME start_owner_run path (same gates and approvals);
  (e) voice_in / voice_out are off by default.

Run: python tests/test_gateway_voice.py
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


from hivemind_gateway import auth as gw_auth  # noqa: E402
from hivemind_gateway.config import GatewayConfig  # noqa: E402

# ── (a) parsing ────────────────────────────────────────────────────────────
def _voice_update(file_id="file-1", duration=12, mime="audio/ogg", key="voice"):
    # Telegram nests the media inside the message object
    return {"update_id": 7,
            "message": {
                key: {"file_id": file_id, "duration": duration, "mime_type": mime,
                      "file_size": 1234},
                "from": {"id": 1}, "chat": {"id": 1, "type": "private"},
                "message_id": 99, "date": 1_700_000_000}}


p = gw_auth.parse_update(_voice_update())
check("voice payload is parsed", p is not None and p.voice_file_id == "file-1")
check("duration is captured", p.voice_duration == 12)
check("mime is captured", p.voice_mime == "audio/ogg")
check("text stays empty for a voice note", p.text == "")
p2 = gw_auth.parse_update(_voice_update(file_id="audio-9", key="audio"))
check("audio files are parsed the same way", p2.voice_file_id == "audio-9")
plain = {"update_id": 8, "message": {"text": "hello", "from": {"id": 1},
                                     "chat": {"id": 1, "type": "private"},
                                     "message_id": 5, "date": 1_700_000_000}}
p3 = gw_auth.parse_update(plain)
check("a plain text message has no voice id", p3.voice_file_id is None and p3.text == "hello")
check("ParsedUpdate keeps its old fields", all(
    hasattr(p3, f) for f in ("update_id", "kind", "from_id", "chat_id", "chat_type",
                             "text", "date", "is_forwarded", "message_id",
                             "callback_id")))

# ── (b)/(c) source contracts ───────────────────────────────────────────────
api = read("hivemind_gateway/telegram_api.py")
check("download_file uses getFile", "get_file(file_id)" in api)
check("download is size-capped", "max_bytes" in api and "file too large" in api)
check("send_voice posts multipart to sendVoice",
      "/sendVoice" in api and "files=files" in api)
check("voice notes are sent as audio/ogg",
      "audio/ogg" in api and '"voice": ("voice.ogg"' in api)

client = read("hivemind_gateway/hive_client.py")
check("stt posts the raw body", 'post("/stt", content=audio' in client)
check("stt returns a code on failure", "return \"\", code" in client or '{", code' in client)
check("tts asks for opus", '"format": "opus"' in client)
check("tts has a longer timeout", "timeout=120.0" in client)

# ── (d) the handler ────────────────────────────────────────────────────────
main = read("hivemind_gateway/main.py")
seg = main.split("async def handle_voice_note", 1)[1].split("async def start_owner_run", 1)[0]
check("the length cap is enforced", "voice_max_seconds" in seg)
check("the byte cap is enforced", "voice_max_bytes" in seg)
check("download errors are answered", "Could not download the voice note" in seg)
check("a transcribing notice is sent", "Transcribing" in seg)
check("stt codes map to hints", "STT_NO_ENGINE" in seg and "STT_EMPTY" in seg)
check("the transcript keeps the text cap", "max_text_chars" in seg)
check("the transcript is re-dispatched through start_owner_run",
      "await self.start_owner_run(voiced)" in seg)
check("voice is only used when enabled", "self.cfg.voice_in" in main)
check("disabled voice gets a clear message", "Voice notes are off" in main)

# ── (e) opt-in defaults ────────────────────────────────────────────────────
cfg = GatewayConfig()
check("voice_in defaults to off", cfg.voice_in is False)
check("voice_out defaults to off", cfg.voice_out is False)
check("the byte cap matches the Telegram limit", cfg.voice_max_bytes == 20 * 1024 * 1024)
check("the duration cap is sane", 60 <= cfg.voice_max_seconds <= 900)

# ── HiveClient.stt behaviour with a stub transport ────────────────────────
class _StubResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _StubClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.resp


from hivemind_gateway.hive_client import HiveClient  # noqa: E402

hc = HiveClient("http://127.0.0.1:8001")
hc._client = _StubClient(_StubResp(200, {"text": "hello world"}))
text, code = asyncio.run(hc.stt(b"OggS..."))
check("stt returns the transcript", text == "hello world" and code == "")
hc._client = _StubClient(_StubResp(409, {"code": "STT_DISABLED"}))
text, code = asyncio.run(hc.stt(b"x"))
check("stt maps the engine code", text == "" and code == "STT_DISABLED")
hc._client = _StubClient(_StubResp(200, {"text": "   "}))
text, code = asyncio.run(hc.stt(b"x"))
check("an empty transcript becomes STT_EMPTY", code == "STT_EMPTY")
hc._client = _StubClient(_StubResp(200, ValueError("no json")))
text, code = asyncio.run(hc.stt(b"x"))
check("non-JSON is reported", code.startswith("http "))

hc._client = _StubClient(_StubResp(200, {"text": "x"}))
hc._client.resp.content = b"OggSvoice"
data, code = asyncio.run(hc.tts("hi"))
check("tts returns the audio bytes", data == b"OggSvoice" and code == "")
hc._client = _StubClient(_StubResp(400, {"code": "TTS_DISABLED"}))
data, code = asyncio.run(hc.tts("hi"))
check("tts maps the engine code", data == b"" and code == "TTS_DISABLED")

print()
print(f"SUMMARY gateway_voice: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
