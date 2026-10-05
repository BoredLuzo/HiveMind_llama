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

import logging
import time

from httpx import HTTPError

from . import render
from .hive_client import HiveUnreachable
from .telegram_api import TelegramApiError

STOP_REASON_TEXT = {
    "model_load_failed":
        "❌ Modell konnte nicht geladen werden (VRAM blockiert? "
        "Am PC andere Modell-Läufe stoppen und erneut senden).",
    "vram_preflight_block":
        "❌ VRAM-Block: Es läuft gerade ein anderer Modell-Lauf am PC. "
        "Diesen beenden und erneut senden.",
    "timeout": "⏱ Lauf wurde wegen Timeout beendet.",
    "hard_stop": "⏹ Lauf wurde hart abgebrochen.",
    "graceful_stop": "⏹ Lauf wurde ordentlich beendet.",
    "aborted": "⏹ Lauf wurde abgebrochen.",
    "loop_detected": "⏹ Lauf abgebrochen (Wiederholungs-Schleife erkannt).",
    "wedge_escalated": "⏹ Lauf abgebrochen (feststeckender Edit, "
                       "Reparatur fehlgeschlagen).",
    "halted": "⏹ Lauf angehalten.",
    "max_tool_rounds": "⏹ Lauf am Rundenlimit angehalten (Teil-Ergebnis "
                       "unten, falls vorhanden).",
}
FINAL_CHUNK_LIMIT = 3
STALE_JOURNAL_S = 180.0

# Run modes accepted via /mode (the /stream BODY field — the browser
# UI's own mode in settings stays untouched). Friendly aliases map to
# the engine's names (index.html mode buttons).
MODE_CHOICES = ("auto", "simple", "pipeline", "automap")
MODE_ALIASES = {"chat": "simple", "direct": "simple"}


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
        return (f"⏳ Es läuft bereits ein Lauf ({rid}). /stop bricht ihn "
                "ab; Warteschlange gibt es bewusst keine.")

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
            return await self._fail(f"🔌 HiveMind nicht erreichbar "
                                    f"(Offline?): {exc}")

    async def _fail(self, text: str) -> str:
        await self.ms.send_message(text)
        return text

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

        status = await self.ms.send_message("⏳ Lauf gestartet …")
        status_id = status.get("message_id")

        parts: list[str] = []
        error_text = ""
        stop_reason = ""
        last_status = ""
        last_edited_text = ""
        last_edit = _now()
        denied = 0

        try:
            ov = self._stream_overrides()
            async for ev in self.hive.stream(q, chat_id,
                                             mode=self.state.data.get("mode")
                                             or "",
                                             overrides=ov):
                etype = ev.get("type")
                if etype == "run_id" and ev.get("run_id"):
                    self.state.data["active_run"]["run_id"] = \
                        str(ev["run_id"])
                    self.state.save()
                elif etype == "token":
                    parts.append(str(ev.get("content") or ""))
                elif etype == "error":
                    error_text = str(ev.get("content") or "unbekannt")
                elif etype == "approval_request":
                    run_id = str(ev.get("run_id") or "")
                    if run_id:
                        resp = await self.hive.decide_approval(run_id, "3")
                        if getattr(resp, "status_code", 200) < 300:
                            denied += 1
                            await self._edit_status(
                                status_id,
                                "🔒 Freigabe-Anfrage automatisch abgelehnt "
                                "(WP2: Freigaben vom Handy kommen mit WP4).")
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
            final_note = await self._finish(
                chat_id, q, parts, stop_reason, error_text, denied,
                status_id)
            self._clear_run()
        return final_note

    async def _finish(self, chat_id, q, parts, stop_reason, error_text,
                      denied, status_id) -> str:
        answer = "".join(parts).strip()
        answer = render.filter_secrets(answer)
        notes = []
        if denied:
            notes.append(f"🔒 {denied} Freigabe(n) automatisch abgelehnt.")
        if stop_reason == "error":
            text = f"❌ Lauf-Fehler: {error_text or 'unbekannt'}"
        elif stop_reason and stop_reason != "completed":
            base = STOP_REASON_TEXT.get(
                stop_reason, f"⏹ Lauf beendet ({stop_reason}).")
            text = base + (f"\n\n{answer}" if answer else "")
        elif answer:
            text = answer
        else:
            text = "⏹ Lauf fertig, aber ohne Textausgabe."
        for n in notes:
            text += "\n" + n

        chunks = render.split_message(text)
        if len(chunks) > FINAL_CHUNK_LIMIT:
            await self.ms.send_document(
                text.encode("utf-8"), "ergebnis.txt")
        else:
            for c in chunks:
                await self.ms.send_message(c)
        try:
            await self._write_turn(
                chat_id, {"role": "assistant", "content": text})
        except (HiveUnreachable, OSError) as exc:
            self._log_note(f"assistant turn write failed: {exc}")
        if status_id is not None:
            await self._edit_status(status_id, "✅ fertig.")
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
        return (f"⏳ Es läuft bereits ein Lauf ({rid}) — vermutlich vom "
                "Browser/UI gestartet. Warteschlange gibt es bewusst "
                "keine.")

    # -- commands -----------------------------------------------------------

    async def stop(self) -> str:
        run = self.state.data.get("active_run") or {}
        rid = run.get("run_id")
        if not rid:
            return "Kein Lauf aktiv."
        try:
            resp = await self.hive.abort_run(rid)
            code = getattr(resp, "status_code", 500)
            if code == 404:
                mapping = self._tg_chat() or {}
                await self.hive.abort_chat(mapping.get("hive_chat_id", ""))
                return (f"⏹ Run {rid} war dem Server unbekannt — "
                        "Chat-Abort als Fallback gesendet.")
            return f"⏹ Abbruch gesendet für {rid}."
        except (HiveUnreachable, OSError) as exc:
            # /stop must fail VISIBLY: an orphaned run holds VRAM
            return f"❌ /stop fehlgeschlagen ({exc}) — Lauf {rid} läuft " \
                   "evtl. weiter, bitte am PC prüfen!"

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
        return f"🆕 Neuer HiveMind-Chat: {title} ({mapping['hive_chat_id']})."

    def status_text(self) -> str:
        run = self.state.data.get("active_run") or {}
        mapping = self._tg_chat() or {}
        mode = self.state.data.get("mode") or "folgt Engine-Einstellungen"
        lines = [
            "📡 Gateway-Status",
            f"HiveMind-Chat: {mapping.get('hive_chat_id', '— (noch keiner)')}",
            f"Aktiver Lauf: {run.get('run_id') or 'keiner'}",
            f"Modus (Telegram): {mode}",
            f"Verbose: {'an' if self.verbose else 'aus'}",
        ]
        return "\n".join(lines)

    def toggle_verbose(self) -> str:
        self.verbose = not self.verbose
        self.state.data["verbose"] = self.verbose
        self.state.save()
        return f"Verbose: {'an' if self.verbose else 'aus'}"

    # -- /mode: telegram-side run mode (stream BODY field, never settings)

    def mode_text(self, arg: str) -> str:
        """/mode            -> current mode
        /mode <name>      -> set for TELEGRAM runs only
        /mode off         -> follow the engine settings again
        Aliases: chat|direct -> simple."""
        arg = (arg or "").strip().lower()
        if not arg:
            current = self.state.data.get("mode") or ""
            if current:
                return (f"Modus (Telegram): {current} — der Modus der "
                        "Browser-UI bleibt unberührt.")
            return ("Modus (Telegram): folgt den Engine-Einstellungen. "
                    "Setzen mit /mode auto|chat|pipeline|automap, "
                    "zurück mit /mode off.")
        if arg in ("off", "aus", "default"):
            self.state.data["mode"] = ""
            self.state.save()
            return "Modus (Telegram): folgt wieder den Engine-Einstellungen."
        name = MODE_ALIASES.get(arg, arg)
        if name not in MODE_CHOICES:
            return ("❌ Unbekannter Modus. Erlaubt: auto, chat (direct), "
                    "pipeline, automap — oder /mode off.")
        self.state.data["mode"] = name
        self.state.save()
        return (f"Modus (Telegram): {name}. Gilt nur für Handy-Läufe — "
                "die Browser-UI läuft weiter in ihrem eigenen Modus.")

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
        pctx = ov.get("planner_ctx")
        if pctx:
            out["duo_planner_ctx_target"] = int(pctx)
            out["duo_planner"] = True
        cctx = ov.get("coder_ctx")
        if cctx:
            out["duo_coder_ctx_agentic"] = int(cctx)
            out["duo_coder_ctx_normal"] = int(cctx)
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
            return f"🔌 HiveMind nicht erreichbar: {exc}"
        models = data.get("models") or []
        profs = {p.get("name"): p for p in (data.get("profiles") or [])}
        if not models:
            return "Keine Modelle gefunden (Engine-Antwort leer)."
        lines = ["Verfügbare Modelle:"]
        for i, m in enumerate(models, 1):
            p = profs.get(m, {})
            flags = [f for f, on in (("thinking", p.get("thinking")),
                                     ("vision", p.get("vision")),
                                     ("tools", p.get("tool_call"))) if on]
            lines.append(f"{i}. {m}"
                         + (f" [{'+'.join(flags)}]" if flags else ""))
        lines.append("Wählen mit /setModel <Nr>")
        return "\n".join(lines)

    async def set_model(self, arg: str) -> str:
        try:
            idx = int((arg or "").strip())
        except ValueError:
            return ("Benutzung: /setModel <Nr> — die Nummern liefert "
                    "/models.")
        try:
            data = await self.hive.get_models()
        except (HiveUnreachable, OSError) as exc:
            return f"🔌 HiveMind nicht erreichbar: {exc}"
        models = data.get("models") or []
        if not (1 <= idx <= len(models)):
            return (f"❌ Nr {idx} außerhalb 1–{len(models)}. Liste: "
                    "/models")
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

        head = f"Modell {idx}: {name}"
        if not can_think:
            head += " (kann kein Thinking — ctx_thinking auf 0)"
        if agentic:
            fmt = ("Antworte mit EINER Zeile, getrennt mit ', ':\n"
                   "preset, planner_ctx_thinking, planner_ctx_model, "
                   "coder_ctx_thinking, coder_ctx_model\n"
                   "(agentic erkannt — Planner und Coder je beide Werte)")
        else:
            fmt = ("Antworte mit EINER Zeile, getrennt mit ', ':\n"
                   "preset, ctx_thinking, ctx_model")
        lines = [
            head,
            fmt,
            "preset: 0 = keiner,"
            + (f" 1..{len(preset_names)} = " + ", ".join(
                f"{i+1}={n}" for i, n in enumerate(preset_names[:8]))
               if preset_names else " keine Presets vorhanden"),
            "Achtung: ein Preset gilt GLOBAL (auch für die Browser-UI).",
            "ctx_thinking 0 = kein Thinking/Planner.",
            "Beispiel: 0, 8192, 16384",
            "Gilt für Duo/Agentic-Läufe; Direct-Overrides kommen mit WP3 "
            "(P9). /cancel bricht ab.",
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
        if not all(x.lstrip("-").isdigit() for x in parts) \
                or len(parts) != expected:
            return (f"Erwartet {expected} Zahlen, getrennt mit ', ' — "
                    "z. B. 0, 8192, 16384. /cancel bricht ab.")
        vals = [int(x) for x in parts]

        # preset (position 0) — global, warned in the question already
        preset_note = "kein Preset"
        if vals[0] > 0:
            try:
                raw = await self.hive.get_presets()
            except (HiveUnreachable, OSError) as exc:
                return f"🔌 Presets nicht abrufbar: {exc}"
            names = sorted(raw.keys()) if isinstance(raw, dict) else \
                [x if isinstance(x, str) else str(x.get("name", "?"))
                 for x in (raw or [])]
            if vals[0] > len(names):
                return (f"❌ Preset {vals[0]} außerhalb 1–{len(names)}. "
                        "Erneut senden.")
            resp = await self.hive.load_preset(names[vals[0] - 1])
            if getattr(resp, "status_code", 500) >= 300:
                return (f"❌ Preset '{names[vals[0] - 1]}' konnte nicht "
                        "geladen werden — nichts geändert, erneut "
                        "senden.")
            preset_note = f"Preset '{names[vals[0] - 1]}' geladen (global)"

        ov = self.state.data.setdefault("run_overrides", {})
        ov["model"] = p["model"]
        if p.get("agentic"):
            p_think, p_ctx, c_think, c_ctx = vals[1:]
            # agentic pairs: (thinking, model) per role; ctx_thinking 0
            # means "no planner/thinking" for that role
            ov["planner_ctx"] = p_ctx if (p_think or p_ctx) else 0
            ov["coder_ctx"] = c_ctx
            detail = (f"planner: thinking_ctx={p_think or 0}, "
                      f"ctx={p_ctx or 'Standard'}; coder: "
                      f"thinking_ctx={c_think or 0} (Anmerkung), "
                      f"ctx={c_ctx or 'Standard'}")
            if not p_think and not p_ctx:
                ov["planner_ctx"] = 0
                detail = (f"planner aus (0/0); coder: ctx="
                          f"{c_ctx or 'Standard'}")
        else:
            think, ctx = vals[1:]
            ov["planner_ctx"] = ctx if think else 0
            ov["coder_ctx"] = ctx
            detail = (f"ctx_thinking={think or 0}, "
                      f"ctx_model={ctx or 'Standard'}")
        self.state.data["pending_setup"] = None
        self.state.save()
        return (f"✅ Gespeichert. Modell: {p['model']} — {preset_note}; "
                f"{detail}. Gilt für kommende Handy-Läufe (Duo/Agentic; "
                "Direct-Overrides: P9). /setModel erneut = ändern.")

    def cancel_setup(self) -> str:
        if self.state.data.get("pending_setup"):
            self.state.data["pending_setup"] = None
            self.state.save()
            return "Setup abgebrochen."
        return "Nichts abzubrechen."

    def reset_overrides(self) -> str:
        self.state.data["run_overrides"] = {}
        self.state.data["pending_setup"] = None
        self.state.save()
        return ("Modell/CTX-Overrides gelöscht — nächste Läufe nutzen "
                "wieder die Engine-Einstellungen.")
