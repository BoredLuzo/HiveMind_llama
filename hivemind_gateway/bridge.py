"""WP2 - the text-run bridge: Telegram message -> HiveMind run -> phone.

Flow per run (contract: docs/gateway_contract.md, Transcript section):
  1. busy checks (own state + server journal heuristic; the server-side
     409 guard is the WP3 proposal)
  2. ensure the [TG] HiveMind chat exists (one per Telegram chat, mapping
     persisted in state)
  3. PUT the user turn into the main json (base_rev; 409 = server wins:
     adopt returned messages, re-append, retry once)
  4. POST /stream, consume SSE: collect tokens, throttle status edits,
     auto-deny approvals (WP4 will replace this), capture errors
  5. on done: readable error mapping, secret filter, split (<=3 messages)
     or .txt document (>3 chunks), PUT the assistant turn
  6. clear active_run - ALWAYS, also on failure (finally)

Every outbound byte goes through the messenger, which routes through
send() (the owner choke point).
"""
from __future__ import annotations

import datetime as _dt
import logging
import time

from httpx import HTTPError

import asyncio
from . import render
from .hive_client import HiveUnreachable
from .telegram_api import TelegramApiError

from .bridge_commands import (  # noqa: F401
    MODE_ALIASES,
    MODE_CHOICES,
    STALE_JOURNAL_S,
    _MODE_DESCRIPTIONS,
    _BridgeCommandsMixin,
)
from .bridge_relay import (  # noqa: F401
    JOURNAL_META_ONLY_AFTER,
    _BridgeRelayMixin,
    _card_did_matches,
    _now,
)
from .bridge_voice import (  # noqa: F401
    FINAL_CHUNK_LIMIT,
    STOP_REASON_TEXT,
    _BridgeVoiceMixin,
)

def _to_epoch(stamp) -> float | None:
    """Parse an epoch number or naive/aware ISO timestamp into a UTC epoch float."""
    if stamp is None or isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        val = float(stamp)
        return val if val > 0 else None
    raw = str(stamp).strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except ValueError:
        try:
            val = float(raw)
            return val if val > 0 else None
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _fit_card_did_token(prefix: str, rid: str, choices: tuple[str, ...],
                        decision_id: str) -> str:
    """Fit decision_id into Telegram's 64-byte callback_data cap across choices."""
    did = str(decision_id or "")
    if not did:
        return ""
    max_choice = max(len(c.encode("utf-8")) for c in choices)
    head_bytes = len(f"{prefix}:{rid}:".encode("utf-8")) + max_choice + 1
    budget = 64 - head_bytes
    if budget <= 0:
        return ""
    did_bytes = did.encode("utf-8")
    if len(did_bytes) <= budget:
        return did
    return did_bytes[:budget].decode("utf-8", errors="ignore")


def _card_cb_data(prefix: str, rid: str, choice: str, did_tok: str = "") -> str:
    """Build callback_data with optional decision_id token."""
    return f"{prefix}:{rid}:{choice}:{did_tok}" if did_tok else f"{prefix}:{rid}:{choice}"




class RunBridge(_BridgeVoiceMixin, _BridgeRelayMixin, _BridgeCommandsMixin):
    def __init__(self, hive, state, cfg, messenger):
        """messenger must provide:
        async send_message(text) -> dict (telegram result, has message_id)
        async edit_message(message_id, text)
        async send_document(data: bytes, filename: str)
        property owner_id (for logging only; send() enforces the policy)
        """
        self.hive = hive
        self.state = state
        self.cfg = cfg
        self.ms = messenger
        self.verbose = bool(state.data.get("verbose", False))
        # P0 (edge audit 2026-11-03): /stop can arrive between "active_run
        # persisted" and the run_id frame. Clearing the latch there crashed the
        # late frame and left the engine run alive; this flag defers the abort.
        self._stop_requested = False
        # ask-mode: the ONE waiting own-run approval (rid/decision_id/tool)
        self._open_own_approval: dict | None = None
        # R3 (audit r3): own runs execute as tasks now - without this
        # lock two rapid texts both passed the busy check (which sits 3
        # awaits away from the latch) and ran concurrently.
        import asyncio as _aio_init
        self._run_lock = _aio_init.Lock()
        # Tool-Call-Relay: suppress consecutive identical activity lines
        self._last_tool_line = ""
        # R6 late-result relay: run ids whose result already reached the
        # phone (never double-send), and this process's start time (late
        # relay only for runs finished after the gateway came up).
        # _handed_over also up front: the fazit path pops it on bridges
        # that never ran a text run.
        # TG-AUDIT: own-run visibility state (ctx milestones etc.)
        self._own_ctx_marks: set = set()
        self._own_ctx_compression_noted: bool = False
        self._handed_over: dict[str, str] = {}
        self._late_relayed: set[str] = set()
        # SEND-FILE dedupe: own runs reset this, mirror runs need it from
        # the first tick (a UI run may be the first thing the gateway sees).
        self._sent_files: set[str] = set()
        # APPROVAL-DUPLICATE (2026-11-04 live): the engine stages an
        # approval card (no decision_id) and later pauses with the real
        # decision_id. Both frames can reach the gateway; remember which
        # tool+preview was already answered so the late staged frame does
        # not produce a second, unanswerable card.
        self._answered_own_approvals: set[str] = set()
        self._proc_start: float = time.time()

    # -- small helpers ----------------------------------------------------

    def _tg_chat(self) -> dict | None:
        return self.state.data.get("tg_chat")

# ── FOLLOW-UI (2026-10-21) ────────────────────────────────────────────
    # Telegram used to write into its own "[TG]" chat forever, so an agentic
    # run started in the browser could not be continued from the phone. With
    # follow_ui (default on) a phone message continues the chat the UI used
    # last, as long as that chat is fresh.
    FOLLOW_UI_MAX_AGE_S = 3600.0

    def follow_ui_enabled(self) -> bool:
        return bool(self.state.data.get("follow_ui", True))

    def set_follow_ui(self, on: bool) -> None:
        self.state.data["follow_ui"] = bool(on)
        self.state.save()

    def _pinned_chat(self) -> dict | None:
        pin = self.state.data.get("pinned_chat")
        return pin if isinstance(pin, dict) and pin.get("hive_chat_id") else None

    async def _ui_chat_target_info(self) -> tuple[str, str]:
        """(chat_id, title) of the newest non-[TG] chat that is still fresh, else ('', '')."""
        if not self.follow_ui_enabled() or self._pinned_chat():
            return "", ""
        try:
            chats = await self.hive.get_chats()
        except (HiveUnreachable, OSError, ValueError):
            return "", ""
        now_ts = time.time()
        tg = self._tg_chat() or {}
        _tg_stamps = [
            ts for ts in (
                _to_epoch(tg.get("last_active_ts")),
                _to_epoch(tg.get("new_chat_ts")),
                _to_epoch(tg.get("created_at")),
            ) if ts is not None
        ]
        tg_ts = max(_tg_stamps) if _tg_stamps else None
        best: tuple[float, str, str] = (0.0, "", "")
        for ch in chats or []:
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("id") or "")
            title = str(ch.get("title") or "")
            if not cid or title.startswith("[TG]"):
                continue
            if int(ch.get("msg_count") or 0) < 1:
                continue
            when_ts = _to_epoch(ch.get("updated_at") or ch.get("created_at"))
            if when_ts is None:
                continue
            age = now_ts - when_ts
            if age < 0 or age > self.FOLLOW_UI_MAX_AGE_S:
                continue
            if when_ts > best[0]:
                best = (when_ts, cid, title)
        if best[1] and tg_ts is not None and tg_ts >= best[0]:
            return "", ""
        return best[1], best[2]

    async def _ui_chat_target(self) -> str:
        """id of the newest non-[TG] chat that is still fresh, else ''."""
        cid, _ = await self._ui_chat_target_info()
        return cid

    async def _ensure_chat(self) -> dict:
        pinned = self._pinned_chat()
        if pinned:
            cid = str(pinned["hive_chat_id"])
            _still_exists = True
            _get_chat = getattr(self.hive, "get_chat", None)
            if _get_chat is not None:
                try:
                    resp = await _get_chat(cid)
                    if getattr(resp, "status_code", 200) == 404:
                        _still_exists = False
                except (HiveUnreachable, HTTPError, OSError, ValueError, AttributeError):
                    pass
            if _still_exists:
                return {
                    "hive_chat_id": cid,
                    "title": str(pinned.get("title") or ""),
                    "created_at": "",
                    "pinned": True,
                }
            self.state.data.pop("pinned_chat", None)
            self.state.save()
        # FOLLOW-UI: continue the chat the browser used last
        _ui, _ui_title = await self._ui_chat_target_info()
        if _ui:
            mapping = {"hive_chat_id": _ui, "title": _ui_title, "created_at": "", "followed": True}
            if self.state.data.get("followed_chat") != _ui:
                self.state.data["followed_chat"] = _ui
                self.state.save()
            return mapping
        if self.state.data.pop("followed_chat", None) is not None:
            self.state.save()
        mapping = self._tg_chat()
        if mapping and mapping.get("hive_chat_id"):
            return mapping
        import datetime as _dt
        now_ts = time.time()
        title = "[TG] Telegram " + _dt.datetime.now().astimezone() \
            .strftime("%Y-%m-%d %H:%M")
        created = await self.hive.create_chat(title)
        mapping = {"hive_chat_id": str(created["id"]),
                   "title": title,
                   "created_at": created.get("chat", {}).get("created_at", ""),
                   "last_active_ts": now_ts}
        self.state.data["tg_chat"] = mapping
        self.state.save()
        return mapping

    async def _placeholder_info(self) -> dict:
        """Query the engine's built-in placeholder workspace, or {} if unavailable."""
        fn = getattr(self.hive, "placeholder_status", None)
        if fn is None:
            return {}
        try:
            data = await fn()
            return data if isinstance(data, dict) else {}
        except (HiveUnreachable, HTTPError, OSError, ValueError):
            return {}

    async def resolve_chat_workspace(self, chat_id: str) -> tuple[str, bool]:
        """Return (workspace_path, is_placeholder) for one Telegram/followed chat.

        Explicit per-chat or /workspace settings win; otherwise phone runs start
        in the isolated HiveMind placeholder folder (never leaking the UI folder
        or install root). Falls back to ('', False) for older engines/fakes.
        """
        ph = await self._placeholder_info()
        ph_path = str(ph.get("path") or "").strip()

        def _is_ph(ws: str) -> bool:
            if not ws or not ph_path:
                return False
            try:
                from pathlib import Path as _P
                return _P(ws).resolve() == _P(ph_path).resolve()
            except OSError:
                return ws == ph_path

        try:
            r = await self.hive.get_chat(chat_id)
            r.raise_for_status()
            _ws = str((r.json() or {}).get("workspace") or "").strip()
            if _ws:
                return _ws, _is_ph(_ws)
        except (HiveUnreachable, HTTPError, OSError, ValueError, AttributeError):
            pass
        _tg_ws = str((self._tg_chat() or {}).get("workspace") or "").strip()
        if _tg_ws:
            return _tg_ws, _is_ph(_tg_ws)
        if ph_path:
            return ph_path, True
        return "", False

    def _format_workspace_label(
        self, ws: str, is_placeholder: bool, *, short_name: bool = False
    ) -> str:
        if is_placeholder:
            return "default_workspace (placeholder)"
        if not ws:
            return "engine default"
        if short_name:
            from pathlib import Path as _P
            return _P(ws).name or ws
        return self._short_path(ws)

    async def workspace_for_chat(self, chat_id: str) -> str:
        """Resolved workspace of one HiveMind chat, or '' if unset."""
        ws, _ = await self.resolve_chat_workspace(chat_id)
        return ws

    async def _write_turn(self, chat_id: str, turn: dict) -> int:
        """Append one turn to the main json with CAS. 409 => adopt the
        server's messages, re-append ours, retry once. Returns the rev."""
        r = await self.hive.get_chat(chat_id)
        r.raise_for_status()
        chat = r.json()
        messages = list(chat.get("messages") or [])
        rev = int(chat.get("rev") or 0)
        for _attempt in (0, 1):
            messages = messages + [turn]
            resp = await self.hive.put_chat_messages(chat_id, messages, rev)
            if resp.status_code == 409:
                body = resp.json()
                messages = list(body.get("messages") or [])
                rev = int(body.get("rev") or 0)
                continue
            resp.raise_for_status()
            return int(resp.json().get("rev") or (rev + 1))
        raise HiveUnreachable("chat write conflict persisted (409 twice)")

    def _busy_note(self) -> str:
        run = self.state.data.get("active_run") or {}
        rid = run.get("run_id") or "?"
        return (f"⏳ A run is already active ({rid}). /stop aborts "
                "it; there is deliberately no queue.")

    # -- status line -------------------------------------------------------

    async def _edit_status(self, message_id, text) -> None:
        if message_id is None:
            return
        # PROGRESS-GATE (1.4.2): telegram_progress_msgs=false silences the info
        # channel (the edited status line). Approval cards and the final answer
        # never pass through here, so a muted channel can still ask and answer.
        try:
            if not await self._progress_ok():
                return
        except (HiveUnreachable, HTTPError, OSError):
            pass
        try:
            await self.ms.edit_message(message_id, text)
        except TelegramApiError as exc:
            self._log_note(f"status edit failed: {exc}")

    def _log_note(self, text: str) -> None:
        logging.getLogger("hivemind_gateway.bridge").info("%s", text)

    # -- the run -----------------------------------------------------------

    async def start_text_run(self, q: str, from_voice: bool = False,
                             images: list | None = None) -> str:
        """Returns the final chat note (for tests); sends everything to the
        phone itself. Never raises to the caller. httpx.HTTPError is
        caught as belt-and-braces - hive_client normally wraps transport
        failures into HiveUnreachable (realrun bug #4)."""
        # R3 (audit r3): the busy check and the latch sit several awaits
        # apart inside _start_text_run_inner - two rapid texts (double-send)
        # both passed the check and ran concurrently. The lock serializes
        # run tasks; the non-blocking peek turns the loser into the busy
        # note instead of queueing it behind the winner.
        if self._run_lock.locked():
            return ("⏳ A run is already active - /stop aborts it.")
        async with self._run_lock:
            try:
                return await self._start_text_run_inner(
                    q, from_voice=from_voice, images=images)
            except (HiveUnreachable, HTTPError, OSError) as exc:
                self._clear_run()
                return await self._fail(f"🔌 HiveMind unreachable "
                                        f"(offline?): {exc}")
            except TelegramApiError as exc:
                self._clear_run()
                self._log_note(f"Telegram delivery error during run: {exc}")
                return f"❌ Telegram delivery error: {exc}"

    @staticmethod
    def _short_path(path: str) -> str:
        """Collapse the user-profile segment out of a path before it
        travels to Telegram (cloud chats are not E2E; the Windows
        username must not ride along). C:/Users/name/x -> ~/x."""
        try:
            from pathlib import Path as _P
            _home = str(_P.home())
            if path and path.startswith(_home):
                return "~" + path[len(_home):]
        except (OSError, ValueError):
            pass
        return path

    async def _fail(self, text: str) -> str:
        try:
            await self.ms.send_message(text)
        except TelegramApiError as exc:
            self._log_note(f"failure notice could not be sent: {exc}")
        return text

    async def _flush_thinking(self, text: str, *, label: str = "") -> None:
        """Send buffered model/planner reasoning to Telegram if progress msgs are on."""
        raw = (text or "").strip()
        if not raw:
            return
        try:
            if not await self._progress_ok():
                return
        except (HiveUnreachable, HTTPError, OSError):
            pass
        clean = " ".join(render.filter_secrets(raw).split())
        if not clean:
            return
        cap = 1200 if self.verbose else 450
        if len(clean) > cap:
            clean = "…" + clean[-(cap - 1):]
        prefix = f"💭 {label}: " if label else "💭 "
        await self.ms.send_message(prefix + clean)

    async def _send_chunk(self, plain: str) -> None:
        """Send one answer chunk as Telegram HTML (code fences render as
        <pre>, 2026-10-05 UX round). A parse rejection falls back to the
        plain text - formatting must never cost the answer itself."""
        try:
            await self.ms.send_message(
                render.md_to_telegram_html(plain), parse_mode="HTML")
        except TelegramApiError as exc:
            # G5 (audit r2): on 429 the immediate plain resend double-hit
            # the limit and the whole result was lost - honor retry_after.
            _ra = getattr(exc, "retry_after", None)
            if _ra:
                await asyncio.sleep(int(_ra) + 1)
            self._log_note(f"HTML send rejected ({exc}) - resending plain")
            await self.ms.send_message(plain)

    def _clear_run(self) -> None:
        # G6 (audit r2): a relayed-but-unanswered approval survived the
        # run end in the persisted state forever (deny-POST noise after
        # every restart). The run is over - its cards are moot.
        _stale_rids = []
        _oa_rid = (self._open_own_approval or {}).get("rid")
        if _oa_rid:
            _stale_rids.append(_oa_rid)
        _ar = self.state.data.get("active_run") or {}
        if _ar.get("run_id"):
            _stale_rids.append(_ar["run_id"])
        _open_apprs = self.state.data.get("open_approvals")
        _popped = False
        if isinstance(_open_apprs, dict) and _stale_rids:
            for _sr in _stale_rids:
                if _open_apprs.pop(_sr, None) is not None:
                    _popped = True
        if self.state.data.get("active_run"):
            self.state.set_active_run(None)
            self.state.save()
        elif _popped:
            self.state.save()  # R3: persist the pop even without a latch
        # the run is over - a waiting own approval is moot (the engine
        # resolved or lost it); never answer a stale card late
        self._open_own_approval = None
        self._stop_requested = False

    async def _deliver_send_file(self, ev: dict, fallback_ws: str = "") -> None:
        """Upload one send_file event; never lets an upload kill the run.

        The engine already validated the file. This is the second, independent
        gate: workspace confinement, secret-name block and the 50 MB cap all
        run again on the gateway side before anything reaches send().
        """
        _sf_path = str(ev.get("path") or "")
        _sf_ws = str(ev.get("workspace") or fallback_ws or "")
        _sf_key = f"{_sf_ws}|{_sf_path}"
        if _sf_key in self._sent_files:
            return
        self._sent_files.add(_sf_key)
        try:
            from core.send_files import (
                SendFileError as _SfErr, read_sendable as _read_sf)
        except ImportError as _sf_imp:
            self._log_note(f"send_file guards unavailable: {_sf_imp}")
            await self.ms.send_message(
                "⚠️ File sending is unavailable on this install.")
            return
        try:
            _sf_data, _sf_name = await asyncio.to_thread(
                _read_sf, _sf_ws, _sf_path)
            await self.ms.send_document(_sf_data, _sf_name)
            self._log_note(
                f"send_file delivered {_sf_name} ({len(_sf_data)} bytes)")
            await self.ms.send_message(
                f"📎 Sent {_sf_name} ({len(_sf_data)} bytes).")
        except _SfErr as _sf_exc:
            await self.ms.send_message(f"⚠️ File not sent: {_sf_exc}")
        except (TelegramApiError, OSError) as _sf_exc:
            self._log_note(f"send_file upload failed: {_sf_exc}")
            await self.ms.send_message(
                "⚠️ File upload failed; the run continues.")

    @staticmethod
    def _format_steer_note(ev: dict) -> str:
        st_txt = str(ev.get("content") or "").strip()
        if not (st_txt or ev.get("images")):
            return ""
        note = "🧭 Steer picked up: " + (st_txt[:300] or "(image)")
        if ev.get("images"):
            note += f" (+{ev.get('images')} img)"
        return note

    @staticmethod
    def _format_file_change_line(ev: dict) -> str:
        fp = str(ev.get("path") or "").replace("\\", "/").split("/")[-1]
        op = str(ev.get("op") or "write")
        ln = ev.get("lines")
        return f"📝 {fp} ({op}" + (f", {ln} lines)" if ln else ")")

    async def _flush_thinking_pair(
        self, pair: list[str], *, swallow_errors: bool = False
    ) -> None:
        for idx, label in ((0, "Planning"), (1, "Reasoning")):
            buf = pair[idx]
            if str(buf or "").strip():
                if swallow_errors:
                    try:
                        await self._flush_thinking(buf, label=label)
                        pair[idx] = ""
                    except (TelegramApiError, OSError):
                        pass
                else:
                    await self._flush_thinking(buf, label=label)
                    pair[idx] = ""

    @staticmethod
    def _compute_card_did(
        rid: str,
        decision_id: str,
        preview: str,
        *,
        kind: str,
        ui_mirror: bool = False,
    ) -> str:
        """Compute callback card_did synchronously before send_message."""
        if kind == "ask":
            if ui_mirror:
                ask_low = preview.lower()
                if "yes" in ask_low and "no" in ask_low and (
                    "confirm" in ask_low or "reply with" in ask_low
                ):
                    return _fit_card_did_token(
                        "ask", rid, ("yes", "no", "later"), decision_id
                    )
            return ""
        return _fit_card_did_token("appr", rid, ("1", "2", "3"), decision_id)

    async def _send_approval_or_ask_card(
        self,
        rid: str,
        decision_id: str,
        tool: str,
        preview: str,
        *,
        kind: str,
        question: str = "",
        ui_mirror: bool = False,
        did_tok: str | None = None,
    ) -> str:
        """Relay an approval or ask_user card to Telegram; returns card_did token."""
        if did_tok is None:
            did_tok = self._compute_card_did(
                rid, decision_id, preview, kind=kind, ui_mirror=ui_mirror
            )
        if kind == "ask":
            if ui_mirror:
                ask_low = preview.lower()
                if "yes" in ask_low and "no" in ask_low and (
                    "confirm" in ask_low or "reply with" in ask_low
                ):
                    await self.ms.send_message(
                        f"❓ The agent asks (run {rid})\n\n"
                        f"{preview}\n\n"
                        "Buttons below - or type your own answer as "
                        "text (e.g. 'yes, but skip the force flag').\n"
                        "(This is the agent's own question - NOT the "
                        "approval gate.)",
                        reply_markup={"inline_keyboard": [
                            [{"text": "✅ Yes",
                              "callback_data": _card_cb_data("ask", rid, "yes", did_tok)},
                             {"text": "⛔ No",
                              "callback_data": _card_cb_data("ask", rid, "no", did_tok)}],
                            [{"text": "⏳ Later",
                              "callback_data": _card_cb_data("ask", rid, "later", did_tok)}],
                        ]},
                    )
                    return did_tok
                await self.ms.send_message(
                    f"❓ The agent asks (run {rid})\n\n"
                    f"{preview}\n\n"
                    "Reply with your answer as text - it goes "
                    "straight back to the agent.\n"
                    "(This is the agent's own question - NOT the "
                    "approval gate.)",
                )
                return ""
            await self.ms.send_message(
                f"❓ The agent asks (run {rid})\n\n"
                f"{question[:2500]}\n\n"
                "Reply with your answer as text - it goes straight back to "
                "the agent. /stop aborts.\n"
                "(This is the agent's own question - NOT the approval gate.)",
            )
            return ""
        if ui_mirror:
            body_txt = (
                f"🛡 Approval needed (UI run {rid})\n\n"
                f"tool: {tool}\n\n"
                f"{preview}\n\n"
                "Buttons below; or reply 1 (once) / 3 (deny) as text.\n"
                "'2' remembers EXACTLY this call for this chat."
            )
        else:
            body_txt = (
                f"🛡 Approval needed (run {rid})\n\n"
                f"tool: {tool}\n\n"
                f"{preview[:2500]}\n\n"
                "Buttons below; or reply 1 (once) / 2 (always, this chat) / "
                "3 (deny)."
            )
        await self.ms.send_message(
            body_txt,
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Once",
                 "callback_data": _card_cb_data("appr", rid, "1", did_tok)},
                {"text": "📁 Always (chat)",
                 "callback_data": _card_cb_data("appr", rid, "2", did_tok)},
                {"text": "⛔ Deny",
                 "callback_data": _card_cb_data("appr", rid, "3", did_tok)},
            ]]},
        )
        return did_tok

    @staticmethod
    def _tool_line(ev: dict) -> str | None:
        """Compact one-line tool activity for the phone (Tool-Call-Relay,
        2026-10-05): '🔧 name: first-line-of-detail'. None when there is
        nothing concise to show."""
        name = str(ev.get("name") or ev.get("tool") or "tool")
        _extra_raw = ev.get("extra")
        extra = _extra_raw if isinstance(_extra_raw, dict) else {}
        detail = ""
        for key in ("cmd", "path", "url", "query", "code", "package"):
            v = str(extra.get(key) or "").strip()
            if v:
                detail = v.splitlines()[0]
                break
        if not detail:
            detail = str(ev.get("label") or ev.get("detail") or "").strip()
        if not detail:
            return None
        if len(detail) > 160:
            detail = detail[:160] + "…"
        return f"🔧 {name}: {detail}"

    def _effective_approval_mode(self, engine_settings: dict) -> str:
        """ask | deny | off for PHONE runs (2026-10-05 owner decision).

        Source: the engine setting telegram_approval_mode (UI select and
        /gate write it; one surgical single-key POST - the blanket
        'gateway never touches POST /settings' taboo is amended for this
        gateway-owned key). The gateway.toml force_approval_gate stays as
        the FLOOR: 'off' degrades to 'deny' while the force is on, so the
        toml can always pin the safe behaviour. Unknown/missing = deny."""
        mode = str((engine_settings or {}).get("telegram_approval_mode")
                   or "deny").strip().lower()
        if mode not in ("ask", "deny", "off"):
            mode = "deny"
        if mode == "off" and self.cfg.force_approval_gate:
            mode = "deny"
        return mode

    def own_approval_pending(self) -> bool:
        """True while an OWN phone-run approval waits for 1/2/3 (ask
        mode): the run is paused engine-side until the answer lands."""
        return bool(self._open_own_approval)

    async def own_answer(self, text: str) -> str:
        """Route the owner's 1/2/3 text answer into the waiting OWN
        approval. Anything else gets the waiting hint - the run is
        paused, a second run would only get the busy note anyway."""
        oa = self._open_own_approval or {}
        t = (text or "").strip()
        _is_ask = oa.get("kind") == "ask"
        if not t:
            return ("❓ The agent is waiting for your answer - reply with "
                    "text. /stop aborts.")
        if not _is_ask and t not in ("1", "2", "3"):
            return ("🛡 Approval is waiting - reply 1 (once), "
                    "2 (always, this chat) or 3 (deny). /stop aborts.")
        resp = await self.hive.decide_approval(
            oa.get("rid"), t,
            decision_id=oa.get("decision_id") or "",
            tool=oa.get("tool") or "")
        code = getattr(resp, "status_code", 500)
        if code >= 300:
            # P1 (edge audit 2026-11-03): keep the card. A 5xx/restart leaves
            # the engine run paused; clearing here lost the only answer path.
            return (f"❌ Decision not accepted (HTTP {code}) - the card is "
                    f"still open; reply again or /stop.")
        self._open_own_approval = None
        self.state.data.setdefault("open_approvals", {}).pop(
            oa.get("rid"), None)
        self.state.save()
        try:
            routed = str((resp.json() or {}).get("routed") or "")
        except (ValueError, TypeError, AttributeError):
            routed = ""
        if (not _is_ask and routed not in ("stale", "expired")
                and oa.get("base_sig")):
            # Approved/denied: the engine may still push its earlier staged
            # card frame (same tool+preview, empty decision_id). Suppress it.
            self._answered_own_approvals.add(str(oa["base_sig"]))
        if routed == "duplicate":
            return "ℹ️ Already answered - nothing changed."
        if routed == "expired":
            return "⏱ card already timed out - the call was denied (fail closed)"
        if routed == "stale":
            # TOAST-HONESTY (2026-10-06): nonce mismatch - the card was
            # replaced by a newer one; the engine dropped this answer.
            return "ℹ️ That card is outdated (a newer one replaced it) - nothing changed."
        if _is_ask:
            return "✅ Answer delivered - the run continues."
        return ("✅ allowed (once) - the run continues."
                if t == "1" else
                "📁 always (this chat, this exact call) - the run "
                "continues." if t == "2" else
                "🛡 denied - pick a different approach.")

    async def _relay_own_approval(self, ev: dict) -> None:
        """ask/off mode: a gated call in an OWN phone run is relayed as a
        tappable card; the engine holds the run (pause) until the
        decision lands via button or 1/2/3 text. Persisted so a gateway
        restart denies it (restart-deny contract)."""
        rid = str(ev.get("run_id") or "")
        if not rid:
            return
        decision_id = str(ev.get("decision_id") or "")
        tool = str(ev.get("tool") or "")
        preview = str(ev.get("preview") or "")
        kind = str(ev.get("kind") or "")
        question = str(ev.get("question") or preview)
        # STAGED-CARD (2026-11-04 live): the engine emits an early card while
        # the tool call is still generating ("(generating, ...)") and the
        # real card with decision_id when it pauses. Relaying both produced
        # two cards for one call, and answering the staged one stored a
        # pre-decision that skipped the real pause. The poll always sees the
        # real card, so the staged frame is ignored on the phone.
        if preview.lstrip().startswith("(generating"):
            return
        if self._stop_requested:
            return
        _sig = f"{rid}|{decision_id}|{tool}|{preview[:80]}"
        _base_sig = f"{rid}|{tool}|{preview[:80]}"
        if _base_sig in self._answered_own_approvals:
            return
        if (self._open_own_approval or {}).get("sig") == _sig:
            return
        if (self._open_own_approval or {}).get("base_sig") == _base_sig:
            return
        self.state.data.setdefault("open_approvals", {})[rid] = {
            "decision_id": decision_id, "tool": tool, "kind": kind}
        self.state.save()
        did_tok = self._compute_card_did(
            rid, decision_id, preview, kind=kind, ui_mirror=False
        )
        self._open_own_approval = {"rid": rid, "decision_id": decision_id,
                                   "card_did": did_tok,
                                   "tool": tool, "sig": _sig,
                                   "base_sig": _base_sig, "kind": kind}
        _sent_card = False
        try:
            await self._send_approval_or_ask_card(
                rid, decision_id, tool, preview,
                kind=kind, question=question, ui_mirror=False, did_tok=did_tok,
            )
            _sent_card = True
        finally:
            if not _sent_card and (self._open_own_approval or {}).get("sig") == _sig:
                self._open_own_approval = None

    async def _poll_own_approval(self, run_id: str, stop_ev: asyncio.Event,
                                 appr_mode: str) -> None:
        """Phone-side replacement for the buffered approval SSE frame.

        ToolLoop emits approval_request only AFTER the blocking tool call, so
        an own phone run could wait forever with no card. Poll the same
        /approval/pending source the browser uses.
        """
        last_sig = ""
        while not stop_ev.is_set():
            if self._stop_requested:
                return
            try:
                resp = await self.hive.pending_approval(run_id)
                body = resp.json() if hasattr(resp, "json") else {}
                code = getattr(resp, "status_code", 200)
            except (HiveUnreachable, HTTPError, OSError, ValueError):
                body, code = {}, 0
            if code == 200 and body.get("active"):
                _kind = str(body.get("kind") or "")
                sig = "|".join(str(body.get(k) or "") for k in (
                    "decision_id", "tool", "preview", "kind"))
                if sig != last_sig:
                    last_sig = sig
                    # The approval mode governs TOOL approval. An ask_user
                    # question is answered by the owner, never auto-denied
                    # with "3" (that would arrive as the literal answer).
                    if appr_mode == "deny" and _kind != "ask":
                        try:
                            await self.hive.decide_approval(
                                run_id, "3",
                                decision_id=str(body.get("decision_id") or ""),
                                tool=str(body.get("tool") or ""))
                        except (HiveUnreachable, HTTPError, OSError):
                            pass
                    else:
                        try:
                            await self._relay_own_approval({
                                "run_id": run_id,
                                "decision_id": str(body.get("decision_id") or ""),
                                "tool": str(body.get("tool") or ""),
                                "preview": str(body.get("preview") or ""),
                                "kind": _kind,
                                "question": str(body.get("question") or ""),
                            })
                        except (TelegramApiError, HTTPError, OSError):
                            last_sig = ""
            elif last_sig:
                # The engine-side pause vanished (ask timeout or fail-closed
                # deny). Drop the stale latch; otherwise the next owner text
                # is swallowed by own_answer with a false "delivered" note.
                _oa = self._open_own_approval or {}
                if _oa.get("rid") == run_id:
                    self._open_own_approval = None
                    self.state.data.setdefault("open_approvals", {}).pop(
                        run_id, None)
                    self.state.save()
                    self._log_note(
                        f"own approval for {run_id} expired engine-side - latch cleared")
                    last_sig = ""
            await asyncio.sleep(1.0)

    def _active_run_fresh_unconfirmed(self) -> bool:
        """True while active_run exists but the engine has not confirmed
        a run_id yet (the window between POST /stream and the run_id
        frame - or an engine outage). Fresh window: 120 s."""
        run = self.state.data.get("active_run") or {}
        if run.get("run_id"):
            return False
        try:
            import datetime as _dt
            t0 = _dt.datetime.fromisoformat(str(run.get("started")))
            return (_dt.datetime.now().astimezone() - t0).total_seconds() < 120
        except (ValueError, TypeError):
            return True

    def _starting_state_stale(self) -> bool:
        """active_run without a confirmed run_id and older than 120 s:
        the process died in the starting window - the latch is stale."""
        run = self.state.data.get("active_run") or {}
        if run.get("run_id"):
            return False
        try:
            import datetime as _dt
            t0 = _dt.datetime.fromisoformat(str(run.get("started")))
            return (_dt.datetime.now().astimezone() - t0).total_seconds() >= 120
        except (ValueError, TypeError):
            # P2 (edge audit 2026-11-03): a malformed `started` used to look
            # fresh forever while the stale check said no - /stop was the only
            # way out. Treat an unparseable start as stale.
            return True

    async def _start_text_run_inner(self, q: str, from_voice: bool = False,
                                     images: list | None = None) -> str:
        if self.state.data.get("active_run"):
            if self._active_run_fresh_unconfirmed():
                # fresh starting window (POST /stream sent, run_id not yet
                # confirmed): a second message would collide - wait
                return ("⏳ A run is starting (waiting for the engine) - "
                        "try again in a moment.")
            if self._starting_state_stale():
                # stale unconfirmed latch (crash in the starting window):
                # self-heal instead of the busy-forever deadlock
                self._clear_run()
                return await self._start_text_run_inner(
                    q, from_voice=from_voice, images=images)
            return self._busy_note()
        journal_note = await self._journal_busy()
        if journal_note:
            return journal_note

        mapping = await self._ensure_chat()
        chat_id = mapping["hive_chat_id"]
        if not mapping.get("followed") and not mapping.get("pinned"):
            tg_map = self._tg_chat()
            if isinstance(tg_map, dict) and str(tg_map.get("hive_chat_id") or "") == chat_id:
                tg_map["last_active_ts"] = time.time()
                self.state.data["tg_chat"] = tg_map
                self.state.save()
        # SEND-FILE (2026-11-04): one workspace lookup per run; the engine
        # already confined the file, this is the gateway-side re-check.
        _run_ws, _run_is_ph = await self.resolve_chat_workspace(chat_id)
        await self._write_turn(chat_id, {"role": "user", "content": q})
        # VOICE-PRELOAD (1.4.2): warm the engine now, not when the answer is done
        await self._preload_voice()

        # persist BEFORE the stream: a crash here loses the update
        # (at-most-once), never runs it twice
        import datetime as _dt
        self._stop_requested = False
        self.state.data["active_run"] = {
            "run_id": None,
            "chat_id": chat_id,
            "started": _dt.datetime.now().astimezone()
                .isoformat(timespec="seconds"),
        }
        self.state.save()

        # info-rich start note (2026-10-05 UX round): mode + the RESOLVED
        # models per role + workspace + the effective approval policy, so
        # the owner sees WHAT will run before tokens arrive. Resolved
        # from the engine settings (one fetch for restriction + models +
        # approval mode): the duo planner inherits the coder model unless
        # duo_planner_model is set (core/duo_runner.py:1038).
        _ws_short = self._format_workspace_label(_run_ws, _run_is_ph, short_name=True)
        _restricted = True
        _s: dict = {}
        try:
            _s = await self.hive.settings()
            _restricted = bool(_s.get("telegram_phone_restricted", True))
        except (HiveUnreachable, OSError, ValueError):
            pass  # engine offline: the run fails anyway, stay restricted
        # MODE-SOURCE (2026-10-21): the frontend mode is the default; a phone
        # override only survives while the engine mode is unchanged.
        _eff_mode, _mode_src = self.effective_phone_mode(_s)
        _sel = self.state.data.get("mode") or ""
        if _sel and not self.state.data.get("mode_engine_seen"):
            # first run after /mode: remember the engine baseline
            self.state.data["mode_engine_seen"] = str(
                (_s or {}).get("mode") or "")
            self.state.save()
        _appr_mode = self._effective_approval_mode(_s)
        # CHAT-RUNCONF: this chat answers with the parameters it started
        # with (see _chat_runconf). sel_mode below comes from the pin, not
        # from the live phone mode.
        _conf = self._chat_runconf(chat_id, _s)
        sel_mode = _conf.get("mode", "")
        _agents = (_s.get("agents") or {}) if isinstance(_s, dict) else {}
        _coder_mdl = (_agents.get("duo_coder") or {}).get("model") or ""
        _planner_mdl = _s.get("duo_planner_model") or _coder_mdl
        _direct_mdl = (_agents.get("direct") or {}).get("model") or ""
        _ov_model = (self.state.data.get("run_overrides") or {}).get("model")
        _eff = _eff_mode
        # VOICE-KEEPS-MODE: a spoken note used to be
        # forced to "auto" even when the UI sat in chat mode, so a voice message
        # silently took a different route than typed text. The CONVERSATIONAL
        # modes (chat/simple/direct) now apply to voice as well. code_duo and
        # friends still hand the route to the engine: forcing them on a spoken
        # note is the old "what time is it started a coder run" bug.
        _voice_mode = _eff if str(_eff).lower() in ("chat", "simple", "direct") else "auto"
        # PIN-WINS (2026-10-31, audit): the note used to show the live phone
        # mode even when this chat was pinned to another one.
        _eff_label = (
            f"{_conf['mode']} (chat)" if _conf.get("mode")
            else f"{_eff} (phone override)" if _mode_src == "phone"
            else _eff)
        if from_voice and not _sel:
            _eff_label = (f"{_voice_mode} (voice)" if _voice_mode != "auto"
                          else "auto (HiveMind decides)")
        if from_voice and not _sel and not _ov_model:
            _model_line = ("model: follows the route "
                           f"(direct: {_direct_mdl or 'engine default'} / "
                           f"agentic: {_coder_mdl or 'engine default'})")
        elif _eff in ("simple", "chat", "direct"):
            _model_line = f"model: {_ov_model or _direct_mdl or 'engine default'}"
        elif _ov_model:
            _model_line = f"model: {_ov_model} (planner + coder)"
        else:
            _model_line = (f"planner: {_planner_mdl or 'engine default'}\n"
                           f"coder: {_coder_mdl or 'engine default'}")
        if _restricted:
            _model_line += ("\nlimited: can search the web and answer - no "
                            "shell, no file changes (turn off 'Restrict "
                            "phone runs' in the UI to unlock; approval "
                            "cards will still ask first)")
        # the info note is PERMANENT (never status-edited): mode/models/
        # approvals/workspace stay visible in the chat while the separate
        # progress message carries the transient ⏳/✅ states. Both sends
        # are INSIDE the guarded region - a Telegram hiccup here must not
        # escape past the persisted active_run (that latch without a
        # cleanup path was the busy-forever deadlock, 2026-10-05).
        status_id = None  # R3: unbound here used to NameError in _finish
                           # (start-note transport failure) and wedge the
                           # latch - NameError is not in the finally tuple.
        _chat_src = ("pinned" if mapping.get("pinned")
                     else ("follow-UI" if mapping.get("followed") else "TG"))
        _chat_title = str(mapping.get("title") or "").strip()
        _chat_line = (f"{_chat_title[:42]} ({chat_id}) [{_chat_src}]"
                      if _chat_title and _chat_title != chat_id
                      else f"{chat_id} [{_chat_src}]")
        _conf_ov = (_conf.get("run_overrides") if isinstance(_conf, dict) else None) or {}
        _eff_main = str(_conf_ov.get("reasoning_effort") or (_s.get("reasoning_effort") if isinstance(_s, dict) else "") or "auto")
        _eff_plan = str(_conf_ov.get("planner_reasoning_effort") or (_s.get("planner_reasoning_effort") if isinstance(_s, dict) else "") or "auto")
        _effort_line = f"effort: {_eff_main}" + (f" · planner: {_eff_plan}" if _eff_plan != "auto" else "") + "\n"
        try:
            await self.ms.send_message(
                "ℹ️ Run\n\n"
                f"chat: {_chat_line}\n"
                f"mode: {_eff_label}\n"
                f"{_effort_line}"
                f"{_model_line}\n"
                f"approvals: {_appr_mode}\n"
                f"workspace: {_ws_short}")
            status = await self.ms.send_message("⏳ working …")
            status_id = status.get("message_id")
        except (TelegramApiError, OSError) as exc:
            self._log_note(f"start note failed ({exc}) - run continues")

        parts: list[str] = []
        truncated = False
        self._last_tool_line = ""  # Tool-Relay dedupe reset per run
        self._own_ctx_marks = set()
        self._own_ctx_compression_noted = False
        self._tool_relay_seen = {}  # R6: repeat-counter reset per run
        self._sent_files: set[str] = set()  # SEND-FILE dedupe per run
        self._answered_own_approvals = set()  # approval dedupe per run
        _approval_poll_stop = asyncio.Event()
        _approval_poll_task: asyncio.Task | None = None
        self._skip_finish = False  # R4: stream-broke handover skips _finish
        # R4 relay: phase/plan/thinking visibility on the phone
        last_phase = ""
        planner_think = ""
        tick_think = ""
        # PHONE-RUNS-IN-UI (2026-10-06): rid -> chat_id of runs handed
        # over to the mirror after a stream break - their answers must
        # land in the chat transcript or the UI never sees them.
        self._handed_over: dict[str, str] = {}
        error_text = ""
        stop_reason = ""
        last_status = ""
        # prebound: every path that reaches the return below assigns it in
        # the finally block; the prebind keeps that statically checkable
        final_note = ""
        last_edited_text = ""
        last_edit = _now()
        denied = 0

        try:
            ov = self._stream_overrides(
                (_conf or {}).get("run_overrides"))
            # source marker: lets the engine suppress desktop toasts for
            # phone runs (the phone surface answers approvals/questions)
            ov["source"] = "telegram"
            if _restricted:
                ov["phone_restricted"] = True
            if _appr_mode in ("ask", "deny"):
                ov["duo_action_approval_enabled"] = True
            if _eff in ("simple", "chat", "direct") and _ov_model:
                # P9 (2026-10-05): the phone model selection now applies
                # to simple/direct runs too (engine lifts direct_model).
                ov["direct_model"] = _ov_model
            # sel_mode comes from the chat pin (see _chat_runconf)
            stream_mode = sel_mode
            if sel_mode == "agentic":
                # composite: engine mode "auto" + an EXPLICIT agentic request.
                # duo_agentic_mode alone is no longer enough (the decider owns
                # mode=auto), so the run carries agentic_force.
                stream_mode = "auto"
                ov["duo_agentic_mode"] = True
                ov["agentic_force"] = True
            elif sel_mode == "auto":
                # PHONE-AUTO-DECIDER (2026-10-28): /mode auto must hand the
                # routing decision to the engine's decider for every message.
                # mode="auto" alone does not win: chat_run short-circuits auto
                # into the agentic loop while the PERSISTED duo_agentic_mode
                # is true and the routing decider is off - that exact
                # combination sent the smalltalk "was geht" through the
                # pathtracer planner. An explicit False keeps the phone
                # choice in charge without touching the owner's saved
                # preference (same pattern as the voice decider below).
                stream_mode = "auto"
                ov["duo_agentic_mode"] = False
            elif from_voice and not sel_mode:
                # VOICE-DECIDER (2026-10-21) with VOICE-KEEPS-MODE (2026-11-01):
                # a spoken note has no pin. In the conversational modes the
                # frontend mode wins (_voice_mode); otherwise "auto" lets the
                # engine's decider pick the route - forcing code_duo on a
                # spoken note is the old "what time is it started a coder run"
                # bug.
                stream_mode = _voice_mode
                ov["source"] = "telegram-voice"
                if _voice_mode == "auto":
                    # The engine short-circuits mode="auto" to code_duo whenever
                    # settings["duo_agentic_mode"] is true (chat_run.py:1322).
                    # An explicit False keeps the decider in charge for a spoken
                    # note without touching the owner's saved preference.
                    ov["duo_agentic_mode"] = False
            async for ev in self.hive.stream(q, chat_id, images=images,
                                             mode=stream_mode,
                                             overrides=ov,
                                             workspace=_run_ws):
                etype = ev.get("type")
                if etype == "run_id" and ev.get("run_id"):
                    _new_rid = str(ev["run_id"])
                    _ar = self.state.data.get("active_run")
                    if _ar is None:
                        # P0 (edge audit 2026-11-03): a stop or a state wipe
                        # cleared the latch before this frame. Abort the run;
                        # never index None.
                        try:
                            await self.hive.abort_run(_new_rid)
                        except (HiveUnreachable, OSError, ValueError):
                            pass
                    else:
                        _ar["run_id"] = _new_rid
                        self.state.save()
                        if _approval_poll_task is None:
                            _approval_poll_task = asyncio.create_task(
                                self._poll_own_approval(
                                    _new_rid, _approval_poll_stop,
                                    _appr_mode))
                        if self._stop_requested:
                            try:
                                await self.hive.abort_run(_new_rid)
                            except (HiveUnreachable, OSError, ValueError):
                                pass
                elif etype == "token":
                    if planner_think.strip():
                        await self._flush_thinking(planner_think, label="Planning")
                        planner_think = ""
                    if tick_think.strip():
                        await self._flush_thinking(tick_think, label="Reasoning")
                        tick_think = ""
                    parts.append(str(ev.get("content") or ""))
                elif etype == "stream_gap":
                    # P0 (edge audit 2026-11-03): the server dropped frames
                    # under backpressure; never persist the truncation as a
                    # complete answer.
                    truncated = True
                elif etype == "error":
                    error_text = str(ev.get("content") or "unknown")
                elif etype == "approval_request":
                    run_id = str(ev.get("run_id") or "")
                    if self._stop_requested:
                        continue
                    if (
                        run_id
                        and _appr_mode == "deny"
                        and str(ev.get("kind") or "approval") != "ask"
                    ):
                        resp = await self.hive.decide_approval(
                            run_id, "3",
                            decision_id=str(ev.get("decision_id") or ""),
                            tool=str(ev.get("tool") or ""))
                        if getattr(resp, "status_code", 200) < 300:
                            denied += 1
                            await self._edit_status(
                                status_id,
                                "🔒 Approval request auto-denied "
                                "(deny mode).")
                    elif run_id:
                        # ask / off: the gated call waits engine-side; the
                        # owner decides via tappable card or 1/2/3 text.
                        await self._relay_own_approval(ev)
                elif etype == "agent":
                    # R4: phase switches as own lines (Planner / Code / …)
                    _tp = [planner_think, tick_think]
                    try:
                        await self._flush_thinking_pair(_tp)
                    finally:
                        planner_think, tick_think = _tp
                    _ph = str(ev.get("content") or "").strip()
                    if _ph and _ph != last_phase:
                        await self.ms.send_message(f"→ {_ph}")
                        last_phase = _ph
                elif etype == "planner_result":
                    if planner_think.strip():
                        await self._flush_thinking(planner_think, label="Planning")
                        planner_think = ""
                    _plan = str(ev.get("content") or "")
                    if not _plan and isinstance(ev.get("chunks"), list):
                        _plan = "".join(str(c) for c in ev["chunks"])
                    _plan = " ".join(_plan.split())[:350]
                    if _plan:
                        await self.ms.send_message(f"🧠 Plan: {_plan}")
                elif ev.get("type") == "duo_round":
                    _msg = f"⚙️ Coder round {ev.get('n')}/{ev.get('total')}"
                    _st = str(ev.get("subtask") or "").strip()
                    if _st:
                        _msg += f": {_st[:110]}"
                    await self.ms.send_message(_msg)
                elif etype == "duo_start":
                    # TG-AUDIT: model visibility parity with the takeover
                    # path - phone runs announce their model/ctx/VRAM too.
                    _ol = str(ev.get("label") or "").strip()
                    if _ol:
                        await self.ms.send_message("🤖 " + _ol[:200])
                elif etype == "ctx_meter":
                    try:
                        _oe = int(ev.get("est_tokens") or 0)
                        _olim = int(ev.get("ctx_limit") or 0)
                    except (TypeError, ValueError):
                        _oe = _olim = 0
                    if _oe > 0 and _olim > 0:
                        _opct = _oe * 100 // _olim
                        for _th in (50, 75, 90):
                            if _opct >= _th and _th not in self._own_ctx_marks:
                                self._own_ctx_marks.add(_th)
                                await self.ms.send_message(
                                    f"🧮 Context {_opct}% "
                                    f"({_oe // 1024}k/{_olim // 1024}k tokens)")
                        if ev.get("compressing") \
                                and not self._own_ctx_compression_noted:
                            self._own_ctx_compression_noted = True
                            await self.ms.send_message(
                                "🗜 Context compressing (older turns are "
                                "being summarized to stay under the "
                                "limit)…")
                elif etype == "file_change":
                    _ofc = self._format_file_change_line(ev)
                    if _ofc != self._last_tool_line:
                        await self.ms.send_message(_ofc)
                        self._last_tool_line = _ofc
                elif etype == "send_file":
                    await self._deliver_send_file(ev, _run_ws)
                elif etype == "planner_thinking_token":
                    # PLANNER VISIBILITY (2026-10-11): the planning-phase tail - same
                    # treatment as the coder thinking tail, flushed with a prefix.
                    planner_think += str(ev.get("content") or "")
                elif etype == "thinking_token":
                    # R4: coder reasoning between tool calls - tail per tick
                    tick_think += str(ev.get("content") or "")
                elif etype == "partition_done":
                    # PRE-EXPLORE VISIBILITY (2026-10-11): the pre-explore workers
                    # emit partition_done once per partition - show it so early
                    # read/search work is not invisible on the phone.
                    _pdl = str(ev.get("label") or "").strip()
                    if _pdl:
                        await self.ms.send_message(f"🔍 pre-explore: {_pdl[:160]}")
                elif etype == "tool_call":
                    if tick_think.strip():
                        await self._flush_thinking(tick_think, label="Reasoning")
                        tick_think = ""
                    # Tool-Call-Relay (2026-10-05) + REPEAT-DEDUPE (R6,
                    # live flood): identical lines relay once, counter
                    # note every 5th repeat.
                    line = self._tool_line(ev)
                    if not line:
                        continue
                    try:
                        if not await self._progress_ok():
                            continue
                    except (HiveUnreachable, HTTPError, OSError):
                        pass
                    _seen = self._tool_relay_seen
                    if len(_seen) > 40:
                        _seen.clear()
                    _cnt = _seen.get(line, 0) + 1
                    _seen[line] = _cnt
                    if _cnt == 1:
                        await self.ms.send_message(line)
                        self._last_tool_line = line
                    elif _cnt % 5 == 0:
                        await self.ms.send_message(f"{line} (repeat #{_cnt})")
                elif etype == "steer":
                    _note = self._format_steer_note(ev)
                    if _note:
                        await self.ms.send_message(_note)
                elif etype == "status":
                    last_status = str(ev.get("content") or "")[:120]
                elif etype == "done":
                    stop_reason = str(ev.get("stop_reason") or "completed")
                if (_now() - last_edit) >= self.cfg.status_min_interval_s:
                    if last_status and last_status != last_edited_text:
                        await self._edit_status(
                            status_id, f"⏳ {last_status} …")
                        last_edited_text = last_status
                        last_edit = _now()
        except HiveUnreachable as _sre:
            # R4 (audit, live 19:48 identity crisis): the SSE stream broke
            # but the run CONTINUES engine-side (detached producer). The old
            # path reported "finished without any text output" + "unreachable"
            # and then the mirror tick adopted our own run as a takeover -
            # three confusing messages for one prompt. If the journal says the
            # run is still active: hand it to the mirror, once, honestly.
            _rid = str((self.state.data.get("active_run") or {}).get("run_id") or "")
            _still_active = False
            if _rid:
                try:
                    _j = await self.hive.journal(_rid)
                    _still_active = bool(
                        isinstance(_j, dict) and _j.get("active")
                        and not _j.get("done") and not _j.get("aborted"))
                except (HiveUnreachable, OSError, ValueError):
                    _still_active = False
            if _still_active:
                self._skip_finish = True
                if _rid:
                    self._handed_over[_rid] = str(chat_id)
                try:
                    await self.ms.send_message(
                        "⚠️ The live stream broke - the run continues on the "
                        "engine. Mirroring it here: your texts steer it, "
                        "/stop aborts it.")
                except (TelegramApiError, OSError):
                    pass
                self._clear_run()
                return ("⚠️ Stream broke - the run continues engine-side "
                        "and is mirrored here.")
            raise  # engine truly unreachable: keep the honest failure path
        finally:
            _tp_fin = [planner_think, tick_think]
            try:
                await self._flush_thinking_pair(_tp_fin, swallow_errors=True)
            finally:
                planner_think, tick_think = _tp_fin
            _approval_poll_stop.set()
            if _approval_poll_task is not None:
                try:
                    await asyncio.wait_for(_approval_poll_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError,
                        RuntimeError):
                    _approval_poll_task.cancel()
            # R4: a stream-broke handover already cleared the latch and told
            # the phone - _finish would only add "finished without any text
            # output" on top of the takeover.
            if not getattr(self, "_skip_finish", False):
                # audit G5: _finish talks to Telegram AND the engine - a failure
                # there (TelegramApiError from delivery, HTTPStatusError from
                # the transcript write) must never skip the run cleanup,
                # otherwise active_run stays latched and every further phone
                # text gets the busy note until the gateway restarts.
                try:
                    final_note = await self._finish(
                        chat_id, q, parts, stop_reason, error_text, denied,
                        status_id, truncated=truncated)
                except (TelegramApiError, HTTPError, HiveUnreachable, OSError,
                        ValueError, TypeError, KeyError, AttributeError) as exc:
                    self._log_note(
                        f"finish failed ({type(exc).__name__}: {exc}) - run "
                        "cleared anyway")
                    try:
                        await self._fail("❌ Result could not be delivered - "
                                         "run was cleaned up.")
                    except (TelegramApiError, HTTPError, HiveUnreachable,
                            OSError) as exc2:
                        self._log_note(f"failure note also failed: {exc2}")
                    final_note = "❌ finish failed (run cleared)"
                self._clear_run()
        return final_note

