"""Duo finalization: the run's closing sequence (2026-11-05 split 2.1).

Extracted from run_code_duo verbatim (stop-reason ladder → final
auto-commit/push → checkpoint consolidation → resume safety net →
project-state save → run audit → send-file delivery → done event →
post-done cleanup/spawns). Runs ONCE per run, after the chunk loop —
no loop control, pure emit order. The done event is the terminal emit
of this path; the runner yields this generator last.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from hive_functions.loop_machine import AgentState

logger = logging.getLogger("hivemind.duo")


async def _duo_run_finalize(ctx, ps):
    """Yield the closing sequence. Reads/writes everything through `ps`
    (the runner syncs all fields back in its finally).

    ctx.duo_stop_reason is SET here (the ladder) and must only be read
    after the runner's sync.
    """
    from core.duo.preflight import _git_checkpoints_enabled
    from core.duo_runner import (
        _auto_commit_chunk, _auto_push_after_commit,
        _load_resume_block, _write_resume_block, _warm_workers_fn,
        _cleanup_governor, _cleanup_pause_state,
    )
    from infra.run_control import clear_graceful_stop
    from context.pause_state import clear_pause_state as _clear_pause_state
    from utils.bg_tasks import spawn
    from vram.loader import _refresh_judge_keepalive

    if ctx.aborted() or (ctx.chat_id and ctx.is_aborted_chat(ctx.chat_id)):
        ctx.duo_stop_reason = "aborted"
    elif ps._graceful_stopped:
        ctx.duo_stop_reason = "graceful_stop"
    elif ps._duo_timed_out:
        ctx.duo_stop_reason = "timeout"
    elif ps._duo_hard_stop:
        ctx.duo_stop_reason = "hard_stop"
    elif ps._loop_detected and ps._wedge_escalated:
        # WEDGE-HANDOFF: distinct stop reason — the run died on a wedged edit
        # whose repair handoff failed or exhausted its budget (human escalation).
        ctx.duo_stop_reason = "wedge_escalated"
    elif ps._model_load_failed:
        ctx.duo_stop_reason = "model_load_failed"
    elif ps._loop_detected:
        ctx.duo_stop_reason = "loop_detected"
    elif ctx.exec_ctrl.state == AgentState.HALTED:
        ctx.duo_stop_reason = (
            ctx.exec_ctrl.stop_reason.value if ctx.exec_ctrl.stop_reason else "halted"
        )
    else:
        ctx.duo_stop_reason = "completed"
    # ── B8.3 FINAL AUTO-COMMIT (Run-Abschluss) ─────────────────────────────
    # LOOP-AUTO-STOP COMMIT (2026-10-08, user feedback: unexpected run stops): the
    # owner aborts stay commit-free by design.
    if (ctx.duo_config.git_autocommit
            and ctx.duo_stop_reason in ("completed", "graceful_stop", "loop_detected")
            and ps._ws_str):
        _ac_final = await _auto_commit_chunk(ctx.user_input, "", ps._ws_str, True,
                                             files=list(ps._cs.written_files[-50:]),
                                             commit_allowed=getattr(ctx, "workspace_commit_allowed", True))
        if _ac_final:
            yield await ctx.emit({"type": "status", "content": _ac_final})
        if _ac_final and _ac_final.startswith("✅"):
            _push_out = await _auto_push_after_commit(ps._ws_str)
            if _push_out:
                yield await ctx.emit({"type": "status", "content": _push_out})
    elif (_git_checkpoints_enabled(ctx)
          and not ctx.duo_config.git_autocommit
          and ctx.duo_stop_reason in ("completed", "graceful_stop")
          and ps._ws_str):
        try:
            from hive_functions.git_tools import exec_git_squash_checkpoints as _sq
            # NO prefix read here anymore (2026-11-03): consolidate_only
            # builds its own "<prefix> checkpoint: session consolidated (N
            # checkpoints)" marker message; the `message` argument is only
            # used by the run-file fold path.
            _sq_out = await _sq("checkpoint consolidation",
                                ps._ws_str, consolidate_only=True)
            if _sq_out:
                yield await ctx.emit({"type": "status", "content": _sq_out})
            if _sq_out and _sq_out.startswith("✅"):
                _push_out = await _auto_push_after_commit(ps._ws_str)
                if _push_out:
                    yield await ctx.emit({"type": "status", "content": _push_out})
        except (ImportError, OSError, RuntimeError, ValueError) as _sq_err:
            logger.debug("[GIT-CONSOLIDATE] skipped: %s", _sq_err)
    # AUDIT-FIX D2: resume safety net — on every non-complete stop
    # (abort/timeout/hard_stop/loop) write a resume block, so the
    # next start can continue. Only if none exists yet.
    if (ctx.chat_id
            and ctx.duo_stop_reason not in ("completed", "graceful_stop")):
        try:
            _di_safe = ps._di
            _n_items_safe = ps._n_items
            _loop_items_safe = ps._loop_items or [{"title": ctx.user_input}]
            if _di_safe < _n_items_safe:
                if not _load_resume_block(ctx.chat_id):
                    _rem_d2 = [{"title": str(t)} for t in _loop_items_safe[_di_safe:]]
                    if _rem_d2:
                        _write_resume_block(
                            chat_id=ctx.chat_id,
                            workspace=ps._ws_str,
                            chunks_total=_n_items_safe,
                            chunks_done=ps._done_tasks,
                            chunks_remaining=_rem_d2,
                            written_files=ps._written_files,
                            last_summary=" | ".join(ps._done_tasks),
                            plan_msgs=[],
                            explore_ctx=ps._explore_ctx,
                            halt_reason=ctx.duo_stop_reason,
                            pending_send_files=list(ps._pending_send_files),
                        )
        except (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt,
                SystemExit):
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                AttributeError, IndexError) as _d2_err:
            if isinstance(_d2_err, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            logger.warning("[RESUME-SAFETY-NET] Write failed: %s", _d2_err)
    if ctx.chat_id and ctx.duo_stop_reason == "completed":
        ctx.clear_resume_block(ctx.chat_id)
    ctx.phase_timer.end("coder_loop", status=ctx.duo_stop_reason)
    logger.info("Phase-Timing: %s", ctx.phase_timer.ui_summary())

    # ── ProjectState: persist run result ──
    if ps._project_state is not None and ctx.chat_id:
        try:
            from context.project_state import ProjectStateManager
            ps._project_state.last_run_timestamp = datetime.now().isoformat()
            ps._project_state.last_run_success = (ctx.duo_stop_reason == "completed")
            if not ps._project_state_run_counted:
                ps._project_state.total_runs += 1
                ps._project_state_run_counted = True
            ProjectStateManager().save(ps._project_state)
            logger.info("[PROJECT] State saved after run #%d (%s)",
                        ps._project_state.total_runs, ctx.duo_stop_reason)
        except (ImportError, OSError, RuntimeError, ValueError, TypeError) as _ps_err:
            logger.warning("[PROJECT] Normal save failed: %s", _ps_err)

    try:
        from core.run_audit import record_run_audit
        record_run_audit(ctx.chat_id, {
            "run_id": ctx.run_id,
            "event": "run_end",
            "stop_reason": str(ctx.duo_stop_reason or ""),
            "written_files": sorted(ps._written_files or []),
            "loop_detected": bool(ps._loop_detected),
            "explore_only_rounds": int(ps._explore_only_rounds),
            "total_tool_rounds": int(ps._lifetime_tool_rounds),
        })
    except (ImportError, OSError, RuntimeError, ValueError, TypeError):
        pass

    # ── SEND-FILE: last action, after final verification ────────────────
    # The tool only queues paths. A completed Telegram run delivers them now,
    # after the verify gate, so a file can never go out before the final test.
    # If the model forgot to call send_file, exactly one deliverable file in
    # the run is sent automatically; multiple candidates stay manual.
    _loop_file_delivery = (ctx.duo_stop_reason == "loop_detected"
                           and bool(ps._pending_send_files))
    if ((ctx.duo_stop_reason == "completed" or _loop_file_delivery)
            and str(ctx.settings.get("source") or "").startswith("telegram")
            and not ctx.aborted()):
        if _loop_file_delivery:
            # The model explicitly queued this file before the loop guard
            # stopped the run; dropping it silently loses work the owner
            # was already told about. Deliver only the explicit queue (no
            # auto-pick), with a warning.
            try:
                yield await ctx.emit({
                    "type": "status",
                    "content": ("⚠ Run ended in a loop — delivering the "
                                "explicitly queued file(s) now."),
                })
            except (OSError, RuntimeError, ValueError, TypeError) as _emit_err:
                logger.debug("[SEND-FILE] loop-delivery status emit failed: %s",
                             _emit_err)
            _deliver_files = list(ps._pending_send_files)
        else:
            try:
                from core.send_files import choose_deliverables as _choose_sf
                _deliver_files = _choose_sf(
                    ps._pending_send_files, ps._written_files, ps._ws_str)
            except (ImportError, TypeError, ValueError):
                _deliver_files = list(ps._pending_send_files)
        for _sfp in _deliver_files:
            logger.info("[SEND-FILE] final delivery after verification: %s",
                        _sfp)
            yield await ctx.emit({
                "type": "send_file",
                "path": _sfp,
                "workspace": ps._ws_str,
            })
    yield await ctx.emit(ctx.done_event(round(time.time() - ctx.t_total, 1), ctx.duo_stop_reason, **ctx.collect_done_metrics()))
    try:
        from tools.runner import _files_written_by_run as _fw_cleanup
        _fw_cleanup.pop(str(ctx.run_id), None)
    except (ImportError, AttributeError, TypeError, KeyError) as _fw_err:
        logger.debug("[SEND-FILE] written-file registry cleanup failed: %s", _fw_err)
    ctx.unregister_abort(ctx.run_id)
    ctx.unregister_step_skip(ctx.run_id)  # P7 FIX: was missing — step_skip event leaked after Duo runs
    clear_graceful_stop(ctx.run_id)
    _cleanup_pause_state(ctx.run_id)
    if ctx.chat_id:
        _clear_pause_state(ctx.chat_id)
    _cleanup_governor(ctx.chat_id or ctx.run_id)
    from core.duo_runner import _current_project_state
    _current_project_state.set(None)
    # AUDIT-FIX 2026-08-03 (preload_workers_after_run, strenges Opt-in, Default False):
    if bool(ctx.settings.get("preload_workers_after_run", False)):
        _wk_slots_run = ps.carry.get("_worker_slots") or []
        if _wk_slots_run:
            # WORKER-PIN (2026-11-01): the opt-in now also pins the reloaded
            # workers — unpin_all() at the next run start releases them.
            spawn(ps.carry["_warm_workers_fn"](list(_wk_slots_run),
                                               ps.carry["exec_mdl"], pin=True))
    spawn(_refresh_judge_keepalive())
    # ── Insight Extractor: Post-loop structured learning ──────────────────
    # Fire-and-forget background task — extracts structured insights from the
    # completed agentic loop using the INSIGHT_EXTRACTOR prompt.
    if ps._written_files and ctx.duo_stop_reason == "completed" and not ctx.aborted():
        spawn(ctx.run_insight_extractor(
            task=ctx.user_input,
            written_files=list(ps._written_files),
            critic_verdict=ps.carry.get("final_verdict"),
            critic_issues=list(ps.carry.get("critic_issues") or []),
            workspace=ps._ws_str,
        ))
    # ── Skill Distillation: Run after every completed Duo loop ─────────────
    # Previously only ran during soul evolution (too infrequent).
    # Now runs after each Duo completion so insights get decayed/merged promptly.
    if ctx.pipeline.memory and ctx.duo_stop_reason == "completed":
        spawn(asyncio.to_thread(
            ctx.run_soul_cycle, ctx.pipeline.memory, ctx.settings,
            list(ps.carry.get("_recent_focus_paths") or []), ps.carry["this_file"]
        ))
    # ── Skill distillation (phase 2): recurring patterns → real skills ──
    # Gated by soul_skill_writing (repurposed as "auto-distillation"). Max 1
    if (ctx.pipeline.memory and ctx.duo_stop_reason == "completed"
            and not ctx.aborted() and ctx.settings.get("soul_skill_writing", False)):
        spawn(ctx.run_skill_distillation(ps._ws_str))
    # Keep the vision slot warm
    _vis_mdl = ctx.vision_cfg.get("model", "") if ctx.vision_cfg.get("enabled") else ""
    if _vis_mdl:
        spawn(ps.carry["_keepalive_vision_fn"](_vis_mdl))
