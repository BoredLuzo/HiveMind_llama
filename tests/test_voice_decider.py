"""VOICE-DECIDER-Tests: Sprachnachricht -> mode=auto (Decider entscheidet).

Nutzt einen minimalen Hive-Client (nur die Methoden, die die Bridge wirklich
anfasst) und prueft, was im Stream-Aufruf landet - kein Netzwerk.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hivemind_gateway.bridge import RunBridge  # noqa: E402
from hivemind_gateway.config import GatewayConfig  # noqa: E402
from hivemind_gateway.state import GatewayState  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _Messenger:
    def __init__(self):
        self.messages, self.edits, self.documents = [], [], []
        self._id = 10

    async def send_message(self, text, *args, **kwargs):
        self.messages.append(text)
        self._id += 1
        return {"message_id": self._id}

    async def send_voice(self, data, *args, **kwargs):
        return {"message_id": self._id}

    async def edit_message(self, message_id, text):
        self.edits.append((message_id, text))

    async def send_document(self, data, filename):
        self.documents.append((bytes(data), filename))


class _Hive:
    """So viel Hive-Client, wie die Bridge fuer einen einfachen Lauf braucht."""

    def __init__(self):
        self.stream_bodies = []
        self.chat = {"id": "chat01", "rev": 1, "messages": []}
        self.settings_body = {"mode": "code_duo",
                              "agents": {"direct": {"model": "direct-m"},
                                         "duo_coder": {"model": "coder-m"}}}

    async def get_chat(self, chat_id):
        return _Resp(200, dict(self.chat))

    async def create_chat(self, messages=None, meta=None):
        return _Resp(200, dict(self.chat))

    async def put_chat_messages(self, chat_id, messages, base_rev=0):
        self.chat["messages"] = list(messages)
        self.chat["rev"] = base_rev + 1
        return _Resp(200, {"ok": True, "rev": self.chat["rev"]})

    async def put_chat_meta(self, chat_id, meta, base_rev=0):
        return _Resp(200, {"ok": True, "rev": base_rev + 1})

    async def stream(self, q, chat_id, images=None, mode="", overrides=None):
        self.stream_bodies.append({"q": q, "mode": mode, "overrides": dict(overrides or {})})
        yield {"type": "run_id", "run_id": "r1"}
        yield {"type": "token", "content": "ok"}
        yield {"type": "done", "elapsed": 0.1, "stop_reason": "completed"}

    async def settings(self):
        return dict(self.settings_body)

    async def journal(self, run_id=""):
        return {}

    async def get_models(self):
        return {}

    async def get_presets(self):
        return {}

    async def pending_approval(self, run_id):
        return _Resp(200, {})

    async def decide_approval(self, *a, **kw):
        return _Resp(200, {})

    async def abort_run(self, run_id):
        return _Resp(200, {})

    async def abort_chat(self, chat_id, silent=False):
        return _Resp(200, {})

    async def steer(self, run_id, text):
        return _Resp(200, {})

    async def set_setting(self, key, value):
        return _Resp(200, {})

    async def load_preset(self, name):
        return _Resp(200, {})

    async def tts(self, text, **kw):
        return _Resp(200, {})

    async def warm_voice(self):
        return _Resp(200, {})


def _run(from_voice, phone_mode=""):
    hive = _Hive()
    tmp = Path(tempfile.mkdtemp(prefix="gwvd_"))
    st = GatewayState(tmp / "gateway_state.json")
    st.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}
    if phone_mode:
        st.data["mode"] = phone_mode
    br = RunBridge(hive, st, GatewayConfig(), _Messenger())
    try:
        asyncio.run(br.start_text_run("wie spaet ist es", from_voice=from_voice))
    except (RuntimeError, TimeoutError, AttributeError) as exc:
        print(f"    (run note: {type(exc).__name__}: {exc})")
    return hive.stream_bodies[-1] if hive.stream_bodies else {}


voice = _run(True)
check("voice note sends mode=auto", voice.get("mode") == "auto", f"(mode={voice.get('mode')!r})")
check("voice note explicitly disables agentic (decider stays in charge)",
      (voice.get("overrides") or {}).get("duo_agentic_mode") is False,
      f"({(voice.get('overrides') or {}).get('duo_agentic_mode')!r})")
check("voice note marks its source",
      (voice.get("overrides") or {}).get("source") == "telegram-voice",
      f"({(voice.get('overrides') or {}).get('source')!r})")

typed = _run(False)
check("typed text keeps the engine's configured mode", typed.get("mode") == "",
      f"(mode={typed.get('mode')!r})")
check("typed text keeps the telegram source marker",
      (typed.get("overrides") or {}).get("source") == "telegram")

forced = _run(True, phone_mode="agentic")
check("/mode agentic still wins for a voice note",
      forced.get("mode") == "auto"
      and (forced.get("overrides") or {}).get("duo_agentic_mode") is True,
      f"(mode={forced.get('mode')!r}, agentic={(forced.get('overrides') or {}).get('duo_agentic_mode')})")
check("an explicit phone mode is not relabelled as auto-decide",
      (forced.get("overrides") or {}).get("source") == "telegram",
      f"({(forced.get('overrides') or {}).get('source')!r})")

src = (ROOT / "hivemind_gateway" / "main.py").read_text(encoding="utf-8", errors="replace")
check("voice handler marks the run as spoken", "voiced.from_voice = True" in src)
check("owner run passes the flag to the bridge",
      'from_voice=bool(getattr(p, "from_voice", False))' in src)

print()
print(f"SUMMARY voice_decider: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
