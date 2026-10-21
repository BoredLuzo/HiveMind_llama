"""Command-Flow: Vollstaendigkeit von /help, Tippen statt Tippen, Ask-back.

Deckt die drei Umbauten vom 2026-10-21 ab:
  1. /help nennt JEDEN Befehl aus COMMAND_SPEC (keine stille Luecke mehr).
  2. Ein Tipp auf einen Befehl (callback_data `cmd:<name>`) laeuft als Befehl -
     niemals als Prompt/Run.
  3. Befehl ohne Pflichtwert fragt nach; die naechste Nachricht ist der Wert,
     "stop" bricht ab.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hivemind_gateway import commands as gw_commands  # noqa: E402
from hivemind_gateway import main as gw_main  # noqa: E402
from hivemind_gateway.auth import ParsedUpdate  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. /help deckt alle Befehle ab ──────────────────────────────────────
missing = [n for n, s in gw_commands.COMMAND_SPEC.items()
           if s["usage"] not in gw_commands.HELP_TEXT]
check("every command appears in /help", not missing, f"(missing: {missing})")
check("usage strings start with the command name",
      all(s["usage"].lower().startswith(f"/{n.lower()}")
          for n, s in gw_commands.COMMAND_SPEC.items()),
      f"({[(n, s['usage']) for n, s in gw_commands.COMMAND_SPEC.items() if not s['usage'].lower().startswith('/' + n.lower())]})")
check("every command has a description",
      all(s.get("desc") for s in gw_commands.COMMAND_SPEC.values()))
check("whitelist is derived from the spec",
      set(gw_commands.COMMAND_WHITELIST) == set(gw_commands.COMMAND_SPEC))

kb = gw_commands.help_keyboard()
flat = [b for row in kb["inline_keyboard"] for b in row]
check("keyboard has a button per command", len(flat) == len(gw_commands.COMMAND_SPEC),
      f"({len(flat)} vs {len(gw_commands.COMMAND_SPEC)})")
check("callback data stays within Telegram's limit",
      all(len(b["callback_data"].encode()) <= 64 for b in flat),
      f"(max {max(len(b['callback_data']) for b in flat)})")
check("callback data addresses the command",
      {b["callback_data"] for b in flat} == {f"cmd:{n}" for n in gw_commands.COMMAND_SPEC})
check("buttons are labelled with the command",
      all(b["text"].startswith("/") for b in flat))

# ── 2. Ask-back-Regeln ──────────────────────────────────────────────────
for name in ("setmodel", "ctx", "preset", "planner", "workspace", "pair"):
    check(f"{name} asks for its value", gw_commands.wants_arg_first(name))
for name in ("stop", "status", "mode", "gate", "help", "purge"):
    check(f"{name} runs immediately", gw_commands.wants_arg_first(name) is False)
check("cancel words", all(gw_commands.is_cancel(w) for w in ("stop", "cancel", "abbrechen")))
check("a value is not a cancel", gw_commands.is_cancel("5") is False)
check("hint names the command", "/setModel" in gw_commands.param_hint("setmodel"))

# ── 3. Tipp auf einen Befehl startet keinen Run ─────────────────────────
class _Api:
    def __init__(self):
        self.toasts = []

    async def answer_callback_query(self, cid, text=""):
        self.toasts.append((cid, text))
        return {"ok": True}


class _Rec:
    """Records every reply the gateway makes."""

    def __init__(self):
        self.replies = []


def _make_gw():
    tmp = Path(tempfile.mkdtemp(prefix="gwcmd_"))
    from hivemind_gateway.bridge import RunBridge
    from hivemind_gateway.config import GatewayConfig
    from hivemind_gateway.state import GatewayState

    class _Hive:
        async def settings(self):
            return {}

    st = GatewayState(tmp / "state.json")
    gw = gw_main.Gateway.__new__(gw_main.Gateway)   # ohne Netzwerk-Init
    gw.owner_id = "1"
    gw.state = st
    gw.cfg = GatewayConfig()
    gw.api = _Api()
    gw.bridge = RunBridge(_Hive(), st, GatewayConfig(), None)
    rec = _Rec()

    async def _reply(chat_id, text, reply_to_message_id=None, reply_markup=None):
        rec.replies.append({"text": text, "markup": reply_markup,
                            "reply_to": reply_to_message_id})

    gw.reply = _reply
    gw._pending_cmd = None
    return gw, rec


def _update(text, kind="callback_query", chat="1"):
    return ParsedUpdate(update_id=1, kind=kind, from_id="1", chat_id=chat,
                        chat_type="private", text=text, date=0,
                        is_forwarded=False, message_id=7,
                        callback_id="cb1" if kind == "callback_query" else None)


async def _tap(name):
    gw, rec = _make_gw()
    await gw.handle_command_callback(_update(f"cmd:{name}"))
    return gw, rec


# Ein Tipp auf /stop darf keinen Run erzeugen: die Bridge wird nicht beruehrt.
gw, rec = asyncio.run(_tap("stop"))
check("tapping /stop does not start a run",
      not getattr(gw.bridge, "_run_lock", None).locked()
      and all("Run" not in r["text"] for r in rec.replies),
      f"({[r['text'][:30] for r in rec.replies]})")
check("tap is acknowledged with a toast", gw.api.toasts and gw.api.toasts[0][1] == "/stop",
      f"({gw.api.toasts})")

# Ein Tipp auf einen Befehl mit Pflichtwert fragt nach und merkt sich das.
gw, rec = asyncio.run(_tap("setmodel"))
check("tapping /setModel asks for the value",
      any("/setModel" in r["text"] and "next message" in r["text"] for r in rec.replies),
      f"({[r['text'][:40] for r in rec.replies]})")
check("ask-back carries a cancel button",
      any(r["markup"] and r["markup"]["inline_keyboard"][0][0]["callback_data"] == "cmdcancel"
          for r in rec.replies))
check("pending command remembered", (gw._pending_cmd or {}).get("name") == "setmodel",
      f"({gw._pending_cmd})")

# Ein Tipp auf /help zeigt die Liste mit Tastatur.
gw, rec = asyncio.run(_tap("help"))
check("tapping /help returns the list + keyboard",
      any(r["markup"] and "inline_keyboard" in r["markup"] for r in rec.replies))

# Ein unbekannter Callback wird verworfen (kein Run, kein Reply).
gw, rec = asyncio.run(_tap("doesnotexist"))
check("unknown callback is ignored", not rec.replies)


# ── 4. Wert als naechste Nachricht / "stop" bricht ab ────────────────────
def _typed(text, kind="message"):
    return ParsedUpdate(update_id=2, kind=kind, from_id="1", chat_id="1",
                        chat_type="private", text=text, date=0,
                        is_forwarded=False, message_id=8, callback_id=None)


async def _pending_flow(answer):
    gw, rec = _make_gw()
    gw._pending_cmd = {"name": "ctx", "ts": __import__("time").time()}
    handled = await gw._run_pending_as_arg(_typed(answer))
    return gw, rec, handled


gw, rec, handled = asyncio.run(_pending_flow("4096"))
check("a plain value is consumed as the parameter", handled is True)
check("pending state cleared after use", gw._pending_cmd is None)

gw, rec, handled = asyncio.run(_pending_flow("stop"))
check("'stop' cancels the pending command", handled is True and gw._pending_cmd is None)
check("cancel is acknowledged", any("Cancelled" in r["text"] for r in rec.replies),
      f"({[r['text'][:30] for r in rec.replies]})")

gw, rec, handled = asyncio.run(_pending_flow("/status"))
check("a new command is not swallowed as a value", handled is False)

gw, rec, handled = asyncio.run(_pending_flow(""))
check("empty message is not swallowed", handled is False)

# abgelaufener Pending-Zustand
gw, rec = _make_gw()
gw._pending_cmd = {"name": "ctx", "ts": 1.0}          # uralt
check("stale pending state expires", gw._pending_get() is None)

# ohne Pending-Zustand wird normales Text-Running nicht angefasst
gw, rec = _make_gw()
check("no pending -> no interception", asyncio.run(gw._run_pending_as_arg(_typed("hallo"))) is False)

print()
print(f"SUMMARY command_flow: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
