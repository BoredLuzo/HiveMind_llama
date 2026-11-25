# -*- coding: utf-8 -*-
"""Regression checks for Telegram gateway thinking visibility, /effort, and chat continuity (/chat)."""
import asyncio
import datetime as dt
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hivemind_gateway import commands as gw_commands  # noqa: E402
from hivemind_gateway.auth import ParsedUpdate  # noqa: E402
from hivemind_gateway.bridge import RunBridge  # noqa: E402
from hivemind_gateway.config import GatewayConfig  # noqa: E402
from hivemind_gateway.state import GatewayState  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


class _FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class _FakeMs:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, text: str, **_kw):
        self.messages.append(text)
        return {"message_id": len(self.messages)}

    async def edit_message(self, _mid: int, _text: str):
        return None

    async def send_document(self, _data: bytes, _filename: str):
        return None

    async def send_voice(self, _ogg: bytes, _dur: int = 0):
        return None


class _FakeHive:
    def __init__(self, *, events=None, chats=None, settings=None, journals=None):
        self.events = list(events or [])
        self.chats = list(chats or [])
        self.settings_body = dict(settings or {"telegram_phone_restricted": False, "telegram_approval_mode": "off"})
        self.journals = list(journals or [])
        self.stream_calls: list[dict] = []
        self.turns: list[tuple[str, list]] = []
        self._created_seq = 0

    async def settings(self):
        return dict(self.settings_body)

    async def get_chats(self):
        return list(self.chats)

    async def get_chat(self, cid: str):
        for ch in self.chats:
            if str(ch.get("id")) == str(cid):
                return _FakeResp(200, {"id": cid, "messages": [], "rev": 1})
        if any(str(c) == cid for c in (f"tg-{i}" for i in range(1, 5))):
            return _FakeResp(200, {"id": cid, "messages": [], "rev": 1})
        return _FakeResp(404, {"error": "not found"})

    async def create_chat(self, title: str, workspace: str | None = None):
        self._created_seq += 1
        cid = f"tg-{self._created_seq}"
        now = dt.datetime.now().isoformat(timespec="seconds")
        rec = {"id": cid, "title": title, "created_at": now, "updated_at": now, "msg_count": 0}
        self.chats.insert(0, rec)
        return {"id": cid, "chat": rec}

    async def put_chat_messages(self, cid: str, messages: list, _base_rev: int):
        self.turns.append((cid, list(messages)))
        return _FakeResp(200, {"ok": True, "rev": len(messages) + 1})

    async def placeholder_status(self):
        return {}

    async def journal(self, _rid: str = "", after: int = 0):
        if int(after or 0) >= (1 << 20):
            return {"active": True, "run_id": _rid or "ui-run-1", "done": False, "aborted": False, "frames": []}
        if self.journals:
            return self.journals.pop(0)
        return {"active": False, "done": True}

    async def pending_approval(self, _rid: str):
        return _FakeResp(200, {"active": False})

    async def stream(self, q: str, chat_id: str, **kw):
        self.stream_calls.append({"q": q, "chat_id": chat_id, **kw})
        for ev in self.events:
            yield ev


def _mk_bridge(hive, *, verbose=False, cfg_kw=None):
    tmp = Path(tempfile.mkdtemp(prefix="gwthink_"))
    st = GatewayState(tmp / "state.json")
    st.data["verbose"] = verbose
    cfg = GatewayConfig(**(cfg_kw or {"force_approval_gate": False}))
    ms = _FakeMs()
    br = RunBridge(hive, st, cfg, ms)
    return br, st, ms


# ── 1. Direct own-run: thinking_token flushes before answer token ─────────
hive_dir = _FakeHive(events=[
    {"type": "run_id", "run_id": "r-dir-1"},
    {"type": "thinking_token", "content": "Let me verify the secret sk-proj1234567890abcdef and calculate."},
    {"type": "token", "content": "42 is the answer."},
    {"type": "done", "stop_reason": "completed"},
])
br_dir, _, ms_dir = _mk_bridge(hive_dir)
asyncio.run(br_dir.start_text_run("What is the answer?"))
_reason_msgs = [m for m in ms_dir.messages if "💭 Reasoning:" in m]
check(
    "Direct own-run flushes 💭 Reasoning before final answer and scrubs secrets",
    len(_reason_msgs) == 1
    and "sk-proj" not in _reason_msgs[0]
    and "***" in _reason_msgs[0]
    and ms_dir.messages.index(_reason_msgs[0]) < ms_dir.messages.index("42 is the answer."),
    repr(ms_dir.messages),
)

# Disabled progress messages must suppress 💭 Reasoning
hive_quiet = _FakeHive(
    events=[
        {"type": "run_id", "run_id": "r-dir-q"},
        {"type": "thinking_token", "content": "internal trace"},
        {"type": "token", "content": "Final output"},
        {"type": "done", "stop_reason": "completed"},
    ],
    settings={"telegram_phone_restricted": False, "telegram_approval_mode": "off", "telegram_progress_msgs": False},
)
br_q, _, ms_q = _mk_bridge(hive_quiet)
asyncio.run(br_q.start_text_run("hi"))
check(
    "telegram_progress_msgs=false suppresses 💭 messages while delivering answer",
    not any("💭" in m for m in ms_q.messages) and "Final output" in ms_q.messages,
)

# ── 2. Agentic own-run: planner_thinking + mid-tool + trailing thinking ──
hive_ag = _FakeHive(events=[
    {"type": "run_id", "run_id": "r-ag-1"},
    {"type": "planner_thinking_token", "content": "Plan step reasoning"},
    {"type": "planner_result", "content": "1. Read app.py"},
    {"type": "thinking_token", "content": "Need to inspect file first"},
    {"type": "tool_call", "name": "read_file", "label": "app.py"},
    {"type": "thinking_token", "content": "Now summarize findings"},
    {"type": "token", "content": "Done inspecting."},
    {"type": "done", "stop_reason": "completed"},
])
br_ag, _, ms_ag = _mk_bridge(hive_ag)
asyncio.run(br_ag.start_text_run("check app"))
_plan_thk_i = next((i for i, m in enumerate(ms_ag.messages) if "💭 Planning: Plan step reasoning" in m), -1)
_plan_res_i = next((i for i, m in enumerate(ms_ag.messages) if "🧠 Plan: 1. Read app.py" in m), -1)
_tool_thk_i = next((i for i, m in enumerate(ms_ag.messages) if "💭 Reasoning: Need to inspect file first" in m), -1)
_final_thk_i = next((i for i, m in enumerate(ms_ag.messages) if "💭 Reasoning: Now summarize findings" in m), -1)
check(
    "Agentic own-run flushes Planning before Plan, tool Reasoning, and final Reasoning before answer",
    0 <= _plan_thk_i < _plan_res_i and _tool_thk_i > _plan_res_i and _final_thk_i > _tool_thk_i
    and _final_thk_i < ms_ag.messages.index("Done inspecting."),
    repr(ms_ag.messages),
)

# ── 3. Mirror run: buffers thinking across multiple ticks & flushes ───────
def _sse(d: dict) -> str:
    return "data: " + json.dumps(d)


hive_mir = _FakeHive(
    settings={"telegram_mirror_enabled": True, "telegram_phone_restricted": False},
    journals=[
        # Tick 1 (fresh adoption: 0 frames at creation, cursor=0)
        {"active": True, "run_id": "ui-run-1", "done": False, "aborted": False, "ts": time.time() + 10, "n": 0, "base": 0, "frames": []},
        # Tick 2: planner thinking arrives (not flushed yet)
        {"active": True, "run_id": "ui-run-1", "done": False, "aborted": False, "ts": time.time() + 10, "n": 1, "base": 0,
         "frames": [_sse({"type": "planner_thinking_token", "content": "Chunk A "})]},
        # Tick 3: planner_result arrives -> flushes Tick 2+3 planner_think, then direct/coder thinking arrives
        {"active": True, "run_id": "ui-run-1", "done": False, "aborted": False, "ts": time.time() + 10, "n": 4, "base": 1,
         "frames": [
             _sse({"type": "planner_thinking_token", "content": "Chunk B"}),
             _sse({"type": "planner_result", "content": "Step 1"}),
             _sse({"type": "thinking_token", "content": "Cross-tick thought 1 "}),
         ]},
        # Tick 4: second half of thought + token + done -> flushes Reasoning before fazit
        {"active": True, "run_id": "ui-run-1", "done": False, "aborted": False, "ts": time.time() + 10, "n": 7, "base": 4,
         "frames": [
             _sse({"type": "thinking_token", "content": "Cross-tick thought 2"}),
             _sse({"type": "token", "content": "Mirror answer"}),
             _sse({"type": "done", "stop_reason": "completed"}),
         ]},
    ],
)
br_mir, _, ms_mir = _mk_bridge(hive_mir)
br_mir._proc_start = 0.0
for _ in range(5):
    asyncio.run(br_mir.mirror_tick())
check(
    "Mirror run buffers planner_thinking_token & thinking_token across ticks without losing them",
    any("💭 Planning: Chunk A Chunk B" in m for m in ms_mir.messages)
    and any("💭 Reasoning: Cross-tick thought 1 Cross-tick thought 2" in m for m in ms_mir.messages)
    and any("Mirror answer" in m for m in ms_mir.messages),
    repr(ms_mir.messages),
)

# ── 4. /effort command + _stream_overrides + ℹ️ Run & /status visibility ─
eff_set = asyncio.run(br_dir.effort_text("high"))
eff_plan = asyncio.run(br_dir.effort_text("planner low"))
ov_out = br_dir._stream_overrides()
st_txt = asyncio.run(br_dir.status_text())
asyncio.run(br_dir.start_text_run("with effort"))
_info_last = [m for m in ms_dir.messages if m.startswith("ℹ️ Run")][-1]
check(
    "/effort sets reasoning_effort + planner_reasoning_effort, overrides propagate, and status/ℹ️ Run show effort",
    "high" in eff_set and "low" in eff_plan
    and ov_out.get("reasoning_effort") == "high"
    and ov_out.get("planner_reasoning_effort") == "low"
    and "effort: high · planner: low" in st_txt
    and "effort: high · planner: low" in _info_last,
)
eff_clr = asyncio.run(br_dir.effort_text("auto"))
check("/effort auto clears phone override", "auto" in eff_clr and "reasoning_effort" not in br_dir._stream_overrides())

# ── 5. Chat continuity: last_active_ts protects [TG] chat + /chat pinning ─
now_dt = dt.datetime.now()
ui_older = {
    "id": "ui-chat-1",
    "title": "Browser Coding Session",
    "updated_at": (now_dt - dt.timedelta(minutes=5)).isoformat(timespec="seconds"),
    "created_at": (now_dt - dt.timedelta(minutes=10)).isoformat(timespec="seconds"),
    "msg_count": 4,
}
tg_prev = {
    "id": "tg-1",
    "title": "[TG] Telegram Session",
    "updated_at": (now_dt - dt.timedelta(minutes=1)).isoformat(timespec="seconds"),
    "created_at": (now_dt - dt.timedelta(minutes=20)).isoformat(timespec="seconds"),
    "msg_count": 2,
}
hive_cont = _FakeHive(chats=[tg_prev, ui_older])
br_cont, st_cont, ms_cont = _mk_bridge(hive_cont)
# User has an active [TG] chat whose last_active_ts is MORE recent (1 min ago) than ui_older (5 min ago),
# even though new_chat_ts was 20 min ago!
st_cont.data["tg_chat"] = {
    "hive_chat_id": "tg-1",
    "title": "[TG] Telegram Session",
    "created_at": (now_dt - dt.timedelta(minutes=20)).isoformat(timespec="seconds"),
    "new_chat_ts": time.time() - 1200,
    "last_active_ts": time.time() - 60,
}
st_cont.save()
_ens_cont = asyncio.run(br_cont._ensure_chat())
check(
    "last_active_ts keeps Telegram in active [TG] chat when browser chat is older",
    _ens_cont.get("hive_chat_id") == "tg-1" and not _ens_cont.get("followed"),
    repr(_ens_cont),
)

# /chat lists chats & /chat 2 pins ui-chat-1 even when a NEWER UI chat appears
chat_list_txt = asyncio.run(br_cont.chat_text(""))
pin_txt = asyncio.run(br_cont.chat_text("2"))
ui_newer = {
    "id": "ui-chat-2",
    "title": "Newer Browser Tab",
    "updated_at": now_dt.isoformat(timespec="seconds"),
    "created_at": now_dt.isoformat(timespec="seconds"),
    "msg_count": 2,
}
hive_cont.chats.insert(0, ui_newer)
_ens_pinned = asyncio.run(br_cont._ensure_chat())
asyncio.run(br_cont.start_text_run("pinned turn"))
_pin_info = [m for m in ms_cont.messages if m.startswith("ℹ️ Run")][-1]
_pin_status = asyncio.run(br_cont.status_text())
check(
    "/chat lists & /chat <n> pins chat against newer browser chats and labels ℹ️ Run + /status",
    "Recent chats:" in chat_list_txt
    and "Pinned chat: Browser Coding Session (ui-chat-1)" in pin_txt
    and _ens_pinned.get("hive_chat_id") == "ui-chat-1"
    and _ens_pinned.get("pinned") is True
    and "chat: Browser Coding Session (ui-chat-1) [pinned]" in _pin_info
    and "chat: Browser Coding Session (ui-chat-1) [pinned]" in _pin_status,
)

unpin_txt = asyncio.run(br_cont.chat_text("auto"))
_ens_after_unpin = asyncio.run(br_cont._ensure_chat())
check(
    "/chat auto releases pin and resumes follow-UI",
    "unpinned" in unpin_txt and _ens_after_unpin.get("hive_chat_id") == "ui-chat-2" and _ens_after_unpin.get("followed"),
    repr(_ens_after_unpin),
)

# ── 6. Gateway command whitelist & main.py dispatch check ─────────────────
main_src = (ROOT / "hivemind_gateway" / "main.py").read_text(encoding="utf-8")
check(
    "/chat and /effort are wired in COMMAND_SPEC and hivemind_gateway/main.py",
    "chat" in gw_commands.COMMAND_SPEC
    and "effort" in gw_commands.COMMAND_SPEC
    and 'elif name == "chat":' in main_src
    and 'elif name == "effort":' in main_src,
)

# Unused import check suppression cleanly via ParsedUpdate smoke
_pu = ParsedUpdate(
    update_id=1, kind="message", from_id="1", chat_id="1",
    chat_type="private", text="/chat", date=0, is_forwarded=False,
    message_id=1, callback_id=None,
)
check("parse command works for /chat", gw_commands.command_from_message(_pu) == ("chat", ""))

sys.exit(h.finish("gateway_thinking_and_chat_continuity"))
