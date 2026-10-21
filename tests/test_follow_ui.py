"""Telegram folgt dem Frontend: Modus-Default und Chat-Fortsetzung.

Deckt die zwei Owner-Wuensche vom 21.10. ab:
  1. Der Frontend-Modus ist der Default; ein /mode-Override verfaellt, sobald
     die UI den Modus aendert.
  2. Ein Telefon-Text setzt den zuletzt benutzten UI-Chat fort (follow_ui),
     solange er frisch ist - so laesst sich ein im Browser gestarteter
     Agentic-Run mit "go" weiterfuehren.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
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


class _Hive:
    def __init__(self, chats=None):
        self.chats = chats or []
        self.calls = 0

    async def get_chats(self):
        self.calls += 1
        return list(self.chats)


def _bridge(chats=None, mode="", engine_mode="code_duo"):
    tmp = Path(tempfile.mkdtemp(prefix="gwfollow_"))
    st = GatewayState(tmp / "state.json")
    st.data["tg_chat"] = {"hive_chat_id": "tg-chat", "created_at": "x"}
    if mode:
        st.data["mode"] = mode
    br = RunBridge(_Hive(chats), st, GatewayConfig(), None)
    return br, st


def _chat(cid, title, minutes_ago, msgs=1):
    when = dt.datetime.now() - dt.timedelta(minutes=minutes_ago)
    return {"id": cid, "title": title,
            "updated_at": when.isoformat(timespec="seconds"),
            "created_at": when.isoformat(timespec="seconds"),
            "msg_count": msgs}


# ── 1. follow_ui: Standard an, schaltbar ────────────────────────────────
br, st = _bridge()
check("follow_ui defaults to on", br.follow_ui_enabled() is True)
br.set_follow_ui(False)
check("follow_ui can be switched off", br.follow_ui_enabled() is False)
check("the switch is persisted", st.data.get("follow_ui") is False)
br.set_follow_ui(True)

# ── 2. Ziel-Chat: der frischeste UI-Chat, [TG] ignoriert ────────────────
br, _ = _bridge([_chat("old", "Older question", 90),
                 _chat("new", "Agentic run", 5),
                 _chat("tg", "[TG] Telegram 2026-10-21 12:00", 1),
                 _chat("empty", "(no text)", 1, msgs=0)])
target = asyncio.run(br._ui_chat_target())
check("newest non-TG chat wins", target == "new", f"({target})")

br, _ = _bridge([_chat("stale", "alt", 120)])
check("a stale chat is ignored", asyncio.run(br._ui_chat_target()) == "")

br, _ = _bridge([])
check("no chats yields empty", asyncio.run(br._ui_chat_target()) == "")

br, _ = _bridge([_chat("new", "Agentic run", 5)])
br.set_follow_ui(False)
check("follow off disables the lookup", asyncio.run(br._ui_chat_target()) == "")

# ── 3. _ensure_chat nutzt das Ziel, sonst den [TG]-Chat ────────────────
br, st = _bridge([_chat("ui-chat", "Agentic run", 3)])
mapping = asyncio.run(br._ensure_chat())
check("followed chat is used for the run", mapping.get("hive_chat_id") == "ui-chat",
      f"({mapping})")
check("followed chat is marked", mapping.get("followed") is True)
check("tg_chat stays untouched",
      (st.data.get("tg_chat") or {}).get("hive_chat_id") == "tg-chat")

br, _ = _bridge([])
mapping = asyncio.run(br._ensure_chat())
check("without a UI chat the [TG] chat stays", mapping.get("hive_chat_id") == "tg-chat",
      f"({mapping})")

# ── 4. Modus: Frontend ist der Default ──────────────────────────────────
br, st = _bridge(engine_mode="code_duo")
mode, src = br.effective_phone_mode({"mode": "code_duo"})
check("without an override the frontend mode is used",
      (mode, src) == ("code_duo", "frontend"), f"({mode}, {src})")

br, st = _bridge(mode="simple")
st.data["mode_engine_seen"] = "code_duo"
mode, src = br.effective_phone_mode({"mode": "code_duo"})
check("an override survives while the engine mode is unchanged",
      (mode, src) == ("simple", "phone"), f"({mode}, {src})")

br, st = _bridge(mode="simple")
st.data["mode_engine_seen"] = "code_duo"
mode, src = br.effective_phone_mode({"mode": "auto"})
check("a UI mode change wins over the phone override",
      (mode, src) == ("auto", "frontend"), f"({mode}, {src})")
check("the stale override is cleared", (st.data.get("mode") or "") == "")

br, st = _bridge(engine_mode="")
mode, src = br.effective_phone_mode({})
check("empty settings fall back to auto", mode == "auto", f"({mode})")

# ── 5. Quelltext-Waechter ───────────────────────────────────────────────
SRC = (ROOT / "hivemind_gateway" / "bridge.py").read_text(encoding="utf-8", errors="replace")
MAIN = (ROOT / "hivemind_gateway" / "main.py").read_text(encoding="utf-8", errors="replace")
CMDS = (ROOT / "hivemind_gateway" / "commands.py").read_text(encoding="utf-8", errors="replace")
check("run path uses the resolved mode", "_eff_mode, _mode_src = self.effective_phone_mode(_s)" in SRC)
check("stream mode only set for a phone override", 'sel_mode = _eff_mode if _mode_src == "phone" else ""' in SRC)
check("/follow exists in the dispatch", 'elif name == "follow":' in MAIN)
check("/follow is documented", '"/follow"' in CMDS or "'/follow'" in CMDS or "/follow <on|off>" in CMDS)
check("/mode baseline is refreshed per run", 'self.state.data["mode_engine_seen"] = str(' in SRC)

print()
print(f"SUMMARY follow_ui: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
