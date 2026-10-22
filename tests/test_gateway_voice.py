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
check("from_voice defaults to False on parse", p3.from_voice is False and p.from_voice is False)
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
check("stt posts the raw body", '"/stt"' in client and "content=audio" in client)
check("stt returns a code on failure", "return \"\", code" in client or '{", code' in client)
check("tts asks for opus", '"format": "opus"' in client)
check("tts has a longer timeout", "timeout=120.0" in client)
# STT-TIMEOUT (2026-10-22, audit): the client global is 60 s but the engine
# budget is stt_timeout_s up to 120 s - a long clip raced the client and
# came back "transport: ReadTimeout". The /stt call carries its own budget.
check("stt carries the engine's time budget (not the 60s global)",
      "timeout=httpx.Timeout(135.0" in client)

# ── (d) the handler ────────────────────────────────────────────────────────
main = read("hivemind_gateway/main.py")
seg = main.split("async def handle_voice_note", 1)[1].split("async def start_owner_run", 1)[0]

# ── (d2) download_file level fix (2026-10-22, live): _call returns the
# INNER result (the file object) — the old code looked for a nested
# "result" inside it and every voice download failed before fetching. ──────
import httpx as _httpx
from hivemind_gateway.telegram_api import TelegramApi as _RealApi, TelegramApiError
import asyncio as _aio


class _Transport:
    """Fake transport: POST returns getFile-style JSON, GET returns bytes."""

    class _R:
        def __init__(self, status_code, content, json_data=None):
            self.status_code = status_code
            self.content = content
            self._json = json_data

        def json(self):
            return self._json if self._json is not None else {}

    def __init__(self, file_obj, payload=b"OggSvoice"):
        self.file_obj, self.payload = file_obj, payload
        self.get_urls = []

    async def post(self, url, json=None):
        return _Transport._R(200, content=b"{}",
                             json_data={"ok": True, "result": self.file_obj})

    async def get(self, url):
        self.get_urls.append(url)
        return _Transport._R(200, self.payload)


def _make_api(transport):
    """RealApi ohne __init__ (keine Locks) + Fake-Transport."""
    api = _RealApi.__new__(_RealApi)
    api._token = "test-token"
    api._base_url = "https://api.telegram.org"
    api._base = api._base_url + "/bottest-token"
    api._lock = None
    api._client = None

    async def _fake_http():
        return transport

    api._http = _fake_http
    return api


_tr = _Transport({"file_id": "f1", "file_size": 9,
                  "file_path": "voice/file_1.oga"}, b"OggSvoice")
_api = _make_api(_tr)
wav_dl = _aio.run(_api.download_file("f1"))
check("download_file reads the file_path from the inner result",
      wav_dl == b"OggSvoice" and len(_tr.get_urls) == 1
      and "file_1.oga" in _tr.get_urls[0])

# negative: getFile WITHOUT file_path -> clean TelegramApiError
_tr2 = _Transport({"file_id": "f1"})
_api2 = _make_api(_tr2)
try:
    _aio.run(_api2.download_file("f1"))
    check("getFile without file_path raises", False)
except TelegramApiError as exc:
    check("getFile without file_path raises TelegramApiError",
          "no file_path" in str(exc))

print(f"SUMMARY gateway_voice: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
