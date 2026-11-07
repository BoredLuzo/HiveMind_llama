"""Duo session epilogue (2026-11-05 split 2.2).

Runs right after the final test bridge and before the finalize tail: loop
auto-summary, chat-context persistence, D4 read-stats, run counter, soul
trigger, combined output, peer ratings, file summary and the optional
pipeline synthesis. Pure emit order, no loop control. Sets `ps.session_out`
— the runner uses it as the single session-write point.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time

import httpx

from utils.bg_tasks import spawn

logger = logging.getLogger("hivemind.duo")


async def _duo_session_epilogue(ctx, ps):
    from backend.llama_client import LlamaError  # plain-Exception base; retry exhaustion re-raises it
    coder_out = ps.coder_out
    _written_files = ps._written_files
    _duo_hard_stop = ps._duo_hard_stop
    _duo_timed_out = ps._duo_timed_out
    _loop_detected = ps._loop_detected
    _lifetime_tool_rounds = ps._lifetime_tool_rounds
    _explore_only_rounds = ps._explore_only_rounds
    _cs = ps._cs
    _est_tokens = ps._est_tokens or 0
    _parts = ps._parts or []
    _plan_result = ps.carry.get("_plan_result")
    _inloop_plan_text = ps.carry.get("_inloop_plan_text") or ""
    _last_plan = ps.carry.get("_last_plan") or ""
    _read_final = ps.carry.get("_read_final")
    _touched_paths = ps.carry.get("_touched_paths")
    _total_tool_errors_ref = ps.carry.get("_total_tool_errors_ref")
    _last_run_bash_failure = ps.carry.get("_last_run_bash_failure")
    if _loop_detected:
        _written_list = sorted(ps._written_files or [])
        _auto_summary = (
            f"[AUTO-STOP] Run terminated (loop_detected).\n"
            f"Files written ({len(_written_list)}): "
            + (", ".join(_written_list[:12]) + ("..." if len(_written_list) > 12 else "")
               if _written_list else "none")
            + f"\nTool rounds used: {ps._lifetime_tool_rounds}."
        )
        # The values live in run_code_duo's scope; the 2.2 carve moved this
        # block into a module, so a locals() probe here always came up empty.
        _drop_diag_names = list(ps.carry.get("_drop_names") or [])[-8:]
        _drop_diag_retries = ps.carry.get("_dr_dropped_tool_retries", "n/a")
        logger.warning(
            "[AUTO-STOP] loop_detected Diagnose: tool_errors=%d explore_only_rounds=%d "
            "last_bash_failure=%s written_files=%d dropped_tool_retries=%s drop_names=%s",
            (ps.carry['_total_tool_errors_ref'][0] if ps.carry['_total_tool_errors_ref'] else -1),
            ps._explore_only_rounds,
            ps.carry['_last_run_bash_failure'],
            len(_written_list),
            _drop_diag_retries,
            _drop_diag_names,
        )
        yield await ctx.emit({"type": "token", "content": "\n\n" + _auto_summary})
        try:
            _auto_parts = _parts
        except (NameError, UnboundLocalError):
            _auto_parts = []
        _auto_parts.append(_auto_summary)

    ctx.memory.add_to_session("user", ctx.user_input)
    # Persist files_read + touched_paths from this run into .context.json
    # so follow-up runs within the same session retain read-guard state.
    # ABORTED-SAVE (audit 2026-11-03): an aborted/disconnected run used to
    # skip this block entirely (making _eff_stop="aborted" dead), so a
    # reopened chat had no last_run and the follow-up answered blind. Save
    # the task/plan even then; the status says "aborted".
    if ctx.chat_id:
        try:
            from tools.runner import _files_read_in_run
            ps.carry['_read_final'] = _files_read_in_run.get(None)
            ps.carry['_last_plan'] = ((_plan_result.plan_content if _plan_result else "") or _inloop_plan_text or "")[:8000]
            if _duo_timed_out:
                _eff_stop = "timeout_guard"
            elif _loop_detected:
                _eff_stop = "loop_detected"
            elif ctx.aborted():
                _eff_stop = "aborted"
            else:
                _eff_stop = "completed"
            # ATOMIC-CONTEXT (audit 2026-11-03): load-modify-save raced with
            # the resume-block writer (which used _mutate_chat_context) and
            # could drop its fields. Mutate under the per-chat lock.
            from context.chat import _mutate_chat_context as _mut_ctx

            def _apply_run_ctx(_c):
                _c["files_read_in_run"] = list(ps.carry['_read_final']) if ps.carry['_read_final'] else []
                _c["touched_paths"] = list(ps.carry['_touched_paths']) if ps.carry['_touched_paths'] else []
                _c["task_sig"] = hashlib.md5(
                    ctx.user_input[:60].encode()).hexdigest()[:8]
                # Persist the session per chat (restart-safe); injection uses
                # compute_char_caps/budget_session_msgs.
                _c["session"] = ctx.memory.get_session_messages(
                    limit=6, user_cap=8000, assistant_cap=8000)
                _c["last_run"] = {
                    "task": ctx.user_input[:8000],
                    "stop_reason": _eff_stop,
                    "written_files": sorted(set(ps._written_files or []))[:50],
                    "plan": ps.carry['_last_plan'],
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "success": bool(_eff_stop == "completed"),
                }

            _mut_ctx(ctx.chat_id, _apply_run_ctx)
        except (ImportError, OSError, RuntimeError, ValueError, TypeError,
                KeyError, AttributeError) as _ctx_err:
            logger.debug("[CTX] Chat context save failed: %s", _ctx_err)

    # D4-DIAG (2026-08-21): measure re-read inflation — repeated read_file
    try:
        _parts_joined = "".join(ps._parts) if isinstance(ps._parts, list) else ""
        _rf_calls = len(re.findall(r"read_file\s*\(", _parts_joined))
        _rf_paths = set(re.findall(r"read_file\([^)]*path\s*[:=]\s*['\"]([^'\"]+)['\"]", _parts_joined))
        _rf_bytes = 0
        for _rfm in re.finditer(
            r"\n?\[[^\]]*(?:total lines|lines \d+-\d+)[^\]]*\]\n(.*?)(?=\n\[[^\]]*(?:total lines|lines \d+-\d+)[^\]]*\]|$)",
            _parts_joined, re.S,
        ):
            _rf_bytes += len(_rfm.group(1))
        logger.warning(
            "[READ-STATS] read_file_calls=%d unique_paths=%d re_read_ratio=%.1f approx_bytes=%d est_ctx=%d",
            _rf_calls, len(_rf_paths),
            ((_rf_calls / len(_rf_paths)) if _rf_paths else 0.0),
            _rf_bytes, int(ps._est_tokens or 0),
        )
    except (OSError, RuntimeError, ValueError, TypeError, re.error) as _rs_err:
        logger.debug("[READ-STATS] Evaluation failed: %s", _rs_err)

    # Same counter increment as the direct_runner and pipeline_runner paths.
    try:
        _duo_run_count = ctx.increment_run_counter()
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as _rc_err:
        _duo_run_count = 0
        logger.warning("[RUN-COUNTER] Duo increment failed: %s", _rc_err)

    # soul_evolve_agent.enabled + Run-Intervall (server.py).
    if _duo_run_count:
        try:
            spawn(ctx.maybe_trigger_soul_evolution(_duo_run_count))
        except (OSError, RuntimeError, ValueError, TypeError) as _se_err:
            logger.warning("[SOUL] Evolution trigger failed: %s", _se_err)


    # KRIT-3 FIX: _duo_session_out as the single session-write point.
    # Old: ctx.memory.add_to_session("assistant", ps.coder_out) in the if-not-duo branch +
    #      ctx.memory.add_to_session("assistant", _combined_session) in the synth branch
    # CHUNKING FIX: Synthesis gets the combined output of all subtasks.
    # ChunkState.combine_outputs() handles the merging + completion notes.
    _combined_coder_out = ps._cs.combine_outputs(fallback_coder_out=ps.coder_out)
    _duo_session_out = _combined_coder_out

    _ratings_agentic = bool(ctx.settings.get("duo_peer_ratings_agentic", False))
    _rating_source = _combined_coder_out or coder_out
    if _rating_source and not ctx.aborted() and not ps._duo_hard_stop and (not ctx.duo_config.agentic_mode or _ratings_agentic):
        _duo_rating_out = {
            "duo_coder":  _rating_source,
            "duo_critic": ps.carry['final_verdict'],
            "duo_coder_model": ps.carry['coder_mdl'],
        }
        spawn(ps.carry['run_peer_ratings'](
            ctx.run_id, ctx.user_input, _duo_rating_out, ctx.use_learned,
            rating_mode="duo",   # Duo-spezifische Rating-Paare
            has_images=bool(ctx.images),
        ))

    # ── File-Change Summary ──
    if ps._cs.has_written_files:
        _fc_summary = ", ".join(list(ps._written_files)[:8]) if _written_files else ""
        _fc_more = f" +{len(ps._written_files)-8} more" if len(ps._written_files or []) > 8 else ""
        yield await ctx.emit({"type": "status",
            "content": f"📁 {len(ps._written_files or [])} file(s) changed: {_fc_summary}{_fc_more}"})

    if ctx.duo_config.use_pipeline and not ctx.duo_config.agentic_mode and _combined_coder_out and not ctx.aborted() and not ps._duo_hard_stop:
        yield await ctx.emit({"type": "status", "content": "⚙ Duo → Pipeline: synthesizer analysis..."})
        yield await ctx.emit({"type": "pipeline_start", "content": ctx.user_input})
        _duo_pipeline_input = (
            f"Task: {ctx.user_input}\n\n"
            f"The following code was generated by the Duo mode:\n\n{_combined_coder_out}\n\n"
            f"Review the code and list OPEN POINTS as concrete developer tasks."
        )
        _pipe_synth = ctx.pipeline.agents.get("synthesizer")
        if _pipe_synth:
            _pipe_sys = (
                ctx.get_effective_prompt_with_override("duo_synthesizer", ctx.active_preset, ctx.use_learned)
                or ctx.get_effective_prompt_with_override("synthesizer", ctx.active_preset, ctx.use_learned)
            )
            _pipe_msgs = ctx.make_messages(ctx.pipeline, _pipe_sys, _duo_pipeline_input, [], False, True,
                                        cached_mem_ctx=ctx.pipeline_mem_ctx)
            yield await ctx.emit({"type": "agent", "content": "Synthesis",
                              "model": ctx.registry_get("synthesizer"),
                              "role": "Final quality assessment"})
            _pparts, _pt = [], time.time()
            try:
                async for _tok in ctx.pipeline_chat_stream(_pipe_synth.model, _pipe_msgs,
                                                        _pipe_synth.temperature, _pipe_synth.max_tokens):
                    if ctx.aborted(): break
                    _pparts.append(_tok)
                    yield await ctx.emit({"type": "token", "content": _tok})
            except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                    AttributeError, httpx.HTTPError, LlamaError) as _pe:
                yield await ctx.emit({"type": "token", "content": f"[Pipeline-Error: {str(_pe)[:80]}]"})
            _pipe_out = "".join(_pparts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
            yield await ctx.emit({"type": "agent_done", "elapsed": round(time.time() - _pt, 1)})
            _duo_session_out = (
                f"[Code generated for: {ctx.user_input[:80]}{'...' if len(ctx.user_input) > 80 else ''}]\n\n"
                f"{_pipe_out}"
            )

    ctx.memory.add_to_session("assistant", _duo_session_out)  # single write point
    # SIDECAR-SESSION-AFTER-ASSISTANT (audit 2026-11-03): the sidecar save
    # above ran before this line, so its "session" was permanently the
    # current user turn; the next run cleared memory and dropped it as a
    # trailing user -> json-less (API) chats never accumulated history.
    if ctx.chat_id:
        try:
            from context.chat import _mutate_chat_context
            _sess_now = ctx.memory.get_session_messages(
                limit=6, user_cap=8000, assistant_cap=8000)
            _mutate_chat_context(
                ctx.chat_id, lambda _c: _c.__setitem__("session", _sess_now))
        except (ImportError, OSError, ValueError, TypeError) as _se:
            logger.debug("[CTX] session sidecar update failed: %s", _se)

    ps.session_out = _duo_session_out
