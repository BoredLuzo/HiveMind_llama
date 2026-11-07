"""Pre-loop coder setup (C2, 2026-11-07 maintainability sprint).

Model/VRAM/evict + tool-budget + coder-load block of run_code_duo, moved
VERBATIM behind an async-generator seam (the established duo_* split
pattern). Inputs/outputs travel through `ps`; early exits set
`ps.stop` and the runner returns after draining the generator.

Source block: core/duo_runner.py, former lines 2762-3360. No logic
changes.
"""
from __future__ import annotations

import asyncio
import dataclasses
import httpx
import logging
import time
from pathlib import Path

from context.pause_state import clear_pause_state as _clear_pause_state
from infra.ask_user_governor import cleanup_governor as _cleanup_governor
from infra.run_control import (
    clear_graceful_stop,
    cleanup_pause_state as _cleanup_pause_state,
)
from core.duo.preflight import write_pre_loop_resume as _pre_loop_resume
from core.duo_helpers import CODER_VISION_NOTE, effective_image_plan
from hive_functions.num_ctx_config import resolve_ctx

logger = logging.getLogger("hivemind.duo")


@dataclasses.dataclass
class _DuoCoderPreloopState:
    """In/out locals of the pre-loop coder setup."""
    # runner inputs
    done_tasks: list = dataclasses.field(default_factory=list)
    written_files: list = dataclasses.field(default_factory=list)
    subtasks: list = dataclasses.field(default_factory=list)
    explore_ctx: str = ""
    ws_str: str = ""
    coder_mdl: str = ""
    exec_mdl: str = ""
    planner_model: str = ""
    duo_coder_tok: int = 0
    duo_pinned: set = dataclasses.field(default_factory=set)
    worker_slots: list = dataclasses.field(default_factory=list)
    plan_ctx_final: int = 0
    plan_port: int | None = None
    use_parallel: bool = False
    xexplore_mdl: str = ""
    workers_were_loaded: bool = False
    skip_coder_pin: bool = False
    img_plan: dict = dataclasses.field(default_factory=dict)
    duo_hard_stop: bool = False
    # outputs
    duo_coder_sys: str = ""
    always_vision: bool = False
    coder_ctx_override: int = 0
    coder_port: int | None = None
    cached_coder_port: int | None = None
    cached_coder_port_ctx: int | None = None
    # early-exit signal (the runner returns after draining)
    stop: bool = False


async def _duo_coder_preloop(ctx, ps: _DuoCoderPreloopState):
    _done_tasks = ps.done_tasks
    _written_files = ps.written_files
    _subtasks = ps.subtasks
    _explore_ctx = ps.explore_ctx
    _ws_str = ps.ws_str
    coder_mdl = ps.coder_mdl
    exec_mdl = ps.exec_mdl
    _planner_model = ps.planner_model
    _duo_coder_tok = ps.duo_coder_tok
    _duo_pinned = ps.duo_pinned
    _worker_slots = ps.worker_slots
    _plan_ctx_final = ps.plan_ctx_final
    _plan_port = ps.plan_port
    _use_parallel = ps.use_parallel
    _xexplore_mdl = ps.xexplore_mdl
    _workers_were_loaded = ps.workers_were_loaded
    _skip_coder_pin = ps.skip_coder_pin
    _img_plan = ps.img_plan
    _duo_hard_stop = ps.duo_hard_stop
    _duo_coder_sys = ps.duo_coder_sys
    _always_vision = ps.always_vision
    _coder_ctx_override = ps.coder_ctx_override
    _coder_port = ps.coder_port
    _cached_coder_port = ps.cached_coder_port
    _cached_coder_port_ctx = ps.cached_coder_port_ctx
    try:
        # WEBSEARCH-DEDUP (audit 2026-10-24): the availability guidance now
        # lives ONLY in _build_duo_coder_sys (duo_helpers gates on the same
        # per-run flag) - this second copy burned ~110 tokens per run saying
        # the same thing twice.
        # Writes were being truncated by the token limit or rejected as too large
        # AFTER full generation (7-minute total loss). Instruct proactively with
        # an exact per-call char cap (same source as the runtime tool check:
        # resolve_write_char_limits).
        try:
            _wb_hint_budget = max(1024, int(_duo_coder_tok))
            _wb_hint_cpt = float(ctx.settings.get("duo_write_chars_per_token", 2.5))
            _wb_limit = 20000
            try:
                from utils.tool import resolve_write_char_limits as _wcl
                _wb_limit = int((_wcl(exec_mdl, _wb_hint_budget, _wb_hint_cpt) or (20000, 20000))[0] or _wb_limit)
            except Exception:
                pass
            _wb_hint_safe = max(2000, int(_wb_limit) - 1000)
            # WRITE-RULES-DEDUPE (2026-09-17): the full chunk/marker contract
            # also lives in the write_file/write_file_append tool descriptions
            # — the system block now carries only the per-model numbers.
            _duo_coder_sys += (
                f"\n\nWRITE RULES (soft target): write_file accepts at most ~{_wb_limit} "
                f"characters per call (chunks well below ~{_wb_hint_safe}) and writes the "
                f"COMPLETE file (create or overwrite). To change parts of an existing "
                f"file: edit_file with old_text copied VERBATIM from your last read_file "
                f"(must be unique in the file) and new_text as the replacement. If a write "
                f"call is cut off by the output limit, the server keeps everything up to "
                f"the last complete line and tells you the line — continue from there with "
                f"write_file_append, never by rewriting from the start."
            )
        except Exception:
            pass

        try:
            _planner_is_coder = (
                _planner_model is not None
                and _planner_model == coder_mdl
            )
        except (NameError, UnboundLocalError):
            _planner_is_coder = False
        try:
            _plan_port_available = _plan_port is not None
        except (NameError, UnboundLocalError):
            _plan_port_available = False
        # STALE-PORT-GUARD (2026-08-31): "Planner=Coder" may only take over the
        # planner port as coder port if a server is really listening there.
        # Otherwise the coder inherits a phantom slot (dead port) and crashes
        # immediately with ConnectError (observed live in the planner path).
        _plan_port_alive = False
        _lsm_pv = None
        if _plan_port_available and _plan_port is not None:
            try:
                from backend.llama_server_manager import manager as _lsm_pv
                _plan_port_alive = await _lsm_pv._port_alive(_plan_port)
            except Exception:
                _plan_port_alive = False
        # CTX-ACTUAL-FOR-PLANNER (2026-09-06): the manager may have silently
        # degraded the slot to a smaller ctx (old: 768-MiB margin → 4096).
        # planner==coder must then NOT inherit the slot as coder — the coder
        # needs its full configured context and otherwise loads fresh.
        _plan_port_actual_ctx = 0
        _plan_port_vision = False
        if _plan_port_available and _plan_port_alive and _lsm_pv is not None:
            try:
                for _s_pp in _lsm_pv._slots:
                    if getattr(_s_pp, "port", None) == _plan_port and getattr(_s_pp, "_num_ctx", 0):
                        _plan_port_actual_ctx = int(_s_pp._num_ctx)
                        _plan_port_vision = bool(getattr(_s_pp, "_vision", False))
                        break
                if not _plan_port_actual_ctx:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0),
                    ) as _c_pp:
                        _props_pp = (await _c_pp.get(f"http://127.0.0.1:{_plan_port}/props")).json()
                    _plan_port_actual_ctx = int((_props_pp.get("default_generation_settings") or {}).get("n_ctx") or 0)
            except Exception:
                _plan_port_actual_ctx = 0
        try:
            _plan_ctx_req = int(_plan_ctx_final or 0)
        except (NameError, TypeError):
            _plan_ctx_req = 0
        # CODER ALWAYS-VISION TOGGLE (2026-10-06, owner idea): when the
        # owner steers images regularly, the mid-run projector upgrade
        # (~15 s stall) is avoidable — load the coder WITH the projector
        # from the start. Opt-in (default OFF): the mmproj costs VRAM every
        # run even when no image ever arrives.
        _always_vision = (bool(ctx.settings.get("duo_coder_always_vision", False))
                          and not bool(ctx.settings.get("duo_coder_images_off", False)))
        # PLANNER-EVICT-BEFORE-CODER (2026-10-06, owner decision): the
        # planner slot may only be REUSED as the coder slot when it already
        # runs at the CODER's effective ctx. The old check compared against
        # the PLANNER's own ctx target — a planner at 8192 handed the coder
        # an 8192 slot while the coder budget said 80896, the exact
        # cached-port ctx mismatch that kills tool rounds (LD-SET 2497,
        # live 2026-10-06 13:03: "[Planner=Coder] stays in VRAM" with
        # ctx=8192 vs budget 80896). With the honest requirement, a normal
        # agentic run always evicts the planner slot before the coder
        # loads: same model → the ctx-mismatch kill+reload in ensure_loaded,
        # different model → the SERIAL-SLOTS evict below.
        try:
            _coder_ctx_need = (
                resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
                if ctx.duo_config.agentic_mode
                else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
            )
        except (NameError, TypeError, ValueError):
            _coder_ctx_need = 0
        # Reuse only if the slot runs at >= planner req AND >= coder need.
        # Query errors (actual==0) → conservatively no reuse (a clean
        # reload is safer).
        _planner_ctx_ok = bool(
            _plan_port_actual_ctx
            and _plan_port_actual_ctx >= _plan_ctx_req
            and (_coder_ctx_need <= 0 or _plan_port_actual_ctx >= _coder_ctx_need)
        )
        if _planner_is_coder and _plan_port_available and _plan_port_alive \
                and _planner_ctx_ok and (not _always_vision or _plan_port_vision):
            _cached_coder_port = _plan_port
            _cached_coder_port_ctx = _plan_port_actual_ctx  # honest slot ctx (>= coder need)
            logger.info("[Planner=Coder] Model stays in VRAM - slot ctx=%d covers coder need=%d (vision=%s)",
                        _plan_port_actual_ctx, max(_plan_ctx_req, _coder_ctx_need),
                        _plan_port_vision)
            yield await ctx.emit({
                "type": "status",
                "content": (
                    f"⚡ Planner=Coder — {_planner_model.split(':')[0]} stays in VRAM, "
                    f"slot ctx={_plan_port_actual_ctx} covers the coder"
                ),
            })
        elif _planner_is_coder and _plan_port_available and _plan_port_alive \
                and (not _planner_ctx_ok or (_always_vision and not _plan_port_vision)):
            _ctx_need_eff = max(_plan_ctx_req, _coder_ctx_need)
            if _planner_ctx_ok and _always_vision and not _plan_port_vision:
                logger.info(
                    "[PLANNER=CODER] always-vision toggle: slot has no projector — "
                    "planner slot evicted, coder loads fresh WITH mmproj")
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⚡ Always-vision: coder loads WITH the projector — "
                        "planner slot freed for a fresh load."
                    ),
                })
            else:
                logger.warning(
                    "[PLANNER=CODER-CTX-MISMATCH] slot ctx=%d < required %d "
                    "(planner=%d, coder=%d) — planner slot evicted, coder "
                    "loads fresh at full ctx",
                    _plan_port_actual_ctx, _ctx_need_eff,
                    _plan_ctx_req, _coder_ctx_need,
                )
            # TRUE EVICTION (2026-10-06, steer retest): the fresh coder load
            # takes a FREE slot — without an explicit kill the planner slot
            # stayed resident. Two slots holding the same model then broke
            # every name-based lookup (incl. the steer-image vision upgrade
            # hitting the planner slot while the coder POSTed the other).
            try:
                from backend.llama_server_manager import manager as _lsm_pev
                await _lsm_pev.evict(_planner_model)
            except (RuntimeError, OSError) as _pev_err:
                logger.warning("[PLANNER=CODER] planner evict failed: %s", _pev_err)
            if not _planner_ctx_ok:
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⚡ Planner=Coder — slot runs at ctx={_plan_port_actual_ctx} only, "
                        f"coder needs {_ctx_need_eff} — planner slot freed, "
                        "loading coder model fresh (full context)."
                    ),
                })
        elif _planner_is_coder and _plan_port_available and not _plan_port_alive:
            logger.warning(
                "[Planner=Coder] Planner port %s dead (phantom slot) — cache discarded, "
                "normal coder load runs.",
                _plan_port,
            )
            yield await ctx.emit({
                "type": "status",
                "content": "⚠ Planner=Coder port unreachable — reloading coder model",
            })

        try:
            _scp = _skip_coder_pin
        except (NameError, UnboundLocalError):
            _scp = False
        from backend.llama_server_manager import manager as _lsm2
        from backend.llama_server_manager import VRAMPreFlightError as _VRAMPreFlightError
        _coder_load_ok = False
        # VRAM-FALLBACK-CTX (2026-09-07): when the coder is pushed to a smaller
        # model at a reduced ctx (_fb_ctx), this override pins _coder_ctx_eff
        # to the fallback ctx for the rest of the run — otherwise the tool
        # round keeps demanding the full target ctx and stops in the cached
        # ctx mismatch (LD-SET 2497). The override is RUN-local: a new run
        # starts again with the full target ctx; within this run there is
        # deliberately no mid-run upgrade path back to the original model
        # (fallback = model switch for the run), so no reset is needed
        # (only fresh across runs).
        _coder_ctx_override = 0
        if (_explore_ctx or _workers_were_loaded) and _scp:
            try:
                yield await ctx.emit({"type": "status", "content": "🧹 Freeing VRAM (unloading worker models)…"})
                _evicted_workers = 0
                if _use_parallel:
                    for _ws_entry in _worker_slots:
                        _wm = _ws_entry.get("model", "")
                        _wm_base = _wm.rsplit("#", 1)[0] if "#" in _wm else _wm
                        if _wm_base != exec_mdl:
                            await _lsm2.evict(_wm)
                            _evicted_workers += 1
                if _xexplore_mdl != exec_mdl:
                    await _lsm2.evict(_xexplore_mdl)
                    _evicted_workers += 1
                try:
                    _loaded_after = await _lsm2.list_loaded()
                    _exec_base = exec_mdl.rsplit("#", 1)[0] if "#" in exec_mdl else exec_mdl
                    _planner_base = _planner_model.rsplit("#", 1)[0] if "#" in _planner_model else _planner_model
                    for _lm in _loaded_after:
                        _lname = str(_lm.get("name") or _lm.get("model") or "")
                        if not _lname:
                            continue
                        _lbase = _lname.rsplit("#", 1)[0] if "#" in _lname else _lname
                        if _lbase != _exec_base and _lbase != _planner_base:
                            await _lsm2.evict(_lname)
                            _evicted_workers += 1
                except Exception:
                    pass
                # hits a dead port → TCP close → frontend: "Error in input stream".
                if _evicted_workers > 0:
                    await asyncio.sleep(1.5)
                if _planner_is_coder and _plan_port_available and _plan_port_alive and _planner_ctx_ok:
                    _coder_load_ok = True
                    _coder_ctx_post = (
                        resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
                        if ctx.duo_config.agentic_mode
                        else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
                    )
                    yield await ctx.emit({"type": "status",
                        "content": f"✅ Coder ready ({exec_mdl.split(':')[0]} — Planner=Coder, no reload)"})
                    # FIX (2026-08-31): The fast path did not emit the "agent"
                    # event (coder phase switch in the frontend) → after
                    # "coder ready" nothing happened in the UI anymore.
                    if _subtasks:
                        yield await ctx.emit({"type": "status",
                            "content": f"🖊️ Coder active — {len(_subtasks)} subtask(s), starting with chunk 1"})
                    yield await ctx.emit({
                        "type": "agent",
                        "content": "Code",
                        "model": exec_mdl.split(":")[0] if exec_mdl else "Agentic",
                        "role": "Writes code",
                    })
                else:
                    _coder_ctx_post = (
                        resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
                        if ctx.duo_config.agentic_mode
                        else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
                    )
                    yield await ctx.emit({"type": "status",
                        "content": f"⏳ Loading coder ({exec_mdl.split(':')[0]}, ctx={_coder_ctx_post})…"})
                    _coder_load_ok = False
                    _coder_ctx_try = _coder_ctx_post
                    _coder_ttl_s = float(ctx.settings.get("duo_coder_ttl_seconds", 0) or 0)
                    _coder_load_timeout = _coder_ttl_s if _coder_ttl_s > 0 else 150.0
                    # SERIAL-SLOTS (2026-09-10): evict the planner before the
                    # coder loads when the models differ. Previously both slots
                    # stayed resident when they fit side by side, which traded
                    # coder ctx headroom for a reload-free transition.
                    if bool(ctx.settings.get("duo_evict_planner_on_coder", True)):
                        try:
                            _planner_base_cmp = _planner_model.rsplit("#", 1)[0]
                            _exec_base_cmp = exec_mdl.rsplit("#", 1)[0]
                            if _planner_base_cmp != _exec_base_cmp:
                                await _lsm2.evict(_planner_model)
                                logger.warning(
                                    "[SERIAL-SLOTS] planner %s evicted for coder %s",
                                    _planner_model, exec_mdl,
                                )
                                yield await ctx.emit({"type": "status",
                                    "content": f"🧹 Planner unloaded — full VRAM for the coder."})
                        except (RuntimeError, OSError, asyncio.TimeoutError) as _serial_err:
                            logger.debug("[SERIAL-SLOTS] planner evict failed: %s", _serial_err)
                    # WORKER-PIN RELEASE (EOD-audit P2-1, 2026-11-01):
                    # unpin_all used to live only in the planner phase —
                    # planner-off/resume/skip runs never released the
                    # keep-warm pins, so pinned workers could block this
                    # coder pre-flight. Released here, right before the
                    # ladder; the planner phase re-pins nothing.
                    try:
                        _lsm2.unpin_all()
                    except Exception as _ua_err:
                        logger.debug("[SERIAL-SLOTS] unpin_all failed: %s", _ua_err)
                    for _cl_attempt in range(5):
                        try:
                            # VISION RELOAD HINT (2026-10-01): if the coder
                            # needs the projector but its slot runs without
                            # one, ensure_loaded will kill + restart with
                            # mmproj — tell the user up front.
                            if _img_plan.get("coder") == "raw":
                                try:
                                    _prev_slot = await _lsm2._find_loaded(exec_mdl)
                                except Exception:
                                    _prev_slot = None
                                if _prev_slot is not None and not _prev_slot._vision:
                                    _gguf_gb = 1.5
                                    try:
                                        from backend.llama_models import resolve_model_path as _rmp
                                        _gp = _rmp(exec_mdl)
                                        if _gp and Path(_gp).exists():
                                            _gguf_gb = Path(_gp).stat().st_size / (1024 ** 3)
                                    except Exception:
                                        pass
                                    yield await ctx.emit({"type": "status",
                                        "content": f"loading with image support (mmproj), ~{max(15, int(_gguf_gb * 8))} s — one-time reload"})
                            yield await ctx.emit({"type": "status",
                                "content": f"⏳ Loading coder ({exec_mdl.split(':')[0]}, ctx={_coder_ctx_try})…"})
                            _coder_port = await asyncio.wait_for(
                                # STRICT-CTX: the coder needs its configured
                                # full ctx OR a clear error — never a silently
                                # degraded slot (manager: 768→256→error).
                                _lsm2.ensure_loaded(exec_mdl, num_ctx=_coder_ctx_try,
                                                    n_parallel=1, ctx_graceful=False, pin=True,
                                                    vision=(_img_plan.get("coder") == "raw" or _always_vision)),
                                timeout=_coder_load_timeout,
                            )
                            _coder_load_ok = True
                            _coder_ctx_post = _coder_ctx_try
                            _cached_coder_port = _coder_port
                            _cached_coder_port_ctx = _coder_ctx_try
                            _duo_pinned.add(exec_mdl)
                            break
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Coder load timeout (attempt %d/5) ctx=%d",
                                _cl_attempt + 1, _coder_ctx_try,
                            )
                            # LOAD-RACE FIX (2026-11-01): a timeout is usually
                            # a STALE half-initialized server from a racing
                            # load (live: a queued groot@131072 preload from
                            # run-end starved the real run's coder load — the
                            # old code logged "attempt 1/3" and hard-stopped
                            # IMMEDIATELY, no attempt 2 ever ran). Evict the
                            # stuck attempt and ride the same ctx ladder the
                            # other branches use; hard-stop only at the floor.
                            # EOD-AUDIT P1-1: the abort check MUST run before
                            # the retry — /stop is a cooperative Event (the
                            # emit succeeds anyway), so without this an
                            # aborted run burned ~10 min of load/kill churn.
                            if ctx.aborted():
                                yield await ctx.emit({"type": "status",
                                    "content": "⏹ Coder load aborted (run was stopped)"})
                                ctx.phase_timer.skip("coder_loop")
                                clear_graceful_stop(ctx.run_id)
                                _cleanup_pause_state(ctx.run_id)
                                if ctx.chat_id:
                                    _clear_pause_state(ctx.chat_id)
                                _cleanup_governor(ctx.chat_id or ctx.run_id)
                                _pre_loop_resume(ctx, _ws_str, _explore_ctx,
                                                 _done_tasks, _written_files, "aborted")
                                ctx.duo_stop_reason = "aborted"
                                yield await ctx.emit(ctx.done_event(
                                    round(time.time() - ctx.t_total, 1),
                                    ctx.duo_stop_reason,
                                    **ctx.collect_done_metrics(),
                                ))
                                ps.stop = True
                                return
                            try:
                                from backend.llama_server_manager import manager as _lsm_to
                                await _lsm_to.evict(exec_mdl)
                            except Exception:
                                pass
                            if _cl_attempt < 4 and _coder_ctx_try > 8192:
                                _next_ctx = max(8192, int(_coder_ctx_try / 2))
                                yield await ctx.emit({"type": "status",
                                    "content": f"⟳ Coder load timeout at ctx={_coder_ctx_try} — evicting the "
                                              f"stuck load, retry {_cl_attempt+2}/5 with ctx={_next_ctx} in 4s…"})
                                await asyncio.sleep(4.0)
                                if ctx.aborted():
                                    # the sleep is the second abort window
                                    yield await ctx.emit({"type": "status",
                                        "content": "⏹ Coder load aborted (run was stopped during retry wait)"})
                                    ctx.phase_timer.skip("coder_loop")
                                    clear_graceful_stop(ctx.run_id)
                                    _cleanup_pause_state(ctx.run_id)
                                    if ctx.chat_id:
                                        _clear_pause_state(ctx.chat_id)
                                    _cleanup_governor(ctx.chat_id or ctx.run_id)
                                    _pre_loop_resume(ctx, _ws_str, _explore_ctx,
                                                     _done_tasks, _written_files, "aborted")
                                    ctx.duo_stop_reason = "aborted"
                                    yield await ctx.emit(ctx.done_event(
                                        round(time.time() - ctx.t_total, 1),
                                        ctx.duo_stop_reason,
                                        **ctx.collect_done_metrics(),
                                    ))
                                    ps.stop = True
                                    return
                                _coder_ctx_try = _next_ctx
                                continue
                            yield await ctx.emit({
                                "type": "status",
                                "content": (
                                    f"⛔ Coder load timeout at ctx={_coder_ctx_try} — "
                                    "ctx too large for model/VRAM. Please reduce ctx."
                                ),
                            })
                            ctx.phase_timer.skip("coder_loop")
                            clear_graceful_stop(ctx.run_id)
                            _cleanup_pause_state(ctx.run_id)
                            if ctx.chat_id:
                                _clear_pause_state(ctx.chat_id)
                            _cleanup_governor(ctx.chat_id or ctx.run_id)
                            _duo_hard_stop = True
                            _pre_loop_resume(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files)
                            ctx.duo_stop_reason = "hard_stop"
                            yield await ctx.emit(ctx.done_event(
                                round(time.time() - ctx.t_total, 1),
                                ctx.duo_stop_reason,
                                **ctx.collect_done_metrics(),
                            ))
                            ps.stop = True
                            return
                        except _VRAMPreFlightError as _vpf:
                            # NO-SILENT-FALLBACK (2026-09-15, user decision): a
                            # different (smaller) model must NEVER take over the
                            # coder role — a fallback model edits files it does
                            # not understand (live: 9b request, 4b fallback
                            # edited files blind). Abort with the exact reason
                            # instead. The ctx/VRAM guard (vram_guard) already
                            # catches the worst mismatches pre-run; this branch
                            # is for everything the guard's table misses.
                            logger.warning(
                                "[CODER-VRAM] %s not loadable (free=%d MiB, external=%d MiB, ctx_try=%s) — "
                                "aborting, no fallback",
                                exec_mdl, _vpf.free_mib, _vpf.external_usage_est_mib, _coder_ctx_try,
                            )
                            _duo_hard_stop = True
                            yield await ctx.emit({"type": "status",
                                "content": (
                                    f"⛔ Coder ({exec_mdl}) does not fit into VRAM: "
                                    f"needed at ctx {_coder_ctx_try}, only {_vpf.free_mib} MiB free "
                                    f"(~{_vpf.external_usage_est_mib} MiB used by other apps). "
                                    "No fallback loaded — reduce the ctx slider, close GPU-heavy "
                                    "apps, or pick a model that fits. Run ended with a resume block."
                                )})
                            # VRAM-BLOCK-CTX-CASCADE (2026-10-11): a pre-flight block is not a
                            # model mismatch - same model, smaller ctx. Cascade like the generic
                            # retry instead of hard-stopping (mellum @120832 blocked, @49152
                            # fits). Falls through to hard_stop on the last attempt or at floor.
                            if _cl_attempt < 4 and _coder_ctx_try > 8192:
                                _next_ctx = max(8192, int(_coder_ctx_try / 2))
                                yield await ctx.emit({"type": "status",
                                    "content": f"⟳ Coder does not fit at ctx={_coder_ctx_try} — retrying "
                                              f"attempt {_cl_attempt+2}/5 with ctx={_next_ctx} in 4s…"})
                                await asyncio.sleep(4.0)
                                _coder_ctx_try = _next_ctx
                                continue
                            _pre_loop_resume(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files)
                            break
                        except Exception as _cl_err:
                            if ctx.aborted():
                                yield await ctx.emit({"type": "status",
                                    "content": "⏹ Coder load aborted (run was stopped)"})
                                ctx.phase_timer.skip("coder_loop")
                                clear_graceful_stop(ctx.run_id)
                                _cleanup_pause_state(ctx.run_id)
                                if ctx.chat_id:
                                    _clear_pause_state(ctx.chat_id)
                                _cleanup_governor(ctx.chat_id or ctx.run_id)
                                # to the in-loop abort) + done_event.
                                _pre_loop_resume(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, "aborted")
                                ctx.duo_stop_reason = "aborted"
                                yield await ctx.emit(ctx.done_event(
                                    round(time.time() - ctx.t_total, 1),
                                    ctx.duo_stop_reason,
                                    **ctx.collect_done_metrics(),
                                ))
                                ps.stop = True
                                return
                            logger.warning(
                                "Coder-load attempt %d/5 failed (%s): %s",
                                _cl_attempt + 1, type(_cl_err).__name__, str(_cl_err)[:120],
                            )
                            if _cl_attempt < 4:
                                _next_ctx = max(8192, int(_coder_ctx_try / 2))
                                yield await ctx.emit({"type": "status",
                                    "content": f"⟳ Coder load error ({type(_cl_err).__name__}), retry {_cl_attempt+2}/5 with ctx={_next_ctx} in 4s…"})
                                await asyncio.sleep(4.0)
                                _coder_ctx_try = _next_ctx
                                if ctx.aborted():
                                    yield await ctx.emit({"type": "status",
                                        "content": "⏹ Coder load aborted (run was stopped)"})
                                    ctx.phase_timer.skip("coder_loop")
                                    clear_graceful_stop(ctx.run_id)
                                    _cleanup_pause_state(ctx.run_id)
                                    if ctx.chat_id:
                                        _clear_pause_state(ctx.chat_id)
                                    _cleanup_governor(ctx.chat_id or ctx.run_id)
                                    _pre_loop_resume(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, "aborted")
                                    ctx.duo_stop_reason = "aborted"
                                    yield await ctx.emit(ctx.done_event(
                                        round(time.time() - ctx.t_total, 1),
                                        ctx.duo_stop_reason,
                                        **ctx.collect_done_metrics(),
                                    ))
                                    ps.stop = True
                                    return
                    if not _coder_load_ok:
                        _duo_hard_stop = True
                        _pre_loop_resume(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files)
                        yield await ctx.emit({"type": "status",
                            "content": f"⛔ Coder ({exec_mdl.split(':')[0]}) could not be loaded — run aborted."})
                        ctx.phase_timer.skip("coder_loop")
                        clear_graceful_stop(ctx.run_id)
                        _cleanup_pause_state(ctx.run_id)
                        if ctx.chat_id:
                            _clear_pause_state(ctx.chat_id)
                        _cleanup_governor(ctx.chat_id or ctx.run_id)
                        yield await ctx.emit({"type": "status", "content": "Coder-Load final failure: process aborted."})
                        ctx.duo_stop_reason = "hard_stop"
                        yield await ctx.emit(ctx.done_event(
                            round(time.time() - ctx.t_total, 1),
                            ctx.duo_stop_reason,
                            **ctx.collect_done_metrics(),
                        ))
                        ps.stop = True
                        return
                    yield await ctx.emit({"type": "status",
                        "content": f"✅ Coder ready ({exec_mdl.split(':')[0]})"})
                    # VISION-TRUTH GATE post-load (2026-10-04):
                    # /props.modalities.vision is read AFTER every load (also
                    # the in-loop reload). A dropped projector must not leave
                    # the raw-image plan + [VISION] note claiming the coder
                    # sees the image. None (/props unreachable) trusts the plan.
                    if _img_plan.get("coder") == "raw" and _coder_port is not None:
                        try:
                            from backend.llama_server_manager import manager as _lsm_vgate2
                            _cslot2 = _lsm_vgate2.get_slot_by_port(int(_coder_port))
                            _cv2 = getattr(_cslot2, "vision_active", None) if _cslot2 is not None else None
                            if _cv2 is False:
                                _img_plan, _vg_warns2 = effective_image_plan(
                                    _img_plan, coder_slot_vision=_cv2,
                                    has_description=bool(str(getattr(ctx, "image_description", "") or "").strip()))
                                # RETRACT EVIDENCE (2026-10-04): the
                                # negative test grades THIS line - it proves
                                # the note left the actually-sent system
                                # prompt (note present=True beforehand).
                                _had_note = CODER_VISION_NOTE in _duo_coder_sys
                                _duo_coder_sys = _duo_coder_sys.replace(CODER_VISION_NOTE, "")
                                logger.warning("[DUO][VISION-GATE] [VISION] note retracted "
                                               "from coder system prompt (was present=%s)", _had_note)
                                for _w in _vg_warns2:
                                    logger.warning("[DUO][VISION-GATE] %s", _w)
                                    yield await ctx.emit({"type": "status", "content": "⚠ " + _w})
                            elif _cv2 is True:
                                # STRICT-VISION (2026-10-17): the projector IS
                                # loaded, so the claim is now proven. The
                                # pre-prompt build could not know it (this slot
                                # was loaded in this run) - add the note here.
                                if _img_plan.get("coder") == "raw" \
                                        and CODER_VISION_NOTE not in _duo_coder_sys:
                                    _duo_coder_sys += CODER_VISION_NOTE
                                    logger.info("[DUO][VISION-GATE] [VISION] note added "
                                                "(projector confirmed by /props)")
                            else:
                                # None = unproven (/props unreachable or no
                                # modalities field). Do not claim the model can
                                # see the image.
                                if CODER_VISION_NOTE in _duo_coder_sys:
                                    _duo_coder_sys = _duo_coder_sys.replace(CODER_VISION_NOTE, "")
                                    logger.warning("[DUO][VISION-GATE] coder vision state UNKNOWN "
                                                   "(/props reports no modalities?) - [VISION] note "
                                                   "not claimed (strict rule)")
                        except (ImportError, AttributeError, TypeError, ValueError) as _vg_err2:
                            logger.debug("[DUO][VISION-GATE] coder post-load check skipped: %s", _vg_err2)
                    if _subtasks:
                        yield await ctx.emit({"type": "status",
                            "content": f"🖊️ Coder active — {len(_subtasks)} subtask(s), starting with chunk 1"})
                    yield await ctx.emit({
                        "type": "agent",
                        "content": "Code",
                        "model": exec_mdl.split(":")[0] if exec_mdl else "Agentic",
                        "role": "Writes code",
                    })
            except Exception as _evict_err:
                # hits a dead port → TCP close → frontend: "Error in input stream".
                logger.warning("Worker-evict/coder-load after explore failed: %s", _evict_err)
                if not _coder_load_ok:
                    yield await ctx.emit({"type": "status",
                        "content": f"⛔ Coder could not be loaded after evict error: {str(_evict_err)[:80]}"})
                    ctx.phase_timer.skip("coder_loop")
                    clear_graceful_stop(ctx.run_id)
                    _cleanup_pause_state(ctx.run_id)
                    if ctx.chat_id:
                        _clear_pause_state(ctx.chat_id)
                    _cleanup_governor(ctx.chat_id or ctx.run_id)
                    # AUDIT-FIX 2026-08-04: Evict-Exception + Load-Fail — hard_stop +
                    _duo_hard_stop = True
                    _pre_loop_resume(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files)
                    ctx.duo_stop_reason = "hard_stop"
                    yield await ctx.emit(ctx.done_event(
                        round(time.time() - ctx.t_total, 1),
                        ctx.duo_stop_reason,
                        **ctx.collect_done_metrics(),
                    ))
                    ps.stop = True
                    return
    finally:
        ps.duo_coder_sys = _duo_coder_sys
        ps.always_vision = _always_vision
        ps.coder_ctx_override = _coder_ctx_override
        ps.coder_port = _coder_port
        ps.cached_coder_port = _cached_coder_port
        ps.cached_coder_port_ctx = _cached_coder_port_ctx
        ps.img_plan = _img_plan
        ps.duo_hard_stop = _duo_hard_stop
