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

import time

import logging

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
        phone itself. Never raises to the caller."""
        try:
            return await self._start_text_run_inner(q)
        except HiveUnreachable as exc:
            self._clear_run()
            return await self._fail(f"🔌 HiveMind nicht erreichbar "
                                    f"(Offline?): {exc}")
        except OSError as exc:
            self._clear_run()
            return await self._fail(f"❌ Netzwerkfehler: {exc}")

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
            async for ev in self.hive.stream(q, chat_id):
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
        lines = [
            "📡 Gateway-Status",
            f"HiveMind-Chat: {mapping.get('hive_chat_id', '— (noch keiner)')}",
            f"Aktiver Lauf: {run.get('run_id') or 'keiner'}",
            f"Verbose: {'an' if self.verbose else 'aus'}",
        ]
        return "\n".join(lines)

    def toggle_verbose(self) -> str:
        self.verbose = not self.verbose
        self.state.data["verbose"] = self.verbose
        self.state.save()
        return f"Verbose: {'an' if self.verbose else 'aus'}"
