"""Per-round duo policies (F13, 2026-11-08).

Small, pure decision helpers extracted from run_code_duo: tool-call-miss
diagnostics, the malformed/truncated tool-call policy and the
text-only/empty-answer ladder. They return a small result object; the
runner keeps loop control and emits the returned events.
"""
from __future__ import annotations

import dataclasses
import json
import logging

logger = logging.getLogger("hivemind.duo")


def _log_tool_call_miss(*, round_no, coder_mdl, thinking, tool_payload,
                        dtool_opts, coder_ctx_eff, result, dr_msg) -> None:
    """One WARNING per round whose tool calls did not parse.

    Pure diagnostics (2026-10-30, live Groot run): 7/7 coder rounds emitted
    tool calls as plain text; the request state of the failing round is the
    only evidence left, so dump it once. Never raises into the run.
    """
    try:
        import hashlib as _hl
        _miss_usage = result.get("dr_usage_final") or {}
        _miss_tools = tool_payload.get("tools") or []
        _miss_tools_json = json.dumps(
            _miss_tools, sort_keys=True, ensure_ascii=False)
        _miss_content = str(dr_msg.get("content") or "")
        _miss_reason = "".join(result.get("dr_thinking_parts") or [])
        # RAW (2026-10-31, audit): dr_msg has already been through <think>
        # preprocessing and fence stripping; the incident needs the
        # untouched SSE accumulation too.
        _miss_raw = "".join(result.get("dr_content_parts") or [])
        logger.warning(
            "[TOOL-CALL-MISS] round=%s model=%s "
            "thinking=%s ctk=%s tools=%d schema_chars=%d "
            "hash=%s tool_choice=%s slot_ctx=%s req_ctx=%s "
            "prompt_tokens=%s cached=%s finish=%s "
            "completion=%s max_tokens=%s "
            "temp=%s presence=%s top_p=%s top_k=%s min_p=%s seed=%s "
            "content[0:500]=%r reasoning[0:500]=%r "
            "raw[0:500]=%r",
            round_no, coder_mdl, thinking,
            tool_payload.get("chat_template_kwargs"),
            len(_miss_tools), len(_miss_tools_json),
            _hl.sha256(_miss_tools_json.encode("utf-8")).hexdigest()[:12],
            tool_payload.get("tool_choice"),
            dtool_opts.get("num_ctx"),
            coder_ctx_eff,
            _miss_usage.get("prompt_tokens"),
            _miss_usage.get("cached_tokens"),
            result.get("dr_finish_reason"),
            _miss_usage.get("completion_tokens"),
            tool_payload.get("max_tokens"),
            tool_payload.get("temperature"),
            tool_payload.get("presence_penalty"),
            tool_payload.get("top_p"),
            tool_payload.get("top_k"),
            tool_payload.get("min_p"),
            tool_payload.get("seed"),
            _miss_content[:500],
            _miss_reason[:500],
            _miss_raw[:500],
        )
    except (TypeError, ValueError, AttributeError, KeyError) as _miss_err:
        logger.debug("[TOOL-CALL-MISS] log failed: %s", _miss_err)


@dataclasses.dataclass
class _DroppedToolRetryResult:
    """Outcome of one malformed/truncated tool-call round."""
    action: str = "proceed"          # "continue" | "break" | "proceed"
    think_only_retries: int = 0
    dropped_tool_retries: int = 0
    force_compress_next: bool = False
    loop_detected: bool = False
    events: list = dataclasses.field(default_factory=list)


def _plan_dropped_tool_retry(
    *, result, drop_names, drop_len, dr, max_tool_rounds,
    max_tokens_round, messages, think_only_retries, dropped_tool_retries,
    force_compress_next, loop_detected, should_compress, build_retry,
    ld_setter,
) -> _DroppedToolRetryResult:
    """Decide the next step after tool calls were dropped/truncated.

    Moved verbatim from the no-tool-call branch: the 3x abort, the
    truncated-edit compression path and the targeted retry. Returns the
    action plus the counters the runner must keep; status events are
    returned for the caller to emit in order.
    """
    res = _DroppedToolRetryResult(
        think_only_retries=0,
        dropped_tool_retries=dropped_tool_retries + 1,
        force_compress_next=force_compress_next,
        loop_detected=loop_detected,
    )
    if res.dropped_tool_retries >= 3:
        ld_setter(3089)
        res.loop_detected = True
        res.events.append({
            "type": "status",
            "content": (f"⚠ Tool-call args {res.dropped_tool_retries}x "
                        "malformed/truncated — loop aborted."),
        })
        res.action = "break"
        return res
    if should_compress(result.get("dr_finish_reason"), drop_names):
        # A truncated edit_file cannot be applied half-way, so the drop is
        # right - but a retry with the SAME clamped budget truncates again
        # by construction. Give the next round fresh headroom instead.
        res.force_compress_next = True
        messages.append({"role": "user", "content": (
            "[OUTPUT LIMIT] The edit_file was cut at "
            f"this round's output budget "
            f"({int(max_tokens_round)} tokens) and was "
            "NOT applied half-way. Compressing first; "
            "then split the edit into smaller edit_file "
            "calls (or use write_file_append).")})
        logger.warning(
            "[TOOL-TRUNCATION] edit_file dropped at "
            "finish=length (budget=%d) - forcing "
            "compression instead of an identical retry",
            int(max_tokens_round))
        res.action = "continue"
        return res
    if dr < max_tool_rounds - 1:
        res.events.append({
            "type": "status",
            "content": (
                f"⚠ Tool call {res.dropped_tool_retries}/3 truncated "
                f"({'/'.join(drop_names[:3]) or '?'}): JSON args malformed/truncated"
                f"{drop_len}. Model should write large writes in steps."
            ),
        })
        messages.append({"role": "user", "content": build_retry(drop_names)})
        res.action = "continue"
        return res
    return res


@dataclasses.dataclass
class _TextOnlyRoundResult:
    """Outcome of a coder round that produced text/empty output, no tools."""
    action: str = "break"             # "continue" | "break"
    think_only_retries: int = 0
    soft_check_done: bool = False
    coder_no_think: bool = False
    zero_activity_nudged: bool = False
    loop_detected: bool = False
    events: list = dataclasses.field(default_factory=list)


def _plan_text_only_round(
    *, final, result, dr_msg, thinking_parts, file_changes, ws_str,
    seen_explore_paths, dr, max_tool_rounds, messages, parts,
    think_only_retries, soft_check_done, zero_activity_nudged,
    coder_no_think, loop_detected, coder_mdl, build_soft_check, ld_setter,
) -> _TextOnlyRoundResult:
    """Decide the next step after a text-only or empty coder round.

    Moved verbatim from the no-tool-call branch: soft-check injection, the
    final-summary acceptance ladder, the zero-activity nudge and the
    empty-answer retry. Returns the action plus counters/flags; status
    events are returned for the caller to emit in order.
    """
    res = _TextOnlyRoundResult(
        think_only_retries=think_only_retries,
        soft_check_done=soft_check_done,
        coder_no_think=coder_no_think,
        zero_activity_nudged=zero_activity_nudged,
        loop_detected=loop_detected,
    )
    if final:
        if not res.soft_check_done and file_changes:
            _sc = build_soft_check(file_changes, ws_str)
            if _sc:
                messages.append({"role": "user", "content": _sc})
                res.soft_check_done = True
                res.think_only_retries = 0
                res.action = "continue"
                return res
        parts.append(final)
        res.think_only_retries += 1
        res.coder_no_think = True
        if res.think_only_retries > 2:
            # FINAL-SUMMARY-ACCEPT: real work + substantial summary is the
            # task's conclusion, not a loop. ZERO-ACTIVITY GUARD: a summary
            # without any read/write is only an echo of the briefing and
            # gets one targeted start-working nudge first.
            _any_activity = bool(file_changes) or bool(seen_explore_paths)
            if _any_activity and ((file_changes and len(final) >= 80) or len(final) >= 200):
                res.events.append({
                    "type": "status",
                    "content": "✅ Coder concluded with a final summary — accepting it as the answer.",
                })
                res.action = "break"
                return res
            if (not _any_activity and not res.zero_activity_nudged
                    and dr < max_tool_rounds - 1):
                res.zero_activity_nudged = True
                res.think_only_retries = 0
                messages.append({"role": "user", "content": (
                    "[ZERO PROGRESS] You replied with text but have not read "
                    "or changed any file. Start implementing NOW: read_file the "
                    "relevant files, then edit_file the changes. Call "
                    "task_complete only once the work is done (or if the task "
                    "is genuinely answer-only).")})
                res.action = "continue"
                return res
            ld_setter(3089)
            res.loop_detected = True
            res.events.append({
                "type": "status",
                "content": (f"⚠ Coder replies with text only, no tool call "
                            f"({res.think_only_retries}x) — loop aborted."),
            })
            res.action = "break"
            return res
        if dr < max_tool_rounds - 1:
            _nudge = (
                "If the task is complete, call task_complete now with a brief status summary. "
                "Only call edit_file/run_bash if something is still missing."
            ) if file_changes else (
                "You must call a tool now. Do not explain — use edit_file or run_bash directly.")
            messages.append({"role": "user", "content": _nudge})
            res.action = "continue"
            return res
        res.action = "break"
        return res

    # EMPTY-RETRY: a single empty coder answer must not end the run as
    # "completed" with zero work (live: Hermes emitted reasoning only).
    res.think_only_retries += 1
    res.coder_no_think = True
    logger.warning(
        "[CODER-EMPTY-RAW] round=%d finish=%s content=%r think=%r",
        dr, result.get("dr_finish_reason"),
        (dr_msg.get("content") or "")[:300],
        "".join(thinking_parts or [])[:300],
    )
    if res.think_only_retries > 2:
        ld_setter(3113)
        res.loop_detected = True
        res.events.append({
            "type": "status",
            "content": (f"⚠ Coder returns empty answers "
                        f"({res.think_only_retries}x) — loop aborted."),
        })
        res.action = "break"
        return res
    if dr < max_tool_rounds - 1:
        if not parts:
            res.events.append({"type": "token", "content":
                f"[Tool loop: empty answer from {coder_mdl} — retrying with a nudge]"})
        messages.append({
            "role": "user",
            "content": "You returned an empty response. Call a tool now — edit_file or run_bash.",
        })
        res.action = "continue"
        return res
    if not parts:
        parts.append(f"[Tool loop: empty answer from {coder_mdl} — check llama-server log]")
    res.action = "break"
    return res


def _log_write_truncation(*, coder_mdl, round_no, finish_reason,
                          had_tool_calls, has_tool_call, content_len,
                          usage, thinking_parts) -> None:
    """Log one countable truncation event for the A/B benchmark.

    had_ counts pre-validation, has_ post-validation, so a dropped salvage
    does not read as reasoning-only. reasoning_tokens is "n/a" when the
    server does not report completion_tokens_details.
    """
    _ru = usage or {}
    _rtd = (_ru.get("completion_tokens_details") or {}
            if isinstance(_ru, dict) else {})
    _rt_raw = _rtd.get("reasoning_tokens") if isinstance(_rtd, dict) else None
    _think_chars = sum(len(p) for p in (thinking_parts or []))
    logger.warning(
        "[WRITE-TRUNCATION] model=%s round=%s finish_reason=%s "
        "had_tool_call=%s has_tool_call=%s content_len=%d "
        "reasoning_tokens=%s think_chars=%d",
        coder_mdl, round_no, finish_reason,
        had_tool_calls, has_tool_call, content_len,
        ("n/a" if _rt_raw is None else _rt_raw), _think_chars)
