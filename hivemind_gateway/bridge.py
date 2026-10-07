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

import dataclasses
import json
import logging
import time

from httpx import HTTPError

import asyncio
from . import render
from .hive_client import HiveUnreachable
from .telegram_api import TelegramApiError

STOP_REASON_TEXT = {
    "model_load_failed":
        "❌ Model could not be loaded (VRAM blocked? "
        "Stop other model runs on the PC and send again).",
    "vram_preflight_block":
        "❌ VRAM block: another model run is active on the PC. "
        "End it and send again.",
    "timeout": "⏱ Run ended due to timeout.",
    "hard_stop": "⏹ Run was hard-aborted.",
    "graceful_stop": "⏹ Run was stopped gracefully.",
    "aborted": "⏹ Run was aborted.",
    "loop_detected": "⏹ Run aborted (repetition loop detected).",
    "wedge_escalated": "⏹ Run aborted (wedged edit, "
                       "repair failed).",
    "halted": "⏹ Run halted.",
    "max_tool_rounds": "⏹ Run stopped at the round limit (partial "
                       "result below, if any).",
}
FINAL_CHUNK_LIMIT = 3
STALE_JOURNAL_S = 180.0

# Run modes accepted via /mode (the /stream BODY field - the browser
# UI's own mode in settings stays untouched). Friendly aliases map to
# the engine's names (index.html mode buttons). "agentic" is a phone-side
# COMPOSITE: engine mode "auto" + duo_agentic_mode body flag (the engine
# reads that key straight from the /stream body).
MODE_CHOICES = ("auto", "agentic", "simple", "pipeline", "automap")
MODE_ALIASES = {"chat": "simple", "direct": "simple"}
_MODE_DESCRIPTIONS = {
    "auto": "duo agent with full tools (files/shell/git) - use for tasks",
    "agentic": "duo + agentic loop (thorough, slower)",
    "simple": "quick chat: talking + web only, NO file tools",
    "pipeline": "pipeline run",
    "automap": "auto-mapping run",
}


def _now() -> float:
    return time.monotonic()


class RunBridge:
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
        self._proc_start: float = time.time()

    # -- small helpers ----------------------------------------------------

    def _tg_chat(self) -> dict | None:
        return self.state.data.get("tg_chat")

    async def _ensure_chat(self) -> dict:
        mapping = self._tg_chat()
        if mapping and mapping.get("hive_chat_id"):
            return mapping
        import datetime as _dt
        title = "[TG] Telegram " + _dt.datetime.now().astimezone() \
            .strftime("%Y-%m-%d %H:%M")
        created = await self.hive.create_chat(title)
        mapping = {"hive_chat_id": str(created["id"]),
                   "created_at": created.get("chat", {}).get("created_at", "")}
        self.state.data["tg_chat"] = mapping
        self.state.save()
        return mapping

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
        try:
            await self.ms.edit_message(message_id, text)
        except TelegramApiError as exc:
            self._log_note(f"status edit failed: {exc}")

    def _log_note(self, text: str) -> None:
        logging.getLogger("hivemind_gateway.bridge").info("%s", text)

    # -- the run -----------------------------------------------------------

    async def start_text_run(self, q: str) -> str:
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
                return await self._start_text_run_inner(q)
            except (HiveUnreachable, HTTPError, OSError) as exc:
                self._clear_run()
                return await self._fail(f"🔌 HiveMind unreachable "
                                        f"(offline?): {exc}")

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
        await self.ms.send_message(text)
        return text

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

    @staticmethod
    def _tool_line(ev: dict) -> str:
        """Compact one-line tool activity for the phone (Tool-Call-Relay,
        2026-10-05): '🔧 name: first-line-of-detail'. None when there is
        nothing concise to show."""
        name = str(ev.get("name") or ev.get("tool") or "tool")
        extra = ev.get("extra") if isinstance(ev.get("extra"), dict) else {}
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
        if t not in ("1", "2", "3"):
            return ("🛡 Approval is waiting - reply 1 (once), "
                    "2 (always, this chat) or 3 (deny). /stop aborts.")
        resp = await self.hive.decide_approval(
            oa.get("rid"), t,
            decision_id=oa.get("decision_id") or "",
            tool=oa.get("tool") or "")
        code = getattr(resp, "status_code", 500)
        self._open_own_approval = None
        self.state.data.setdefault("open_approvals", {}).pop(
            oa.get("rid"), None)
        self.state.save()
        if code >= 300:
            return (f"❌ Decision not accepted (HTTP {code}) - maybe "
                    "answered in the UI.")
        try:
            routed = str((resp.json() or {}).get("routed") or "")
        except (ValueError, TypeError, AttributeError):
            routed = ""
        if routed == "duplicate":
            return "ℹ️ Already answered - nothing changed."
        if routed == "expired":
            return "⏱ card already timed out - the call was denied (fail closed)"
        if routed == "stale":
            # TOAST-HONESTY (2026-10-06): nonce mismatch - the card was
            # replaced by a newer one; the engine dropped this answer.
            return "ℹ️ That card is outdated (a newer one replaced it) - nothing changed."
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
        self.state.data.setdefault("open_approvals", {})[rid] = {
            "decision_id": decision_id, "tool": tool}
        self.state.save()
        self._open_own_approval = {"rid": rid, "decision_id": decision_id,
                                   "tool": tool}
        await self.ms.send_message(
            f"🛡 Approval needed (run {rid})\n\n"
            f"tool: {tool}\n\n"
            f"{preview}\n\n"
            "Buttons below; or reply 1 (once) / 2 (always, this chat) / "
            "3 (deny).",
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Once", "callback_data": f"appr:{rid}:1"},
                {"text": "📁 Always (chat)",
                 "callback_data": f"appr:{rid}:2"},
                {"text": "⛔ Deny", "callback_data": f"appr:{rid}:3"},
            ]]})

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
            return False

    async def _start_text_run_inner(self, q: str) -> str:
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
                return await self._start_text_run_inner(q)
            return self._busy_note()
        journal_note = await self._journal_busy()
        if journal_note:
            return journal_note

        mapping = await self._ensure_chat()
        chat_id = mapping["hive_chat_id"]
        await self._write_turn(chat_id, {"role": "user", "content": q})

        # persist BEFORE the stream: a crash here loses the update
        # (at-most-once), never runs it twice
        import datetime as _dt
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
        _ws = (self._tg_chat() or {}).get("workspace") or "engine default"
        _ws_short = (_ws.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                     if _ws != "engine default" else _ws)
        _sel = self.state.data.get("mode") or ""
        _restricted = True
        _s: dict = {}
        try:
            _s = await self.hive.settings()
            _restricted = bool(_s.get("telegram_phone_restricted", True))
        except (HiveUnreachable, OSError, ValueError):
            pass  # engine offline: the run fails anyway, stay restricted
        _appr_mode = self._effective_approval_mode(_s)
        _agents = (_s.get("agents") or {}) if isinstance(_s, dict) else {}
        _coder_mdl = (_agents.get("duo_coder") or {}).get("model") or ""
        _planner_mdl = _s.get("duo_planner_model") or _coder_mdl
        _direct_mdl = (_agents.get("direct") or {}).get("model") or ""
        _ov_model = (self.state.data.get("run_overrides") or {}).get("model")
        _eff = _sel or (_s.get("mode") if isinstance(_s, dict) else "") \
            or "auto"
        _eff_label = f"{_eff} (phone override)" if _sel else _eff
        if _eff in ("simple", "chat", "direct"):
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
        try:
            await self.ms.send_message(
                "ℹ️ Run\n\n"
                f"mode: {_eff_label}\n"
                f"{_model_line}\n"
                f"approvals: {_appr_mode}\n"
                f"workspace: {_ws_short}")
            status = await self.ms.send_message("⏳ working …")
            status_id = status.get("message_id")
        except (TelegramApiError, OSError) as exc:
            self._log_note(f"start note failed ({exc}) - run continues")

        parts: list[str] = []
        self._last_tool_line = ""  # Tool-Relay dedupe reset per run
        self._own_ctx_marks = set()
        self._own_ctx_compression_noted = False
        self._tool_relay_seen = {}  # R6: repeat-counter reset per run
        self._skip_finish = False  # R4: stream-broke handover skips _finish
        # R4 relay: phase/plan/thinking visibility on the phone
        last_phase = ""
        tick_think = ""
        # PHONE-RUNS-IN-UI (2026-10-06): rid -> chat_id of runs handed
        # over to the mirror after a stream break - their answers must
        # land in the chat transcript or the UI never sees them.
        self._handed_over: dict[str, str] = {}
        error_text = ""
        stop_reason = ""
        last_status = ""
        last_edited_text = ""
        last_edit = _now()
        denied = 0

        try:
            ov = self._stream_overrides()
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
            sel_mode = _sel
            stream_mode = sel_mode
            if sel_mode == "agentic":
                # composite: engine mode "auto" + the agentic body flag
                stream_mode = "auto"
                ov["duo_agentic_mode"] = True
            async for ev in self.hive.stream(q, chat_id,
                                             mode=stream_mode,
                                             overrides=ov):
                etype = ev.get("type")
                if etype == "run_id" and ev.get("run_id"):
                    self.state.data["active_run"]["run_id"] = \
                        str(ev["run_id"])
                    self.state.save()
                elif etype == "token":
                    parts.append(str(ev.get("content") or ""))
                elif etype == "error":
                    error_text = str(ev.get("content") or "unknown")
                elif etype == "approval_request":
                    run_id = str(ev.get("run_id") or "")
                    if run_id and _appr_mode == "deny":
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
                    _ph = str(ev.get("content") or "").strip()
                    if _ph and _ph != last_phase:
                        await self.ms.send_message(f"→ {_ph}")
                        last_phase = _ph
                elif etype == "planner_result":
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
                    _ofp = str(ev.get("path") or "").replace(
                        "\\", "/").split("/")[-1]
                    _ofc = (f"📝 {_ofp} ({ev.get('op') or 'write'}"
                            + (f", {ev.get('lines')} lines)"
                               if ev.get("lines") else ")"))
                    if _ofc != self._last_tool_line:
                        await self.ms.send_message(_ofc)
                        self._last_tool_line = _ofc
                elif etype == "thinking_token":
                    # R4: coder reasoning between tool calls - tail per tick
                    tick_think += str(ev.get("content") or "")
                elif etype == "tool_call":
                    if tick_think.strip():
                        _tt = " ".join(tick_think.split())[-160:]
                        await self.ms.send_message(f"💭 …{_tt}")
                        tick_think = ""
                    # Tool-Call-Relay (2026-10-05) + REPEAT-DEDUPE (R6,
                    # live flood): identical lines relay once, counter
                    # note every 5th repeat.
                    line = self._tool_line(ev)
                    if not line:
                        continue
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
                    # STEER CARD (2026-10-06): the phone typed the steer -
                    # confirm on its own message that the run picked it up
                    # (a status edit would be overwritten by the next tick).
                    _st_txt = str(ev.get("content") or "").strip()
                    if _st_txt or ev.get("images"):
                        _note = "🧭 Steer picked up: " + (_st_txt[:300] or "(image)")
                        if ev.get("images"):
                            _note += f" (+{ev.get('images')} img)"
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
                        status_id)
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

    async def _finish(self, chat_id, q, parts, stop_reason, error_text,
                      denied, status_id) -> str:
        answer = "".join(parts).strip()
        answer = render.filter_secrets(answer)
        notes = []
        if denied:
            notes.append(f"🔒 {denied} approval(s) auto-denied.")
        if stop_reason == "error":
            text = f"❌ Run error: {error_text or 'unknown'}"
        elif stop_reason and stop_reason != "completed":
            base = STOP_REASON_TEXT.get(
                stop_reason, f"⏹ Run finished ({stop_reason}).")
            text = base + (f"\n\n{answer}" if answer else "")
        elif answer:
            text = answer
        else:
            text = "⏹ Run finished without any text output."
        for n in notes:
            text += "\n" + n

        chunks = render.split_message(text)
        if len(chunks) > FINAL_CHUNK_LIMIT:
            await self.ms.send_document(
                text.encode("utf-8"), "result.txt")
        else:
            for c in chunks:
                await self._send_chunk(c)
        try:
            await self._write_turn(
                chat_id, {"role": "assistant", "content": text})
        except (HiveUnreachable, OSError) as exc:
            self._log_note(f"assistant turn write failed: {exc}")
        if status_id is not None:
            await self._edit_status(status_id, "✅ done.")
        return text

    # -- busy heuristic against the server journal ------------------------

    async def _journal_busy(self) -> str | None:
        try:
            j = await self.hive.journal()
        except (HiveUnreachable, OSError):
            return None  # offline is handled by the run itself
        if not isinstance(j, dict) or not j.get("active"):
            return None
        if j.get("done") or j.get("aborted"):
            return None
        age = time.time() - float(j.get("ts") or 0)
        if age > STALE_JOURNAL_S:
            return None
        rid = str(j.get("run_id") or "?")
        return (f"⏳ A run is already active ({rid}) - probably started "
                "from the browser/UI. There is deliberately no "
                "queue.")

    # -- commands -----------------------------------------------------------

    async def stop(self) -> str:
        run = self.state.data.get("active_run") or {}
        rid = run.get("run_id")
        if not rid:
            if run:
                # unconfirmed starting-state: nothing to abort engine-side
                # (no run_id = the engine never started it) - clear it
                self._clear_run()
                return ("Cleared a starting-state that never got a run id "
                        "(the engine never confirmed it). Try again.")
            return "No run active."
        try:
            resp = await self.hive.abort_run(rid)
            code = getattr(resp, "status_code", 500)
            if code == 404:
                # 2026-10-05 (live): 404 = the engine no longer knows this
                # run (it already ended). Clear the latch EITHER WAY - a
                # failed fallback must not leave active_run latched, or
                # every further text gets the busy note until restart.
                # The run's own chat_id is the reliable fallback target
                # (tg_chat can be absent after a state wipe).
                self._clear_run()
                chat_id = ((self._tg_chat() or {}).get("hive_chat_id")
                           or run.get("chat_id") or "")
                if chat_id:
                    await self.hive.abort_chat(chat_id)
                    return (f"⏹ Run {rid} was unknown to the server - "
                            "chat abort sent as fallback; run cleared.")
                return (f"⏹ Run {rid} was unknown to the server - it "
                        "had already ended; run cleared.")
            return f"⏹ Abort sent for {rid}."
        except HTTPError as exc:
            # deep audit N6: raise_for_status in the fallback must not
            # break the visible-failure invariant of /stop
            return (f"❌ /stop failed ({exc}) - run {rid} may still "
                    "be running, please check the PC!")
        except (HiveUnreachable, OSError) as exc:
            # /stop must fail VISIBLY: an orphaned run holds VRAM
            return f"❌ /stop failed ({exc}) - run {rid} may still " \
                   "be running, please check the PC!"

    async def new_chat(self) -> str:
        if self.state.data.get("active_run"):
            return self._busy_note()
        import datetime as _dt
        title = "[TG] Telegram " + _dt.datetime.now().astimezone() \
            .strftime("%Y-%m-%d %H:%M")
        created = await self.hive.create_chat(title)
        mapping = {"hive_chat_id": str(created["id"]),
                   "created_at": created.get("chat", {}).get("created_at", "")}
        self.state.data["tg_chat"] = mapping
        self.state.save()
        return f"🆕 New HiveMind chat: {title} ({mapping['hive_chat_id']})."

    def status_text(self) -> str:
        run = self.state.data.get("active_run") or {}
        mapping = self._tg_chat() or {}
        mode = self.state.data.get("mode") or "engine default"
        ov = self.state.data.get("run_overrides") or {}
        tools = self.state.data.get("tools")
        tools_txt = ("on" if tools is True else "off" if tools is False
                     else "engine default")
        _restr = getattr(self.ms, "phone_restricted", True)
        restr_txt = ("web + text only" if _restr
                     else "off" if _restr is False else "unknown")
        # audit G1 + version-skew detection: state the invariant plainly -
        # the gateway forces the approval gate for every phone run, but
        # ONLY if the engine lifts the body key (gateway_overrides marker).
        if getattr(self.ms, "engine_gate_support", True):
            gate_line = ("approvals: phone runs gated (ask/deny per /gate; "
                         "UI runs follow the engine toggle live)")
        else:
            gate_line = ("⚠️ gate enforcement INACTIVE - engine too old; "
                         "switch on duo_action_approval_enabled in the UI")
        return (
            "📡 Gateway status\n"
            "\n"
            f"chat: {mapping.get('hive_chat_id', '- none yet')}\n"
            f"workspace: {self._short_path(mapping.get('workspace')) if mapping.get('workspace') else 'engine default'}\n"
            f"mode: {mode}  (phone runs only)\n"
            f"model: {ov.get('model') or 'engine default'}\n"
            f"ctx: planner {ov.get('planner_ctx') or '-'} · coder "
            f"{ov.get('coder_ctx') or '-'}\n"
            f"tools (simple runs): {tools_txt}\n"
            f"phone restrict: {restr_txt}\n"
            f"verbose: {'on' if self.verbose else 'off'}\n"
            "\n"
            f"{gate_line}\n"
            f"engine: {getattr(self.ms, 'engine_version', '?')}\n"
            "\n"
            f"active run: {run.get('run_id') or 'none'}")

    def toggle_verbose(self) -> str:
        self.verbose = not self.verbose
        self.state.data["verbose"] = self.verbose
        self.state.save()
        return f"Verbose: {'on' if self.verbose else 'off'}"

    # -- /mode: telegram-side run mode (stream BODY field, never settings)

    def mode_text(self, arg: str) -> str:
        """/mode            -> current mode + per-mode explanations
        /mode <name>      -> set for TELEGRAM runs only
        /mode off         -> follow the engine settings again
        Aliases: chat|direct -> simple."""
        arg = (arg or "").strip().lower()
        listing = "\n".join(
            f"  • {name} - {_MODE_DESCRIPTIONS[name]}"
            for name in MODE_CHOICES)
        if not arg:
            current = self.state.data.get("mode") or ""
            head = (f"Mode (Telegram): {current}" if current
                    else "Mode (Telegram): engine default")
            return (head + "\n\n" + listing +
                    "\n\nSet with /mode <name>; /mode off follows the "
                    "engine settings again. Phone-only - the browser UI "
                    "keeps its own mode.")
        if arg in ("off", "aus", "default"):
            self.state.data["mode"] = ""
            self.state.save()
            return ("Mode (Telegram): engine default\n\n"
                    "Runs now follow the engine settings again.")
        name = MODE_ALIASES.get(arg, arg)
        if name not in MODE_CHOICES:
            return ("❌ Unknown mode.\n\n" + listing +
                    "\n\nSet with /mode <name> or /mode off.")
        self.state.data["mode"] = name
        self.state.save()
        why = ("File-capable - use this for tasks."
               if name != "simple" else
               "NO file tools here - talk + web only!")
        return (f"Mode (Telegram): {name}\n\n"
                f"  • {_MODE_DESCRIPTIONS[name]}\n"
                f"{why}\n\n"
                "Phone-only - the browser UI keeps its own mode.")

    # -- /models, /setModel, /cancel: model + ctx/preset selection -------

    PENDING_TTL_S = 600

    def _stream_overrides(self) -> dict:
        """Model/ctx keys from the last /setModel flow - merged into the
        run's settings snapshot by the engine (chat_run.py:154). Empty
        unless configured."""
        ov = self.state.data.get("run_overrides") or {}
        out: dict = {}
        model = ov.get("model")
        if model:
            out["duo_planner_model"] = model
            out["duo_coder_model"] = model
        # /planner override: a SEPARATE planner model (duo runs)
        planner_model = ov.get("planner_model")
        if planner_model:
            out["duo_planner_model"] = planner_model
        # deep audit N5: clamp - the engine clamps the planner ctx at
        # 131072 but NOT the coder ctx; a fat-fingered phone value must
        # not wedge the shared VRAM
        pctx = ov.get("planner_ctx")
        if pctx:
            out["duo_planner_ctx_target"] = min(int(pctx), 131072)
            out["duo_planner"] = True
        cctx = ov.get("coder_ctx")
        if cctx:
            out["duo_coder_ctx_agentic"] = min(int(cctx), 131072)
            out["duo_coder_ctx_normal"] = min(int(cctx), 131072)
        tools = self.state.data.get("tools")
        if tools is not None:
            out["direct_tools_enabled"] = bool(tools)
        # audit G1: FORCE the engine's approval gate for every phone run -
        # this restores the documented "gated tools are auto-denied from
        # the phone" invariant even when the engine-global toggle
        # (duo_action_approval_enabled, default false) is off. The body
        # can only raise the gate to ON, never lower it. 2026-10-05: the
        # owner can opt out for assistant-style use (phone runs that
        # SHOULD write files) via gateway.toml force_approval_gate=false -
        # with it off, the engine-global toggle alone governs the gate.
        if self.cfg.force_approval_gate:
            out["duo_action_approval_enabled"] = True
        return out

    def _pending(self) -> dict | None:
        p = self.state.data.get("pending_setup")
        if not p:
            return None
        if time.time() - float(p.get("ts", 0)) > self.PENDING_TTL_S:
            self.state.data["pending_setup"] = None
            self.state.save()
            return None
        return p

    async def models_text(self) -> str:
        try:
            data = await self.hive.get_models()
        except (HiveUnreachable, OSError) as exc:
            return f"🔌 HiveMind unreachable: {exc}"
        models = data.get("models") or []
        profs = {p.get("name"): p for p in (data.get("profiles") or [])}
        if not models:
            return "No models found (empty engine response)."
        lines = ["Available models:"]
        for i, m in enumerate(models, 1):
            p = profs.get(m, {})
            flags = [f for f, on in (("thinking", p.get("thinking")),
                                     ("vision", p.get("vision")),
                                     ("tools", p.get("tool_call"))) if on]
            lines.append(f"{i}. {m}"
                         + (f" [{'+'.join(flags)}]" if flags else ""))
        lines.append("Choose with /setModel <number>")
        return "\n".join(lines)

    async def set_model(self, arg: str) -> str:
        """Pick a model by /models number - applies IMMEDIATELY to
        upcoming runs (no quiz). ctx via /ctx, preset via /preset."""
        try:
            idx = int((arg or "").strip())
        except ValueError:
            return ("Usage: /setModel <number> - /models lists the "
                    "numbers. ctx via /ctx <number>, preset via /preset.")
        try:
            data = await self.hive.get_models()
        except (HiveUnreachable, OSError) as exc:
            return f"🔌 HiveMind unreachable: {exc}"
        models = data.get("models") or []
        if not (1 <= idx <= len(models)):
            return (f"❌ {idx} out of range 1–{len(models)}. List: /models")
        name = models[idx - 1]
        prof = next((p for p in (data.get("profiles") or [])
                     if p.get("name") == name), {})
        can_think = bool(prof.get("thinking"))
        ov = self.state.data.setdefault("run_overrides", {})
        ov["model"] = name
        self.state.data["pending_setup"] = None
        self.state.save()
        out = f"✅ Model set: {name}"
        if not can_think:
            out += " (cannot think)"
        out += ("\nctx via /ctx <number> (0 = engine default), "
                "preset via /preset <number>.\nApplies to upcoming "
                "phone runs.")
        return out

    async def ctx_text(self, arg: str) -> str:
        """/ctx <number|off> - context override for coder + planner."""
        a = (arg or "").strip().lower()
        ov = self.state.data.setdefault("run_overrides", {})
        if a in ("", "status"):
            cur = ov.get("coder_ctx") or ov.get("planner_ctx")
            return ("ctx override: "
                    + (f"{cur} tokens" if cur else "off (engine default)")
                    + ".\nSet with /ctx <number> (0 = off). Applies to "
                    "upcoming duo/agentic phone runs.")
        if a in ("0", "off", "aus"):
            ov.pop("coder_ctx", None)
            ov.pop("planner_ctx", None)
            self.state.save()
            return "ctx override cleared - engine defaults apply."
        if not a.isdigit():
            return "❌ /ctx <number> (tokens) or /ctx off."
        ov["coder_ctx"] = int(a)
        ov["planner_ctx"] = int(a)
        self.state.save()
        return (f"✅ ctx: {a} tokens (coder + planner target) - applies "
                "to upcoming duo/agentic phone runs.")

    async def planner_text(self, arg: str) -> str:
        """/planner <no|off> - separate planner model for duo runs."""
        a = (arg or "").strip()
        ov = self.state.data.setdefault("run_overrides", {})
        if a in ("", "status"):
            cur = ov.get("planner_model")
            return ("planner model: " + (cur or "inherits the coder model")
                    + ".\n/planner <number from /models> | /planner off")
        if a in ("off", "aus"):
            ov.pop("planner_model", None)
            self.state.save()
            return "planner model cleared - inherits the coder model."
        try:
            idx = int(a)
        except ValueError:
            return "❌ /planner <number from /models> | /planner off"
        try:
            data = await self.hive.get_models()
        except (HiveUnreachable, OSError) as exc:
            return f"🔌 HiveMind unreachable: {exc}"
        models = data.get("models") or []
        if not (1 <= idx <= len(models)):
            return f"❌ {idx} out of range 1–{len(models)}. List: /models"
        ov["planner_model"] = models[idx - 1]
        self.state.save()
        return f"✅ planner model: {models[idx - 1]} (duo runs)."

    async def preset_text(self, arg: str) -> str:
        """/preset <no> - load a preset globally (phone + UI)."""
        a = (arg or "").strip()
        if not a.isdigit() or int(a) < 1:
            return "Usage: /preset <number>."
        try:
            raw = await self.hive.get_presets()
        except (HiveUnreachable, OSError) as exc:
            return f"🔌 Presets unavailable: {exc}"
        names = sorted(raw.keys()) if isinstance(raw, dict) else \
            [x if isinstance(x, str) else str(x.get("name", "?"))
             for x in (raw or [])]
        pick = int(a)
        if pick > len(names):
            return f"❌ Preset {pick} out of range 1–{len(names)}."
        resp = await self.hive.load_preset(names[pick - 1])
        if getattr(resp, "status_code", 500) >= 300:
            return (f"❌ Preset '{names[pick - 1]}' could not be loaded - "
                    "nothing changed.")
        return (f"✅ Preset '{names[pick - 1]}' loaded (global - phone + "
                "UI).")

    def cancel_setup(self) -> str:
        """/cancel: clear the model/ctx overrides for phone runs (the
        pending /setModel quiz no longer exists - commands apply live)."""
        ov = self.state.data.setdefault("run_overrides", {})
        had = any(ov.get(k) for k in ("model", "planner_model",
                                      "coder_ctx", "planner_ctx"))
        ov.pop("model", None)
        ov.pop("planner_model", None)
        ov.pop("coder_ctx", None)
        ov.pop("planner_ctx", None)
        self.state.save()
        return ("Overrides cleared - upcoming phone runs use the engine "
                "settings again." if had else
                "No overrides set - nothing to clear.")

    def reset_overrides(self) -> str:
        self.state.data["run_overrides"] = {}
        self.state.data["pending_setup"] = None
        self.state.save()
        return ("Model/CTX overrides cleared - upcoming runs use "
                "the engine settings again.")

    # -- P10: run takeover - mirror a UI/engine run to the phone ---------

    def _mirror(self) -> dict:
        if not hasattr(self, "_mirror_state"):
            self._mirror_state = {"run_id": None, "after": 0,
                                  "status_msg_id": None,
                                  "last_status": "", "approval_sig": None,
                                  "approval_decision_id": "",
                                  "approval_tool": "",
                                  "other_run_noted": "", "last_phase": "",
                                  "tool_relay_seen": {},
                                  "answer_parts": [],
                                  "last_tick": 0.0}
        return self._mirror_state

    def _mirror_reset(self, m: dict) -> None:
        """End a mirror session - every reset site must clear the approval
        card metadata too (G2: decision_id/tool of the relayed card)."""
        m.update({"run_id": None, "after": 0, "status_msg_id": None,
                  "last_status": "", "approval_sig": None,
                  "approval_decision_id": "", "approval_tool": "",
                  "other_run_noted": "", "last_phase": "",
                                  "tool_relay_seen": {}, "answer_parts": [],
                  "ctx_marks": set(), "ctx_compression_noted": False,
                  "answer_sent_upto": 0})

    def _own_run_id(self) -> str | None:
        run = self.state.data.get("active_run") or {}
        return run.get("run_id")

    def _mirror_enabled(self, engine_settings: dict) -> bool:
        """The UI toggle (telegram_mirror_enabled) gates the takeover.
        Key absent = off (fail-closed, same rule as the veto)."""
        return bool(engine_settings.get("telegram_mirror_enabled"))

    async def mirror_tick(self) -> None:
        """One mirror step (called every few seconds from the mirror
        task). Watches the engine journal for an active run that is NOT
        the gateway's own, relays it to the phone, relays approval
        cards. Never raises."""
        try:
            await self._mirror_tick_inner()
        except (HiveUnreachable, OSError) as exc:
            self._log_note(f"mirror tick: engine unreachable ({exc})")
        except (TelegramApiError, ValueError, TypeError, KeyError,
                AttributeError) as exc:
            # deep audit N3: a malformed frame must not silently kill the
            # mirror task - log and continue with the next tick
            self._log_note(f"mirror tick failed: {type(exc).__name__}: {exc}")

    def _parse_done(self, frames: list) -> str | None:
        """stop_reason of the LAST done frame in the journal, if any."""
        for raw in reversed(frames or []):
            if isinstance(raw, str) and '"type": "done"' in raw:
                try:
                    payload = json.loads(raw[6:])
                    return str(payload.get("stop_reason") or "completed")
                except (ValueError, TypeError):
                    continue
        return None

    async def _mirror_tick_inner(self) -> None:
        m = self._mirror()
        try:
            s = await self.hive.settings()
        except (HiveUnreachable, OSError, ValueError):
            return  # engine unreachable: nothing to mirror, keep quiet
        # UI toggle telegram_phone_restricted: stash for /status + the
        # supervisor heartbeat (best-effort view, refreshed every tick);
        # missing key = restricted (safe default)
        if hasattr(self.ms, "phone_restricted"):
            self.ms.phone_restricted = bool(s.get("telegram_phone_restricted",
                                                  True))
        if not self._mirror_enabled(s):
            if m["run_id"]:
                self._mirror_reset(m)
                await self.ms.send_message(
                    "📴 Mirror ended (setting off in the UI).")
            return
        # audit G7: once a session is active, fetch THAT run's journal -
        # the unscoped endpoint answers with the most recently ACTIVE run,
        # which can be the wrong one when a second run starts.
        j = await self.hive.journal(m["run_id"]) if m["run_id"] \
            else await self.hive.journal()
        if not isinstance(j, dict) or not j.get("active") \
                or j.get("done") or j.get("aborted"):
            # nothing active: end a session, report how it ended
            if m["run_id"]:
                rid, frames = m["run_id"], j.get("frames") \
                    if isinstance(j, dict) else None
                reason = self._parse_done(frames) or "finished"
                # R6 FAZIT ON EARLY DONE (owner report, live 10:2x): a
                # run that finished between two ticks landed HERE and
                # the phone got a bare "finished" line - the produced
                # answer was discarded. Drain accumulated tokens; fall
                # back to the full journal frames when empty.
                _acc = (self._accumulate_frames(frames or [])[0]
                        if isinstance(j, dict) else "")
                _ans = "".join(m.get("answer_parts") or []).strip() or _acc
                self._mirror_reset(m)
                await self._mirror_fazit(rid, _ans, reason)
            elif isinstance(j, dict) and j.get("done") and not j.get("aborted"):
                # R6 LATE RESULT (owner: messages must arrive): a run
                # that started AND finished while the mirror was not
                # attached still delivers its result - once per run id,
                # only for runs finished during THIS gateway process.
                _lr_id = str(j.get("run_id") or "")
                if (_lr_id and float(j.get("ts") or 0) > self._proc_start
                        and _lr_id not in self._late_relayed
                        and _lr_id != str(self._own_run_id() or "")):
                    self._late_relayed.add(_lr_id)
                    _lr_ans, _ = self._accumulate_frames(j.get("frames") or [])
                    await self._mirror_fazit(
                        _lr_id, _lr_ans,
                        self._parse_done(j.get("frames")) or "completed")
            return
        rid = str(j.get("run_id") or "")
        # deep audit N7 (confirmed as G7): while OUR run sits between
        # POST /stream and its run_id frame, active_run exists with
        # run_id None - the unscoped journal is then necessarily our own
        # run. Skip the takeover decision this tick instead of announcing
        # the gateway's own run as a takeover.
        _own = self.state.data.get("active_run")
        if not m["run_id"] and isinstance(_own, dict) \
                and not _own.get("run_id"):
            return
        if rid == str(self._own_run_id() or ""):
            self._late_relayed.add(rid)  # own path relays it
            return  # our own run reports natively; do not double-mirror
        fresh = m["run_id"] != rid
        if fresh:
            m.update({"run_id": rid, "after": int(j.get("n") or 0),
                      "status_msg_id": None, "last_status": "",
                      "approval_sig": None, "approval_decision_id": "",
                      "approval_tool": "", "other_run_noted": "",
                      "last_phase": "", "answer_parts": [],
                      "ctx_marks": set(), "ctx_compression_noted": False,
                      "answer_sent_upto": 0})
            # R6 CATCH-UP (owner: messages must arrive): process the
            # pre-adoption frames SILENTLY - the answer accumulates so
            # the fazit relay at done carries the FULL result, and tool
            # repeats dedupe against pre-adoption calls. No sends here.
            _catchup_pct = 0
            _catchup_comp = False
            _catchup_lim = 0
            _duo_label = ""
            for _raw in (j.get("frames") or []):
                if not isinstance(_raw, str):
                    continue
                try:
                    _cev = json.loads(
                        _raw[6:] if _raw.startswith("data: ") else _raw)
                except ValueError:
                    continue
                if _cev.get("type") == "token":
                    m["answer_parts"].append(str(_cev.get("content") or ""))
                elif _cev.get("type") == "agent" and m["answer_parts"]:
                    m["answer_parts"] = []  # phase switch: last phase wins
                elif _cev.get("type") == "tool_call":
                    # pre-count so a repeat loop already running before
                    # the adoption dedupes instead of re-flooding
                    _tl = self._tool_line(_cev)
                    if _tl:
                        _saw = m.setdefault("tool_relay_seen", {})
                        _saw[_tl] = _saw.get(_tl, 0) + 1
                elif _cev.get("type") == "ctx_meter":
                    # TG-AUDIT: STATE events are silently skipped in the
                    # replay (no spam), but the mirror must still land on
                    # the CURRENT state - milestones already passed are
                    # pre-marked and a live compression is announced once.
                    try:
                        _e = int(_cev.get("est_tokens") or 0)
                        _l = int(_cev.get("ctx_limit") or 0)
                    except (TypeError, ValueError):
                        _e = _l = 0
                    if _e > 0 and _l > 0:
                        _catchup_pct = max(_catchup_pct, _e * 100 // _l)
                        _catchup_lim = _l
                        _catchup_comp = bool(_cev.get("compressing"))
                elif _cev.get("type") == "duo_start":
                    _dl = str(_cev.get("label") or "").strip()
                    if _dl:
                        _duo_label = _dl
            if _catchup_pct:
                m["ctx_marks"] = {t for t in (50, 75, 90)
                                  if _catchup_pct >= t}

            _note = (f"📢 Engine run taken over (mirror): {rid}\n"
                     "Your texts are queued (steering), approvals come "
                     "to you here. /stop aborts it.")
            if len(j.get("frames") or []) > 80:
                _note = ("📢 Re-attached to engine run "
                         f"{rid} (gateway restarted) - mid-run context "
                         "was rebuilt silently.\n"
                         "Steering queued, approvals come here. "
                         "/stop aborts it.")
            if _duo_label:
                _note += "\n🤖 " + _duo_label[:200]
            await self.ms.send_message(_note)
            if _catchup_comp and _catchup_lim:
                await self.ms.send_message(
                    "🗜 Context is compressing "
                    f"(~{_catchup_pct}% of {_catchup_lim // 1024}k) - "
                    "started before the takeover.")
            m["after"] = int(j.get("n") or 0)
            return  # next tick streams the delta

        # SECOND-RUN VISIBILITY (2026-10-06, audit T2): while bound to one
        # run the mirror never looks at another one - a second engine run
        # used to run entirely unseen on the phone. Peek the unscoped
        # journal (most-recently-active) once per tick; when a DIFFERENT
        # active run surfaces, say so once per foreign run id.
        if m["run_id"]:
            try:
                _j_all = await self.hive.journal()
                _other = str((_j_all or {}).get("run_id") or "")
                if (_other and _other != m["run_id"]
                        and _other != str(self._own_run_id() or "")
                        and (_j_all or {}).get("active")
                        and not (_j_all or {}).get("done")
                        and not (_j_all or {}).get("aborted")
                        and m.get("other_run_noted") != _other):
                    m["other_run_noted"] = _other
                    await self.ms.send_message(
                        f"ℹ️ Another engine run is active ({_other[:18]}…) - "
                        f"still mirroring {m['run_id'][:18]}… (only one run "
                        "mirrors at a time).")
            except (HiveUnreachable, OSError, ValueError, TypeError,
                    KeyError, TelegramApiError):
                pass  # the mirrored run's own relay continues regardless

        # delta frames since last tick (TG-AUDIT 2026-10-07): `n` is the
        # LOGICAL frame count and `base` the window offset once the
        # engine's 20k-frame cap sliced old frames away. The old
        # len(frames)-tracking went BLIND at the cap: the length stayed
        # pinned while logical frames kept flowing, so every delta - tool
        # lines, statuses, the fazit answer - was lost forever.
        frames = j.get("frames") or []
        _base = int(j.get("base") or 0)
        after = int(m["after"] or 0)
        _start = after - _base
        new_frames = frames[max(0, _start):] if _start < len(frames) else []
        m["after"] = int(j.get("n") or (_base + len(frames)))
        done_reason = self._parse_done(new_frames)
        last_status = ""
        tool_lines: list[str] = []
        # R4 relay: phase/plan/thinking visibility on the phone
        tick_think = ""
        last_phase = m.get("last_phase", "")
        for raw in new_frames:
            if not isinstance(raw, str):
                continue
            try:
                ev = json.loads(raw[6:]) if raw.startswith("data: ") else {}
            except ValueError:
                continue
            if ev.get("type") == "status":
                last_status = str(ev.get("content") or "")[:120]
            elif ev.get("type") == "token":
                # FAZIT RELAY (2026-10-06, owner report): the takeover never
                # delivered the run's RESULT - the phone watched status lines
                # and then got a bare "🏁 completed". Accumulate the answer
                # tokens; an agent event starts a new phase, so only the LAST
                # phase's tokens survive (planner/critic tokens use their own
                # event types and never land here).
                m["answer_parts"].append(str(ev.get("content") or ""))
            elif ev.get("type") == "agent":
                if m["answer_parts"]:
                    m["answer_parts"] = []
            elif ev.get("type") == "steer":
                # STEER CARD (2026-10-06): relay the takeover run's steer
                # pickups as their own phone message.
                _st_txt = str(ev.get("content") or "").strip()
                if _st_txt or ev.get("images"):
                    _note = "🧭 Steer picked up: " + (_st_txt[:300] or "(image)")
                    if ev.get("images"):
                        _note += f" (+{ev.get('images')} img)"
                    await self.ms.send_message(_note)
            elif ev.get("type") == "agent":
                # R4: phase switches (Planner / Code / Answer) as own lines
                _ph = str(ev.get("content") or "").strip()
                if _ph and _ph != last_phase:
                    await self.ms.send_message(f"→ {_ph}")
                    last_phase = _ph
            elif ev.get("type") == "planner_result":
                # R4: the plan itself (the most informative moment of a duo
                # run) - compact single-line preview
                _plan = str(ev.get("content") or "")
                if not _plan and isinstance(ev.get("chunks"), list):
                    _plan = "".join(str(c) for c in ev["chunks"])
                _plan = " ".join(_plan.split())[:350]
                if _plan:
                    await self.ms.send_message(f"🧠 Plan: {_plan}")
            elif ev.get("type") == "duo_round":
                _rn = ev.get("n")
                _rt = ev.get("total")
                _st = str(ev.get("subtask") or "").strip()
                _msg = f"⚙️ Coder round {_rn}/{_rt}"
                if _st:
                    _msg += f": {_st[:110]}"
                await self.ms.send_message(_msg)
            elif ev.get("type") == "duo_start":
                # MODEL VISIBILITY (2026-10-07, owner): WHICH model with
                # which settings runs - the label carries model, VRAM, ctx.
                _lbl = str(ev.get("label") or "").strip()
                if _lbl:
                    await self.ms.send_message("🤖 " + _lbl[:200])
            elif ev.get("type") == "ctx_meter":
                # CONTEXT/COMPRESSION VISIBILITY (owner: "compression etc
                # sichtbar"): milestone lines at 50/75/90 % (once each) and
                # a one-shot note while the engine compresses context.
                try:
                    _est = int(ev.get("est_tokens") or 0)
                    _lim = int(ev.get("ctx_limit") or 0)
                except (TypeError, ValueError):
                    _est = _lim = 0
                if _est > 0 and _lim > 0:
                    _pct = _est * 100 // _lim
                    _marks = m.setdefault("ctx_marks", set())
                    for _th in (50, 75, 90):
                        if _pct >= _th and _th not in _marks:
                            _marks.add(_th)
                            await self.ms.send_message(
                                f"🧮 Context {_pct}% "
                                f"({_est // 1024}k/{_lim // 1024}k tokens)")
                    _comp = bool(ev.get("compressing"))
                    if _comp and not m.get("ctx_compression_noted"):
                        # once per RUN (the flag can flip every frame while
                        # the engine hovers at the limit - no re-notes)
                        m["ctx_compression_noted"] = True
                        await self.ms.send_message(
                            "🗜 Context compressing (older turns are being "
                            "summarized to stay under the limit)…")
            elif ev.get("type") == "file_change":
                # FILE ACTIVITY (owner): what the coder actually writes.
                _fp = str(ev.get("path") or "").replace("\\", "/").split("/")[-1]
                _op = str(ev.get("op") or "write")
                _ln = ev.get("lines")
                _fc = f"📝 {_fp} ({_op}" + (f", {_ln} lines)" if _ln else ")")
                if not tool_lines or tool_lines[-1] != _fc:
                    tool_lines.append(_fc)
            elif ev.get("type") == "usage_meta":
                # CODER OUTPUT (owner: real coder text instead of a bare
                # "round finished"): when a coder LLM call ends, relay the
                # free text it produced since the last relay (token events
                # only - tool-call payloads are separate events).
                if str(ev.get("phase") or "") == "coder":
                    _parts = m.get("answer_parts") or []
                    _upto = int(m.get("answer_sent_upto") or 0)
                    _delta = "".join(str(x) for x in _parts[_upto:]).strip()
                    if len(_delta) >= 80:
                        m["answer_sent_upto"] = len(_parts)
                        # AUDIT FIX (1.3.3 release audit): the fazit path
                        # filters secrets BEFORE sending - the mid-run
                        # coder relay must not leak past that guard.
                        _txt = " ".join(
                            render.filter_secrets(_delta).split())
                        await self.ms.send_message(
                            "💬 " + _txt[:700] + ("…" if len(_txt) > 700 else ""))
            elif ev.get("type") == "thinking_token":
                # R4: coder reasoning between tool calls - keep a tail, sent
                # with the next tool line (one 💭 per tick max)
                tick_think += str(ev.get("content") or "")
            elif ev.get("type") == "tool_call":
                if tick_think.strip():
                    _tt = " ".join(tick_think.split())[-160:]
                    await self.ms.send_message(f"💭 …{_tt}")
                    tick_think = ""
                # Tool-Call-Relay (2026-10-05) + REPEAT-DEDUPE (R6, live
                # flood 10:21): identical lines (same tool, same args -
                # model retry loops) relay once, then a compact counter
                # every 5th repeat. Cross-tick via the mirror state.
                line = self._tool_line(ev)
                if not line:
                    continue
                _seen = m.setdefault("tool_relay_seen", {})
                if len(_seen) > 40:
                    _seen.clear()
                _cnt = _seen.get(line, 0) + 1
                _seen[line] = _cnt
                if _cnt == 1:
                    tool_lines.append(line)
                elif _cnt % 5 == 0:
                    tool_lines.append(f"{line} (repeat #{_cnt})")
        for tl in tool_lines[-6:]:  # cap per tick, newest win
            await self.ms.send_message(tl)
        if last_status and last_status != m["last_status"] \
                and (_now() - m["last_tick"]) >= self.cfg.status_min_interval_s:
            if m["status_msg_id"] is None:
                res = await self.ms.send_message(f"⏳ {last_status} …")
                m["status_msg_id"] = res.get("message_id")
            else:
                await self._edit_status(m["status_msg_id"],
                                        f"⏳ {last_status} …")
            m["last_status"] = last_status
            m["last_tick"] = _now()
        m["last_phase"] = last_phase  # F5: cross-tick dedupe state
        if done_reason:
            # FAZIT RELAY (2026-10-06, R6-hardened): deliver what the run
            # PRODUCED, not just the fact that it ended. Empty accumulator
            # falls back to the full journal frames (late adoption, phase
            # switches) so the phone always gets the final answer.
            _ans = "".join(m.get("answer_parts") or []).strip()
            if not _ans:
                _ans, _ = self._accumulate_frames(frames)
            self._mirror_reset(m)
            await self._mirror_fazit(rid, _ans, done_reason)
            return

        # approval relay (the point of the takeover)
        pend = await self.hive.pending_approval(rid)
        body = pend.json() if hasattr(pend, "json") else {}
        if getattr(pend, "status_code", 200) == 200 and body.get("active"):
            sig = f"{body.get('decision_id') or ''}|{body.get('preview') or ''}"
            if sig != m["approval_sig"]:
                m["approval_sig"] = sig
                # G2: remember which card is on the phone, so the phone
                # decision echoes decision_id + tool and the engine can
                # drop duplicates / tool mismatches.
                m["approval_decision_id"] = str(body.get("decision_id") or "")
                m["approval_tool"] = str(body.get("tool") or "")
                m["approval_kind"] = str(body.get("kind") or "approval")
                # deep audit N2: the pending preview is truncated to 300
                # chars by the engine; the full command lives in the
                # journal's tool_call frames - dig it out so the owner
                # never approves blind.
                full = self._full_command_from_frames(
                    j.get("frames"), str(body.get("tool") or ""))
                preview = full or (str(body.get("preview") or "") + "\n"
                                   "(server preview truncated - full "
                                   "command in the UI)")
                if len(preview) > 2500:
                    preview = preview[:2500] + " …"
                if m["approval_kind"] == "ask":
                    # ASK-USER ON THE PHONE (2026-10-06, owner): an agent
                    # question is relayed as a TEXT card - the reply IS the
                    # answer (free text beats 1/3 for open questions).
                    # YES/NO BUTTONS (2026-10-07, owner: "nicht YES
                    # abtippen"): a yes/no-shaped question (destructive
                    # gate) gets tappable buttons; free text still wins
                    # for own commentary.
                    _ask_low = preview.lower()
                    if "yes" in _ask_low and "no" in _ask_low and (
                            "confirm" in _ask_low or "reply with" in _ask_low):
                        await self.ms.send_message(
                            f"❓ The agent asks (run {rid})\n\n"
                            f"{preview}\n\n"
                            "Buttons below - or type your own answer as "
                            "text (e.g. 'yes, but skip the force flag').\n"
                            "(This is the agent's own question - NOT the "
                            "approval gate.)",
                            reply_markup={"inline_keyboard": [
                                [{"text": "✅ Yes",
                                  "callback_data": f"ask:{rid}:yes"},
                                 {"text": "⛔ No",
                                  "callback_data": f"ask:{rid}:no"}],
                                [{"text": "⏳ Later",
                                  "callback_data": f"ask:{rid}:later"}]]})
                    else:
                        await self.ms.send_message(
                            f"❓ The agent asks (run {rid})\n\n"
                            f"{preview}\n\n"
                            "Reply with your answer as text - it goes "
                            "straight back to the agent.\n"
                            "(This is the agent's own question - NOT the "
                            "approval gate.)")
                else:
                    await self.ms.send_message(
                        f"🛡 Approval needed (UI run {rid})\n\n"
                        f"tool: {body.get('tool')}\n\n"
                        f"{preview}\n\n"
                        "Buttons below; or reply 1 (once) / 3 (deny) as text.\n"
                        "'2' remembers EXACTLY this call for this chat.",
                        reply_markup={
                            "inline_keyboard": [[
                                {"text": "✅ Once",
                                 "callback_data": f"appr:{rid}:1"},
                                {"text": "📁 Always (chat)",
                                 "callback_data": f"appr:{rid}:2"},
                                {"text": "⛔ Deny",
                                 "callback_data": f"appr:{rid}:3"},
                            ]]})
        elif m["approval_sig"] and not body.get("active"):
            # R5 (owner): show WHAT happened, not just that the card
            # closed. Evidence in the journal tail: a fail-closed timeout
            # denies with a status line; an approved call leaves a
            # tool_result for the same tool.
            _appr_tool = str(body.get("tool") or "")
            _tail = [f for f in (j.get("frames") or [])[-40:]
                     if isinstance(f, str)]
            _timed_out = any("DENIED (fail closed)" in f for f in _tail)
            _ran = any('"type": "tool_result"' in f and _appr_tool in f
                       for f in _tail)
            if _timed_out:
                await self.ms.send_message(
                    "🛡 The call was auto-DENIED (no answer within the "
                    "timeout, fail-closed). The model picks another way.")
            elif _ran and _appr_tool:
                await self.ms.send_message(
                    f"✅ Approved in the UI: {_appr_tool} ran on the PC.")
            else:
                await self.ms.send_message(
                    "ℹ️ Card closed (answered in the UI, or timed out).")
            m["approval_sig"] = None

    async def _mirror_fazit(self, rid: str, answer: str, reason: str) -> None:
        """FAZIT RELAY (2026-10-06): deliver what the run PRODUCED, not
        just the fact that it ended. Chunked messages, .txt document past
        the chunk cap, transcript persistence for handed-over chats;
        marks the run relayed so the late-result path never re-sends."""
        _ans = render.filter_secrets((answer or "").strip())  # R3: filter
        # BEFORE split - per-chunk filtering missed secrets straddling
        # hard-split boundaries (each half matched no pattern alone)
        text = STOP_REASON_TEXT.get(
            reason, f"🏁 Mirror run finished ({reason}).")
        if reason == "completed":
            text = "🏁 Mirror run completed."
        _full = (_ans + "\n\n" + text) if _ans else text
        _chunks = render.split_message(_full)
        if len(_chunks) > FINAL_CHUNK_LIMIT:
            await self.ms.send_document(
                _ans.encode("utf-8"), "mirror_result.txt")
            await self.ms.send_message(text)
        else:
            for _c in _chunks:
                await self.ms.send_message(_c)
        # PHONE-RUNS-IN-UI: persist the answer into the chat transcript
        # so the run exists in the UI, not only on the phone.
        _ho_chat = self._handed_over.pop(rid, None)
        if _ho_chat and _ans:
            try:
                await self._write_turn(_ho_chat, {
                    "role": "assistant", "content": _ans})
            except (HiveUnreachable, OSError, HTTPError) as _tw:
                self._log_note(f"handover transcript write failed: {_tw}")
        self._late_relayed.add(rid)

    @staticmethod
    def _accumulate_frames(frames: list) -> tuple[str, str]:
        """R6 late relay: accumulate (answer, last phase) from journal
        frames without any sends - used for runs that finished while the
        mirror was not attached."""
        parts: list[str] = []
        phase = ""
        for raw in frames or []:
            if not isinstance(raw, str):
                continue
            try:
                ev = json.loads(raw[6:] if raw.startswith("data: ") else raw)
            except ValueError:
                continue
            et = ev.get("type")
            if et == "token":
                parts.append(str(ev.get("content") or ""))
            elif et == "agent" and parts:
                parts = []  # phase switch: last phase wins
            elif et == "phase":
                phase = str(ev.get("content") or phase)
        return "".join(parts).strip(), phase

    @staticmethod
    def _full_command_from_frames(frames: list, tool: str) -> str:
        """Last tool_call event for `tool` in the journal frames, with
        the untruncated command from its extra (sse/events.py keeps
        extra['cmd'] whole). Empty string when not found."""
        for raw in reversed(frames or []):
            if not isinstance(raw, str) or '"type": "tool_call"' not in raw:
                continue
            try:
                ev = json.loads(raw[6:] if raw.startswith("data: ") else raw)
            except ValueError:
                continue
            if ev.get("type") != "tool_call":
                continue
            if tool and ev.get("name") != tool:
                continue
            extra = ev.get("extra") or {}
            for key in ("cmd", "path", "url", "query", "code", "package"):
                v = extra.get(key) or ev.get("label")
                if isinstance(v, str) and v.strip():
                    return f"{ev.get('name', tool)}: {v}"
        return ""

    def mirror_intercept(self, text: str) -> bool:
        """True = this text belongs to the mirrored engine run (approval
        answer or steering); the caller must await mirror_send(text)
        instead of starting a run."""
        m = self._mirror()
        return bool(m.get("run_id")) and bool((text or "").strip())

    async def approval_callback(self, callback_id: str, data: str) -> str:
        """Tappable approval buttons (2026-10-05): callback_data is
        'appr:<rid>:<1|2|3>' from a card's inline keyboard - mirrored UI
        runs AND own phone runs (ask mode). The decision rides the SAME
        path as text answers (decision_id + tool echoed for the engine's
        duplicate/tool guards); '2' uses the engine's chat-scoped
        exact-call memory. Returns the toast text for
        answerCallbackQuery."""
        m = self._mirror()
        try:
            _, rid, answer = data.split(":", 2)
        except ValueError:
            return "malformed button"
        if data.startswith("ask:"):
            # ASK BUTTONS (2026-10-07, owner: "yes/no als buttons, nicht
            # YES abtippen"): yes/no cards (destructive gate + agent
            # yes/no questions) ride the SAME decide_approval path as a
            # text answer; decision_id/tool from the mirrored card guard
            # duplicates. Free text stays possible alongside the buttons.
            # 'later' (owner, 2nd round) does NOT consume the card: the
            # run keeps waiting and the free-text answer stays open.
            if answer.startswith("l"):
                return "⏳ later - the run waits; answer with text anytime"
            ask_ans = "yes" if answer.startswith("y") else "no"
            if rid != m.get("run_id") or not m.get("approval_sig"):
                return "card already gone"
            resp = await self.hive.decide_approval(
                rid, ask_ans,
                decision_id=m.get("approval_decision_id") or "",
                tool=m.get("approval_tool") or "")
            code = getattr(resp, "status_code", 500)
            m["approval_sig"] = None
            if code >= 300:
                return f"not accepted (HTTP {code})"
            try:
                routed = str((resp.json() or {}).get("routed") or "")
            except (ValueError, TypeError, AttributeError):
                routed = ""
            if routed == "duplicate":
                return "already answered (UI) - nothing changed"
            if routed == "expired":
                return "⏱ card already timed out - denied (fail closed)"
            if routed == "stale":
                return "ℹ️ card outdated - nothing changed"
            try:
                await self.ms.send_message(
                    "✅ Confirmed (yes) - answer delivered." if ask_ans == "yes"
                    else "⛔ Declined (no) - answer delivered.")
            except (TelegramApiError, OSError):
                pass  # the decision itself is delivered
            return f"{ask_ans} - delivered"
        if answer not in ("1", "2", "3"):
            return "unknown button"
        own = self._open_own_approval or {}
        if rid == own.get("rid"):
            resp = await self.hive.decide_approval(
                rid, answer,
                decision_id=own.get("decision_id") or "",
                tool=own.get("tool") or "")
            code = getattr(resp, "status_code", 500)
            self._open_own_approval = None
            self.state.data.setdefault("open_approvals", {}).pop(rid, None)
            self.state.save()
            if code >= 300:
                return f"not accepted (HTTP {code})"
            try:
                routed = str((resp.json() or {}).get("routed") or "")
            except (ValueError, TypeError, AttributeError):
                routed = ""
            if routed == "duplicate":
                return "already answered - nothing changed"
            if routed == "expired":
                # TOAST-HONESTY (2026-10-06): the engine discarded this
                # decision (fail-closed timeout already denied the call) -
                # "delivered" would be a lie.
                return "⏱ card already timed out - the call was denied (fail closed)"
            if routed == "stale":
                return "ℹ️ That card is outdated (a newer one replaced it) - nothing changed."
            if answer == "2":
                return "always (this chat, this exact call) - delivered"
            return "allowed (once) - delivered" if answer == "1" \
                else "denied - delivered"
        if rid != m.get("run_id") or not m.get("approval_sig"):
            return "card already gone"
        resp = await self.hive.decide_approval(
            rid, answer,
            decision_id=m.get("approval_decision_id") or "",
            tool=m.get("approval_tool") or "")
        code = getattr(resp, "status_code", 500)
        m["approval_sig"] = None
        if code >= 300:
            return f"not accepted (HTTP {code})"
        try:
            routed = str((resp.json() or {}).get("routed") or "")
        except (ValueError, TypeError, AttributeError):
            routed = ""
        if routed == "duplicate":
            return "already answered (UI) - nothing changed"
        if routed == "expired":
            # TOAST-HONESTY (2026-10-06): see approval_callback - the engine
            # discarded this decision, never claim "delivered".
            return "⏱ card already timed out - the call was denied (fail closed)"
        if routed == "stale":
            return "ℹ️ That card is outdated (a newer one replaced it) - nothing changed."
        if answer == "2":
            return "always (this chat, this exact call) - delivered"
        return "allowed (once) - delivered" if answer == "1" \
            else "denied - delivered"

    async def mirror_send(self, text: str) -> str:
        """Route a phone message into the mirrored run: while an APPROVAL
        card is open, 1/3 answers it ('2'/always does not exist from the
        phone); while an ASK-USER pause is open, ANY text is the answer
        (free text beats yes/no for open questions); otherwise steering."""
        m = self._mirror()
        rid = m.get("run_id")
        _pending_kind = ""
        _pending_did = ""
        if m.get("approval_sig"):
            try:
                _pk = await self.hive.pending_approval(rid)
                _pk_body = _pk.json() if hasattr(_pk, "json") else {}
                if _pk_body.get("active"):
                    _pending_kind = str(_pk_body.get("kind") or "approval")
                    _pending_did = str(_pk_body.get("decision_id") or "")
            except (HiveUnreachable, OSError, ValueError):
                _pending_kind = ""
        if m.get("approval_sig"):
            t = (text or "").strip()
            _kind_now = _pending_kind or (m.get("approval_kind") or "")
            if _kind_now == "ask" and t not in ("1", "3"):
                # ASK-USER FREE TEXT (2026-10-06, owner): the phone answer
                # IS the reply to the agent's question. F3 (audit r3): the
                # LIVE decision_id from the fetched pending body is used,
                # not the relay-time one; a stale card routes "stale" and
                # says so instead of claiming delivery.
                resp = await self.hive.decide_approval(
                    rid, t,
                    decision_id=_pending_did or m.get("approval_decision_id") or "",
                    tool=m.get("approval_tool") or "")
                code = getattr(resp, "status_code", 500)
                m["approval_sig"] = None
                if code >= 300:
                    return (f"❌ Answer not accepted (HTTP {code}) - maybe "
                            "answered in the UI.")
                try:
                    _routed = str((resp.json() or {}).get("routed") or "")
                except (ValueError, TypeError, AttributeError):
                    _routed = ""
                if _routed == "stale":
                    return ("ℹ️ That card is outdated (a newer one "
                            "replaced it) - nothing changed.")
                return ("✅ Answer delivered to the agent: " + t[:200])
            if t == "2":
                return ("❌ '2' (always allow) deliberately does not "
                        "exist from the phone - 1 or 3.")
            if t in ("1", "3"):
                resp = await self.hive.decide_approval(
                    rid, t,
                    decision_id=m.get("approval_decision_id") or "",
                    tool=m.get("approval_tool") or "")
                code = getattr(resp, "status_code", 500)
                m["approval_sig"] = None
                if code >= 300:
                    return (f"❌ Decision not accepted "
                            f"(HTTP {code}) - maybe answered in the UI.")
                try:
                    routed = str((resp.json() or {}).get("routed") or "")
                except (ValueError, TypeError, AttributeError):
                    routed = ""
                if routed == "duplicate":
                    # G2: the UI already answered this card - be honest
                    # instead of confirming a decision that was dropped.
                    return ("ℹ️ This card was already answered (UI) - "
                            "nothing changed.")
                if routed == "expired":
                    return ("⏱ card already timed out - the call was "
                            "denied (fail closed).")
                if routed == "stale":
                    return ("ℹ️ That card is outdated (a newer one "
                            "replaced it) - nothing changed.")
                return ("✅ allowed (once) - delivered."
                        if t == "1" else "🛡 denied - delivered.")
            return ("🛡 An approval is waiting - reply 1 or 3 "
                    "(or answer it in the UI).")
        # audit G8: steering bypassed the max_text_chars cap (the intercept
        # runs before the run-start length check) - cap at the same limit.
        text = (text or "").strip()[: self.cfg.max_text_chars]
        resp = await self.hive.steer(rid, text)
        if getattr(resp, "status_code", 500) == 404:
            return (f"❌ Run {rid} is no longer active - the mirror "
                    "ends on the next tick.")
        return ("🧭 Queued - injected at the next boundary "
                "(mode dependent; pipeline accepts no steering).")

    async def gate_text(self, arg: str) -> str:
        """Chat control of the phone approval policy (ask|deny|off) plus
        the MID-RUN TOGGLE (R4, owner): /gate on|off flips the ENGINE-WIDE
        duo_action_approval_enabled - that key is read per gated call, so
        a mirrored UI run picks it up live (cards start/stop mid-run).
        Posts EXACTLY one gateway-owned key to /settings - the blanket
        'never touch POST /settings' taboo is amended for this single
        surgical write by owner decision 2026-10-05 (the UI select writes
        the same key)."""
        arg = (arg or "").strip().lower()
        if arg in ("on", "off"):
            # R5 (audit): explicit semantics - on = approvals everywhere
            # (engine-wide toggle + phone ask), off = everywhere off. The
            # phone policy keys ride along so both surfaces stay consistent.
            _on = arg == "on"
            try:
                await self.hive.set_setting("duo_action_approval_enabled", _on)
                await self.hive.set_setting(
                    "telegram_approval_mode", "ask" if _on else "off")
            except (HiveUnreachable, OSError) as exc:
                return f"🔌 could not set: {exc}"
            if hasattr(self.ms, "telegram_approval_mode"):
                self.ms.telegram_approval_mode = "ask" if _on else "off"
            # OWNER OVERRIDE (2026-10-07, live "gate ausschalten am handy
            # verhält sich komisch"): the chat command is the owner's voice
            # - it lifts the gateway.toml force_approval_gate floor as
            # well. Without this, /gate off degraded phone runs to DENY
            # (floor re-pinned the gate: no questions, but shell/write/git
            # silently refused) while the chat claimed approvals were off.
            # Persisted in the gateway state; /gate ask|deny does NOT touch
            # it, and a fresh install falls back to the toml default.
            self.cfg = dataclasses.replace(
                self.cfg, force_approval_gate=_on)
            try:
                self.state.data["gate_force"] = _on
                self.state.save()
            except (OSError, AttributeError, TypeError):
                pass  # runtime override still active for this process
            return ("🛡 Approvals " + ("ON - approval cards for gated calls, "
                    "everywhere (UI + phone); phone runs gated again."
                    if _on else
                    "OFF - gated calls run without asking, UI runs included "
                    "(live, mid-run). Careful. Phone-STARTED runs are "
                    "ungated too (the safety force is lifted)."))
        arg = (arg or "").strip().lower()
        if arg in ("ask", "deny", "off"):
            try:
                await self.hive.set_setting("telegram_approval_mode", arg)
            except (HiveUnreachable, OSError) as exc:
                return f"🔌 could not set: {exc}"
            if hasattr(self.ms, "telegram_approval_mode"):
                self.ms.telegram_approval_mode = arg
            why = {
                "ask": "gated calls wait for YOUR tap (1 once / "
                       "2 always-this-chat / 3 deny).",
                "deny": "gated calls are auto-denied (safe default).",
                "off": "no gate force - the engine global toggle "
                       "decides (ungated when it is off).",
            }[arg]
            return (f"🛡 Approval mode (phone runs): {arg}\n\n"
                    f"  • {why}\n\n"
                    "Applies from the very next run. The UI select shows "
                    "the same setting.")
        cur = getattr(self.ms, "telegram_approval_mode", None)
        cur_txt = cur if cur in ("ask", "deny", "off") else "deny (default)"
        _floor = ("safety force ON" if self.cfg.force_approval_gate
                  else "safety force lifted by /gate")
        return ("🛡 Approval mode (phone runs): " + cur_txt + "\n"
                + _floor + "\n\n"
                "  • ask - gated calls wait for YOUR tap\n"
                "  • deny - gated calls are auto-denied (safe default)\n"
                "  • off - no gate force (engine global toggle decides)\n\n"
                "Set with /gate ask|deny|off; /gate on|off is the master.")

    async def workspace_text(self, arg: str) -> str:
        """/workspace <path> - set the workspace of the [TG] chat so
        phone runs execute there (chat workspace wins at resolve time)."""
        path = (arg or "").strip().strip('"').strip("'")
        if not path:
            current = (self._tg_chat() or {}).get("workspace", "")
            label = current or "- not set (engine default)"
            return (f"Workspace (Telegram): {label}.\n"
                    "Set with /workspace <path>.")
        # audit G11 / deep-audit N9: the chat workspace IS the tool
        # confinement root for phone runs - a typo must not silently
        # re-point the agent. The gateway runs on the same machine, so a
        # local existence check is authoritative here (POST /settings does
        # the same for its workspace key). Drive roots are refused: with
        # G1's gate force they would be safe-ish, but "agent rooted at C:\"
        # is never what a typo meant.
        from pathlib import Path as _Path
        _p = _Path(path)
        if not _p.exists():
            return (f"❌ Path does not exist: {path}\n"
                    "/workspace needs an existing folder.")
        if _p.parent == _p:
            return (f"❌ Drive root ({path}) is deliberately not "
                    "allowed - please name a concrete folder.")
        mapping = await self._ensure_chat()
        chat_id = mapping["hive_chat_id"]
        r = await self.hive.get_chat(chat_id)
        r.raise_for_status()
        chat = r.json()
        rev = int(chat.get("rev") or 0)
        for _attempt in (0, 1):
            resp = await self.hive.put_chat_meta(
                chat_id, {"workspace": path, "base_rev": rev})
            if resp.status_code == 409:
                body = resp.json()
                rev = int(body.get("rev") or 0)
                continue
            resp.raise_for_status()
            mapping["workspace"] = path
            self.state.data["tg_chat"] = mapping
            self.state.save()
            return (f"📁 Workspace (Telegram) set: {self._short_path(path)}\n"
                    "Phone runs now work there - the agent can read "
                    "from this folder. /workspace without an argument "
                    "shows the current path.")
        raise HiveUnreachable("workspace write conflict (409 twice)")

    def tools_text(self, arg: str) -> str:
        """/tools on|off - per-run direct tools access level (rides the
        run body; needs the engine lift to take effect)."""
        a = (arg or "").strip().lower()
        cur = self.state.data.get("tools")
        cur_txt = ("on" if cur is True else "off" if cur is False
                   else "engine default")
        if not a:
            return (f"Access level (direct, Telegram): {cur_txt}.\n"
                    "Set with /tools on or /tools off.")
        if a in ("on", "an", "1"):
            self.state.data["tools"] = True
        elif a in ("off", "aus", "0"):
            self.state.data["tools"] = False
        else:
            return "❌ /tools on oder /tools off."
        self.state.save()
        new = self.state.data["tools"]
        return (f"Access level (direct, Telegram): "
                f"{'on' if new else 'off'} - per run "
                "(engine lift required, see P10/T).")

    async def stop_mirror(self) -> str | None:
        """Abort the mirrored engine run, if any. Returns the note or
        None when no mirror session is active."""
        m = self._mirror()
        rid = m.get("run_id")
        if not rid:
            return None
        try:
            resp = await self.hive.abort_run(rid)
            code = getattr(resp, "status_code", 500)
        except (HiveUnreachable, OSError) as exc:
            return (f"❌ /stop for mirror run {rid} failed "
                    f"({exc}) - may still be running, check the PC!")
        self._mirror_reset(m)
        if code == 404:
            return (f"⏹ Mirror run {rid} was unknown to the server - "
                    "mirror ended.")
        return f"⏹ Abort sent for mirror run {rid}."
