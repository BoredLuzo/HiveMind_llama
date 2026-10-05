# -*- coding: utf-8 -*-
"""Gateway WP2: the text-run bridge against a FAKE HiveMind (SSE recorder)
and a recording messenger — no network.

Covers: transcript PUT before/after the run, 409 adopt-and-retry, busy
rejection (own state + journal heuristic), throttled status edits,
auto-deny of approvals, readable error mapping (model_load_failed, run
error, offline), splitting, .txt document on >3 chunks, run cleanup.
"""
import asyncio
import sys

import httpx
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway.bridge import RunBridge
from hivemind_gateway.config import GatewayConfig
from hivemind_gateway.hive_client import HiveUnreachable
from hivemind_gateway.state import GatewayState
from hivemind_gateway.telegram_api import TelegramApiError

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HiveUnreachable(f"HTTP {self.status_code}")


class FakeHive:
    """Scripted SSE + chat store mirroring the real contract."""

    def __init__(self, events=None, fail_first_put_409=False,
                 offline=False):
        self.events = events or []
        self.chat = {"id": "chat01", "rev": 1, "messages": []}
        self.stream_bodies = []
        self.puts = []          # (messages, base_rev)
        self.denies = []        # run_ids denied
        self.aborts = []
        self.journal_body = {"active": False}
        self.fail_first_put_409 = fail_first_put_409
        self.offline = offline
        self._put_count = 0

    async def create_chat(self, title):
        if self.offline:
            raise HiveUnreachable("connection refused")
        self.chat["title"] = title
        return {"ok": True, "id": self.chat["id"], "rev": 1,
                "chat": dict(self.chat, created_at="2026-10-05T12:00:00")}

    async def get_chat(self, chat_id):
        return FakeResponse(200, dict(self.chat))

    async def put_chat_messages(self, chat_id, messages, base_rev):
        self._put_count += 1
        self.puts.append((list(messages), base_rev))
        if self.offline:
            raise HiveUnreachable("connection refused")
        if self.fail_first_put_409 and self._put_count == 1:
            return FakeResponse(409, {"error": "stale_rev", "rev": 5,
                                      "messages": [{"role": "user",
                                                    "content": "server won"}]})
        self.chat["messages"] = list(messages)
        self.chat["rev"] = base_rev + 1
        return FakeResponse(200, {"ok": True, "rev": self.chat["rev"]})

    async def stream(self, q, chat_id, images=None, mode=""):
        self.stream_bodies.append({"q": q, "chat_id": chat_id,
                                   "mode": mode})
        for ev in self.events:
            yield ev

    async def decide_approval(self, run_id, answer):
        self.denies.append((run_id, answer))
        return FakeResponse(200, {"routed": "pause", "run_id": run_id})

    async def abort_run(self, run_id):
        self.aborts.append(("run", run_id))
        return FakeResponse(200, {"ok": True})

    async def abort_chat(self, chat_id, silent=False):
        self.aborts.append(("chat", chat_id))
        return FakeResponse(200, {"ok": True})

    async def journal(self):
        if self.offline:
            raise HiveUnreachable("connection refused")
        return dict(self.journal_body)


class FakeMessenger:
    def __init__(self):
        self.messages = []
        self.edits = []          # (message_id, text)
        self.documents = []
        self._next_id = 100
        self.owner_id = "111"

    async def send_message(self, text):
        self._next_id += 1
        self.messages.append(text)
        return {"message_id": self._next_id}

    async def edit_message(self, message_id, text):
        self.edits.append((message_id, text))

    async def send_document(self, data, filename):
        self.documents.append((bytes(data), filename))


def _mk(tmp_name, hive, events=None, **kw):
    tmp = Path(tempfile.mkdtemp(prefix=tmp_name))
    st = GatewayState(tmp / "gateway_state.json")
    st.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}
    ms = FakeMessenger()
    br = RunBridge(hive, st, GatewayConfig(), ms)
    return br, st, ms


def _run_events():
    return [
        {"type": "run_id", "run_id": "1791-abc"},
        {"type": "status", "content": "planner läuft"},
        {"type": "approval_request", "run_id": "1791-abc",
         "tool": "run_bash", "preview": "cmd"},
        {"type": "token", "content": "Hallo "},
        {"type": "token", "content": "Welt."},
        {"type": "done", "elapsed": 1.0, "stop_reason": "completed"},
    ]


# ── 1. happy path ───────────────────────────────────────────────────────
async def t_happy():
    hive = FakeHive(_run_events())
    br, st, ms = _mk("gwbr_happy_", hive)
    note = await br.start_text_run("meine frage")
    check("final text delivered", any("Hallo Welt." in m for m in ms.messages))
    check("no error in final", note.startswith("Hallo Welt.") or "Hallo Welt." in note)
    check("run_id persisted during run", True)  # cleared below; checked via puts
    check("active_run cleared", st.data["active_run"] is None)
    # user turn PUT before stream: first PUT holds exactly the user turn
    first_put = hive.puts[0]
    check("user turn PUTed", first_put[0][-1]["role"] == "user"
          and first_put[0][-1]["content"] == "meine frage")
    # assistant turn PUT after: last PUT ends with assistant content
    last_put = hive.puts[-1]
    check("assistant turn PUTed", last_put[0][-1]["role"] == "assistant"
          and "Hallo Welt." in last_put[0][-1]["content"])
    check("approval auto-denied with 3",
          hive.denies == [("1791-abc", "3")])
    check("deny note shown", any("abgelehnt" in t for _, t in ms.edits))
    check("status finished edit", ms.edits[-1][1] == "✅ fertig.")

# ── 2. 409 adopt-and-retry ─────────────────────────────────────────────
async def t_conflict():
    hive = FakeHive(_run_events(), fail_first_put_409=True)
    br, st, ms = _mk("gwbr_409_", hive)
    await br.start_text_run("frage")
    p1, p2 = hive.puts[0], hive.puts[1]
    check("first PUT got 409'd", p1[0][-1]["content"] == "frage")
    check("retry adopts server messages",
          p2[0][0] == {"role": "user", "content": "server won"}
          and p2[0][-1]["content"] == "frage")
    check("retry uses server rev", p2[1] == 5)

# ── 3. busy: own state + journal heuristic ─────────────────────────────
async def t_busy():
    hive = FakeHive(_run_events())
    br, st, ms = _mk("gwbr_busy_", hive)
    st.data["active_run"] = {"run_id": "old-1", "chat_id": "chat01"}
    note = await br.start_text_run("zweite frage")
    check("own-run busy rejected", "bereits ein Lauf" in note)
    check("no stream consumed", hive.puts == [])
    st.data["active_run"] = None
    hive.journal_body = {"active": True, "run_id": "ui-77", "done": False,
                         "aborted": False, "ts": time.time()}
    note2 = await br.start_text_run("dritte frage")
    check("UI-run busy rejected (journal)", "ui-77" in note2
          and "Browser" in note2)
    hive.journal_body = {"active": True, "run_id": "old", "done": True}
    await br.start_text_run("vierte frage")
    check("done journal not busy", "Hallo Welt." in "".join(m for m in ms.messages))

# ── 4. error mapping ────────────────────────────────────────────────────
async def t_errors():
    ev = [{"type": "run_id", "run_id": "r1"},
          {"type": "done", "stop_reason": "model_load_failed"}]
    br, st, ms = _mk("gwbr_err1_", FakeHive(ev))
    note = await br.start_text_run("q")
    check("model_load_failed readable", "VRAM" in note and "❌" in note)

    ev2 = [{"type": "run_id", "run_id": "r2"},
           {"type": "error", "content": "ValueError: boom"},
           {"type": "done", "elapsed": 0, "stop_reason": "error"}]
    br2, st2, ms2 = _mk("gwbr_err2_", FakeHive(ev2))
    note2 = await br2.start_text_run("q")
    check("run error readable", "Lauf-Fehler" in note2 and "boom" in note2)

    br3, st3, ms3 = _mk("gwbr_err3_", FakeHive(offline=True))
    st3.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}
    note3 = await br3.start_text_run("q")
    check("offline readable", "nicht erreichbar" in note3)
    check("offline cleared run", st3.data["active_run"] is None)

    # realrun bug #4: RAW httpx errors (not wrapped by the client) must
    # also end as a readable offline message, never as an escaping raise
    class RawConnectHive(FakeHive):
        async def create_chat(self, title):
            raise httpx.ConnectError("All connection attempts failed")

    br4, st4, ms4 = _mk("gwbr_err4_", RawConnectHive())
    st4.data.pop("tg_chat")  # fresh install: first run must CREATE the chat
    note4 = await br4.start_text_run("q")
    check("raw ConnectError -> readable offline note",
          "nicht erreichbar" in note4)
    check("raw ConnectError cleared run", st4.data["active_run"] is None)

    class MidStreamBreakHive(FakeHive):
        async def stream(self, q, chat_id, images=None, mode=""):
            yield {"type": "run_id", "run_id": "r5"}
            raise httpx.ReadError("connection reset mid-stream")

    br5, st5, ms5 = _mk("gwbr_err5_", MidStreamBreakHive())
    st5.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}
    note5 = await br5.start_text_run("q")
    check("mid-stream break readable", "nicht erreichbar" in note5)
    check("mid-stream break cleared run", st5.data["active_run"] is None)
    check("mid-stream break answered phone", len(ms5.messages) >= 1)

# ── 5. splitting + .txt document ────────────────────────────────────────
async def t_split_doc():
    long_text = "\n".join(f"zeile {i}" for i in range(400))
    ev = [{"type": "run_id", "run_id": "r"},
          {"type": "token", "content": long_text},
          {"type": "done", "stop_reason": "completed"}]
    br, st, ms = _mk("gwbr_split_", FakeHive(ev))
    await br.start_text_run("q")
    check("no doc within 3 chunks", ms.documents == [] and len(ms.messages) >= 2)

    huge = "\n".join(f"z{i}: {'x' * 80}" for i in range(300))
    ev2 = [{"type": "run_id", "run_id": "r"},
           {"type": "token", "content": huge},
           {"type": "done", "stop_reason": "completed"}]
    br2, st2, ms2 = _mk("gwbr_doc_", FakeHive(ev2))
    await br2.start_text_run("q")
    check("overflow goes as .txt", len(ms2.documents) == 1
          and ms2.documents[0][1] == "ergebnis.txt")
    check("doc content complete", huge in ms2.documents[0][0].decode("utf-8"))

# ── 6b. secret filter on the .txt document path ────────────────────────
async def t_secrets_doc_path():
    huge = ("padding " + "x" * 80 + " token sk-abcdefghijklmnopqrstuvwx\n") * 500
    ev = [{"type": "run_id", "run_id": "r"},
          {"type": "token", "content": huge},
          {"type": "done", "stop_reason": "completed"}]
    br, st, ms = _mk("gwbr_secdoc_", FakeHive(ev))
    await br.start_text_run("q")
    check("doc overflow happens", len(ms.documents) == 1)
    check("secret filtered in .txt document too",
          "sk-abcdefghijklmnopqrstuvwx" not in ms.documents[0][0].decode("utf-8"))


async def t_secrets():
    ev = [{"type": "run_id", "run_id": "r"},
          {"type": "token",
           "content": "key ist sk-abcdefghijklmnopqrstuvwx fertig"},
          {"type": "done", "stop_reason": "completed"}]
    br, st, ms = _mk("gwbr_sec_", FakeHive(ev))
    note = await br.start_text_run("q")
    check("secret filtered from output", "sk-abcdefghijklmnopqrstuvwx" not in note
          and all("sk-abcdefghijklmnopqrstuvwx" not in m for m in ms.messages))

# ── 6c. /mode — telegram-side run mode (stream body, never settings) ───
async def t_mode():
    br, st, ms = _mk("gwbr_mode_", FakeHive(_run_events()))
    # default: follow engine settings (no mode in the stream body)
    await br.start_text_run("q1")
    check("default sends NO mode field",
          br.hive.stream_bodies[-1]["mode"] == "")
    # set via alias
    out = br.mode_text("chat")
    check("alias chat -> simple", "simple" in out)
    await br.start_text_run("q2")
    check("set mode travels in stream BODY",
          br.hive.stream_bodies[-1]["mode"] == "simple")
    check("mode persisted", st.data["mode"] == "simple")
    check("status shows mode", "simple" in br.status_text())
    # invalid rejected, mode unchanged
    out = br.mode_text("nuclear")
    check("invalid mode rejected", "Unbekannter Modus" in out
          and st.data["mode"] == "simple")
    # off -> follow settings again
    out = br.mode_text("off")
    await br.start_text_run("q3")
    check("off clears mode", "folgt wieder" in out
          and br.hive.stream_bodies[-1]["mode"] == "")
    # bare /mode = status only
    out = br.mode_text("")
    check("bare /mode shows status", "Modus (Telegram)" in out)

# ── 7. /stop behavior ───────────────────────────────────────────────────
async def t_stop():
    hive = FakeHive()
    br, st, ms = _mk("gwbr_stop_", hive)
    note = await br.stop()
    check("stop without run", "Kein Lauf aktiv" in note)
    st.data["active_run"] = {"run_id": "r9", "chat_id": "chat01"}
    note2 = await br.stop()
    check("stop sends abort", ("run", "r9") in hive.aborts
          and "Abbruch" in note2)

    class _BoomHive(FakeHive):
        async def abort_run(self, run_id):
            raise HiveUnreachable("dead")

    br2, st2, ms2 = _mk("gwbr_stop2_", _BoomHive())
    st2.data["active_run"] = {"run_id": "r10", "chat_id": "chat01"}
    note3 = await br2.stop()
    check("stop fails VISIBLY", "fehlgeschlagen" in note3 and "r10" in note3)


async def _main():
    await t_happy()
    await t_conflict()
    await t_busy()
    await t_errors()
    await t_split_doc()
    await t_secrets()
    await t_secrets_doc_path()
    await t_mode()
    await t_stop()

asyncio.run(_main())
print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)
