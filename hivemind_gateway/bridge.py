"""WP2 — the text-run bridge: Telegram message -> HiveMind run -> phone.

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
  6. clear active_run — ALWAYS, also on failure (finally)

Every outbound byte goes through the messenger, which routes through
send() (the owner choke point).
"""
from __future__ import annotations

import json
import logging
import time

from httpx import HTTPError

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

# Run modes accepted via /mode (the /stream BODY field — the browser
# UI's own mode in settings stays untouched). Friendly aliases map to
# the engine's names (index.html mode buttons). "agentic" is a phone-side
# COMPOSITE: engine mode "auto" + duo_agentic_mode body flag (the engine
# reads that key straight from the /stream body).
MODE_CHOICES = ("auto", "agentic", "simple", "pipeline", "automap")
MODE_ALIASES = {"chat": "simple", "direct": "simple"}
_MODE_DESCRIPTIONS = {
    "auto": "duo agent with full tools (files/shell/git) — use for tasks",
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
        caught as belt-and-braces — hive_client normally wraps transport
        failures into HiveUnreachable (realrun bug #4)."""
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
        plain text — formatting must never cost the answer itself."""
        try:
            await self.ms.send_message(
                render.md_to_telegram_html(plain), parse_mode="HTML")
        except TelegramApiError as exc:
            self._log_note(f"HTML send rejected ({exc}) — resending plain")
            await self.ms.send_message(plain)

    def _clear_run(self) -> None:
        if self.state.data.get("active_run"):
            self.state.set_active_run(None)
            self.state.save()

    async def _start_text_run_inner(self, q: str) -> str:
        if self.state.data.get("active_run"):
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

        # info-rich start note (2026-10-05 UX round): mode/model/workspace
        # up front, so the owner sees WHAT will run before tokens arrive
        _ws = (self._tg_chat() or {}).get("workspace") or "engine default"
        _ws_short = (_ws.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                     if _ws != "engine default" else _ws)
        _sel_label = (f"{self.state.data.get('mode')} (phone override)"
                      if self.state.data.get("mode")
                      else "engine default")
        status = await self.ms.send_message(
            "⏳ Run started\n\n"
            f"mode: {_sel_label}\n"
            "model: "
            + ((self.state.data.get("run_overrides") or {}).get("model")
               or "engine default") + "\n"
            f"workspace: {_ws_short}")
        status_id = status.get("message_id")

        parts: list[str] = []
        error_text = ""
        stop_reason = ""
        last_status = ""
        last_edited_text = ""
        last_edit = _now()
        denied = 0

        # UI toggle telegram_phone_restricted (2026-10-05): restrict this
        # run to web + read-only tools. Read FRESH per run, so flipping
        # the toggle applies from the very next message — no restart.
        # DEFAULT ON (safe by default): a missing key restricts; only an
        # explicit false (toggle off in the UI) unlocks phone runs.
        _restricted = True
        try:
            _s = await self.hive.settings()
            _restricted = bool(_s.get("telegram_phone_restricted", True))
        except (HiveUnreachable, OSError, ValueError):
            pass  # engine offline: the run fails anyway, stay restricted
        try:
            ov = self._stream_overrides()
            if _restricted:
                ov["phone_restricted"] = True
            sel_mode = self.state.data.get("mode") or ""
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
                    if run_id:
                        resp = await self.hive.decide_approval(
                            run_id, "3",
                            decision_id=str(ev.get("decision_id") or ""),
                            tool=str(ev.get("tool") or ""))
                        if getattr(resp, "status_code", 200) < 300:
                            denied += 1
                            await self._edit_status(
                                status_id,
                                "🔒 Approval request auto-denied "
                                "(WP2: approvals from the phone arrive with WP4).")
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
        finally:
            # audit G5: _finish talks to Telegram AND the engine — a failure
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
                    f"finish failed ({type(exc).__name__}: {exc}) — run "
                    "cleared anyway")
                try:
                    await self._fail("❌ Result could not be delivered — "
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
        return (f"⏳ A run is already active ({rid}) — probably started "
                "from the browser/UI. There is deliberately no "
                "queue.")

    # -- commands -----------------------------------------------------------

    async def stop(self) -> str:
        run = self.state.data.get("active_run") or {}
        rid = run.get("run_id")
        if not rid:
            return "No run active."
        try:
            resp = await self.hive.abort_run(rid)
            code = getattr(resp, "status_code", 500)
            if code == 404:
                # 2026-10-05 (live): 404 = the engine no longer knows this
                # run (it already ended). Clear the latch EITHER WAY — a
                # failed fallback must not leave active_run latched, or
                # every further text gets the busy note until restart.
                # The run's own chat_id is the reliable fallback target
                # (tg_chat can be absent after a state wipe).
                self._clear_run()
                chat_id = ((self._tg_chat() or {}).get("hive_chat_id")
                           or run.get("chat_id") or "")
                if chat_id:
                    await self.hive.abort_chat(chat_id)
                    return (f"⏹ Run {rid} was unknown to the server — "
                            "chat abort sent as fallback; run cleared.")
                return (f"⏹ Run {rid} was unknown to the server — it "
                        "had already ended; run cleared.")
            return f"⏹ Abort sent for {rid}."
        except HTTPError as exc:
            # deep audit N6: raise_for_status in the fallback must not
            # break the visible-failure invariant of /stop
            return (f"❌ /stop failed ({exc}) — run {rid} may still "
                    "be running, please check the PC!")
        except (HiveUnreachable, OSError) as exc:
            # /stop must fail VISIBLY: an orphaned run holds VRAM
            return f"❌ /stop failed ({exc}) — run {rid} may still " \
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
        # audit G1 + version-skew detection: state the invariant plainly —
        # the gateway forces the approval gate for every phone run, but
        # ONLY if the engine lifts the body key (gateway_overrides marker).
        if getattr(self.ms, "engine_gate_support", True):
            gate_line = ("approvals: shell/write/git auto-denied on phone "
                         "runs (gate enforced)")
        else:
            gate_line = ("⚠️ gate enforcement INACTIVE — engine too old; "
                         "switch on duo_action_approval_enabled in the UI")
        return (
            "📡 Gateway status\n"
            "\n"
            f"chat: {mapping.get('hive_chat_id', '— none yet')}\n"
            f"workspace: {self._short_path(mapping.get('workspace')) if mapping.get('workspace') else 'engine default'}\n"
            f"mode: {mode}  (phone runs only)\n"
            f"model: {ov.get('model') or 'engine default'}\n"
            f"ctx: planner {ov.get('planner_ctx') or '—'} · coder "
            f"{ov.get('coder_ctx') or '—'}\n"
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
            f"  • {name} — {_MODE_DESCRIPTIONS[name]}"
            for name in MODE_CHOICES)
        if not arg:
            current = self.state.data.get("mode") or ""
            head = (f"Mode (Telegram): {current}" if current
                    else "Mode (Telegram): engine default")
            return (head + "\n\n" + listing +
                    "\n\nSet with /mode <name>; /mode off follows the "
                    "engine settings again. Phone-only — the browser UI "
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
        why = ("File-capable — use this for tasks."
               if name != "simple" else
               "NO file tools here — talk + web only!")
        return (f"Mode (Telegram): {name}\n\n"
                f"  • {_MODE_DESCRIPTIONS[name]}\n"
                f"{why}\n\n"
                "Phone-only — the browser UI keeps its own mode.")

    # -- /models, /setModel, /cancel: model + ctx/preset selection -------

    PENDING_TTL_S = 600

    def _stream_overrides(self) -> dict:
        """Model/ctx keys from the last /setModel flow — merged into the
        run's settings snapshot by the engine (chat_run.py:154). Empty
        unless configured."""
        ov = self.state.data.get("run_overrides") or {}
        out: dict = {}
        model = ov.get("model")
        if model:
            out["duo_planner_model"] = model
            out["duo_coder_model"] = model
        # deep audit N5: clamp — the engine clamps the planner ctx at
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
        # audit G1: FORCE the engine's approval gate for every phone run —
        # this restores the documented "gated tools are auto-denied from
        # the phone" invariant even when the engine-global toggle
        # (duo_action_approval_enabled, default false) is off. The body
        # can only raise the gate to ON, never lower it. 2026-10-05: the
        # owner can opt out for assistant-style use (phone runs that
        # SHOULD write files) via gateway.toml force_approval_gate=false —
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
        try:
            idx = int((arg or "").strip())
        except ValueError:
            return ("Usage: /setModel <number> — /models lists the "
                    "numbers.")
        try:
            data = await self.hive.get_models()
        except (HiveUnreachable, OSError) as exc:
            return f"🔌 HiveMind unreachable: {exc}"
        models = data.get("models") or []
        if not (1 <= idx <= len(models)):
            return (f"❌ {idx} out of range 1–{len(models)}. "
                    "List: /models")
        name = models[idx - 1]
        prof = next((p for p in (data.get("profiles") or [])
                     if p.get("name") == name), {})
        can_think = bool(prof.get("thinking"))

        # presets: numbered for the follow-up question
        try:
            raw = await self.hive.get_presets()
        except (HiveUnreachable, OSError):
            raw = {}
        if isinstance(raw, dict):
            preset_names = sorted(raw.keys())
        elif isinstance(raw, list):
            preset_names = [x if isinstance(x, str) else
                            str(x.get("name", "?")) for x in raw]
        else:
            preset_names = []

        agentic = False
        try:
            s = await self.hive.settings()
            agentic = bool(s.get("duo_agentic_mode"))
        except (HiveUnreachable, OSError, ValueError):
            pass

        self.state.data["pending_setup"] = {
            "model": name, "agentic": agentic, "ts": time.time()}
        self.state.save()

        head = f"Model {idx}: {name}"
        if not can_think:
            head += " (cannot think — ctx_thinking will be 0)"
        if agentic:
            fmt = ("Reply with ONE line, separated by ', ':\n"
                   "preset, planner_ctx_thinking, planner_ctx_model, "
                   "coder_ctx_thinking, coder_ctx_model\n"
                   "(agentic detected — planner and coder, both values)")
        else:
            fmt = ("Reply with ONE line, separated by ', ':\n"
                   "preset, ctx_thinking, ctx_model")
        lines = [
            head,
            fmt,
            "preset: 0 = none,"
            + (f" 1..{len(preset_names)} = " + ", ".join(
                f"{i+1}={n}" for i, n in enumerate(preset_names[:8]))
               if preset_names else " keine Presets vorhanden"),
            "Note: a preset applies GLOBALLY (also to the browser UI).",
            "ctx_thinking 0 = no thinking/planner.",
            "Example: 0, 8192, 16384",
            "Applies to duo/agentic runs; direct overrides arrive with "
            "WP3 (P9). /cancel aborts.",
        ]
        return "\n".join(lines)

    async def consume_setup(self, text: str) -> str | None:
        """While a /setModel flow is pending, a numeric comma answer is
        consumed here. Returns None when nothing is pending (normal run)."""
        p = self._pending()
        if p is None:
            return None
        parts = [x.strip() for x in (text or "").split(",") if x.strip()]
        expected = 5 if p.get("agentic") else 3
        # deep audit N5: "--5"/"²" passed the old check but crashed int()
        if not all(x.isascii() and x.isdigit() for x in parts) \
                or len(parts) != expected:
            return (f"Expected {expected} numbers separated by ', ' — "
                    "e.g. 0, 8192, 16384. /cancel aborts.")
        vals = [int(x) for x in parts]

        # preset (position 0) — global, warned in the question already
        preset_note = "no preset"
        if vals[0] > 0:
            try:
                raw = await self.hive.get_presets()
            except (HiveUnreachable, OSError) as exc:
                return f"🔌 Presets unavailable: {exc}"
            names = sorted(raw.keys()) if isinstance(raw, dict) else \
                [x if isinstance(x, str) else str(x.get("name", "?"))
                 for x in (raw or [])]
            if vals[0] > len(names):
                return (f"❌ Preset {vals[0]} out of range 1–{len(names)}. "
                        "Send again.")
            resp = await self.hive.load_preset(names[vals[0] - 1])
            if getattr(resp, "status_code", 500) >= 300:
                return (f"❌ Preset '{names[vals[0] - 1]}' could not be "
                        "loaded — nothing changed, "
                        "send again.")
            preset_note = f"Preset '{names[vals[0] - 1]}' loaded (global)"

        ov = self.state.data.setdefault("run_overrides", {})
        ov["model"] = p["model"]
        if p.get("agentic"):
            p_think, p_ctx, c_think, c_ctx = vals[1:]
            # agentic pairs: (thinking, model) per role; ctx_thinking 0
            # means "no planner/thinking" for that role
            ov["planner_ctx"] = p_ctx if (p_think or p_ctx) else 0
            ov["coder_ctx"] = c_ctx
            detail = (f"planner: thinking_ctx={p_think or 0}, "
                      f"ctx={p_ctx or 'standard'}; coder: "
                      f"thinking_ctx={c_think or 0} (note), "
                      f"ctx={c_ctx or 'standard'}")
            if not p_think and not p_ctx:
                ov["planner_ctx"] = 0
                detail = (f"planner off (0/0); coder: ctx="
                          f"{c_ctx or 'Standard'}")
        else:
            think, ctx = vals[1:]
            ov["planner_ctx"] = ctx if think else 0
            ov["coder_ctx"] = ctx
            detail = (f"ctx_thinking={think or 0}, "
                      f"ctx_model={ctx or 'standard'}")
        self.state.data["pending_setup"] = None
        self.state.save()
        return (f"✅ Saved. Model: {p['model']} — {preset_note}; "
                f"{detail}. Applies to upcoming phone runs (duo/agentic; "
                "direct overrides: P9). Send /setModel again to change.")

    def cancel_setup(self) -> str:
        if self.state.data.get("pending_setup"):
            self.state.data["pending_setup"] = None
            self.state.save()
            return "Setup aborted."
        return "Nothing to abort."

    def reset_overrides(self) -> str:
        self.state.data["run_overrides"] = {}
        self.state.data["pending_setup"] = None
        self.state.save()
        return ("Model/CTX overrides cleared — upcoming runs use "
                "the engine settings again.")

    # -- P10: run takeover — mirror a UI/engine run to the phone ---------

    def _mirror(self) -> dict:
        if not hasattr(self, "_mirror_state"):
            self._mirror_state = {"run_id": None, "after": 0,
                                  "status_msg_id": None,
                                  "last_status": "", "approval_sig": None,
                                  "approval_decision_id": "",
                                  "approval_tool": "",
                                  "last_tick": 0.0}
        return self._mirror_state

    def _mirror_reset(self, m: dict) -> None:
        """End a mirror session — every reset site must clear the approval
        card metadata too (G2: decision_id/tool of the relayed card)."""
        m.update({"run_id": None, "after": 0, "status_msg_id": None,
                  "last_status": "", "approval_sig": None,
                  "approval_decision_id": "", "approval_tool": ""})

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
            # mirror task — log and continue with the next tick
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
        # audit G7: once a session is active, fetch THAT run's journal —
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
                self._mirror_reset(m)
                await self.ms.send_message(
                    f"🏁 Mirror run {rid} finished ({reason}).")
            return
        rid = str(j.get("run_id") or "")
        # deep audit N7 (confirmed as G7): while OUR run sits between
        # POST /stream and its run_id frame, active_run exists with
        # run_id None — the unscoped journal is then necessarily our own
        # run. Skip the takeover decision this tick instead of announcing
        # the gateway's own run as a takeover.
        _own = self.state.data.get("active_run")
        if not m["run_id"] and isinstance(_own, dict) \
                and not _own.get("run_id"):
            return
        if rid == str(self._own_run_id() or ""):
            return  # our own run reports natively; do not double-mirror
        fresh = m["run_id"] != rid
        if fresh:
            m.update({"run_id": rid, "after": int(j.get("n") or 0),
                      "status_msg_id": None, "last_status": "",
                      "approval_sig": None, "approval_decision_id": "",
                      "approval_tool": ""})
            await self.ms.send_message(
                f"📢 Engine run taken over (mirror): {rid}\n"
                "Your texts are queued (steering), approvals come "
                "to you here. /stop aborts it.")
            m["after"] = int(j.get("n") or 0)
            return  # next tick streams the delta

        # delta frames since last tick
        frames = j.get("frames") or []
        after = m["after"]
        new_frames = frames[after:] if after < len(frames) else []
        m["after"] = len(frames)
        done_reason = self._parse_done(new_frames)
        last_status = ""
        for raw in new_frames:
            if not isinstance(raw, str):
                continue
            try:
                ev = json.loads(raw[6:]) if raw.startswith("data: ") else {}
            except ValueError:
                continue
            if ev.get("type") == "status":
                last_status = str(ev.get("content") or "")[:120]
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
        if done_reason:
            text = STOP_REASON_TEXT.get(
                done_reason, f"🏁 Mirror run finished ({done_reason}).")
            if done_reason == "completed":
                text = "🏁 Mirror run completed."
            await self.ms.send_message(text)
            self._mirror_reset(m)
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
                # deep audit N2: the pending preview is truncated to 300
                # chars by the engine; the full command lives in the
                # journal's tool_call frames — dig it out so the owner
                # never approves blind.
                full = self._full_command_from_frames(
                    j.get("frames"), str(body.get("tool") or ""))
                preview = full or (str(body.get("preview") or "") + "\n"
                                   "(server preview truncated — full "
                                   "command in the UI)")
                if len(preview) > 2500:
                    preview = preview[:2500] + " …"
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
            m["approval_sig"] = None  # card resolved elsewhere (UI)

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

    async def mirror_callback(self, callback_id: str, data: str) -> str:
        """Tappable approval buttons (2026-10-05 UX round): callback_data
        is 'appr:<rid>:<1|2|3>' from the card's inline keyboard. The
        decision rides the SAME path as text answers (decision_id + tool
        echoed for the engine's duplicate/tool guards); '2' uses the
        engine's chat-scoped exact-call memory. Returns the toast text
        for answerCallbackQuery."""
        m = self._mirror()
        try:
            _, rid, answer = data.split(":", 2)
        except ValueError:
            return "malformed button"
        if rid != m.get("run_id") or not m.get("approval_sig"):
            return "card already gone"
        if answer not in ("1", "2", "3"):
            return "unknown button"
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
            return "already answered (UI) — nothing changed"
        if answer == "2":
            return "always (this chat, this exact call) — delivered"
        return "allowed (once) — delivered" if answer == "1" \
            else "denied — delivered"

    async def mirror_send(self, text: str) -> str:
        """Route a phone message into the mirrored run: while an
        approval card is open, 1/3 answers it ('2'/always does not exist
        from the phone); anything else is steering."""
        m = self._mirror()
        rid = m.get("run_id")
        if m.get("approval_sig"):
            t = (text or "").strip()
            if t == "2":
                return ("❌ '2' (always allow) deliberately does not "
                        "exist from the phone — 1 or 3.")
            if t in ("1", "3"):
                resp = await self.hive.decide_approval(
                    rid, t,
                    decision_id=m.get("approval_decision_id") or "",
                    tool=m.get("approval_tool") or "")
                code = getattr(resp, "status_code", 500)
                m["approval_sig"] = None
                if code >= 300:
                    return (f"❌ Decision not accepted "
                            f"(HTTP {code}) — maybe answered in the UI.")
                try:
                    routed = str((resp.json() or {}).get("routed") or "")
                except (ValueError, TypeError, AttributeError):
                    routed = ""
                if routed == "duplicate":
                    # G2: the UI already answered this card — be honest
                    # instead of confirming a decision that was dropped.
                    return ("ℹ️ This card was already answered (UI) — "
                            "nothing changed.")
                return ("✅ allowed (once) — delivered."
                        if t == "1" else "🛡 denied — delivered.")
            return ("🛡 An approval is waiting — reply 1 or 3 "
                    "(or answer it in the UI).")
        # audit G8: steering bypassed the max_text_chars cap (the intercept
        # runs before the run-start length check) — cap at the same limit.
        text = (text or "").strip()[: self.cfg.max_text_chars]
        resp = await self.hive.steer(rid, text)
        if getattr(resp, "status_code", 500) == 404:
            return (f"❌ Run {rid} is no longer active — the mirror "
                    "ends on the next tick.")
        return ("🧭 Queued — injected at the next boundary "
                "(mode dependent; pipeline accepts no steering).")

    async def workspace_text(self, arg: str) -> str:
        """/workspace <path> — set the workspace of the [TG] chat so
        phone runs execute there (chat workspace wins at resolve time)."""
        path = (arg or "").strip().strip('"').strip("'")
        if not path:
            current = (self._tg_chat() or {}).get("workspace", "")
            label = current or "— not set (engine default)"
            return (f"Workspace (Telegram): {label}.\n"
                    "Set with /workspace <path>.")
        # audit G11 / deep-audit N9: the chat workspace IS the tool
        # confinement root for phone runs — a typo must not silently
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
                    "allowed — please name a concrete folder.")
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
                    "Phone runs now work there — the agent can read "
                    "from this folder. /workspace without an argument "
                    "shows the current path.")
        raise HiveUnreachable("workspace write conflict (409 twice)")

    def tools_text(self, arg: str) -> str:
        """/tools on|off — per-run direct tools access level (rides the
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
                f"{'on' if new else 'off'} — per run "
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
                    f"({exc}) — may still be running, check the PC!")
        self._mirror_reset(m)
        if code == 404:
            return (f"⏹ Mirror run {rid} was unknown to the server — "
                    "mirror ended.")
        return f"⏹ Abort sent for mirror run {rid}."
