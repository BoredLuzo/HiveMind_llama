"""Voice, result-mode, and finish delivery mixin for RunBridge."""
from __future__ import annotations

from httpx import HTTPError

from . import render
from .hive_client import HiveUnreachable
from .send import PolicyViolation
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


class _BridgeVoiceMixin:

    async def _progress_ok(self) -> bool:
        """telegram_progress_msgs=false silences the info channel.

        Evaluated lazily and cached for a few seconds: the bridge emits progress
        in tight loops and the settings call is an HTTP round trip.
        """
        from . import bridge as _b
        now = _b._now()
        cached = getattr(self, "_progress_cache", None)
        if cached and now - cached[0] < 5.0:
            return cached[1]
        ok = await self._progress_msgs()
        self._progress_cache = (now, ok)
        return ok


    async def _preload_voice(self) -> None:
        """Warm the TTS sidecar at run start when the result may be a voice note.

        This is what makes a phone-only flow work without touching the PC: the
        engine is up before the answer arrives, so the first spoken sentence costs
        ~1 s instead of a ~12 s sidecar start. Best effort - a failure only means
        the first sentence is late, never that the run fails.
        """
        try:
            s = await self.hive.settings()
        except (HiveUnreachable, HTTPError, OSError):
            return
        if not (s or {}).get("tts_enabled", False):
            return
        mode = str((s or {}).get("telegram_result_mode") or "both").strip().lower()
        if mode == "text" and not (s or {}).get("telegram_voice_out", False):
            return
        try:
            await self.hive.warm_voice()
        except (HiveUnreachable, HTTPError, OSError) as exc:
            self._log_note(f"voice preload skipped: {exc}")

    async def _result_mode(self) -> str:
        """text | voice | both for the FINAL answer (telegram_result_mode).

        "voice" is the pure results channel: the model-generated answer as a
        voice note, the progress lines stay text (telegram_progress_msgs can drop
        those too). Falls back to "both" when the setting is missing or invalid so
        an old settings.json never silences a run.
        """
        try:
            s = await self.hive.settings()
        except (HiveUnreachable, HTTPError, OSError):
            return "both"
        mode = str((s or {}).get("telegram_result_mode") or "both").strip().lower()
        return mode if mode in ("text", "voice", "both") else "both"

    async def _progress_msgs(self) -> bool:
        try:
            s = await self.hive.settings()
        except (HiveUnreachable, HTTPError, OSError):
            return True
        return bool((s or {}).get("telegram_progress_msgs", True))

    async def _voice_out_enabled(self) -> bool:
        """cfg.voice_out (gateway.toml) OR the engine's telegram_voice_out."""
        if self.cfg.voice_out:
            return True
        try:
            s = await self.hive.settings()
        except (HiveUnreachable, HTTPError, OSError):
            return False
        return bool((s or {}).get("telegram_voice_out", False))

    _voice_sent: bool = False

    async def _maybe_voice_answer(self, answer: str) -> None:
        """VOICE OUT (1.4, opt-in): send the final answer as a voice note.

        Never blocks or fails the run: every error is logged as a note and the
        text answer is already on its way/in the chat. The engine applies the
        voice and speed from the settings (cosette, 1.08x by default) and caps
        the text at 600 characters itself.
        """
        answer = (answer or "").strip()
        if not answer:
            return
        self._voice_sent = False
        if not await self._voice_out_enabled():
            return
        try:
            ogg, code = await self.hive.tts(answer)
        except (HiveUnreachable, HTTPError, OSError) as exc:
            self._log_note(f"voice answer skipped: {exc}")
            return
        if not ogg:
            self._log_note(f"voice answer skipped: {code or 'no audio'}")
            return
        try:
            await self.ms.send_voice(ogg, 0)
            self._voice_sent = True
        except (PolicyViolation, HiveUnreachable, HTTPError, OSError, TelegramApiError) as exc:
            self._log_note(f"voice answer send failed: {exc}")

    async def _finish(self, chat_id, q, parts, stop_reason, error_text,
                      denied, status_id, truncated: bool = False) -> str:
        from . import bridge as _b
        answer = "".join(parts).strip()
        answer = render.filter_secrets(answer)
        if truncated:
            answer = (answer + "\n\n[⚠️ Answer incomplete: tokens were dropped "
                      "by the Telegram rate limit.]").strip()
        notes = []
        if denied:
            notes.append(f"🔒 {denied} approval(s) auto-denied.")
        if stop_reason == "error":
            text = f"❌ Run error: {error_text or 'unknown'}"
        elif stop_reason and stop_reason != "completed":
            base = _b.STOP_REASON_TEXT.get(
                stop_reason, f"⏹ Run finished ({stop_reason}).")
            text = base + (f"\n\n{answer}" if answer else "")
        elif answer:
            text = answer
        else:
            text = "⏹ Run finished without any text output."
        for n in notes:
            text += "\n" + n

        # RESULT-MODE (1.4.2): text | voice | both. "voice" only makes sense for
        # a real answer - an error or an empty result stays text, otherwise the
        # owner would get silence with no explanation.
        _mode = await self._result_mode() if stop_reason != "error" and answer else "both"
        # a voice-only channel must still carry the SAFETY lines: auto-denied
        # approvals and a stop reason that is not "completed" are information the
        # owner cannot get anywhere else.
        _safety_note = bool(notes) or (stop_reason or "completed") != "completed"
        if _mode == "voice":
            if stop_reason != "error" and answer:
                await self._maybe_voice_answer(answer)
                if not self._voice_sent:
                    chunks = render.split_message(text)
                    if len(chunks) > _b.FINAL_CHUNK_LIMIT:
                        await self.ms.send_document(text.encode("utf-8"), "result.txt")
                    else:
                        for chunk in chunks:
                            await self._send_chunk(chunk)
                elif _safety_note:
                    _tail = "\n".join([n for n in notes] +
                                       ([] if (stop_reason or "completed") == "completed"
                                        else [_b.STOP_REASON_TEXT.get(
                                            stop_reason, f"⏹ Run finished ({stop_reason}).")]))
                    if _tail.strip():
                        await self._send_chunk(_tail.strip())
            else:
                chunks = render.split_message(text)
                if len(chunks) > _b.FINAL_CHUNK_LIMIT:
                    await self.ms.send_document(text.encode("utf-8"), "result.txt")
                else:
                    for c in chunks:
                        await self._send_chunk(c)
        else:
            chunks = render.split_message(text)
            if len(chunks) > _b.FINAL_CHUNK_LIMIT:
                await self.ms.send_document(
                    text.encode("utf-8"), "result.txt")
            else:
                for c in chunks:
                    await self._send_chunk(c)
            # VOICE OUT: the clean answer (no ❌/⏹ decorations) as a voice note,
            # only when the mode includes voice and the run produced text.
            if stop_reason != "error" and answer and _mode == "both":
                await self._maybe_voice_answer(answer)
        try:
            await self._write_turn(
                chat_id, {"role": "assistant", "content": text})
        except (HiveUnreachable, OSError) as exc:
            self._log_note(f"assistant turn write failed: {exc}")
        if status_id is not None:
            await self._edit_status(status_id, "✅ done.")
        return text

