# -*- coding: utf-8 -*-
"""Chat run orchestration (run_stream) -- extracted from server.py (M2b).

run_stream builds the RunContext (settings, presets, prefetch machinery)
and delegates to run_stream_orchestrated (core/stream.py).
"""
from __future__ import annotations

import asyncio
from utils.bg_tasks import spawn  # noqa: E402
import json
import logging
import threading
import time
import uuid

import httpx

from core import state as _state
from core.chat_history_seed import _ChatHistorySeedState, _chat_history_seed
from core.chat_route import _ChatRoutePreState, _chat_route_pre
from core.chat_route_decision import _ChatRouteDecisionState, _chat_route_decision
from core.chat_shortcuts import _ChatShortcutState, _chat_memory_shortcuts
from core.chat_vision_pre import _ChatVisionPreState, _chat_vision_pre
from core.state import (
    settings,
    registry_get, apply_settings_to_pipeline, _get_num_ctx,
)
from core.duo_config import DuoConfig
from core.run_context import RunContext
from core.stream import run_stream_orchestrated
from core.model_sampling import _model_profile
from core.duo_helpers import (
    DEFAULT_VRAM_BUDGET_GB,
    _resolve_duo_runtime_profile, _resolve_duo_run_timeout_seconds,
    _bucket_stop_reason,
)
from context.chat_util import (
    _make_messages, _extract_ws_query, _auto_memory_from_input,
)
from infra.phase_timer import PhaseTimer
from routing import decider as _routing_decider
from infra import editor_context as _editor_ctx
from infra.token_stats import _estimate_tokens_from_content
from utils.math import percentile_float as _percentile_float
from utils.httpx_utils import make_httpx_timeout as _make_httpx_timeout


def _history_budget_tokens(settings) -> int:
    """History budget: override wins; 0 derives from the direct model's
    ctx (35% of n_ctx minus a 4096 reserve for system prompt + output)."""
    _ov = int(settings.get("session_history_budget_tokens", 0) or 0)
    if _ov > 0:
        return _ov
    try:
        _ref = registry_get("direct") or registry_get("analyst") or ""
        _nctx = int(_get_num_ctx(_ref) or 0) if _ref else 0
    except Exception:
        _nctx = 0
    # NOTE: with llama-server parallel slots the per-slot ctx is
    # n_ctx / n_parallel - divide here if parallel slots ever get enabled
    # (the current config runs one slot, default n_parallel=1).
    if _nctx <= 0:
        _nctx = 16384  # sane default when no ctx override is configured
    return max(1024, int((_nctx - 4096) * 0.35))
from tools.websearch import _safe_web_search
from hive_functions.prompts import VISION_AGENT_PROMPT
from hive_functions.soul_engine import (
    build_soul_prompt_layer, load_soul, run_soul_cycle,
)

logger = logging.getLogger("hivemind.chat_run")
_logger = logger


def approval_gate_force(model_overrides: dict | None) -> bool:
    """G1 (fixed 2026-10-07): the per-run approval-gate force rides ONLY the
    body/model overrides (gateway phone runs). A True that merely sickers in
    from the global setting must NOT pin the run — the override ContextVar
    would beat the live toggle for the whole run and a mid-run /gate off
    could never reach it (live: cards kept arriving after "Approvals OFF").
    UI runs take the global toggle LIVE per gated call instead."""
    return bool((model_overrides or {}).get("duo_action_approval_enabled"))


async def run_stream(
    user_input: str,
    images: list,
    mode: str,
    iterations: int,
    active_preset,
    constraint_mode: bool = True,
    force_complexity: str | None = None,
    skip_agents: set = frozenset(),
    judge_bias: int = 50,
    chat_id: str | None = None,
    duo_config: DuoConfig | None = None,
    model_overrides: dict | None = None,
    request_workspace: str = "",
    agentic_force: bool = False,
):
    # CYCLE-BREAK (2026-11-05): category-A names come from their home
    # modules now — server only re-exported them. The 15 real server-owned
    # globals/functions (S_models_cache, THIS_FILE, _VRAM_LOOKUP_GB,
    # _pipeline_chat_stream, ...) stay on the lazy server import until the
    # core/runtime.py extraction.
    from infra.run_control import (
        _abort_event, _is_aborted,
        _register_abort, _unregister_abort,
        _register_step_skip, _unregister_step_skip, _clear_step_skip,
    )
    from vram.loader import (
        _bk_evict, _bk_load, _bk_pin,
        _get_loaded_models_set, _refresh_judge_keepalive,
        smart_preload_if_needed,
    )
    from routing.complexity import _check_complexity_with_bias
    from routing.model_picker import _pick_direct_model
    from context.chat import (
        _load_chat_context, get_chat_workspace,
    )
    from context.resume import _clear_resume_block, _load_resume_block
    from vision.preprocess import _filter_vision_images
    from tools.definitions import _get_inline_tools
    from infra.run_counter import _increment_run_counter
    from infra.token_stats import _record_run
    from learning.insights import _run_insight_extractor, _run_skill_distillation
    from model_configs import (
        append_learning_log, get_effective_config,
    )
    from server import (
        S_models_cache,
        THIS_FILE,
        _COMPLEX_DIRECT_MODELS,
        _SIMPLE_DIRECT_MODELS,
        _VRAM_LOOKUP_GB,
        _WEBSEARCH_AVAILABLE,
        _flush_prefetch_settings,
        _get_avg_elapsed,
        _maybe_trigger_soul_evolution,
        _pipeline_chat_stream,
        _registry,
        _update_prefetch_lead,
        _vision_cfg,
        apply_learned_configs_for_run,
        get_effective_prompt_with_override,
    )
    from context.chat import load_chat_images, save_chat_images

    if duo_config is None:
        duo_config = DuoConfig()

    _ui_mode_entry = mode   # MODE-TRACKING: `mode` wird spaeter ueberschrieben
    # EDITOR CONTEXT (2026-10-21): what the IDE has open right now. The block
    # itself goes into the prompts (chat_util / duo_helpers); here we only keep
    # the summary for the run record.
    _editor_meta = _editor_ctx.context_meta_for()
    if _editor_meta:
        logger.info("[EDITOR-CONTEXT] run carries editor context: file=%s "
                    "selection=%d chars diagnostics=%d age=%.0fs",
                    _editor_meta.get("file") or "-",
                    int(_editor_meta.get("selection_chars") or 0),
                    int(_editor_meta.get("diagnostics") or 0),
                    float(_editor_meta.get("age_s") or 0))
    logger.warning(
        "[RUN-ENTRY] mode=%s agentic=%s pre_explore=%s planner=%s chunking=%s "
        "chat_id=%s force=%s until_finished=%s coding_mode=%s",
        mode, duo_config.agentic_mode, duo_config.pre_explore, duo_config.planner,
        duo_config.chunking, chat_id, force_complexity, duo_config.until_finished,
        duo_config.coding_mode,
    )

    async def _decide_route(text: str):
        """Route/Planner ueber den Resolver; der Judge laeuft nur, wenn er gefragt wird."""
        _judge_enabled = _routing_decider.normalise_mode(
            _run_settings.get("routing_judge")) != "off"

        async def _judge_async(_text: str):
            _level = await _check_complexity_with_bias(_text, judge_bias)
            return _level, 0.0

        try:
            _d = await _routing_decider.decide_async(
                _run_settings, text,
                forced_complexity=force_complexity,
                judge_call_async=_judge_async if _judge_enabled else None,
            )
        except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError,
                ImportError, AttributeError, NameError, UnboundLocalError) as _exc:
            # nie stumm: Traceback ins Log, dann das bisherige Verhalten
            logger.exception("[DECIDER] routing failed (%s: %s) - falling back to the judge",
                             type(_exc).__name__, _exc)
            _level = await _check_complexity_with_bias(text, judge_bias)
            _d = _routing_decider.Decision(
                route=("agentic" if _level in ("complex", "code_duo") else "direct"),
                planner=(_level in ("complex", "code_duo")),
                source="judge-fallback", reason=f"judge level={_level}",
            )
        try:
            _d.details["judge_verdict"] = dict(getattr(_state.pipeline, "_last_judge_verdict", {}) or {})
        except (AttributeError, TypeError):
            _d.details["judge_verdict"] = {}
        if _d.route == "agentic":
            duo_config.planner = bool(_d.planner)
        logger.warning(
            "[DECIDER] route=%s planner=%s source=%s conf=%.2f escalated=%s latency=%.0fms",
            _d.route, _d.planner, _d.source, _d.confidence, _d.escalated, _d.latency_ms)
        return _d

    _run_settings = dict(settings)
    if model_overrides:
        _run_settings.update(model_overrides)
    logger.warning("[RUN-TRACE] settings-kopie ok (overrides=%s)", sorted(model_overrides or {}))
    # G1 (full audit 2026-10-05; FIXED 2026-10-07): the per-run gate force
    # rides ONLY the body/model overrides (gateway phone runs). A True that
    # merely SICKERED IN from the global setting must not pin the run: the
    # override ContextVar wins over the live toggle for the WHOLE run, so a
    # mid-run /gate off could never reach it (live 12:46 — approval cards
    # kept arriving after "Approvals OFF"). UI runs take the global toggle
    # LIVE per gated call instead.
    if approval_gate_force(model_overrides):
        from tools.runner import _approval_gate_run_override as _gate_cv
        _gate_cv.set(True)
    # Phone restriction (2026-10-05 UI toggle): clamp this run's toolset
    # to web + read-only at the single dispatch choke point.
    if _run_settings.get("phone_restricted"):
        from tools.runner import _tools_restricted_run as _tr_cv
        _tr_cv.set(True)
    if str(_run_settings.get("source") or "").startswith("telegram"):
        from tools.runner import _phone_source_run as _ps_cv
        _ps_cv.set(True)

    def _pt_key_for_phase(phase: str) -> str:
        """Phase -> PhaseTimer-Schluessel (Pipeline-Stages haben eigene Toepfe)."""
        return {
            "coder": "coder_loop", "planner": "soft_planner",
            "pre_explore": "pre_explore", "critic": "coder_loop",
            "analyst": "pipeline_analyst", "refiner": "pipeline_refiner",
            "synthesizer": "pipeline_synthesizer", "vision": "pipeline_vision",
            "pipeline_critic": "pipeline_critic",
        }.get(str(phase or ""), "coder_loop")

    # Per-run token estimate accumulator - incremented by emit() on content events
    _run_token_estimate: int = 0
    _run_real_tokens_by_phase: dict = {}
    _run_prompt_by_phase: dict = {}
    _run_cached_by_phase: dict = {}
    _run_models: dict = {}
    _run_requests: int = 0

    async def emit(data: dict) -> str:
        nonlocal _run_token_estimate, _run_real_tokens_by_phase, _run_models, _run_requests
        nonlocal _run_prompt_by_phase, _run_cached_by_phase
        # Track generated output tokens from content events (all token types)
        _tok_types = {"token", "thinking_token", "planner_token", "planner_thinking_token", "planner_plan_token"}
        if data.get("type") in _tok_types and data.get("content"):
            n = _estimate_tokens_from_content(str(data["content"]))
            _run_token_estimate += n
            _phase_timer.add_tokens(n)
        # A-P2-7: echte completion_tokens vom llama.cpp-Server akkumulieren (pro Phase).
        if data.get("type") == "usage_meta" and data.get("completion_tokens"):
            _ph = str(data.get("phase") or "coder")
            _run_requests += 1
            _run_real_tokens_by_phase[_ph] = _run_real_tokens_by_phase.get(_ph, 0) + int(data["completion_tokens"])
            _run_prompt_by_phase[_ph] = _run_prompt_by_phase.get(_ph, 0) + int(data.get("prompt_tokens") or 0)
            _run_cached_by_phase[_ph] = _run_cached_by_phase.get(_ph, 0) + int(data.get("cached_tokens") or 0)
            # D2-DIAG (2026-08-21): detect cache miss — large prompt, small cached,
            # correlatable with [CTX-COMPRESS].
            try:
                _cached_n = int(data.get("cached_tokens") or 0)
                _prompt_n = int(data.get("prompt_tokens") or 0)
                if _prompt_n >= 20000 and _cached_n < _prompt_n * 0.25:
                    logger.warning(
                        "[CACHE-MISS] phase=%s prompt=%d cached=%d (reuse %.0f%%)",
                        _ph, _prompt_n, _cached_n,
                        (100.0 * _cached_n / _prompt_n) if _prompt_n else 0.0,
                    )
            except Exception:
                pass
            _pt_key = str(data.get("timer_key") or _pt_key_for_phase(_ph))
            _phase_timer.add_real(_pt_key, int(data["completion_tokens"]), float(data.get("gen_ms") or 0.0))
        if data.get("type") == "run_meta" and isinstance(data.get("models"), dict):
            _run_models.update(data["models"])
            return ""
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _pipeline_timer_key(phase: str) -> str:
        """Eigener PhaseTimer-Topf je Pipeline-Stage (critic kollidiert sonst mit duo)."""
        return "pipeline_" + str(phase or "pipeline")

    def _phase_from_role(role) -> str:
        """agent_role -> Phase: analyst | refiner | critic | synthesizer | vision | pipeline"""
        _r = str(role or "").strip().lower()
        if _r in ("vision_agent", "vision", "va"):
            return "vision"
        for _known in ("analyst", "refiner", "critic", "synthesizer"):
            if _r.startswith(_known):
                return _known
        return "pipeline"

    async def _pipeline_stream_tracked(model, msgs, temp, max_tok, phase=None, **kw):
        """PIPELINE-METRICS (2026-10-20): eine Stage = ein LLM-Call.

        Phase kommt aus `agent_role` (die pipeline_runner ohnehin mitschickt),
        damit der Aufrufer nichts aendern muss. Misst Generierungszeit, holt die
        echten usage-Zahlen (stream_options.include_usage) und meldet das Modell
        der Stage als run_meta - daraus baut der Collector Phasen, Zeiten und die
        Modellzuordnung wie bei den anderen Modi.
        """
        phase = phase or _phase_from_role(kw.get("agent_role"))
        if phase == "critic":
            phase = "critic"   # Anzeige-Phase; Timer-Topf setzt der Wrapper unten
        if model:
            await emit({"type": "run_meta", "models": {phase: str(model)}})
        _usage: dict = {}
        _t0 = time.perf_counter()
        _t_first = None
        async for tok in _pipeline_chat_stream(model, msgs, temp, max_tok,
                                               usage_out=_usage, **kw):
            if _t_first is None:
                _t_first = time.perf_counter()
            yield tok
        _end = time.perf_counter()
        _gen_ms = max(0.0, (_end - (_t_first if _t_first is not None else _t0)) * 1000.0)
        _ct = int(_usage.get("completion_tokens") or 0)
        _pt = int(_usage.get("prompt_tokens") or 0)
        _det = _usage.get("prompt_tokens_details") or {}
        _tim = _usage.get("timings") or {}
        _ca = int((_det.get("cached_tokens") if isinstance(_det, dict) else 0)
                  or _usage.get("cached_tokens") or 0
                  or (_tim.get("cache_n") if isinstance(_tim, dict) else 0) or 0)
        if _ct > 0:
            await emit({"type": "usage_meta", "phase": phase,
                        "timer_key": _pipeline_timer_key(phase),
                        "completion_tokens": _ct, "prompt_tokens": _pt,
                        "cached_tokens": _ca, "gen_ms": _gen_ms})
        else:
            # Server ohne usage-Block: Zeit wenigstens als Phase verbuchen
            _phase_timer.add_real(_pipeline_timer_key(phase), 0, _gen_ms)

    def _stop_reason_bucket(stop_reason: str) -> str:
        return _bucket_stop_reason(stop_reason)

    def _done_event(elapsed: float, stop_reason: str = "completed", **extra) -> dict:
        nonlocal _run_token_estimate, _run_real_tokens_by_phase
        nonlocal _run_prompt_by_phase, _run_cached_by_phase
        _real_total = sum(int(v) for v in _run_real_tokens_by_phase.values() if isinstance(v, (int, float)))
        _prompt_total = sum(int(v) for v in _run_prompt_by_phase.values() if isinstance(v, (int, float)))
        _cached_total = sum(int(v) for v in _run_cached_by_phase.values() if isinstance(v, (int, float)))
        _tokens_to_record = _real_total if _real_total > 0 else _run_token_estimate
        if _real_total > 0:
            logger.info("[A-P2-7] real eval_counts=%d prompt=%d cached=%d (by phase=%s) heuristic=%d -- recording %d",
                        _real_total, _prompt_total, _cached_total,
                        _run_real_tokens_by_phase, _run_token_estimate, _tokens_to_record)
        _record_ok = stop_reason in ("completed", "loop_detected", "timeout", "hard_stop", "graceful_stop", "aborted")
        if _record_ok and _tokens_to_record > 0:
            _phases = dict(_run_real_tokens_by_phase)
            _phases_ms: dict = {}
            try:
                _snap = _phase_timer.snapshot()
                _PIPELINE_PHASES = (("analyst", "pipeline_analyst"),
                                    ("refiner", "pipeline_refiner"),
                                    ("critic", "pipeline_critic"),
                                    ("synthesizer", "pipeline_synthesizer"),
                                    ("vision", "pipeline_vision"))
                for _pname, _skey in (("pre_explore", "pre_explore"),
                                      ("planner", "soft_planner"),
                                      ("coder", "coder_loop")) + _PIPELINE_PHASES:
                    _ent = _snap.get(_skey) or {}
                    # Pipeline-Stages kennen nur die reine Generierungszeit
                    # (add_real fuellt gen_s, kein start/end) -> als Dauer nutzen
                    _ds = _ent.get("duration_s")
                    if _ds is None:
                        _ds = _ent.get("gen_s")
                    if _ds:
                        _phases_ms[_pname] = int(round(float(_ds) * 1000))
                for _pname, _skey in (("pre_explore", "pre_explore"), ("planner", "soft_planner"),
                                      ("coder", "coder_loop")) + _PIPELINE_PHASES:
                    if not _phases.get(_pname):
                        _ot = (_snap.get(_skey) or {}).get("output_tokens", 0)
                        if _ot:
                            _phases[_pname] = int(_ot)
            except Exception:
                pass
            # a chat/direct run knows its model only through the registry
            _direct_model_for_stats: dict = {}
            if not _run_models:
                try:
                    from core.state import registry_get as _rg_stats
                    _rm_stats = _rg_stats("direct") or _rg_stats("analyst") or ""
                    if _rm_stats:
                        _direct_model_for_stats = {"direct": _rm_stats}
                except (ImportError, AttributeError, LookupError):
                    _direct_model_for_stats = {}
            # resolved route: the orchestrator dispatches on `complexity`
            _route_for_stats = (
                "code_duo" if str(complexity or "") == "code_duo"
                else ("direct" if str(complexity or "") in ("trivial", "simple") else "pipeline")
            )
            threading.Thread(
                target=_record_run,
                args=({
                    "run_id": run_id,
                    "tokens": _tokens_to_record,
                    "elapsed_s": float(elapsed),
                    "stop_reason": str(stop_reason),
                    "phases": _phases,
                    # DIRECT/CHAT (2026-10-20): only the duo runner emits
                    # run_meta, so a plain chat run recorded no model at all and
                    # never showed up in the per-model stats.
                    "models": _run_models or _direct_model_for_stats,
                    # TOKEN-TRACKER 1.1.0: input/cache dimension
                    "prompt_tokens": _prompt_total,
                    "cached_tokens": _cached_total,
                    "requests": _run_requests,
                    "phases_prompt": dict(_run_prompt_by_phase),
                    "phases_cached": dict(_run_cached_by_phase),
                    # METRICS-EXPAND (2026-10-19)
                    "phases_ms": _phases_ms,
                    # MODE-TRACKING (2026-10-20): what actually ran, not just what
                    # the UI requested - pipeline/direct/code_duo are the routes.
                    "mode": _route_for_stats,
                    "ui_mode": str(_ui_mode_entry or ""),
                    # EDITOR-CONTEXT (2026-10-21)
                    "editor": _editor_meta,
                },),
                daemon=True,
            ).start()
        if stop_reason == "completed":
            try:
                from infra.notify import notify_run_completed
                _cmsg = str(extra.get("phase_summary", "") if extra else "") or "Run completed successfully"
                notify_run_completed(_cmsg)
            except Exception:
                pass
        elif stop_reason not in ("graceful_stop",):
            try:
                from infra.notify import notify_run_stopped
                notify_run_stopped(str(stop_reason), str(extra.get("summary", "") if extra else ""))
            except Exception:
                pass
        # RUN-STATS (2026-10-23): the UI renders a per-answer stats line and
        # needs the model that actually produced the answer. duo/pipeline
        # report through run_meta; a plain chat run only knows the registry
        # slot (same fallback the run record uses).
        _ev_model = ""
        if _run_models:
            _ev_model = str(
                _run_models.get("coder")
                or _run_models.get("direct")
                or _run_models.get("synthesizer")
                or next(iter(_run_models.values()), "")
            )
        else:
            try:
                from core.state import registry_get as _rg_ev
                _ev_model = str(_rg_ev("direct") or _rg_ev("analyst") or "")
            except (ImportError, AttributeError, LookupError):
                _ev_model = ""
        ev = {
            "type": "done",
            "elapsed": float(elapsed),
            "stop_reason": str(stop_reason or "completed"),
            "stop_reason_bucket": _stop_reason_bucket(stop_reason),
            "tokens_generated": _tokens_to_record,
            "prompt_tokens_total": _prompt_total,
            "cached_tokens_total": _cached_total,
            "requests_total": _run_requests,
            "model": _ev_model,
        }
        if extra:
            ev.update(extra)
        return ev

    t_total     = time.time()
    _phase_timer = PhaseTimer()
    run_id      = f"{int(t_total)}-{uuid.uuid4().hex[:8]}"
    # WORKSPACE-PERSISTENCE (2026-08-25 REWORK): Central resolution via
    from utils.workspace_resolve import (
        resolve_workspace as _resolve_workspace,
        commit_allowed_for as _commit_allowed_for,
        sync_env_workspace as _sync_env_workspace,
        save_last_workspace as _save_last_ws,
        WorkspaceForceInvalid as _WsForceInvalid,
    )
    try:
        _chat_ctx_ws = _load_chat_context(chat_id) if chat_id else {}
        # WORKSPACE PER CHAT (2026-10-03): the run resolver must see the
        # same workspace GET /chats/{id} serves - the json value wins over
        # the sidecar (silent cross-project switch, audit finding).
        if chat_id and not (_chat_ctx_ws or {}).get("workspace"):
            try:
                _json_ws = get_chat_workspace(chat_id)
                if _json_ws:
                    _chat_ctx_ws = {**(_chat_ctx_ws or {}), "workspace": _json_ws}
            except (OSError, ValueError):
                pass
        _ws_str, _ws_src = _resolve_workspace(settings, _chat_ctx_ws, user_input,
                                              request_workspace=request_workspace)
        # COMMIT-GATE (2026-10-14, security fix): auto-commit only when the
        # run explicitly addressed a workspace (request parameter) or
        # continues a chat session with a persisted workspace (UI flow). A
        # stray API run with neither reads the fallback but never commits.
        _ws_commit_allowed = _commit_allowed_for(request_workspace, _chat_ctx_ws)
        if _ws_src == "request_invalid":
            _ws_commit_allowed = False
    except _WsForceInvalid as _wfi:
        logger.error("[WS-RESOLVE] %s", _wfi)
        yield await emit({"type": "status", "content": f"\u26d4 {_wfi}"})
        yield await emit(_done_event(0.0, "blocked",
                                    summary=f"Workspace invalid: {str(_wfi.raw)[:120]}"))
        return
    # REQUEST-INVALID-FALLBACK: a regression report ("the model forgets the
    # workspace, it worked before") showed that 1.3.9 lets the
    # /stream workspace parameter beat every other source (the gravSandbox
    # incident). Correct for stray API runs - but when the UI keeps sending a
    # path that no longer exists on THIS machine (another box's folder, a renamed
    # or moved project), the run lost its workspace entirely: no root for
    # write_file, and the model looked like it had forgotten the project. 1.3.8
    # ignored the parameter, which is exactly why it used to work. Interactive
    # sessions (a chat is in scope) now fall back to the normal chain WITHOUT
    # auto-commit; stray API runs keep the strict empty-workspace behaviour.
    if _ws_src == "request_invalid" and chat_id:
        _ws_commit_allowed = False
        try:
            from utils.workspace_resolve import (
                fallback_for_invalid_request as _fb_invalid,
            )
            _fb_ws, _fb_src = _fb_invalid(settings, _chat_ctx_ws, user_input)
        except (ImportError, AttributeError, TypeError, ValueError):
            _fb_ws, _fb_src = "", ""
        if _fb_ws:
            logger.warning(
                "[WS-RESOLVE] request workspace %r is invalid on this machine - "
                "continuing in %s (source=%s, auto-commit stays OFF; fix the "
                "Workspace field in the UI to silence this)",
                str(request_workspace)[:120], _fb_ws, _fb_src,
            )
            yield await emit({
                "type": "status",
                "content": ("\u26a0 The workspace from the request does not exist "
                            f"on this machine - using {_fb_ws} instead "
                            "(auto-commit is off)."),
            })
            _ws_str, _ws_src = _fb_ws, _fb_src
    # ENV-Sync: Subprozesse (run_bash/run_python/browser) + Fallback-Lesezugriffe
    _sync_env_workspace(_ws_str)
    # WS-FALLBACK-WARN: with nothing configured the resolver falls back to the
    # process CWD (= the install folder) and the run then persists THAT as
    # "last used" - so one accidental run pins the install directory as the
    # workspace for every later run and the model keeps writing next to the
    # app. Never persist a fallback, and say it out loud once per run.
    if _ws_src == "cwd_fallback":
        logger.warning(
            "[WS-RESOLVE] no workspace configured - using the engine directory "
            "%s. Set the 'Workspace' field in the UI (per chat) so files land "
            "where you expect; this fallback is NOT saved as last-used.",
            _ws_str,
        )
        _ws_commit_allowed = False
    else:
        _save_last_ws(_ws_str)
    logger.warning("[RUN-TRACE] workspace=%s (source=%s)", _ws_str, _ws_src)

    _exec_ctrl                = None  # duo_runner / pipeline_runner
    _resolve_resume_block     = None  # Runner bei Resume
    _do_prefetch              = None  # _schedule_prefetch()
    _prefetch_judge_once      = None
    run_peer_ratings          = None

    use_learned   = settings.get("learning_preset_mode", False)
    smart_preload = settings.get("smart_preload_enabled", False)
    # P1-2 FIX: cache VRAM budget ONCE per run — prevents mid-run setting changes
    # from causing VRAM overcommit — OOM — model crash. Previously parsed from
    # mutable settings dict 13+ times per run with inconsistent values possible.
    _raw_budget = settings.get("vram_budget_gb")
    _vram_budget = float(_raw_budget) if _raw_budget is not None else DEFAULT_VRAM_BUDGET_GB
    abort_ev      = _register_abort(run_id)
    step_skip_ev = _register_step_skip(run_id)  # NEW
    # TAINT-RESET (2026-11-02): every run starts with no untrusted content
    # seen; a previous run's web/MCP reads must not gate this one.
    try:
        from tools import taint as _run_taint
        _run_taint.clear()
    except ImportError:
        pass
    logger.warning("[RUN-TRACE] abort/stepskip registered run_id=%s", run_id)
    _agent_elapsed: dict = {}   # agent_key - elapsed seconds, fÃƒÂ¼r Log + Prefetch-Kalibrierung
    _vram_cache:    set  = await _get_loaded_models_set() if smart_preload else set()

    # Phase H: run-scoped perf metrics + prefetch tuning state.
    _ctx_pressure_peak = 0.0
    _ctx_peak_tokens = 0
    _ctx_limit_seen = 0
    _ctx_evictions = 0
    _ctx_compressions = 0
    _tool_round_durations: list[float] = []
    _prefetch_inflight = 0
    _prefetch_inflight_max = 0
    _prefetch_run_active = True
    _prefetch_state = {
        "prefetch_agent_avgs": dict(settings.get("prefetch_agent_avgs", {}) or {}),
        "prefetch_lead_seconds": float(settings.get("prefetch_lead_seconds", 8.0) or 8.0),
    }

    def _runtime_telemetry_snapshot() -> dict:
        try:
            import backend.llama_server_manager as _lsm_metrics
            _mgr = getattr(_lsm_metrics, "manager", None)
            if _mgr and hasattr(_mgr, "telemetry_snapshot"):
                return dict(_mgr.telemetry_snapshot() or {})
        except Exception:
            pass
        return {}

    _runtime_telemetry_start = _runtime_telemetry_snapshot()

    def _runtime_delta(now: dict, key: str) -> int:
        try:
            return int(now.get(key, 0)) - int(_runtime_telemetry_start.get(key, 0))
        except Exception:
            return 0

    def _collect_done_metrics() -> dict:
        _agent_vals = [float(v) for v in _agent_elapsed.values() if isinstance(v, (int, float))]
        _tool_vals = [float(v) for v in _tool_round_durations if isinstance(v, (int, float))]
        _rt_now = _runtime_telemetry_snapshot()
        return {
            "metrics_ctx_pressure_peak": round(_ctx_pressure_peak, 3),
            "metrics_ctx_peak_tokens": int(_ctx_peak_tokens),
            "metrics_ctx_limit": int(_ctx_limit_seen),
            "metrics_ctx_evictions": int(_ctx_evictions),
            "metrics_ctx_compressions": int(_ctx_compressions),
            "metrics_tool_rounds": int(len(_tool_vals)),
            "metrics_tool_round_p50_s": round(_percentile_float(_tool_vals, 0.5), 3),
            "metrics_tool_round_p95_s": round(_percentile_float(_tool_vals, 0.95), 3),
            "metrics_agent_p50_s": round(_percentile_float(_agent_vals, 0.5), 3),
            "metrics_agent_p95_s": round(_percentile_float(_agent_vals, 0.95), 3),
            "metrics_prefetch_lead_s": float(_prefetch_state.get("prefetch_lead_seconds", settings.get("prefetch_lead_seconds", 8.0))),
            "metrics_prefetch_inflight_max": int(_prefetch_inflight_max),
            "metrics_runtime_evictions": _runtime_delta(_rt_now, "evictions_total"),
            "metrics_runtime_evictions_manual": _runtime_delta(_rt_now, "evictions_manual"),
            "metrics_runtime_evictions_lru": _runtime_delta(_rt_now, "evictions_lru"),
            "metrics_runtime_evictions_idle": _runtime_delta(_rt_now, "evictions_idle"),
            "metrics_runtime_evictions_ctx_reload": _runtime_delta(_rt_now, "evictions_ctx_reload"),
            "metrics_runtime_evictions_parallel_reload": _runtime_delta(_rt_now, "evictions_parallel_reload"),
            "metrics_runtime_orphan_rehabilitations": _runtime_delta(_rt_now, "orphan_rehabilitations"),
            "metrics_runtime_prefetch_enqueued": _runtime_delta(_rt_now, "prefetch_enqueued"),
            "metrics_runtime_prefetch_dequeued": _runtime_delta(_rt_now, "prefetch_dequeued"),
            "metrics_runtime_prefetch_failures": _runtime_delta(_rt_now, "prefetch_failures"),
            "metrics_runtime_prefetch_pending_end": int(_rt_now.get("prefetch_pending", 0)),
            "metrics_runtime_prefetch_active_end": int(_rt_now.get("prefetch_active", 0)),
            "phase_timings": _phase_timer.snapshot(),
            "phase_summary": _phase_timer.ui_summary(),
        }

    async def _maybe_preload(model: str) -> str | None:
        nonlocal _vram_cache
        if not smart_preload:
            return None
        did_load, _vram_cache = await smart_preload_if_needed(model, _vram_cache)
        if did_load:
            return await emit({"type": "status", "content": f">> Smart Preload: {model.split(':')[0]} -> VRAM"})
        return None

    def _schedule_prefetch(next_model: str, current_agent: str, agent_start_time: float | None = None):


        if not smart_preload:
            return
        current_model = registry_get(current_agent) if current_agent in _state.pipeline.agents else ""
        if next_model == current_model:
            return
        lead  = float(_prefetch_state.get("prefetch_lead_seconds", settings.get("prefetch_lead_seconds", 8.0)))
        avg   = float(_prefetch_state.get("prefetch_agent_avgs", {}).get(current_agent, _get_avg_elapsed(current_agent)))
        now   = time.time()
        start = agent_start_time or now

        # Phase H: load-aware lead backoff under VRAM contention.
        try:
            _budget = max(2.0, _vram_budget)
            _used = sum(float(_VRAM_LOOKUP_GB.get(m, 4.0)) for m in _vram_cache)
            _pressure = _used / max(0.1, _budget)
            if _pressure >= 0.85:
                lead = max(2.0, lead * 0.7)
            elif _pressure >= 0.75:
                lead = max(2.5, lead * 0.82)
            if _prefetch_inflight >= 2:
                lead = max(2.0, lead * 0.85)
        except Exception:
            pass

        delay = max(0.5, (start + avg - lead) - now) if avg > 0 else 0.5

        async def _do_prefetch():
            nonlocal _vram_cache, _prefetch_inflight, _prefetch_inflight_max
            _prefetch_inflight += 1
            _prefetch_inflight_max = max(_prefetch_inflight_max, _prefetch_inflight)
            try:
                await asyncio.sleep(delay)
                if not _prefetch_run_active or _abort_event(run_id) is None:
                    return
                # Refresh the cache before prefetching.
                try:
                    _fresh = await _get_loaded_models_set(max_age=0)
                    _vram_cache = _fresh
                except Exception:
                    pass
                # Pipeline agents bypass judge protection via a direct httpx call.
                _judge_mdl = _registry.get("judge", "")
                _is_judge_model = bool(_judge_mdl and (
                    next_model == _judge_mdl or next_model.split(":")[0] == _judge_mdl.split(":")[0]
                ))
                if _is_judge_model:
                    _already_warm = next_model in _vram_cache or any(
                        next_model.split(":")[0] == m.split(":")[0] for m in _vram_cache
                    )
                    if not _already_warm:
                        try:
                            _pf_ctx = _get_num_ctx(next_model)
                            from backend import api_prefetch_next as _api_pf_next
                            await _api_pf_next(next_model, num_ctx=_pf_ctx)
                            _vram_cache |= {next_model}
                        except Exception:
                            pass
                else:
                    from backend import api_prefetch_next as _api_pf_next
                    await _api_pf_next(next_model, num_ctx=_get_num_ctx(next_model))
            except asyncio.CancelledError:
                raise
            except Exception as _pf_err:
                logger.warning("Prefetch task failed (%s via %s): %s", next_model, current_agent, str(_pf_err)[:120])
            finally:
                _prefetch_inflight = max(0, _prefetch_inflight - 1)

        spawn(_do_prefetch())

    logger.warning("[RUN-TRACE] vor run_id-yield")
    yield await emit({"type": "run_id", "run_id": run_id})

    if use_learned:
        apply_learned_configs_for_run()
    else:
        apply_settings_to_pipeline(settings)
    logger.warning("[RUN-TRACE] run_id-yield + pipeline-apply ok (use_learned=%s)", use_learned)

    _judge_prefetch_task: asyncio.Task | None = None
    _can_need_judge = (
        mode in ("auto", "automap")
        and not duo_config.agentic_mode
        and not force_complexity
        and 10 < int(judge_bias) < 90
    )
    if _can_need_judge and bool(settings.get("judge_prefetch_before_complexity", False)):
        async def _prefetch_judge_once():
            try:
                _judge_model = _registry.get("judge", "")
                if not _judge_model:
                    return
                _budget = _vram_budget
                _judge_gb = float(_VRAM_LOOKUP_GB.get(_judge_model, 2.1))
                _loaded = await _get_loaded_models_set(max_age=3.0)
                _jbase = _judge_model.split(":")[0]
                _other_gb = sum(
                    float(_VRAM_LOOKUP_GB.get(m, 4.0))
                    for m in _loaded
                    if m.split(":")[0] != _jbase
                )
                if _other_gb + _judge_gb > _budget:
                    return
                from backend import api_prefetch_next as _api_pf_next
                await _api_pf_next(_judge_model, num_ctx=_get_num_ctx(_judge_model, "judge"))
            except Exception:
                pass
        _judge_prefetch_task = asyncio.create_task(_prefetch_judge_once())

    # IMAGE-CONTEXT (2026-11-04): the transcript stores text, so a text-only
    # follow-up in a direct chat used to lose the photo it referred to. Keep
    # the last image set per chat and re-attach it when this run carries none.
    _images_from_context = False
    if not images and chat_id:
        try:
            images = load_chat_images(chat_id)
            _images_from_context = bool(images)
            if _images_from_context:
                logger.info("[IMAGE-CONTEXT] reattached %d image(s) for chat=%s",
                            len(images), chat_id)
        except (OSError, ValueError, TypeError) as _img_ctx_err:
            logger.warning("[IMAGE-CONTEXT] load failed for chat=%s: %s",
                           chat_id, _img_ctx_err)
            images = []

    _vision_images, _vision_input_status = _filter_vision_images(images)
    if _vision_input_status:
        yield await emit({"type": "status", "content": _vision_input_status})
    images = _vision_images
    if images and chat_id and not _images_from_context:
        try:
            save_chat_images(chat_id, images)
        except (OSError, ValueError, TypeError):
            pass

    image_description: str | None = None
    effective_images = images

    _pipeline_soul = load_soul(THIS_FILE)

    def _aborted() -> bool:
        return abort_ev.is_set()
     
    def _step_skipped() -> bool:
        return step_skip_ev.is_set()

    # OUTSOURCED (split 3.1): memory list/forget/store + self-query early
    # exits → _chat_memory_shortcuts(); `handled` comes back as the return.
    _sc_ps = _ChatShortcutState(
        user_input=user_input, images=images, run_id=run_id, mode=mode,
        iterations=iterations, use_learned=use_learned,
        constraint_mode=constraint_mode, t_total=t_total,
        pipeline_soul=_pipeline_soul,
        carry={"emit": emit, "_done_event": _done_event},
    )
    async for _ev in _chat_memory_shortcuts(_sc_ps):
        yield _ev
    if _sc_ps.handled:
        return
    # OUTSOURCED (split 3.2): image persistence + vision preprocess →
    # _chat_vision_pre(); outputs come back through the state.
    _vp_ps = _ChatVisionPreState(
        _images_from_context=_images_from_context, images=images,
        user_input=user_input, mode=mode, _ws_str=_ws_str,
        _vram_budget=_vram_budget, _run_settings=_run_settings,
        effective_images=effective_images,
        image_description=image_description,
        carry={"emit": emit},
    )
    async for _ev in _chat_vision_pre(_vp_ps):
        yield _ev
    if _vp_ps._judge_gb is not None:
        _judge_gb = _vp_ps._judge_gb
    _saved_image_paths = _vp_ps._saved_image_paths
    effective_images = _vp_ps.effective_images
    image_description = _vp_ps.image_description
    # OUTSOURCED (split 3.3): history seed + session window + compression →
    # _chat_history_seed().
    _hs_ps = _ChatHistorySeedState(
        chat_id=chat_id, mode=mode, duo_config=duo_config,
    )
    await _chat_history_seed(_hs_ps)
    _pipeline_mem_ctx = _hs_ps._pipeline_mem_ctx
    _pipeline_sess_msgs = _hs_ps._pipeline_sess_msgs
    _sess_compress_threshold = _hs_ps._sess_compress_threshold
    # OUTSOURCED (split 3.4): intent agent + task type + automap suggestion +
    # judge-prefetch wait → _chat_route_pre(); handled = early return.
    _rp_ps = _ChatRoutePreState(
        _aborted=_aborted,
        _judge_prefetch_task=_judge_prefetch_task,
        _pipeline_mem_ctx=_pipeline_mem_ctx,
        _pipeline_sess_msgs=_pipeline_sess_msgs,
        _step_skipped=_step_skipped,
        active_preset=active_preset,
        duo_config=duo_config,
        emit=emit,
        force_complexity=force_complexity,
        image_description=image_description,
        images=images,
        mode=mode,
        run_id=run_id,
        t_total=t_total,
        use_learned=use_learned,
        user_input=user_input,
        _done_event=_done_event,
        _collect_done_metrics=_collect_done_metrics,
    )
    async for _ev in _chat_route_pre(_rp_ps):
        yield _ev
    if _rp_ps.handled:
        return
    _complexity_source = _rp_ps._complexity_source
    _effective_task_type = _rp_ps._effective_task_type
    _judge_verdict = _rp_ps._judge_verdict
    _prepro_success = _rp_ps._prepro_success
    _task_type = _rp_ps._task_type
    # OUTSOURCED (split 3.5): auto-decider + judge route + learned hint +
    # direct-tools decision → _chat_route_decision().
    _rd_ps = _ChatRouteDecisionState(
        _decide_route=_decide_route,
        _prepro_success=_prepro_success,
        _run_settings=_run_settings,
        _sess_compress_threshold=_sess_compress_threshold,
        _task_type=_task_type,
        agentic_force=agentic_force,
        duo_config=duo_config,
        emit=emit,
        force_complexity=force_complexity,
        images=images,
        mode=mode,
        model_overrides=model_overrides,
        user_input=user_input,
        _complexity_source=_complexity_source,
        _effective_task_type=_effective_task_type,
            _judge_verdict=_judge_verdict,
        _pipeline_sess_msgs=_pipeline_sess_msgs,
            )
    async for _ev in _chat_route_decision(_rd_ps):
        yield _ev
    _complexity_source = _rd_ps._complexity_source
    _effective_task_type = _rd_ps._effective_task_type
    _judge_toolsize = _rd_ps._judge_toolsize
    _judge_verdict = _rd_ps._judge_verdict
    _pipeline_sess_msgs = _rd_ps._pipeline_sess_msgs
    _route_as_tool = _rd_ps._route_as_tool
    complexity = _rd_ps.complexity

    if _route_as_tool:
        _available = list(S_models_cache) if S_models_cache else []
        # small: simple file reads, git - qwen2.5:3b suffices
        if _judge_toolsize == "large":
            _tool_priority = ["qwen3.5:4b", "qwen3.5:9b-ud", "qwen3.5:2b", "granite-4.1:3b"]
        else:
            _tool_priority = ["qwen3.5:2b", "granite-4.1:3b", "qwen3.5:4b", "qwen3.5:9b-ud"]
        _tool_model = next((m for m in _tool_priority if m in _available),
                           registry_get("direct"))
        _tool_ctx_override = max(_get_num_ctx(_tool_model) or 4096, 8192)
        _tool_opts: dict = {"temperature": 0.1, "num_predict": 1200, "num_ctx": _tool_ctx_override}
        _tool_sys = (
            "Use the available tools to complete file and code operations. Be direct and concise.\n\n"
            "Available tools:\n"
            "  read_file(path, start_line?, end_line?) - read file (optional: line range only)\n"
            "  get_signatures(path, max_items?) - structural overview with line numbers\n"
            "  find_files(pattern, path?)  - glob search: '**/*.py', 'src/*.ts'\n"
            "  list_dir(path)              - list directory contents\n"
            "  search_code(pattern, path?) - regex search across code files\n"
            "  edit_file(path, old_text, new_text) - surgical edit (preferred for modifications)\n"
            "  edit_file(path, edits)      - create new files OR modify existing ones\n"
            "  run_bash(cmd)               - shell command: tests, pip install, npm run, grep...\n"
            "  run_python(code)            - execute python snippet directly\n"
            "  git_status(cmd)             - git: status|diff|log|show\n"
            "  git_commit(message)         - commit all changes with message\n\n"
            "Workflow:\n"
            "  1. Explore first (list_dir / find_files / read_file)\n"
            "  2. Modify existing files with edit_file\n"
            "  3. After implementing, run tests (run_bash pytest / npm test)\n"
            "  4. On failure: re-read the region and edit_file again\n"
            "  Do not ask for confirmation - just act."
        )
        _tool_msgs = _make_messages(_state.pipeline, _tool_sys, user_input, effective_images, False, False)
        yield await emit({"type": "complexity", "content": "simple", "source": "tool"})
        yield await emit({"type": "agent", "content": "Tool Agent", "model": _tool_model})
        _tool_t = time.time()
        _tool_loop_msgs = list(_tool_msgs)
        _tool_out_parts: list[str] = []
        _tool_max_rounds = 6
        _tool_calls: list = []
        _tool_round = 0
        _tool_stop_reason = "completed"
        _tool_ws = bool(settings.get("duo_websearch_enabled", False)) and _WEBSEARCH_AVAILABLE
        # SCHEMA-FIT (2026-10-17): this path shipped the full 25-tool schema
        # (~15.6k chars) whatever the tool-agent ctx was. Fit it to the window.
        _tool_schema_budget = 0
        try:
            from core.budgets import schema_budget_for as _sbf
            _tool_schema_budget = _sbf(_tool_ctx_override, _tool_model)
        except (ImportError, AttributeError, TypeError, ValueError):
            _tool_schema_budget = 0
        _active_tools = _get_inline_tools(include_websearch=_tool_ws, mode="tool_agent",
                                          budget_chars=_tool_schema_budget or None)
        # Timeout strategy:
        _tool_read_timeout_s = float(settings.get("duo_llm_slow_timeout_s", 300))
        _tool_http_timeout = _make_httpx_timeout(read_s=_tool_read_timeout_s)
        try:
            from backend.llama_server_manager import manager as _lsm
            _tool_port = await _lsm.ensure_loaded(_tool_model, num_ctx=_tool_opts.get("num_ctx", 4096))
            async with httpx.AsyncClient(timeout=_tool_http_timeout) as _tc:
                from core.tool_loop import ToolLoop, ToolLoopConfig
                _loop = ToolLoop(
                    config=ToolLoopConfig(
                        stream=False, max_rounds=_tool_max_rounds, max_post_attempts=1,
                        model=_tool_model, temperature=_tool_opts.get("temperature", 0.2),
                        max_tokens=_tool_opts.get("num_predict", 1200),
                        num_ctx=_tool_opts.get("num_ctx", 4096),
                        tools=_active_tools, tool_mode="tool_agent",
                        include_websearch=_tool_ws,
                        read_timeout_s=_tool_read_timeout_s,
                    ),
                    http_client=_tc, port=_tool_port,
                    workspace=_ws_str,
                    abort_check=_aborted,
                )
                async for _ev in _loop.run(_tool_loop_msgs):
                    if _ev["type"] == "token":
                        _tool_out_parts.append(_ev["content"])
                    yield await emit(_ev)
                _tool_stop_reason = _loop.state.stop_reason
                _tool_calls = bool(_loop.state.tool_calls_made)
        except httpx.TimeoutException as _te:
            _elapsed_so_far = round(time.time() - _tool_t, 1)
            _err = f"[Tool timeout after {_elapsed_so_far}s - model did not respond in time. try a smaller model or simplify the request.]"
            yield await emit({"type": "token", "content": _err})
            _tool_out_parts.append(_err)
            _tool_stop_reason = "timeout"
        except Exception as _te:
            _err = f"[Tool error: {type(_te).__name__}: {str(_te)[:200]}]"
            yield await emit({"type": "token", "content": _err})
            _tool_out_parts.append(_err)
            _tool_stop_reason = "error"
        else:
            if _tool_stop_reason == "max_tool_rounds" and _tool_calls and not _aborted():
                _limit_msg = f"\n[Max. {_tool_max_rounds} tool rounds reached]"
                yield await emit({"type": "token", "content": _limit_msg})
                _tool_out_parts.append(_limit_msg)
            elif _aborted():
                _tool_stop_reason = "aborted"
        _tool_out = "".join(_tool_out_parts)
        yield await emit({"type": "agent_done", "elapsed": round(time.time() - _tool_t, 1)})
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", _tool_out)
        yield await emit(_done_event(round(time.time() - t_total, 1), _tool_stop_reason, **_collect_done_metrics()))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        return

    # --------------------------------------------------------------------------

    def _build_run_context():
        """Build RunContext with all shared state for extracted runners."""
        return RunContext(
            user_input=user_input,
            images=images,
            mode=mode,
            iterations=iterations,
            active_preset=active_preset,
            constraint_mode=constraint_mode,
            force_complexity=force_complexity,
            skip_agents=skip_agents,
            judge_bias=judge_bias,
            duo_config=duo_config,
            chat_id=chat_id,
            run_id=run_id,
            t_total=t_total,
            complexity=complexity,
            complexity_source=_complexity_source,
            effective_task_type=_effective_task_type,
            use_learned=use_learned,
            workspace=_ws_str,
            request_workspace=request_workspace,
            workspace_commit_allowed=_ws_commit_allowed,
            image_paths=_saved_image_paths,
            prepro_success=_prepro_success,
            image_description=image_description,
            effective_images=effective_images,
            vision_cfg=_vision_cfg,
            models_cache=S_models_cache,
            pipeline=_state.pipeline,
            settings=_run_settings,
            memory=_state.memory,
            registry=_registry,
            vram_budget=_vram_budget,
            vram_cache=_vram_cache,
            websearch_available=_WEBSEARCH_AVAILABLE,
            pipeline_mem_ctx=_pipeline_mem_ctx,
            pipeline_sess_msgs=_pipeline_sess_msgs,
            pipeline_soul=_pipeline_soul,
            exec_ctrl=_exec_ctrl,
            agent_elapsed=_agent_elapsed,
            prefetch_state=_prefetch_state,
            prefetch_run_active=_prefetch_run_active,
            this_file=THIS_FILE,
            simple_direct_models=_SIMPLE_DIRECT_MODELS,
            complex_direct_models=_COMPLEX_DIRECT_MODELS,
            vram_lookup_gb=_VRAM_LOOKUP_GB,
            # Callables (inner functions)
            emit=emit,
            done_event=_done_event,
            aborted=_aborted,
            step_skipped=_step_skipped,
            maybe_preload=_maybe_preload,
            maybe_trigger_soul_evolution=_maybe_trigger_soul_evolution,
            auto_memory_from_input=_auto_memory_from_input,
            pick_direct_model=_pick_direct_model,
            model_profile=_model_profile,
            get_num_ctx=_get_num_ctx,
            bk_load=_bk_load,
            bk_pin=_bk_pin,
            bk_evict=_bk_evict,
            safe_web_search=_safe_web_search,
            extract_ws_query=_extract_ws_query,
            make_messages=_make_messages,
            refresh_judge_keepalive=_refresh_judge_keepalive,
            increment_run_counter=_increment_run_counter,
            collect_done_metrics=_collect_done_metrics,
            unregister_abort=_unregister_abort,
            unregister_step_skip=_unregister_step_skip,
            schedule_prefetch=_schedule_prefetch,
            pipeline_chat_stream=_pipeline_stream_tracked,
            get_loaded_models_set=_get_loaded_models_set,
            smart_preload_if_needed=smart_preload_if_needed,
            clear_step_skip=lambda *a: _clear_step_skip(run_id),
            update_prefetch_lead=_update_prefetch_lead,
            flush_prefetch_settings=_flush_prefetch_settings,
            register_abort=_register_abort,
            register_step_skip=_register_step_skip,
            prefetch_judge_once=_prefetch_judge_once,
            do_prefetch=_do_prefetch,
            runtime_telemetry_snapshot=_runtime_telemetry_snapshot,
            runtime_delta=_runtime_delta,
            # VISION-PROMPT-PRESET (2026-11-01): the vision role is stored per
            # preset but was passed as the built-in constant - an edit in the
            # prompt editor never reached a run. Preset override wins now.
            vision_agent_prompt=(
                get_effective_prompt_with_override(
                    "vision_agent", active_preset, use_learned)
                or VISION_AGENT_PROMPT),
            phase_timer=_phase_timer,
            resolve_duo_runtime_profile=_resolve_duo_runtime_profile,
            resolve_duo_run_timeout_seconds=_resolve_duo_run_timeout_seconds,
            build_soul_prompt_layer=build_soul_prompt_layer,
            # Additional module-level functions
            registry_get=registry_get,
            get_effective_prompt_with_override=get_effective_prompt_with_override,
            run_peer_ratings=run_peer_ratings,
            run_soul_cycle=run_soul_cycle,
            get_effective_config=get_effective_config,
            is_aborted_global=_aborted,
            is_aborted_chat=_is_aborted,
            clear_resume_block=_clear_resume_block,
            run_insight_extractor=_run_insight_extractor,
            run_skill_distillation=_run_skill_distillation,
            resolve_resume_block=_resolve_resume_block,
            load_resume_block=_load_resume_block,
            append_learning_log=append_learning_log,
        )

    # --- Route to extracted runner ---
    _ctx = _build_run_context()
    logger.warning("[RUN-TRACE] vor runner-dispatch complexity=%s", _ctx.complexity)
    async for event in run_stream_orchestrated(_ctx):
        if isinstance(event, dict) and event.get("type") == "ctx_meter":
            _est = int(event.get("est_tokens") or 0)
            _lim = int(event.get("ctx_limit") or 0)
            if _est > 0:
                _ctx_peak_tokens = max(_ctx_peak_tokens, _est)
                _ctx_limit_seen = max(_ctx_limit_seen, _lim)
                if _lim > 0:
                    _ctx_pressure_peak = max(_ctx_pressure_peak, _est / _lim)
        yield event
    return
