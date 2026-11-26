"""Telegram gateway slash-command & run-override mixin for RunBridge."""
from __future__ import annotations

import copy
import dataclasses
import datetime as _dt

from httpx import HTTPError

from .hive_client import HiveUnreachable

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



class _BridgeCommandsMixin:
    # -- busy heuristic against the server journal ------------------------

    async def _journal_busy(self) -> str | None:
        from . import bridge as _b
        try:
            j = await self.hive.journal()
        except (HiveUnreachable, OSError):
            return None  # offline is handled by the run itself
        if not isinstance(j, dict) or not j.get("active"):
            return None
        if j.get("done") or j.get("aborted"):
            return None
        age = _b.time.time() - float(j.get("ts") or 0)
        if age > _b.STALE_JOURNAL_S:
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
                # P0 (edge audit 2026-11-03): the engine may still be starting.
                # Clearing the latch here made the late run_id frame crash and
                # left the engine run alive. Ask for a stop; the run_id frame
                # aborts as soon as it arrives.
                self._stop_requested = True
                return ("⏹ Stop requested - the run will be aborted as soon "
                        "as the engine confirms it.")
            # ORPHAN-STOP (audit 2026-11-03): after a restart the latch may
            # be cleared while the engine still runs (adopted as mirror).
            # /stop must still be able to abort it via the journal.
            try:
                _j = await self.hive.journal() if hasattr(self.hive, "journal") else None
            except (HiveUnreachable, OSError):
                _j = None
            _jid = ""
            if isinstance(_j, dict) and _j.get("active") and not _j.get("done"):
                _jid = str(_j.get("run_id") or "")
            if _jid:
                try:
                    await self.hive.abort_run(_jid)
                    return f"⏹ Abort sent for {_jid}."
                except (HiveUnreachable, OSError, HTTPError) as exc:
                    return f"❌ /stop failed ({exc}) - the run may still be running."
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
                chat_id = (run.get("chat_id")
                           or (self._tg_chat() or {}).get("hive_chat_id")
                           or "")
                if chat_id:
                    await self.hive.abort_chat(chat_id)
                    return (f"⏹ Run {rid} was unknown to the server - "
                            "chat abort sent as fallback; run cleared.")
                return (f"⏹ Run {rid} was unknown to the server - it "
                        "had already ended; run cleared.")
            if code < 300:
                self._stop_requested = True
                return f"⏹ Abort sent for {rid}."
            # A rejected abort is NOT a success: the run may still hold VRAM.
            return (f"❌ /stop not accepted (HTTP {code}) - run {rid} may "
                    "still be running, please check the PC!")
        except HTTPError as exc:
            # deep audit N6: raise_for_status in the fallback must not
            # break the visible-failure invariant of /stop
            return (f"❌ /stop failed ({exc}) - run {rid} may still "
                    "be running, please check the PC!")
        except (HiveUnreachable, OSError) as exc:
            # /stop must fail VISIBLY: an orphaned run holds VRAM
            return f"❌ /stop failed ({exc}) - run {rid} may still " \
                   "be running, please check the PC!"

    async def new_chat_busy_note(self) -> str | None:
        """Return a busy message if an own or UI/engine run is active, else None."""
        if self._run_lock.locked() or self.state.data.get("active_run"):
            return self._busy_note()
        return await self._journal_busy()

    async def prepare_new_chat(self) -> dict:
        """Check whether /new should ask before clearing default_workspace files."""
        busy = await self.new_chat_busy_note()
        if busy:
            return {"status": "busy", "text": busy}
        fn = getattr(self.hive, "placeholder_status", None)
        ph: dict = {}
        if fn is not None:
            res = await fn()
            ph = res if isinstance(res, dict) else {}
        files = list(ph.get("files") or [])
        if files:
            return {
                "status": "confirm",
                "files": files,
                "count": int(ph.get("count") or len(files)),
                "path": str(ph.get("path") or ""),
            }
        text = await self.new_chat(clear_placeholder=True)
        return {"status": "done", "text": text}

    async def new_chat(self, *, clear_placeholder: bool = True) -> str:
        busy = await self.new_chat_busy_note()
        if busy:
            return busy
        cleared = 0
        if clear_placeholder:
            clear_fn = getattr(self.hive, "clear_placeholder", None)
            if clear_fn is not None:
                try:
                    res = await clear_fn()
                except HTTPError as exc:
                    if getattr(getattr(exc, "response", None), "status_code", None) == 409:
                        return ("⏳ An active HiveMind run is in progress — "
                                "cannot clear default_workspace.")
                    raise
                if isinstance(res, dict):
                    cleared = int(res.get("cleared") or 0)
        from . import bridge as _b
        now_ts = _b.time.time()
        title = "[TG] Telegram " + _dt.datetime.now().astimezone() \
            .strftime("%Y-%m-%d %H:%M")
        created = await self.hive.create_chat(title)
        mapping = {
            "hive_chat_id": str(created["id"]),
            "title": title,
            "created_at": created.get("chat", {}).get("created_at", ""),
            "new_chat_ts": now_ts,
        }
        self.state.data["tg_chat"] = mapping
        self.state.data.pop("followed_chat", None)
        self.state.data.pop("pinned_chat", None)
        self.state.save()
        extra = (f"\n🧹 Cleared {cleared} file(s) from default_workspace."
                 if cleared > 0 else "")
        return f"🆕 New HiveMind chat: {title} ({mapping['hive_chat_id']}).{extra}"

    async def decider_text(self, arg: str) -> str:
        """DECIDER (2026-10-22): /decider bare = status, on|off|shadow =
        set routing_decider in the ENGINE settings. off frees the encoder
        model (~1.8 GB RAM); on loads it lazily at the next decision."""
        _a = (arg or "").strip().lower()
        try:
            s = await self.hive.settings()
        except (HiveUnreachable, OSError) as exc:
            return f"❌ Engine unreachable: {exc}"
        if not _a:
            _m = (s or {}).get("routing_decider") or "off"
            _mdl = (s or {}).get("routing_decider_model") or "default"
            return (f"🎛 Decider: {_m} (on|off|shadow)\n"
                    f"model: {_mdl}\n"
                    "off frees the encoder RAM; on routes via the CPU encoder.")
        if _a not in ("on", "off", "shadow"):
            return "❌ /decider on|off|shadow"
        try:
            await self.hive.patch_settings({"routing_decider": _a})
        except (HiveUnreachable, OSError) as exc:
            return f"❌ Engine unreachable: {exc}"
        extra = (" - the encoder model is freed from RAM." if _a == "off"
                 else " - the model loads at the next decision.")
        return f"🎛 Decider: {_a}{extra}"

    async def status_text(self) -> str:
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
        # ACTIVE-RUN VISIBILITY (2026-10-16, live report): /status showed
        # "active run: none" while an agentic coder run was active in the
        # UI - the gateway state only tracks PHONE runs. Ask the engine
        # journal (unscoped = most recently active run) so a UI run shows
        # too; when it IS the phone run, no extra line.
        own = str(run.get("run_id") or "")
        engine_extra = ""
        try:
            j = await self.hive.journal()
        except (HiveUnreachable, OSError, ValueError):
            engine_extra = "engine run: unknown (engine unreachable)"
        else:
            _jid = str((j or {}).get("run_id") or "")
            if _jid and (j or {}).get("active") \
                    and not (j or {}).get("done") \
                    and not (j or {}).get("aborted") \
                    and _jid != own:
                engine_extra = f"engine run (UI): {_jid[:18]}…"
        active_txt = f"active run: {own}" if own else "active run: none"
        if engine_extra:
            active_txt += "\n" + engine_extra
        # ROUTING VISIBILITY (2026-10-22, user request): /status shows the
        # decider state, planner source and model — so a spoken note's route
        # is explainable from the phone.
        try:
            _settings_fn = getattr(self.hive, "settings", None)
            _rs = await _settings_fn() if _settings_fn else {}
        except (HiveUnreachable, OSError, AttributeError):
            _rs = {}  # fakes/old engines without a settings endpoint
        _rd = str((_rs or {}).get("routing_decider") or "").strip()
        _mode_now = str(mode or "").lower()
        if _mode_now in ("chat", "simple", "direct"):
            # ROUTING-N/A: a conversational mode answers directly; the decider
            # is never consulted, so naming it here only caused confusion.
            _routing = f"routing: not used in {_mode_now} mode"
        elif _rd:
            _routing = (f"routing: decider {_rd}"
                        + (f" · planner {(_rs or {}).get('routing_planner') or 'auto'}"
                           if (_rs or {}).get("routing_planner") else "")
                        + f" · model {(_rs or {}).get('routing_decider_model') or 'default'}")
        else:
            _routing = "routing: decider off (keyword rules)"
        _pin = self._pinned_chat()
        _ui_cid, _ui_title = ("", "")
        if _pin is None and hasattr(self.hive, "get_chats"):
            _ui_cid, _ui_title = await self._ui_chat_target_info()
        if _pin:
            _active_cid = str(_pin["hive_chat_id"])
            _active_title = str(_pin.get("title") or "").strip()
            _chat_line = (f"{_active_title[:42]} ({_active_cid}) [pinned]"
                          if _active_title and _active_title != _active_cid
                          else f"{_active_cid} [pinned]")
        elif _ui_cid:
            _active_cid = _ui_cid
            _chat_line = (f"{_ui_title[:42]} ({_ui_cid}) [follow-UI]"
                          if _ui_title and _ui_title != _ui_cid
                          else f"{_ui_cid} [follow-UI]")
        else:
            _active_cid = str(mapping.get("hive_chat_id") or "")
            _chat_line = (_active_cid + " [TG]") if _active_cid else "- none yet"
        _ws_cid = _active_cid
        _ws_map = mapping.get("workspace") or ""
        if _ws_cid:
            _ws_res, _ws_is_ph = await self.resolve_chat_workspace(_ws_cid)
            _ws_txt = self._format_workspace_label(_ws_res, _ws_is_ph)
        elif _ws_map:
            _ws_txt = self._short_path(_ws_map)
        else:
            _ws_ph = await self._placeholder_info()
            _ws_txt = ("default_workspace (placeholder)"
                       if _ws_ph.get("path") else "engine default")
        _eff_m = str(ov.get("reasoning_effort") or (_rs or {}).get("reasoning_effort") or "auto")
        _eff_p = str(ov.get("planner_reasoning_effort") or (_rs or {}).get("planner_reasoning_effort") or "auto")
        _eff_txt = _eff_m + (f" · planner: {_eff_p}" if _eff_p != "auto" else "")
        return (
            "📡 Gateway status\n"
            "\n"
            f"chat: {_chat_line}\n"
            f"workspace: {_ws_txt}\n"
            f"mode: {mode}  (phone runs only)\n"
            f"effort: {_eff_txt}\n"
            f"model: {ov.get('model') or 'engine default'}\n"
            f"ctx: planner {ov.get('planner_ctx') or '-'} · coder "
            f"{ov.get('coder_ctx') or '-'}\n"
            f"tools (simple runs): {tools_txt}\n"
            f"phone restrict: {restr_txt}\n"
            f"verbose: {'on' if self.verbose else 'off'}\n"
            f"{_routing}\n"
            "\n"
            f"{gate_line}\n"
            f"engine: {getattr(self.ms, 'engine_version', '?')}\n"
            "\n"
            f"{active_txt}")

    def toggle_verbose(self) -> str:
        self.verbose = not self.verbose
        self.state.data["verbose"] = self.verbose
        self.state.save()
        return f"Verbose: {'on' if self.verbose else 'off'}"

    # -- /mode: telegram-side run mode (stream BODY field, never settings)

    def effective_phone_mode(self, engine_settings: dict) -> tuple[str, str]:
        """(mode, source) for a phone run.

        The UI owns the default: a `/mode` override only survives while the
        engine mode is unchanged since it was set. Changing the mode in the
        browser therefore flips Telegram back to the frontend mode.
        """
        override = str(self.state.data.get("mode") or "").strip()
        engine_mode = str((engine_settings or {}).get("mode") or "").strip()
        seen = str(self.state.data.get("mode_engine_seen") or "")
        if override:
            if engine_mode and seen and engine_mode != seen:
                self.state.data["mode"] = ""
                self.state.data["mode_engine_seen"] = ""
                self.state.save()
                return (engine_mode, "frontend")
            return (override, "phone")
        return (engine_mode or "auto", "frontend")

    async def mode_text(self, arg: str) -> str:
        """/mode            -> current mode + per-mode explanations
        /mode <name>      -> set for TELEGRAM runs only
        /mode off         -> follow the engine settings again
        Aliases: chat|direct -> simple."""
        from . import bridge as _b
        arg = (arg or "").strip().lower()
        listing = "\n".join(
            f"  • {name} - {_b._MODE_DESCRIPTIONS[name]}"
            for name in _b.MODE_CHOICES)
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
            self.state.data["mode_engine_seen"] = ""
            self._invalidate_chat_runconf()
            self.state.save()
            return ("Mode (Telegram): engine default\n\n"
                    "Runs now follow the engine settings again.")
        name = _b.MODE_ALIASES.get(arg, arg)
        if name not in _b.MODE_CHOICES:
            return ("❌ Unknown mode.\n\n" + listing +
                    "\n\nSet with /mode <name> or /mode off.")
        self.state.data["mode"] = name
        # MODE-BASELINE: remember the engine mode AT THE MOMENT of the
        # override. Filling it lazily on the next run meant a UI change made
        # in between never counted as a change, and a stale "auto" override
        # kept the phone on the decider route while the browser said chat.
        try:
            _s = await self.hive.settings()
            self.state.data["mode_engine_seen"] = str((_s or {}).get("mode") or "")
        except (HiveUnreachable, OSError, AttributeError, ValueError):
            self.state.data["mode_engine_seen"] = ""
        self._invalidate_chat_runconf()
        self.state.save()
        why = ("File-capable - use this for tasks."
               if name != "simple" else
               "NO file tools here - talk + web only!")
        return (f"Mode (Telegram): {name}\n\n"
                f"  • {_b._MODE_DESCRIPTIONS[name]}\n"
                f"{why}\n\n"
                "Phone-only - the browser UI keeps its own mode.")

    # -- /models, /setModel, /cancel: model + ctx/preset selection -------

    PENDING_TTL_S = 600

    def _stream_overrides(self, ov_store=None) -> dict:
        """Model/ctx keys for a run: from the chat's pinned run config
        (chat_runconf, ov_store) or the owner's current defaults. Merged
        into the run's settings snapshot by the engine (chat_run.py:154).
        Empty unless configured."""
        ov = (ov_store if ov_store is not None
              else self.state.data.get("run_overrides")) or {}
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
        tier = str(self.state.data.get("direct_tools_tier") or "").strip().lower()
        if tier in ("off", "websearch", "readonly", "mcp", "python", "full"):
            tier = "readonly" if tier in ("websearch", "readonly") else tier
            out["direct_tools_tier"] = tier
            out["direct_tools_enabled"] = tier != "off"
        elif tools is not None:
            out["direct_tools_enabled"] = bool(tools)
        for _rk in ("reasoning_effort", "planner_reasoning_effort"):
            _rv = str(ov.get(_rk) or "").strip().lower()
            if _rv and _rv != "auto":
                out[_rk] = _rv
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

    def _chat_runconf(self, chat_id: str, engine_settings: dict) -> dict:
        """The phone-side parameters this chat started with.

        Owner rule (2026-10-28): UI runs use the UI settings, phone chats
        use the phone settings, and ANSWERING inside an existing chat keeps
        the parameters that chat started with. Only a phone command
        (/mode, /setModel, /ctx, /planner) changes them - it drops the pin
        (_invalidate_chat_runconf) and the next run re-snapshots the new
        defaults. The engine settings stay shared with the UI; only these
        overrides are pinned per chat."""
        store = self.state.data.setdefault("chat_runconf", {})
        conf = store.get(chat_id)
        if conf is None:
            _eff, _src = self.effective_phone_mode(engine_settings)
            conf = {"run_overrides": copy.deepcopy(
                        self.state.data.get("run_overrides") or {}),
                    "mode": (_eff if _src == "phone" else "")}
            store[chat_id] = conf
            self.state.save()
        return conf

    def _invalidate_chat_runconf(self) -> None:
        """A phone command changed the phone defaults - drop every chat pin
        so the next run re-snapshots them (the command wins over the pin)."""
        if self.state.data.pop("chat_runconf", None) is not None:
            self.state.save()

    def _pending(self) -> dict | None:
        from . import bridge as _b
        p = self.state.data.get("pending_setup")
        if not p:
            return None
        if _b.time.time() - float(p.get("ts", 0)) > self.PENDING_TTL_S:
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
        self._invalidate_chat_runconf()
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
            self._invalidate_chat_runconf()
            self.state.save()
            return "ctx override cleared - engine defaults apply."
        if not a.isdigit():
            return "❌ /ctx <number> (tokens) or /ctx off."
        ov["coder_ctx"] = int(a)
        ov["planner_ctx"] = int(a)
        self._invalidate_chat_runconf()
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
            self._invalidate_chat_runconf()
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
        self._invalidate_chat_runconf()
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

    async def chat_text(self, arg: str) -> str:
        """/chat [number|id|auto] - inspect or pin active HiveMind chat."""
        a = (arg or "").strip()
        al = a.lower()
        if al in ("auto", "off", "unpin", "clear", "reset"):
            had = self.state.data.pop("pinned_chat", None) is not None
            if had:
                self.state.save()
            follow_s = "follow-UI" if self.follow_ui_enabled() else "[TG] default"
            return (f"🔓 Chat unpinned — back to automatic ({follow_s})."
                    if had else f"Chat is already automatic ({follow_s}).")
        if a:
            cached = list(self.state.data.get("last_chat_list") or [])
            if a.isdigit() and not cached:
                chats = await self.hive.get_chats()
                cached = [
                    {"id": str(c.get("id") or ""), "title": str(c.get("title") or "Untitled")}
                    for c in (chats or [])[:10] if isinstance(c, dict) and c.get("id")
                ]
            picked = None
            if a.isdigit():
                idx = int(a)
                if 1 <= idx <= len(cached):
                    picked = cached[idx - 1]
                elif cached:
                    return f"❌ {idx} out of range 1–{len(cached)}. List: /chat"
            if picked is None:
                chats = await self.hive.get_chats()
                for c in chats or []:
                    if not isinstance(c, dict):
                        continue
                    cid = str(c.get("id") or "")
                    if cid == a or cid.startswith(a):
                        picked = {"id": cid, "title": str(c.get("title") or "Untitled")}
                        break
            if picked is None or not picked.get("id"):
                return f"❌ Chat '{a}' not found. Run /chat to list recent chats."
            cid = str(picked["id"])
            title = str(picked.get("title") or "Untitled")
            self.state.data["pinned_chat"] = {"hive_chat_id": cid, "title": title}
            self.state.data.pop("followed_chat", None)
            self._invalidate_chat_runconf()
            self.state.save()
            return (f"📌 Pinned chat: {title[:48]} ({cid})\n"
                    "All Telegram messages stay in this chat until /new or /chat auto.")

        chats = await self.hive.get_chats()
        pin = self._pinned_chat()
        tg = self._tg_chat() or {}
        ui_cid, _ = await self._ui_chat_target_info()
        active_cid = (str(pin["hive_chat_id"]) if pin
                      else (ui_cid or str(tg.get("hive_chat_id") or "")))
        items = [c for c in (chats or []) if isinstance(c, dict) and c.get("id")][:10]
        self.state.data["last_chat_list"] = [
            {"id": str(c.get("id")), "title": str(c.get("title") or "Untitled")}
            for c in items
        ]
        self.state.save()
        if pin:
            hdr = f"📌 Pinned: {str(pin.get('title') or pin['hive_chat_id'])[:42]} ({pin['hive_chat_id']})"
        elif ui_cid:
            hdr = f"🔗 Auto (follow-UI): {ui_cid}"
        elif active_cid:
            hdr = f"💬 Auto (TG chat): {active_cid}"
        else:
            hdr = "💬 No active chat yet"
        lines = [hdr, ""]
        if not items:
            lines.append("No saved HiveMind chats found.")
        else:
            lines.append("Recent chats:")
            for i, c in enumerate(items, 1):
                cid = str(c.get("id"))
                title = str(c.get("title") or "Untitled").replace("\n", " ")[:38]
                cnt = int(c.get("msg_count") or 0)
                mark = " ◀ active" if cid == active_cid else ""
                lines.append(f"{i}. {title} ({cid}, {cnt}m){mark}")
        lines.append("")
        lines.append("Pin: /chat <number> · Unpin: /chat auto · Fresh: /new")
        return "\n".join(lines)

    async def effort_text(self, arg: str) -> str:
        """/effort [planner] [level|auto] - thinking effort override for phone runs."""
        aliases = {
            "auto": "auto", "default": "auto", "reset": "auto",
            "off": "none", "none": "none", "disabled": "none", "false": "none", "0": "none",
            "on": "on", "enabled": "on", "true": "on", "1": "on",
            "min": "minimal", "minimal": "minimal",
            "low": "low", "med": "medium", "medium": "medium",
            "high": "high", "max": "max", "xhigh": "max", "maximum": "max",
        }
        tokens = [t.strip().lower() for t in (arg or "").split() if t.strip()]
        ov = self.state.data.setdefault("run_overrides", {})
        if not tokens or (tokens == ["planner"]):
            try:
                _s = await self.hive.settings()
            except (HiveUnreachable, OSError, ValueError):
                _s = {}
            _pm = str(ov.get("reasoning_effort") or "")
            _pp = str(ov.get("planner_reasoning_effort") or "")
            _em = str((_s or {}).get("reasoning_effort") or "auto")
            _ep = str((_s or {}).get("planner_reasoning_effort") or "auto")
            return (
                "🧠 Thinking effort (Telegram):\n"
                f"  Direct / Coder: {_pm + ' (phone)' if _pm else _em + ' (engine)'}\n"
                f"  Planner: {_pp + ' (phone)' if _pp else _ep + ' (engine)'}\n\n"
                "Set: /effort <auto|off|minimal|low|medium|high|max>\n"
                "Planner: /effort planner <auto|off|low|medium|high|max>"
            )
        target_key = "reasoning_effort"
        target_lbl = "Direct / Coder"
        if tokens[0] in ("planner", "plan"):
            target_key = "planner_reasoning_effort"
            target_lbl = "Planner"
            tokens = tokens[1:]
        if len(tokens) != 1 or tokens[0] not in aliases:
            return "❌ Use /effort [planner] <auto|off|on|minimal|low|medium|high|max>."
        level = aliases[tokens[0]]
        if level == "auto":
            ov.pop(target_key, None)
            msg = f"🧠 Effort ({target_lbl}): auto (follows engine/UI defaults)."
        else:
            ov[target_key] = level
            msg = f"🧠 Effort ({target_lbl}): {level} (applies to upcoming phone runs)."
        self._invalidate_chat_runconf()
        self.state.save()
        return msg

    def reset_overrides(self) -> str:
        self.state.data["run_overrides"] = {}
        self.state.data["pending_setup"] = None
        self.state.save()
        return ("Model/CTX overrides cleared - upcoming runs use "
                "the engine settings again.")


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
        """/workspace <path|default> - set the workspace of the [TG] chat so
        phone runs execute there (chat workspace wins at resolve time)."""
        path = (arg or "").strip().strip('"').strip("'")
        if not path:
            cid = str((self._tg_chat() or {}).get("hive_chat_id") or "")
            if cid:
                cur, is_ph = await self.resolve_chat_workspace(cid)
                label = self._format_workspace_label(cur, is_ph)
            else:
                ph = await self._placeholder_info()
                label = ("default_workspace (placeholder)"
                         if ph.get("path") else "- not set (engine default)")
            return (f"Workspace (Telegram): {label}.\n"
                    "Set with /workspace <path> (or /workspace default).")
        if path.lower() in ("default", "clear", "reset", "placeholder"):
            mapping = await self._ensure_chat()
            chat_id = mapping["hive_chat_id"]
            r = await self.hive.get_chat(chat_id)
            r.raise_for_status()
            chat = r.json()
            rev = int(chat.get("rev") or 0)
            for _attempt in (0, 1):
                resp = await self.hive.put_chat_meta(
                    chat_id, {"workspace": "", "base_rev": rev})
                if resp.status_code == 409:
                    body = resp.json()
                    rev = int(body.get("rev") or 0)
                    continue
                resp.raise_for_status()
                tg_map = self._tg_chat()
                if isinstance(tg_map, dict):
                    tg_map.pop("workspace", None)
                    self.state.data["tg_chat"] = tg_map
                    self.state.save()
                return ("📁 Workspace (Telegram) reset to default_workspace "
                        "(placeholder).\nFiles in default_workspace stay "
                        "until /new.")
            raise HiveUnreachable("workspace reset conflict (409 twice)")
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
        if not _p.is_dir():
            return (f"❌ Path is not a folder: {path}\n"
                    "/workspace needs an existing folder.")
        try:
            _p_res = _p.resolve()
        except OSError:
            _p_res = _p
        if _p.parent == _p or _p_res.parent == _p_res:
            return (f"❌ Drive root ({path}) is deliberately not "
                    "allowed - please name a concrete folder.")
        mapping = await self._ensure_chat()
        chat_id = mapping["hive_chat_id"]
        if not mapping.get("followed") and not mapping.get("pinned"):
            tg_map = self._tg_chat()
            if isinstance(tg_map, dict) and str(tg_map.get("hive_chat_id") or "") == chat_id:
                from . import bridge as _b
                tg_map["last_active_ts"] = _b.time.time()
                self.state.data["tg_chat"] = tg_map
                self.state.save()
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
            if not mapping.get("followed"):
                self.state.data["tg_chat"] = mapping
                self.state.save()
            return (f"📁 Workspace (Telegram) set: {self._short_path(path)}\n"
                    "Phone runs now work there - the agent can read "
                    "from this folder. /workspace without an argument "
                    "shows the current path.")
        raise HiveUnreachable("workspace write conflict (409 twice)")

    def tools_text(self, arg: str) -> str:
        """/tools <off|websearch|python|full> - per-run direct access level.

        Mirrors the UI's Chat Tools radio group. It rides the run body
        (direct_tools_enabled + direct_tools_tier), so the desktop setting
        stays untouched. File access is still confined to the workspace.
        """
        aliases = {"on": "full", "an": "full", "1": "full",
                   "off": "off", "aus": "off", "0": "off",
                   "web": "readonly", "websearch": "readonly",
                   "read": "readonly", "readonly": "readonly",
                   "mcp": "mcp",
                   "py": "python", "python": "python",
                   "full": "full", "all": "full"}
        labels = {"off": "off (plain chat)", "readonly": "websearch only",
                  "mcp": "MCP + web + read",
                  "python": "read + python", "full": "full (read/write/shell)"}
        a = (arg or "").strip().lower()
        cur = str(self.state.data.get("direct_tools_tier") or "").strip().lower()
        if not cur:
            cur = "full" if self.state.data.get("tools") is True else (
                "off" if self.state.data.get("tools") is False else "")
        if not a:
            cur_txt = labels.get(cur, "engine default")
            return (f"Direct access (Telegram): {cur_txt}.\n\n"
                    "  /tools off        - plain chat, no tools\n"
                    "  /tools websearch  - web search/fetch only\n"
                    "  /tools mcp        - connected MCP servers + web + read\n"
                    "  /tools python     - read files + run_python\n"
                    "  /tools full       - read/write/shell (workspace-locked)")
        if a not in aliases:
            return ("❌ Use /tools off | websearch | mcp | python | full.")
        tier = aliases[a]
        self.state.data["direct_tools_tier"] = tier
        self.state.data["tools"] = tier != "off"
        self.state.save()
        return (f"Direct access (Telegram): {labels[tier]}. "
                "Applies to the next phone run; the workspace lock stays on. "
                "For file/shell access also check /restrict off.")

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
