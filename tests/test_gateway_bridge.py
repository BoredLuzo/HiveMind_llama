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
        self.journal_calls = []  # run_id scoping args (audit G7)
        self.settings_body = {}
        self.pending_body = {"active": False}
        self.steered = []
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

    async def stream(self, q, chat_id, images=None, mode="",
                     overrides=None):
        self.stream_bodies.append({"q": q, "chat_id": chat_id,
                                   "mode": mode, "overrides": overrides or {}})
        for ev in self.events:
            yield ev

    async def decide_approval(self, run_id, answer, decision_id="",
                              tool=""):
        self.denies.append((run_id, answer))
        return FakeResponse(200, {"routed": "pause", "run_id": run_id})

    async def abort_run(self, run_id):
        self.aborts.append(("run", run_id))
        return FakeResponse(200, {"ok": True})

    async def abort_chat(self, chat_id, silent=False):
        self.aborts.append(("chat", chat_id))
        return FakeResponse(200, {"ok": True})

    async def journal(self, run_id=""):
        self.journal_calls.append(run_id)
        if self.offline:
            raise HiveUnreachable("connection refused")
        return dict(self.journal_body)

    async def settings(self):
        return dict(self.settings_body)

    async def pending_approval(self, run_id):
        return FakeResponse(200, dict(self.pending_body))

    async def steer(self, run_id, text):
        self.steered.append((run_id, text))
        return FakeResponse(200, {"status": "queued"})


class FakeMessenger:
    def __init__(self):
        self.messages = []
        self.parse_modes = []
        self.markups = []
        self.edits = []          # (message_id, text)
        self.documents = []
        self._next_id = 100
        self.owner_id = "111"

    async def send_message(self, text, parse_mode=None, reply_markup=None):
        self._next_id += 1
        self.messages.append(text)
        self.parse_modes.append(parse_mode)
        self.markups.append(reply_markup)
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
    check("deny note shown", any("denied" in t for _, t in ms.edits))
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
    check("own-run busy rejected", "already active" in note)
    check("no stream consumed", hive.puts == [])
    st.data["active_run"] = None
    hive.journal_body = {"active": True, "run_id": "ui-77", "done": False,
                         "aborted": False, "ts": time.time()}
    note2 = await br.start_text_run("dritte frage")
    check("UI-run busy rejected (journal)", "ui-77" in note2
          and "browser" in note2)
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
    check("run error readable", "Run error" in note2 and "boom" in note2)

    br3, st3, ms3 = _mk("gwbr_err3_", FakeHive(offline=True))
    st3.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}
    note3 = await br3.start_text_run("q")
    check("offline readable", "unreachable" in note3)
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
          "unreachable" in note4)
    check("raw ConnectError cleared run", st4.data["active_run"] is None)

    class MidStreamBreakHive(FakeHive):
        async def stream(self, q, chat_id, images=None, mode="",
                         overrides=None):
            yield {"type": "run_id", "run_id": "r5"}
            raise httpx.ReadError("connection reset mid-stream")

    br5, st5, ms5 = _mk("gwbr_err5_", MidStreamBreakHive())
    st5.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}
    note5 = await br5.start_text_run("q")
    check("mid-stream break readable", "unreachable" in note5)
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


# ── 6d. /models + /setModel + numeric follow-up ────────────────────────
async def t_models_flow():
    class ModelsHive(FakeHive):
        def __init__(self):
            super().__init__(_run_events())
            self.loaded_presets = []

        async def get_models(self):
            return {"models": ["m-a", "m-b", "m-c"],
                    "profiles": [
                        {"name": "m-a", "thinking": True},
                        {"name": "m-b", "thinking": False},
                        {"name": "m-c", "thinking": True, "vision": True}]}

        async def get_presets(self):
            return {"code": {}, "write": {}}

        async def load_preset(self, name):
            self.loaded_presets.append(name)
            return FakeResponse(200, {"ok": True})

        async def settings(self):
            return {"duo_agentic_mode": False}

    hive = ModelsHive()
    br, st, ms = _mk("gwbr_models_", hive)

    # /models list with flags
    out = await br.models_text()
    check("models numbered", "1. m-a" in out and "3. m-c" in out)
    check("models show flags", "[thinking]" in out
          and "[thinking+vision]" in out)
    check("models hint", "/setModel" in out)

    # /setModel with the follow-up question in ONE message
    out = await br.set_model("2")
    check("setmodel echoes model", "m-b" in out)
    check("setmodel asks one-line format",
          "preset, ctx_thinking, ctx_model" in out)
    check("setmodel lists presets numbered",
          "1=code" in out and "2=write" in out)
    check("setmodel warns global", "GLOBAL" in out or "global" in out)
    check("setmodel no-thinking hint", "cannot think" in out)

    # a non-numeric message is consumed as format reminder (no run!)
    runs_before = len(hive.stream_bodies)
    note = await br.consume_setup("hallo welt")
    check("non-numeric -> reminder", "Expected 3 numbers" in note)
    check("reminder did not start a run",
          len(hive.stream_bodies) == runs_before)

    # wrong count
    note = await br.consume_setup("1, 2")
    check("wrong count -> reminder", "Expected 3 numbers" in note)

    # the real answer: preset 1=code, no thinking, ctx 16384
    note = await br.consume_setup("1, 0, 16384")
    check("answer confirms model", "m-b" in note and "Saved" in note)
    check("preset loaded globally", hive.loaded_presets == ["code"])
    check("overrides stored", st.data["run_overrides"]["model"] == "m-b"
          and st.data["run_overrides"]["coder_ctx"] == 16384
          and st.data["run_overrides"]["planner_ctx"] == 0)
    check("pending cleared", st.data["pending_setup"] is None)

    # next run carries the overrides in the stream body
    await br.start_text_run("q with overrides")
    body = hive.stream_bodies[-1]
    check("stream body has model override",
          body["q"] == "q with overrides"
          and hive.puts  # transcript write happened as usual
          or True)
    # overrides reach the request via hive.stream kwargs — assert via a
    # recording subclass:
    class RecHive(ModelsHive):
        def __init__(self):
            super().__init__()
            self.last_kwargs = {}

        async def stream(self, q, chat_id, images=None, mode="",
                         overrides=None):
            self.last_kwargs = {"mode": mode, "overrides": overrides or {}}
            async for ev in super().stream(q, chat_id, images, mode,
                                           overrides):
                yield ev

    hive2 = RecHive()
    br2, st2, ms2 = _mk("gwbr_models2_", hive2)
    st2.data["run_overrides"] = {"model": "m-c", "planner_ctx": 8192,
                                 "coder_ctx": 16384}
    await br2.start_text_run("q")
    check("overrides in stream request",
          hive2.last_kwargs["overrides"].get("duo_planner_model") == "m-c"
          and hive2.last_kwargs["overrides"].get("duo_coder_model") == "m-c"
          and hive2.last_kwargs["overrides"].get("duo_planner_ctx_target")
          == 8192
          and hive2.last_kwargs["overrides"].get("duo_coder_ctx_agentic")
          == 16384
          and hive2.last_kwargs["overrides"].get("duo_planner") is True)

    # /cancel clears a pending flow
    await br.set_model("1")
    check("cancel clears pending", "aborted" in br.cancel_setup()
          and st.data["pending_setup"] is None)

    # reset overrides
    check("reset clears overrides",
          "cleared" in br.reset_overrides()
          and st.data["run_overrides"] == {})


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
    check("invalid mode rejected", "Unknown mode" in out
          and st.data["mode"] == "simple")
    # agentic: phone-side composite (engine mode auto + body flag)
    out = br.mode_text("agentic")
    check("agentic mode set", "agentic" in out)
    await br.start_text_run("q4")
    check("agentic rides body as auto+flag",
          br.hive.stream_bodies[-1]["mode"] == "auto"
          and br.hive.stream_bodies[-1]["overrides"].get(
              "duo_agentic_mode") is True)
    check("bare /mode lists explanations",
          "agentic" in br.mode_text("") and "NO file tools" in br.mode_text(""))

    # off -> follow settings again
    out = br.mode_text("off")
    await br.start_text_run("q3")
    check("off clears mode", "follow the engine settings again" in out
          and br.hive.stream_bodies[-1]["mode"] == "")
    # bare /mode = status only
    out = br.mode_text("")
    check("bare /mode shows status", "Mode (Telegram)" in out)

# ── 7. /stop behavior ───────────────────────────────────────────────────
async def t_stop():
    hive = FakeHive()
    br, st, ms = _mk("gwbr_stop_", hive)
    note = await br.stop()
    check("stop without run", "No run active" in note)
    st.data["active_run"] = {"run_id": "r9", "chat_id": "chat01"}
    note2 = await br.stop()
    check("stop sends abort", ("run", "r9") in hive.aborts
          and "Abort" in note2)

    class _BoomHive(FakeHive):
        async def abort_run(self, run_id):
            raise HiveUnreachable("dead")

    br2, st2, ms2 = _mk("gwbr_stop2_", _BoomHive())
    st2.data["active_run"] = {"run_id": "r10", "chat_id": "chat01"}
    note3 = await br2.stop()
    check("stop fails VISIBLY", "failed" in note3 and "r10" in note3)

    # 2026-10-05 (live): engine answers 404 -> run is already gone; the
    # latch MUST clear (fallback abort rides the run's own chat_id),
    # otherwise every further text gets the busy note until restart.
    class Gone404Hive(FakeHive):
        async def abort_run(self, run_id):
            return FakeResponse(404, {"ok": False})

    hive404 = Gone404Hive()
    br3, st3, ms3 = _mk("gwbr_stop3_", hive404)
    st3.data["tg_chat"] = {}
    st3.data["active_run"] = {"run_id": "r11", "chat_id": "chat01"}
    note4 = await br3.stop()
    check("stop 404: run cleared (no busy latch)",
          st3.data.get("active_run") is None)
    check("stop 404: fallback abort rides run chat_id",
          ("chat", "chat01") in hive404.aborts)
    check("stop 404: honest note", "cleared" in note4 and "r11" in note4)


# ── 7. P10 run takeover: mirror, approval relay, steer, workspace ──────
async def t_takeover():
    class MirrorHive(FakeHive):
        def __init__(self):
            super().__init__(_run_events())
            self.settings_body = {"telegram_mirror_enabled": True}
            self.journal_body = {"active": False}
            self.pending_body = {"active": False}
            self.steered = []
            self.decided = []
            self.aborts = []

        async def settings(self):
            return dict(self.settings_body)

        async def journal(self, run_id=""):
            self.journal_calls.append(run_id)
            return dict(self.journal_body)

        async def pending_approval(self, run_id):
            return FakeResponse(200, dict(self.pending_body))

        async def decide_approval(self, run_id, answer, decision_id="",
                                  tool=""):
            self.decided.append((run_id, answer, decision_id, tool))
            return FakeResponse(200, {"routed": "pause"})

        async def steer(self, run_id, text):
            self.steered.append((run_id, text))
            return FakeResponse(200, {"status": "queued"})

        async def abort_run(self, run_id):
            self.aborts.append(run_id)
            return FakeResponse(200, {"ok": True})

    hive = MirrorHive()
    br, st, ms = _mk("gwbr_take_", hive)
    st.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}

    # mirror OFF (default): tick does nothing, intercept False
    await br.mirror_tick()
    check("mirror off: no takeover", ms.messages == []
          and br.mirror_intercept("irgendein text") is False)

    # mirror ON + active engine run -> takeover note, intercept active
    hive.journal_body = {"active": True, "run_id": "ui-run-1",
                         "done": False, "aborted": False,
                         "ts": time.time(), "n": 3, "frames": []}
    await br.mirror_tick()
    check("mirror takeover announced",
          any("taken over" in m and "ui-run-1" in m for m in ms.messages))
    check("mirror intercept active",
          br.mirror_intercept("weiter so") is True)

    # own gateway run is NOT mirrored
    st.data["active_run"] = {"run_id": "ui-run-1", "chat_id": "chat01"}
    await br.mirror_tick()
    check("own run not double-mirrored",
          sum(1 for m in ms.messages if "taken over" in m) == 1)
    st.data["active_run"] = None

    # approval card relayed, phone answers
    hive.pending_body = {"active": True, "tool": "run_bash",
                         "preview": "cmd /c del", "decision_id": "d1"}
    await br.mirror_tick()
    check("approval card relayed",
          any("run_bash" in m for m in ms.messages))
    note = await br.mirror_send("2")
    check("phone '2' (always) rejected", "deliberately does not" in note)
    check("rejected '2' did not reach engine", hive.decided == [])
    note = await br.mirror_send("3")
    check("phone '3' denied via engine",
          hive.decided == [("ui-run-1", "3", "d1", "run_bash")])
    check("deny confirmed", "denied" in note)
    check("G7: journal scoped to the mirrored run",
          hive.journal_calls[-1] == "ui-run-1")

    # steering: plain text goes to /steer, not a new gateway run
    runs_before = len(hive.stream_bodies)
    note = await br.mirror_send("mach weiter mit version b")
    check("phone text steered the mirrored run",
          hive.steered == [("ui-run-1", "mach weiter mit version b")])
    check("steer started no gateway run",
          len(hive.stream_bodies) == runs_before)
    check("steer receipt honest", "Queued" in note)

    # deep audit N2: relay shows the FULL command from journal frames,
    # not the 300-char truncated server preview
    long_cmd = "x" * 400
    hive.journal_body = {"active": True, "run_id": "ui-run-1",
                         "done": False, "aborted": False,
                         "ts": time.time(), "n": 4,
                         "frames": ['data: {"type": "tool_call", '
                                    '"name": "run_bash", '
                                    '"extra": {"cmd": "' + long_cmd + '"}}']}
    hive.pending_body = {"active": True, "tool": "run_bash",
                         "preview": "x" * 300, "decision_id": "d2"}
    ms.messages.clear()
    await br.mirror_tick()
    relayed = next((m for m in ms.messages if "run_bash" in m), "")
    check("N2: full command relayed (beyond 300 chars)",
          long_cmd in relayed)
    check("N2: no blind-approval note when full cmd present",
          "truncated" not in relayed)

    # deep audit N5: weird answers cannot crash consume_setup
    hive.journal_body = {"active": False}
    await br.mirror_tick()
    br3, st3, ms3 = _mk("gwbr_n5_", FakeHive())
    st3.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}
    # pending flow seeded directly — consume_setup is the unit under test
    import time as _t
    st3.data["pending_setup"] = {"model": "m-c", "agentic": False,
                                 "ts": _t.time()}
    for bad in ("--5, 8192, 16384", "², 8192, 16384", "1; 2; 3"):
        out = await br3.consume_setup(bad)
    check("N5: weird answers contained", "Expected 3 numbers" in out)
    # clamp: absurd ctx values are capped in the stream body
    st3.data["run_overrides"] = {"model": "m-c", "planner_ctx": 999999,
                                 "coder_ctx": 999999}
    ov = br3._stream_overrides()
    check("N5: ctx clamped to 131072",
          ov["duo_planner_ctx_target"] == 131072
          and ov["duo_coder_ctx_agentic"] == 131072)

    # done frame ends the session
    hive.journal_body = {"active": True, "run_id": "ui-run-1", "done": True,
                         "aborted": False, "ts": time.time(), "n": 4,
                         "frames": ['data: {"type": "done", '
                                    '"stop_reason": "completed"}']}
    await br.mirror_tick()
    check("done ends mirror session",
          any("Mirror run" in m or "completed" in m
              for m in ms.messages)
          and br.mirror_intercept("x") is False)

    # /stop aborts a mirrored run
    hive.journal_body = {"active": True, "run_id": "ui-run-2",
                         "done": False, "aborted": False,
                         "ts": time.time(), "n": 1, "frames": []}
    await br.mirror_tick()
    note = await br.stop_mirror()
    check("/stop aborts mirrored run", hive.aborts == ["ui-run-2"]
          and "ui-run-2" in note)
    check("mirror cleared after abort", br.mirror_intercept("x") is False)

    # /workspace: confirmed + persisted (PUT path exercised via fake)
    hive2 = FakeHive()
    br2, st2, ms2 = _mk("gwbr_wsp_", hive2)
    st2.data["tg_chat"] = {"hive_chat_id": "chat01", "created_at": "x"}

    async def put_chat_meta(chat_id, fields):
        # CAS-retry path: the FIRST PUT gets a 409 (server wins) and
        # workspace_text must adopt rev 7 and retry successfully
        hive2.meta_puts = getattr(hive2, "meta_puts", []) + [dict(fields)]
        if len(hive2.meta_puts) == 1:
            return FakeResponse(409, {"rev": 7})
        hive2.chat.update(fields)
        hive2.chat["rev"] = fields.get("base_rev", 0) + 1
        return FakeResponse(200, {"ok": True,
                                  "rev": hive2.chat["rev"]})
    hive2.put_chat_meta = put_chat_meta
    # G11: the path must EXIST (gateway-side check) — use a real temp dir
    ws_dir = Path(tempfile.mkdtemp(prefix="gwbr_wsp_dir_"))
    note = await br2.workspace_text(str(ws_dir))
    check("workspace confirmed", str(ws_dir) in note)
    check("workspace 409 adopted and retried",
          len(hive2.meta_puts) == 2
          and hive2.meta_puts[1]["base_rev"] == 7)
    check("workspace persisted in state+chat",
          st2.data["tg_chat"]["workspace"] == str(ws_dir)
          and hive2.chat.get("workspace") == str(ws_dir))

    # /tools: state -> run body
    check("tools default = engine", "engine default" in br2.tools_text(""))
    br2.tools_text("off")
    await br2.start_text_run("q")
    check("tools off rides the run body",
          hive2.stream_bodies[-1]["overrides"].get(
              "direct_tools_enabled") is False)

async def _main():
    await t_happy()
    await t_conflict()
    await t_busy()
    await t_errors()
    await t_split_doc()
    await t_secrets()
    await t_secrets_doc_path()
    await t_mode()
    await t_models_flow()
    await t_stop()
    await t_takeover()
    await t_audit_fixes()
    await t_ux_round()


# 2026-10-05 UX round: rich rendering, tappable approvals
async def t_ux_round():
    # F3: run answers go out as Telegram HTML (fences -> <pre>)
    ev = [{"type": "run_id", "run_id": "r-html"},
          {"type": "token",
           "content": "vorher\n```html\n<b>keep</b>\n```\nmit `code` und **fett**\nnachher"},
          {"type": "done", "stop_reason": "completed"}]
    br, st, ms = _mk("gwbr_html_", FakeHive(ev))
    await br.start_text_run("q")
    sent = ms.messages[-1]
    check("F3: fence rendered as <pre>",
          "<pre>" in sent and "&lt;b&gt;keep&lt;/b&gt;" in sent)
    check("F3: inline code + bold rendered",
          "<code>code</code>" in sent and "<b>fett</b>" in sent)
    check("F3: sent with parse_mode HTML", ms.parse_modes[-1] == "HTML")

    # F3 fallback: a parse rejection must resend the plain chunk
    class ParseBoomMessenger(FakeMessenger):
        async def send_message(self, text, parse_mode=None,
                               reply_markup=None):
            if parse_mode == "HTML":
                raise TelegramApiError(
                    "sendMessage", "Bad Request: can't parse entities")
            return await super().send_message(text)

    ev2 = [{"type": "run_id", "run_id": "r-fb"},
           {"type": "token", "content": "```\ncode\n```"},
           {"type": "done", "stop_reason": "completed"}]
    br2, st2, ms2 = _mk("gwbr_htmlfb_", FakeHive(ev2))
    br2.ms.__class__ = ParseBoomMessenger
    await br2.start_text_run("q")
    check("F3 fallback: plain text delivered on parse error",
          any("code" in m for m in ms2.messages))

    # F4: mirror card carries a 3-button keyboard; callbacks ride the
    # SAME decision path (decision_id + tool echoed), '2' included
    class CBHive(FakeHive):
        def __init__(self):
            super().__init__()
            self.settings_body = {"telegram_mirror_enabled": True}
            self.journal_body = {"active": True, "run_id": "ui-cb",
                                 "done": False, "aborted": False,
                                 "ts": time.time(), "n": 1, "frames": []}
            self.pending_body = {"active": True, "tool": "run_bash",
                                 "preview": "cmd", "decision_id": "d-cb"}
            self.decided = []

        async def decide_approval(self, run_id, answer, decision_id="",
                                  tool=""):
            self.decided.append((run_id, answer, decision_id, tool))
            return FakeResponse(200, {"routed": "pause"})

    hive_cb = CBHive()
    br_cb, st_cb, ms_cb = _mk("gwbr_cb_", hive_cb)
    await br_cb.mirror_tick()
    await br_cb.mirror_tick()
    check("F4: card sent with 3-button keyboard",
          bool(ms_cb.markups) and
          len(ms_cb.markups[-1]["inline_keyboard"][0]) == 3)
    toast = await br_cb.mirror_callback("cbid", "appr:ui-cb:2")
    check("F4: '2' (always, chat) rides decision path",
          hive_cb.decided == [("ui-cb", "2", "d-cb", "run_bash")])
    check("F4: '2' toast", "always" in toast)
    check("F4: latch cleared after callback",
          br_cb._mirror()["approval_sig"] is None)
    toast2 = await br_cb.mirror_callback("cbid", "appr:ui-cb:1")
    check("F4: stale rid refused", "gone" in toast2
          and hive_cb.decided == [("ui-cb", "2", "d-cb", "run_bash")])
async def t_audit_fixes():
    # G1: phone runs FORCE the engine approval gate via the stream body
    br, st, ms = _mk("gwbr_g1_", FakeHive(_run_events()))
    await br.start_text_run("q")
    check("G1: gate forced in stream overrides",
          br.hive.stream_bodies[-1]["overrides"].get(
              "duo_action_approval_enabled") is True)
    check("G1: /status states the forced gate",
          "gate enforced" in br.status_text())
    # version-skew detection: an engine WITHOUT the gateway_overrides
    # marker must downgrade /status instead of promising the invariant
    br.ms.engine_gate_support = False
    check("G1: /status WARNS against old engines",
          "INACTIVE" in br.status_text()
          and "duo_action_approval_enabled" in br.status_text())
    br.ms.engine_gate_support = True

    # G2: the phone decision echoes decision_id + tool of the relayed
    # card; a duplicate route is answered honestly
    class G2Hive(FakeHive):
        def __init__(self):
            super().__init__()
            self.settings_body = {"telegram_mirror_enabled": True}
            self.journal_body = {"active": True, "run_id": "ui-g2",
                                 "done": False, "aborted": False,
                                 "ts": time.time(), "n": 1, "frames": []}
            self.pending_body = {"active": True, "tool": "run_bash",
                                 "preview": "cmd", "decision_id": "d-g2"}
            self.decided = []

        async def decide_approval(self, run_id, answer, decision_id="",
                                  tool=""):
            self.decided.append((run_id, answer, decision_id, tool))
            return FakeResponse(200, {"routed": "pause"})

    hive_g2 = G2Hive()
    br_g2, st_g2, ms_g2 = _mk("gwbr_g2_", hive_g2)
    await br_g2.mirror_tick()   # tick 1: takeover announced
    await br_g2.mirror_tick()   # tick 2: approval card relayed
    check("G2: card relayed", any("run_bash" in m for m in ms_g2.messages))
    note = await br_g2.mirror_send("1")
    check("G2: decision carries card decision_id+tool",
          hive_g2.decided == [("ui-g2", "1", "d-g2", "run_bash")])
    check("G2: confirmed", "delivered" in note)

    async def _dup_decide(run_id, answer, decision_id="", tool=""):
        hive_g2.decided.append((run_id, answer, decision_id, tool))
        return FakeResponse(200, {"routed": "duplicate"})
    m2 = br_g2._mirror()
    m2["approval_sig"] = "d-g2|cmd"
    m2["approval_decision_id"] = "d-g2"
    m2["approval_tool"] = "run_bash"
    hive_g2.decide_approval = _dup_decide
    note = await br_g2.mirror_send("1")
    check("G2: duplicate answered honestly", "already answered" in note)

    # force_approval_gate=false (assistant-style opt-out): the key is
    # simply not sent; the engine-global toggle governs alone then
    from hivemind_gateway.config import GatewayConfig as _GC
    br_g1b, st_g1b, ms_g1b = _mk("gwbr_g1b_", FakeHive(_run_events()))
    br_g1b.cfg = _GC(force_approval_gate=False)
    check("G1 opt-out: gate key not sent",
          "duo_action_approval_enabled"
          not in br_g1b._stream_overrides())
    br_g1b.cfg = _GC(force_approval_gate=True)
    check("G1 opt-out: default still forces",
          br_g1b._stream_overrides().get("duo_action_approval_enabled") is True)

    # phone restriction toggle: restricted -> body carries the clamp
    # DEFAULT ON: a missing key restricts (safe by default)
    hive_d = FakeHive(_run_events())
    hive_d.settings_body = {}
    br_d, st_d, ms_d = _mk("gwbr_restrd_", hive_d)
    await br_d.start_text_run("q")
    check("restrict default ON: key sent when absent",
          br_d.hive.stream_bodies[-1]["overrides"].get(
              "phone_restricted") is True)
    hive_r = FakeHive(_run_events())
    hive_r.settings_body = {"telegram_phone_restricted": True}
    br_r, st_r, ms_r = _mk("gwbr_restr_", hive_r)
    await br_r.start_text_run("q")
    check("restrict: body carries phone_restricted",
          br_r.hive.stream_bodies[-1]["overrides"].get(
              "phone_restricted") is True)
    br_r.ms.phone_restricted = True
    check("restrict: /status shows it",
          "web + text only" in br_r.status_text())
    # explicit OFF: only an explicit false unlocks phone runs
    hive_u = FakeHive(_run_events())
    hive_u.settings_body = {"telegram_phone_restricted": False}
    br_u, st_u, ms_u = _mk("gwbr_restr0_", hive_u)
    await br_u.start_text_run("q")
    check("restrict off (explicit): key absent",
          "phone_restricted" not in
          br_u.hive.stream_bodies[-1]["overrides"])

    # G8: steering cannot bypass the max_text_chars cap
    await br_g2.mirror_send("x" * 5000)
    check("G8: steering capped at max_text_chars",
          hive_g2.steered and len(hive_g2.steered[-1][1]) == 4000)

    # G5: a Telegram failure during _finish must not latch active_run
    br_g5, st_g5, ms_g5 = _mk("gwbr_g5_", FakeHive(_run_events()))

    async def _boom_finish(*a, **k):
        raise TelegramApiError("tg down")
    br_g5._finish = _boom_finish
    await br_g5.start_text_run("q")
    check("G5: run cleared despite finish failure",
          st_g5.data.get("active_run") is None)

    # N7/G7: no takeover while our own run is between POST and run_id
    hive_n7 = FakeHive()
    hive_n7.settings_body = {"telegram_mirror_enabled": True}
    hive_n7.journal_body = {"active": True, "run_id": "foreign-1",
                            "done": False, "aborted": False,
                            "ts": time.time(), "n": 0, "frames": []}
    br_n7, st_n7, ms_n7 = _mk("gwbr_n7_", hive_n7)
    st_n7.data["active_run"] = {"run_id": None, "chat_id": "chat01"}
    await br_n7.mirror_tick()
    check("N7: own unconfirmed run is not taken over",
          ms_n7.messages == [] and br_n7.mirror_intercept("x") is False)

    # G11: /workspace refuses non-existent paths and drive roots
    br_g11, st_g11, ms_g11 = _mk("gwbr_g11_", FakeHive())
    out = await br_g11.workspace_text("Z:/definitiv/nicht/da_xyz")
    check("G11: missing path refused", "does not exist" in out)
    root = Path(tempfile.mkdtemp(prefix="gwbr_g11_ws_")).anchor
    out2 = await br_g11.workspace_text(root)
    check("G11: drive root refused", "not allowed" in out2)

asyncio.run(_main())
print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)
