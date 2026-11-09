"""Duo compress cycle (2026-11-05 split 2.8).

The in-round context compression: pin-preserving LLM compression with
the light compression-model choice, partial (byte-stable tail)
compression, repo-map delta, mini-shrink retry, the rule-based
fallback, the fail-streak stop and the post-compress bookkeeping
(est-tokens reset, read-guard path harvest). Runs inside the runner's
`if _compress_ok:` branch of the tool-round loop. Verbatim code motion
— no logic changes. The two "compression repeatedly fails" round-loop
exits set `ps.stop = True` and return; the runner does the `break`.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import asyncio
import logging
import os
import re

import httpx

if TYPE_CHECKING:
    from core.plan_tracker import PlanTracker
    from hive_functions.planner import PlannerResult

from context.compression import _compress_rule_based, _compress_tool_context
from context.ctx_guard import (
    plan_partial_cut_index as _ctx_plan_partial_cut_index,
    should_use_partial as _ctx_should_use_partial,
)
from core.duo.preflight import (
    _build_plan_anchor_text, _build_plan_pin_msg, _ld_setter,
    _strip_stale_ctx_notices,
)
from core.duo_helpers import (
    _RE_UNIX_PATH, _RE_WIN_PATH, _compress_fail_streak_update,
)
from utils.file import normalize_tool_path as _normalize_tool_path
from utils.token import estimate_ctx_tokens as _estimate_ctx_tokens

logger = logging.getLogger("hivemind.duo")

# F4 (deep audit 2026-11-07): the post-compress re-anchor inventory is a full
# os.walk over the workspace - synchronous I/O that must never run on the
# asyncio event loop. Extracted verbatim from the inline loop (same ignore
# list, same stop-at-first-`cap`-files semantics) so it can be dispatched with
# asyncio.to_thread. The final sort only makes the message order deterministic
# (the old walk order was whatever the filesystem enumerated); the set of
# collected files and the cap are unchanged.
_COMPRESS_INVENTORY_IGNORE_DIRS = ("node_modules", ".git", "dist",
                                   ".hive_uploads", "__pycache__")


def _walk_workspace_inventory(ws_str: str, cap: int = 80) -> list[str]:
    """SYNC helper — the caller MUST run it via ``asyncio.to_thread``.

    Relative (forward-slash) paths of the first `cap` files found by a
    top-down workspace walk, sorted for deterministic output.
    """
    _inv: list[str] = []
    _inv_n = 0
    for _iroot, _idirs, _ifnames in os.walk(ws_str):
        _idirs[:] = [x for x in _idirs if x not in _COMPRESS_INVENTORY_IGNORE_DIRS]
        for _ifn in _ifnames:
            if _inv_n >= cap:
                break
            _irel = os.path.relpath(
                os.path.join(_iroot, _ifn), ws_str)
            _inv.append(_irel.replace("\\", "/"))
            _inv_n += 1
        if _inv_n >= cap:
            break
    return sorted(_inv)


async def _duo_compress_cycle(ctx, ps):
    exec_mdl = ps.carry["exec_mdl"]
    _call_sigs = ps._call_sigs
    _coder_est_at_real = ps._coder_est_at_real
    _coder_real_prompt_tokens = ps._coder_real_prompt_tokens
    _comp_llm_cfg = ps._comp_llm_cfg
    _comp_llm_timeout_s = ps._comp_llm_timeout_s
    _compress_local_only = ps._compress_local_only
    _compress_threshold = ps._compress_threshold
    _di = ps._di
    _done_tasks = ps._done_tasks
    _dport = ps._dport
    _dtc = ps._dtc
    _dtool_ctx = ps._dtool_ctx
    _dtool_sys_eff = ps._dtool_sys_eff
    _explore_ctx = ps._explore_ctx
    _goal_pin_msg = ps._goal_pin_msg
    _guard_decision = ps._guard_decision
    _guard_tokens = ps._guard_tokens
    _last_test_status = ps._last_test_status
    _lifetime_tool_rounds = ps._lifetime_tool_rounds
    _max_tool_rounds = ps._max_tool_rounds
    _n_items = ps._n_items
    _partial_compression = ps._partial_compression
    _plan_result = ps._plan_result
    _plan_tracker = ps._plan_tracker
    _read_counts = ps._read_counts
    _seen_explore_paths = ps._seen_explore_paths
    _subtasks = ps._subtasks
    _tool_ctx_lru = ps._tool_ctx_lru
    _written_files = ps._written_files
    _ws_str = ps._ws_str
    _MAX_COMPRESSIONS = ps._MAX_COMPRESSIONS
    _compress_fail_streak = ps._compress_fail_streak
    _ctx_compressions = ps._ctx_compressions
    _ctx_critical_warned = ps._ctx_critical_warned
    _ctx_full_skips = ps._ctx_full_skips
    _ctx_pressure_warned = ps._ctx_pressure_warned
    _dtool_msgs = ps._dtool_msgs
    _est_tokens = ps._est_tokens
    _explore_only_rounds = ps._explore_only_rounds
    _force_compress_next = ps._force_compress_next
    _llm_compress_fails = ps._llm_compress_fails
    _loop_detected = ps._loop_detected
    _no_write_nudged = ps._no_write_nudged
    _no_write_rounds = ps._no_write_rounds
    _partial_escalated = ps._partial_escalated
    _rule_compress_fails = ps._rule_compress_fails
    _rule_compress_used = ps._rule_compress_used
    _total_tool_rounds = ps._total_tool_rounds

    def _sync_state() -> None:
        """Write every field the runner consumes; every exit calls this.

        F1 (deep audit 2026-11-07): the cycle used to sync 18 fields at the
        natural end but only 5/11 at the two stop returns. Callers of the
        early exits silently lost whichever counter changed before the stop.
        """
        ps._MAX_COMPRESSIONS = _MAX_COMPRESSIONS
        ps._compress_fail_streak = _compress_fail_streak
        ps._ctx_compressions = _ctx_compressions
        ps._ctx_critical_warned = _ctx_critical_warned
        ps._ctx_full_skips = _ctx_full_skips
        ps._ctx_pressure_warned = _ctx_pressure_warned
        ps._dtool_msgs = _dtool_msgs
        ps._est_tokens = _est_tokens
        ps._explore_only_rounds = _explore_only_rounds
        ps._force_compress_next = _force_compress_next
        ps._llm_compress_fails = _llm_compress_fails
        ps._loop_detected = _loop_detected
        ps._no_write_nudged = _no_write_nudged
        ps._no_write_rounds = _no_write_rounds
        ps._partial_escalated = _partial_escalated
        ps._rule_compress_fails = _rule_compress_fails
        ps._rule_compress_used = _rule_compress_used
        ps._total_tool_rounds = _total_tool_rounds

    # COMPRESS-DIAG (2026-08-17): the reason is logged with the trigger below.
    _compress_reason = (
        "force" if _force_compress_next else
        getattr(_guard_decision, "reason", "threshold")
    )
    logger.warning(
        "[CTX-COMPRESS] trigger=%s est=%d threshold=%d ctx=%d rounds=%d real=%d",
        _compress_reason, int(_est_tokens), int(_compress_threshold),
        int(_dtool_ctx), _total_tool_rounds, int(_guard_tokens),
    )
    _force_compress_next = False
    yield await ctx.emit({"type": "status",
        "content": f"🗜 Context compression ({int(_est_tokens)} est. tokens → compressing...)"})
    yield await ctx.emit({"type": "ctx_meter",
        "est_tokens": int(_coder_real_prompt_tokens[0] or _est_tokens),
        "real_tokens": int(_coder_real_prompt_tokens[0]), "ctx_limit": _dtool_ctx,
        "compressing": True})
    # PIN-PRESERVING COMPRESSION (2026-09-06): the system
    # message that is actually in _dtool_msgs may carry the
    # pinned static repo-map (byte-stable prefix). Rebuilding
    # it from the raw template (_dtool_sys_eff) would drop
    # the map on every compression and reset the cache base
    # to ~system+tools again. Prefer the live system content.
    _live_sys = (
        _dtool_msgs[0].get("content", "")
        if _dtool_msgs and isinstance(_dtool_msgs[0], dict)
        and _dtool_msgs[0].get("role") == "system"
        else ""
    )
    _sys_for_compress = str(_live_sys or _dtool_sys_eff)
    _plan_state = ""
    if _plan_tracker and _plan_tracker.total > 0:
        _cur = _plan_tracker._current_step()
        _cur_label = _cur.intent if _cur else "?"
        _done = len(_plan_tracker._plan.completed_step_ids)
        _plan_state = (
            f"Plan: step {_done + 1}/{_plan_tracker.total} "
            f"— {_cur_label}"
        )
    # PLAN-ANCHOR-FIX (2.2): anchor text via pure module function
    # (testable, identical logic — see _build_plan_anchor_text).
    # BODY-EXCERPT: plan_content rides along so visual
    # details survive local-only compression.
    _plan_anchor_text = _build_plan_anchor_text(
        _subtasks, _plan_tracker,
        plan_content=(getattr(_plan_result, "plan_content", "") or "")
        if _plan_result is not None else "")
    # CACHE-FRIENDLY partial vs full (2026-09-04):
    # partial keeps a large raw tail byte-identical
    # at the end -> llama.cpp --cache-reuse KV shift
    # can rescue the suffix after the rebuild.
    _comp_mode = "full"
    _comp_cut = -1
    if _partial_compression and not _partial_escalated:
        if _ctx_should_use_partial(partial_enabled=True, messages=_dtool_msgs):
            _cut_ratio = (
                (int(_guard_tokens) / int(_est_tokens))
                if int(_est_tokens or 0) > 0 else 1.0
            )
            _comp_cut = _ctx_plan_partial_cut_index(
                _dtool_msgs,
                ctx_tokens=int(_dtool_ctx),
                guard_tokens=int(_guard_tokens),
                token_ratio=_cut_ratio,
            )
            if _comp_cut is not None and int(_comp_cut) >= 2:
                _comp_mode = "partial"
    _msgs_before_compress = _dtool_msgs
    # EST-REUSE: _est_tokens stays current through the round.
    _est_tokens_before_compress = int(_est_tokens)
    # COMPRESSION-MODEL (2026-09-05): the summary runs
    # on a light model (duo_compress_model) when it
    # fits without evicting the coder (VRAM can_fit);
    # otherwise fall back to the coder model. Read
    # timeout configurable (duo_compress_llm_timeout_s).
    # FOLLOW-UP (2026-09-07, observed live): ensure_loaded
    # can evict the coder (Vulkan serialization,
    # VRAM 4.9+2.1>7.5) — light compression is faster
    # (~50s vs ~110s) but costs a coder reload (~20s)
    # + complete cache loss (next round reuse=0%).
    # Trade-off consciously accepted. Later candidates:
    # duo_partial_compression (byte-stable tail -> KV shift)
    # and/or pinning the coder during the run. Do not
    # silently forget — see docs/architecture.md.
    _comp_mdl = exec_mdl
    _comp_port = _dport
    # LOCAL-ONLY: no LLM compression call happens, so
    # loading a light compressor (up to 240s wait, can
    # evict the coder) would be pure wasted pause.
    if (_comp_llm_cfg and _comp_llm_cfg != exec_mdl
            and not _compress_local_only):
        try:
            from backend.llama_server_manager import manager as _lsm_comp
            # NO-EVICT (2026-10-14, audit phase 4): the
            # fit check must treat the coder slot's VRAM
            # as unavailable — the documented live case
            # (light model evicted the coder: ~20s
            # reload + reuse=0%) cost more than the
            # slower self-compression. If the light
            # model does not fit NEXT TO the coder, the
            # coder compresses itself (default below).
            _coder_slot_for_comp = None
            try:
                _coder_slot_for_comp = _lsm_comp.get_slot_by_port(_dport)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError):
                _coder_slot_for_comp = None
            _coder_slot_id = getattr(_coder_slot_for_comp, "slot_id", None)
            _comp_ctx_try = 16384
            _fit_comp = _lsm_comp.can_fit(
                _comp_llm_cfg, _comp_ctx_try,
                exclude_slot_id=_coder_slot_id)
            if not _fit_comp.ok:
                _comp_ctx_try = 8192
                _fit_comp = _lsm_comp.can_fit(
                    _comp_llm_cfg, _comp_ctx_try,
                    exclude_slot_id=_coder_slot_id)
            if _fit_comp.ok:
                _comp_port_new = await asyncio.wait_for(
                    _lsm_comp.ensure_loaded(_comp_llm_cfg, num_ctx=_comp_ctx_try, n_parallel=1),
                    timeout=240.0,
                )
                _comp_mdl = _comp_llm_cfg
                _comp_port = int(_comp_port_new)
                logger.warning(
                    "[COMPRESS-MODEL] compression via %s (port %d, ctx=%d) instead of %s",
                    _comp_mdl, _comp_port, _comp_ctx_try, exec_mdl,
                )
            else:
                logger.warning(
                    "[COMPRESS-MODEL] light model %s does not fit (needed=%.0fMiB free=%.0fMiB) - using coder model for compression",
                    _comp_llm_cfg, _fit_comp.needed_mib, _fit_comp.free_mib,
                )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                AttributeError, ImportError, httpx.HTTPError,
                asyncio.TimeoutError) as _cm_err:
            logger.warning(
                "[COMPRESS-MODEL] light model %s unavailable (%s) - using coder model for compression",
                _comp_llm_cfg, _cm_err,
            )
    _mini_retry_done = False  # MINI-SHRINK-RETRY (2026-09-07)
    # COMP-ARGS-ONCE (2026-09-10): shared kwargs for all
    # compression calls this cycle (messages passed per call).
    _comp_kwargs: dict = dict(
        model=_comp_mdl,
        port=_comp_port,
        client=_dtc,
        read_timeout=float(_comp_llm_timeout_s or 180),
        system_prompt=_sys_for_compress,
        original_task=ctx.user_input,
        written_files=_written_files,
        done_tasks=_done_tasks,
        goal_pin=_goal_pin_msg,
        keep_recent_msgs=(18 if ctx.duo_config.until_finished else 12),
        tail_tool_max_chars=int(ctx.settings.get("duo_compress_tail_tool_max_chars", 8000) or 0),
        plan_state=_plan_state,
        plan_anchor_text=_plan_anchor_text,
        last_test_status=_last_test_status,
        explore_ctx=_explore_ctx,
        tool_rounds=_total_tool_rounds,
        max_tool_rounds=_max_tool_rounds,
        compression_mode=_comp_mode,
        cut_index=_comp_cut,
        local_only=_compress_local_only,
        # WHOLE-TAIL-DROP (2026-10-31): if summary +
        # kept raw tail still exceed the compression
        # threshold, drop the oldest whole tail turns.
        target_tokens=int(_compress_threshold),
    )
    # REPO-MAP-DELTA (2026-11-02): the pinned map is
    # frozen for the run, so files created or deleted
    # since the pin are invisible to it. Compression
    # rewrites the prefix anyway - the delta rides
    # along for free at exactly that moment.
    if ctx.settings.get("duo_pin_static_map", True):
        try:
            from core.repomap_pin import append_repo_map_delta as _rmd
            if _rmd(_dtool_msgs, list(_written_files or [])):
                logger.info("[REPO-MAP-DELTA] appended %d changed path(s) at compression",
                            len(list(_written_files or [])))
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError,
                IndexError, ImportError) as _rmd_err:   # never break a run for the map (logged)
            logger.debug("[REPO-MAP-DELTA] skipped: %s", _rmd_err)
        # DELTA-BEFORE-COMPRESS (audit 2026-11-03):
        # _sys_for_compress was captured before the
        # delta was appended; compression then rebuilt
        # the prefix from the stale string and threw
        # the delta away. Re-capture after the append.
        _delta_sys = (
            _dtool_msgs[0].get("content", "")
            if _dtool_msgs and isinstance(_dtool_msgs[0], dict)
            and _dtool_msgs[0].get("role") == "system"
            else ""
        )
        if _delta_sys:
            _sys_for_compress = str(_delta_sys)
    # KWARGS-CARRY-THE-SYSTEM (audit 2026-11-03):
    # _comp_kwargs was built before the delta append;
    # compression reads system_prompt from the kwargs,
    # so updating only the local variable still threw
    # the delta away.
    _comp_kwargs["system_prompt"] = _sys_for_compress
    _dtool_msgs, _condensed_files, _compress_usage = await _compress_tool_context(messages=_dtool_msgs, **_comp_kwargs)
    # MINI-SHRINK-RETRY (2026-09-07): if the full compression
    # barely shrank (<15% of the before value), an early
    # next 2-minute compress follows (cost cascade). Then
    # ONE escalated retry (aggressive_retry=soft escalation
    # instead of a hard token target). The retry runs
    # BEFORE validation — the existing
    # _validate_compression_summary flow (+ rule fallback)
    # below checks the RETRY result.
    # LOCAL-ONLY: the local summary is deterministic — a
    # second identical attempt cannot shrink more.
    if (not _mini_retry_done
            and not _compress_local_only
            and str(_comp_mode or "full") == "full"
            and int(_est_tokens_before_compress or 0) > 2048):
        _mini_retry_done = True
        _after1_est = int(_estimate_ctx_tokens(_dtool_msgs))
        _shrink1 = (int(_est_tokens_before_compress) - _after1_est) / max(1, int(_est_tokens_before_compress))
        if _shrink1 < 0.15:
            logger.warning(
                "[CTX-COMPRESS-RETRY] before=%d after1=%d shrink=%.2f < 0.15 — starting escalated retry",
                int(_est_tokens_before_compress), _after1_est, _shrink1,
            )
            yield await ctx.emit({"type": "status",
                "content": "🗜 Mini-shrink detected — second (escalated) compression attempt …"})
            _dtool_msgs2, _condensed_files2, _compress_usage2 = await _compress_tool_context(messages=_msgs_before_compress, **_comp_kwargs, aggressive_retry=True)
            _after2_est = int(_estimate_ctx_tokens(_dtool_msgs2))
            _retry_helped = bool(
                (_after2_est < int(_after1_est * 0.85))
                or ((int(_est_tokens_before_compress) - _after2_est)
                    / max(1, int(_est_tokens_before_compress)) >= 0.15)
            )
            logger.warning(
                "[CTX-COMPRESS-RETRY] before=%d after1=%d after2=%d retry_helped=%s",
                int(_est_tokens_before_compress), _after1_est, _after2_est,
                _retry_helped,
            )
            _dtool_msgs = _dtool_msgs2
            # POST-COMPRESS RE-ANCHOR (2026-10-08,
            # owner-approved fix 1+2): compression
            # replaces file contents with summaries -
            # the model loses the exact file state and
            # starts editing from memory (the
            # old_text-not-found loops). Re-anchor with
            # a fresh project inventory right after the
            # compressed messages are installed.
            try:
                # F4 (deep audit 2026-11-07): the os.walk inventory is sync
                # I/O — run it on a worker thread, never on the loop.
                _inv = await asyncio.to_thread(_walk_workspace_inventory, _ws_str)
                if _inv:
                    _dtool_msgs.append({"role": "user", "content":
                        "[PROJECT FILES - these exist in the workspace "
                        "right now]\n" + "\n".join(_inv[:80])
                        + "\nread_file any file before editing it - "
                        "do not edit from memory."})
            except (OSError, RuntimeError, ValueError, TypeError) as _inv_err:
                logger.debug("[COMPRESS-INVENTORY] workspace inventory skipped: %s", _inv_err)
            _condensed_files = _condensed_files2
            _compress_usage = _compress_usage2
    if _compress_usage and _compress_usage.get("completion_tokens"):
        yield await ctx.emit({"type": "usage_meta", "phase": "coder",
            "completion_tokens": int(_compress_usage["completion_tokens"]),
            "prompt_tokens": int(_compress_usage.get("prompt_tokens") or 0),
            # TOKEN-TRACKER (2026-08-25): compression request
            "cached_tokens": int(_compress_usage.get("cached_tokens") or 0),
            "gen_ms": int(_compress_usage.get("gen_ms") or 0)})
    if _condensed_files:
        from tools.runner import _files_read_in_run as _fic_read_ctx
        _read_set_c = _fic_read_ctx.get(None)
        if _read_set_c:
            _removed_read = set()
            for _cfp in _condensed_files:
                _cfp_norm = _normalize_tool_path(_cfp, _ws_str)
                if _cfp_norm and _cfp_norm in _read_set_c:
                    _read_set_c.discard(_cfp_norm)
                    _removed_read.add(_cfp_norm)
            if _removed_read:
                logger.warning(
                    "[READ-GUARD] Compression: removed %d compacted read_file paths from the read guard (keep for tail remains)",
                    len(_removed_read),
                )
    # Validate compression summary quality
    _cs_summary = (_dtool_msgs[1].get("content", "") if len(_dtool_msgs) >= 2 else "")
    from context.compression import _validate_compression_summary
    _known_parts = re.findall(r'partition\s*=\s*"([^"]+)"', _explore_ctx or "")
    _cs_valid = _validate_compression_summary(
        _cs_summary, _written_files, _done_tasks,
        known_partitions=_known_parts,
        plan_anchor=_plan_anchor_text)
    if not _cs_valid:
        logger.warning("[COMPRESSION] Summary failed validation — using enriched fallback")
    # BUG-2 FIX: Compress-Fallback erkennen → UI-Warning.
    if not _cs_valid:
        # Allow structural fallback (no "COMPRESSION FAILED" sentinel)
        _cs_valid = "COMPRESSION FAILED" not in (
            _dtool_msgs[-1].get("content", "") or ""
        )
    if not _cs_valid:
        _llm_compress_fails += 1
        # RULE-BASED FALLBACK (2026-09-04): compress
        # deterministically instead of letting the
        # context grow uncontrolled. After 2 LLM fails
        # (and otherwise immediately) rule-based
        # replacement kicks in.
        _rule_target = max(1, int(_est_tokens_before_compress * 0.80))
        _rule_msgs, _rule_evicted = _compress_rule_based(
            messages=_msgs_before_compress,
            target_tokens=_rule_target,
            keep_recent_msgs=(18 if ctx.duo_config.until_finished else 12),
        tail_tool_max_chars=int(ctx.settings.get("duo_compress_tail_tool_max_chars", 8000) or 0),
        )
        _est_rule = _estimate_ctx_tokens(_rule_msgs)
        if _est_rule < _est_tokens_before_compress * 0.90:
            _rule_compress_used = True
            _llm_compress_fails = 0
            _rule_compress_fails = 0
            _dtool_msgs = _strip_stale_ctx_notices(_rule_msgs)
            _est_tokens = _est_rule
            _condensed_files = [str(p) for p in (_rule_evicted or [])]
            logger.warning(
                "[CTX-COMPRESS-RULE] LLM summary failed (%d) — rule-based fallback applied (%d -> %d est. tokens, evicted=%d)",
                _llm_compress_fails, int(_est_tokens_before_compress), int(_est_rule),
                len(_condensed_files),
            )
            yield await ctx.emit({"type": "status",
                "content": f"🧰 LLM compression failed — rule-based compression ({int(_est_tokens_before_compress)} → {int(_est_rule)} est. tokens)"})
            # Manual cleanup analogous to the LLM success
            # path (the else branch doesn't run for the
            # fallback): reset LRU/rounds/read-guard/plan pin.
            _tool_ctx_lru.reset()
            _total_tool_rounds = 0
            ctx.exec_ctrl.sync_tool_rounds(0)
            _ctx_pressure_warned = False
            _ctx_critical_warned = False
            _compress_fail_streak, _compress_stop = _compress_fail_streak_update(
                _compress_fail_streak, True, 3,
            )
            try:
                from tools.runner import _files_read_in_run as _fic_rule
                _rset_rule = _fic_rule.get(None)
                if _rset_rule is not None and _ws_str:
                    for _cp in (_rule_evicted or []):
                        _wpn = _normalize_tool_path(str(_cp), _ws_str)
                        if _wpn:
                            _rset_rule.discard(_wpn)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                    AttributeError, IndexError, ImportError) as _rg_err:
                logger.debug("[COMPRESS-READ-GUARD] rule-evict read-guard update skipped: %s", _rg_err)
            try:
                _pin = _build_plan_pin_msg(_subtasks, _di, _n_items)
                if _pin:
                    _dtool_msgs.append({"role": "user", "content": _pin})
                    logger.warning("[COMPRESS-PLAN-PIN] %d subtasks re-injected (current=%d)", _n_items, _di + 1)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _pp_err:
                logger.debug("[COMPRESS-PLAN-PIN] Pin failed: %s", _pp_err)
        else:
            _rule_compress_fails += 1
            yield await ctx.emit({"type": "status",
                "content": "⚠ Compression failed — keeping uncompressed context"})
            _dtool_msgs = _msgs_before_compress  # Restore original, don't inject degraded summary
            if _rule_compress_fails >= 3:
                _compress_fail_streak, _compress_stop = _compress_fail_streak_update(
                    _compress_fail_streak, False, 3,
                )
                if _compress_stop:
                    _ld_setter(2906); _loop_detected = True
                    yield await ctx.emit({
                        "type": "status",
                        "content": "⛔ Context compression repeatedly fails to shrink the context (3x) — run stopped.",
                    })
                    # STOP-SYNC (audit 2026-11-06): the early return must not
                    # discard state the runner's outer stop funnel reads.
                    _sync_state()
                    ps.stop = True
                    return
    else:
        _ctx_compressions += 1
        _ctx_full_skips = 0
        _llm_compress_fails = 0
        _rule_compress_fails = 0
        if _ctx_compressions < _MAX_COMPRESSIONS:
            _total_tool_rounds = 0
            ctx.exec_ctrl.sync_tool_rounds(0)
            if _lifetime_tool_rounds >= _max_tool_rounds * 6:
                _MAX_COMPRESSIONS = -1
                yield await ctx.emit({
                    "type": "system_warning",
                    "code": "lifetime_tool_cap",
                    "message": f"Lifetime tool-round cap reached ({_lifetime_tool_rounds} rounds) — no further compressions allowed",
                })
        else:
            _force_compress_next = False
            _MAX_COMPRESSIONS = -1
            yield await ctx.emit({
                "type": "system_warning",
                "code": "compression_cap_reached",
                "message": "Context compression cap reached — oldest context may be lost",
            })
        _ctx_pressure_warned = False
        _ctx_critical_warned = False
    # can run up to the run deadline (24h in until_finished). Success = >=10%
    _est_tokens_after_compress = _estimate_ctx_tokens(_dtool_msgs)
    # PARTIAL-ESCALATE (2026-09-08): a partial attempt that
    # did not shrink must not repeat identically — the next
    # attempt runs full mode (summary + recent tail only).
    if (_comp_mode == "partial"
            and _est_tokens_after_compress >= _est_tokens_before_compress * 0.90):
        _partial_escalated = True
        logger.warning(
            "[CTX-COMPRESS] partial shrank nothing (%d -> %d) — escalating to full mode",
            int(_est_tokens_before_compress), int(_est_tokens_after_compress),
        )
        # ESCALATE-NOW (2026-09-10): rerun full mode
        # immediately instead of waiting for the next
        # trigger — a weak partial otherwise wastes a
        # whole cycle for ~0 gain (live: 20495->19244).
        if _comp_kwargs:
            _kw_full = dict(_comp_kwargs)
            _kw_full.update(
                messages=_msgs_before_compress,
                compression_mode="full",
                cut_index=-1,
                aggressive_retry=True,
            )
            try:
                _msgs_full, _, _ = await _compress_tool_context(**_kw_full)
                _est_full = _estimate_ctx_tokens(_msgs_full)
                if _est_full < _est_tokens_after_compress:
                    _dtool_msgs = _msgs_full
                    _est_tokens_after_compress = _est_full
                    logger.warning(
                        "[CTX-COMPRESS] immediate full escalation: -> %d",
                        int(_est_full),
                    )
            except (RuntimeError, OSError, asyncio.TimeoutError) as _esc_err:
                logger.warning("[CTX-COMPRESS] immediate escalation failed: %s", str(_esc_err)[:120])
    _compress_fail_streak, _compress_stop = _compress_fail_streak_update(
        _compress_fail_streak,
        _est_tokens_after_compress < _est_tokens_before_compress * 0.90,
        3,
    )
    if _compress_stop and _total_tool_rounds > 0:
        _ld_setter(2906); _loop_detected = True
        yield await ctx.emit({
            "type": "status",
            "content": "⛔ Context compression repeatedly fails to shrink the context (3x) — run stopped.",
        })
        # STOP-SYNC (audit 2026-11-06, F1 2026-11-07): one sync point covers
        # the compressed messages and every counter the stop paths mutate.
        _sync_state()
        ps.stop = True
        return
    if _compress_stop:
        # ROUND0-INCOMPRESSIBLE (2026-09-09): at round 0
        # the payload is the injected plan/task itself —
        # there is nothing condensable yet. Dying here
        # produced zero-output runs (live: fallback coder
        # @10k ctx). Continue with a minimal budget;
        # compression becomes meaningful once tool
        # outputs accumulate.
        _compress_fail_streak = 0
        _force_compress_next = False
        logger.warning(
            "[CTX-COMPRESS] incompressible at round 0 — continuing with minimal output budget"
        )
    _est_tokens = _est_tokens_after_compress
    # METER-FIX (2026-08-25): real prompt tokens are STALE
    # after the compression (pre-compress value). The reset
    # forces the next ctx_meter onto the fresh heuristic,
    # until the next usage_meta delivers the new real value.
    _coder_real_prompt_tokens[0] = 0
    _coder_est_at_real[0] = 0
    # D1-DIAG: log the result (shrinkage).
    logger.warning(
        "[CTX-COMPRESS] done before=%d after=%d condensed_files=%d mode=%s llm_fails=%d rule_fails=%d rule_used=%s",
        int(_est_tokens_before_compress), int(_est_tokens_after_compress),
        len(_condensed_files or []), _comp_mode,
        _llm_compress_fails, _rule_compress_fails, _rule_compress_used,
    )
    # TELEMETRY (2026-10-09): tag the following coder
    # rounds with a compression id (core/telemetry).
    from core import telemetry as _tel
    _tel.bump_compression()
    yield await ctx.emit({
        "type": "status",
        "content": f"✅ Context compressed ({int(_est_tokens_before_compress)} → {int(_est_tokens_after_compress)} est. tokens, real {int((_est_tokens_before_compress - _est_tokens_after_compress) / max(1, _est_tokens_before_compress) * 100)}%)",
    })
    yield await ctx.emit({
        "type": "ctx_meter",
        "est_tokens": int(_est_tokens_after_compress),
        "real_tokens": int(_coder_real_prompt_tokens[0]),
        "ctx_limit": int(_dtool_ctx),
        "compressing": False,
    })
    _tool_ctx_lru.reset()
    # COMPRESS-CLEANUP-FIX (2026-08-31): after a successful
    # compression, pre-compression loop-detection signatures
    # and stale CTX warnings are stale.
    # 1) clear call_sigs: pre-compression signatures must not
    #    form ABAB/3x with post-compression calls
    #    (observed live: compression -> 3 calls -> loop_detected).
    # 2) remove stale [RUNTIME NOTICE]/[CTX CRITICAL] messages
    #    (the context just became free — the warning contradicts).
    # 3) re-inject the plan pin: the coder must know after the
    #    compression which subtask is running and what still
    #    comes (the fallback summary loses the anchor otherwise).
    try:
        _call_sigs.clear()
        _read_counts.clear()
        _seen_explore_paths.clear()
        _explore_only_rounds = 0
        _no_write_rounds = 0
        _no_write_nudged = False
        _dtool_msgs = _strip_stale_ctx_notices(_dtool_msgs)
        logger.warning("[COMPRESS-CLEANUP] call_sigs cleared + stale CTX notices removed (msgs=%d)", len(_dtool_msgs))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _cc_err:
        # Never break a completed compression over cleanup cosmetics.
        logger.debug("[COMPRESS-CLEANUP] Cleanup failed: %s", _cc_err)
    # PLAN-REINJECT-FIX (2026-08-31): plan pin as its own
    # user message, so the coder keeps plan overview even
    # with LLM-fallback compression. _plan_anchor_text
    # already flowed into the summary (compression.py);
    # this pin additionally provides done/current/remaining as
    # a compact checklist.
    try:
        _pin = _build_plan_pin_msg(_subtasks, _di, _n_items)
        if _pin:
            _dtool_msgs.append({"role": "user", "content": _pin})
            logger.warning("[COMPRESS-PLAN-PIN] %d subtasks re-injected (current=%d)", _n_items, _di + 1)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _pp_err:
        # Plan-pin is best-effort.
        logger.debug("[COMPRESS-PLAN-PIN] Pin failed: %s", _pp_err)
    # READ-GUARD-FIX: update _files_in_context with paths from the compression summary
    # so the model doesn't re-read files already summarized.
    # empty pre-explore (static-map fallback) + first compression crashed
    if _cs_summary:
        from tools.runner import _files_in_context as _fic_guard
        _ctx_set = _fic_guard.get(None)
        if _ctx_set is not None and _ws_str:
            _compressed_paths = set()
            for _pat in (_RE_WIN_PATH, _RE_UNIX_PATH):
                for _m in _pat.finditer(_cs_summary):
                    _compressed_paths.add(_m.group().replace("\\", "/"))
            for _cp in _compressed_paths:
                _wp = _normalize_tool_path(_cp, _ws_str)
                if _wp:
                    _ctx_set.add(_wp)

    # ── sync back: everything the runner consumes after this block ──
    _sync_state()


@dataclasses.dataclass
class _DuoCompressCycleState:
    """Compress-cycle state (split 2.8, 2026-11-05): the tool-round
    compression inputs and the counters it rebinds; `stop` is the
    fail-streak round-loop exit signal (runner does the break)."""
    _call_sigs: list = dataclasses.field(default_factory=list)
    _coder_est_at_real: list = dataclasses.field(default_factory=list)
    _coder_real_prompt_tokens: list = dataclasses.field(default_factory=list)
    _comp_llm_cfg: str = ""
    _comp_llm_timeout_s: int = 180
    _compress_local_only: bool = False
    _compress_threshold: int = 0
    _di: int = 0
    _done_tasks: list = dataclasses.field(default_factory=list)
    _dport: object = None
    _dtc: object = None
    _dtool_ctx: int = 0
    _dtool_sys_eff: str = ""
    _explore_ctx: str = ""
    _goal_pin_msg: dict | None = None
    _guard_decision: object = None
    _guard_tokens: float = 0.0
    _last_test_status: object = None
    _lifetime_tool_rounds: int = 0
    _max_tool_rounds: int = 0
    _n_items: int = 1
    _partial_compression: bool = False
    _plan_result: "PlannerResult | None" = None
    _plan_tracker: "PlanTracker | None" = None
    _read_counts: dict = dataclasses.field(default_factory=dict)
    _seen_explore_paths: set = dataclasses.field(default_factory=set)
    _subtasks: list = dataclasses.field(default_factory=list)
    _tool_ctx_lru: object = None
    _written_files: list = dataclasses.field(default_factory=list)
    _ws_str: str = ""
    _MAX_COMPRESSIONS: int = 0
    _compress_fail_streak: int = 0
    _ctx_compressions: int = 0
    _ctx_critical_warned: bool = False
    _ctx_full_skips: int = 0
    _ctx_pressure_warned: bool = False
    _dtool_msgs: list = dataclasses.field(default_factory=list)
    _est_tokens: int = 0
    _explore_only_rounds: int = 0
    _force_compress_next: bool = False
    _llm_compress_fails: int = 0
    _loop_detected: bool = False
    _no_write_nudged: bool = False
    _no_write_rounds: int = 0
    _partial_escalated: bool = False
    _rule_compress_fails: int = 0
    _rule_compress_used: bool = False
    _total_tool_rounds: int = 0
    stop: bool = False
    carry: dict = dataclasses.field(default_factory=dict)
