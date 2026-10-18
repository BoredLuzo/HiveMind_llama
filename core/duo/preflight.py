"""Preflight helpers extracted from duo_runner.run_code_duo (1.4.x).

Pure move: the bodies are byte-identical, they were module-level already and
no closure state is involved. Moving them keeps the 6,700-line function
readable without touching its control flow.
"""
from __future__ import annotations
from pathlib import Path

from core.duo._pre_explore import _explore_extract_files
import httpx
from vram.loader import _bk_load, _bk_evict, _get_loaded_models_set

import asyncio, hashlib, json, logging, os, re, sys, time, uuid
from context.chat import (
    _load_chat_context, _chat_context_valid, _mutate_chat_context, _save_chat_context,
)
from context.resume import _check_abort_and_maybe_save_resume, _write_resume_block, _load_resume_block
from utils.tool import parse_tool_args as _parse_tool_args, run_bash_failed as _run_bash_failed

logger = logging.getLogger("hivemind.duo")
_DUO_HEARTBEAT_KEY = "duo_heartbeat_seconds"
_DUO_HEARTBEAT_DEFAULT = 10
_FALLBACK_MODEL_PREFERENCE = (
    # MTP variants first (2026-09-15): the plain tags resolve to the same
    # GGUFs nowadays but WITHOUT the mtp/reasoning launch flags — a fallback
    # to "qwen3.5:4b" ran the coder without spec-decode and reasoning off.
    "qwen3.5:4b-mtp", "qwen3.5:2b-mtp", "qwen3.5:4b", "qwen3.5:2b-ud",
    "qwen3.5:2b", "lfm2.5:2.6b",
    "ministral:3b-instruct-2410", "gemma-4:e4b-it-obliterated", "qwen3.5:0.8b-ud",
)
BRIEFING_FULL = "full"
BRIEFING_PLAN_ONLY = "plan-only"
BRIEFING_OFF = "off"
BRIEFING_MAX_TOKENS_DEFAULT = 1500


def _build_plan_pin_msg(_subtasks, _di, _n_items):
    """PLAN-PIN-DEDUP (2026-09-09): shared by the LLM and rule-fallback
    compression paths (was two identical inline block builds)."""
    if not (_subtasks and _di is not None and _n_items):
        return None
    _pin_lines = []
    for _pi, _pt in enumerate(_subtasks):
        if _pi < _di:
            _pin_lines.append(f"  {_pi+1}. ✓ {str(_pt)[:120]}")
        elif _pi == _di:
            _pin_lines.append(f"  {_pi+1}. → {str(_pt)[:120]}  ◀ YOU ARE HERE")
        else:
            _pin_lines.append(f"  {_pi+1}. ○ {str(_pt)[:120]}")
    if not _pin_lines:
        return None
    return f"[PLAN-PIN - current subtask {_di+1}/{_n_items}]" + chr(10) + chr(10).join(_pin_lines)


def _build_os_hint() -> str:
    """OS-HINT (2026-09-02): platform-specific run_bash guidance, appended to the
    coder system prompt. SHELL-FIX (2026-10-15): the hint names the shell
    run_bash will ACTUALLY use (settings.run_bash_shell / Git-Bash probe),
    not a hardcoded "Windows PowerShell" - auto-mode runs Git Bash whenever
    it is installed, and the old PS-only prompt sent PowerShell syntax into
    bash (live: Test-Path -> exit 2 -> identical retry loop)."""
    import sys as _sys
    try:
        from tools.handlers.exec_tools import _run_bash_shell_mode as _mode_fn
        _mode = _mode_fn()
    except ImportError:
        _mode = "powershell" if _sys.platform == "win32" else "bash"
    from tools.handlers.exec_tools import shell_os_hint
    return shell_os_hint(_mode, _sys.platform)


def _read_hb_interval(settings: dict | None) -> int:
    try:
        _iv = int((settings or {}).get(_DUO_HEARTBEAT_KEY, _DUO_HEARTBEAT_DEFAULT) or _DUO_HEARTBEAT_DEFAULT)
        return max(3, _iv)
    except Exception:
        return _DUO_HEARTBEAT_DEFAULT


def _llama_prefill_progress(port) -> dict | None:
    """PREFILL-METER (2026-09-20): tail the coder slot's llama-server log for
    the newest prompt-processing progress line and interpolate forward from
    it (llama.cpp only logs once per 1024-token batch — on a 40 tok/s Vulkan
    slot that is one line per ~25s, which read raw looked useless). The file
    mtime gives the line's age, the line itself gives total (n/progress) and
    rate, so the meter moves smoothly between log flushes. Returns None for
    small prompts (<3k tokens — they finish before the first log line and
    the plain elapsed counter is the better UI) and while decoding."""
    if not port:
        return None
    try:
        from pathlib import Path as _Path
        import re as _re
        import time as _time
        _lp = _Path(__file__).parent.parent / "logs" / f"llama_server_{int(port)}.log"
        if not _lp.exists():
            return None
        _mtime = _lp.stat().st_mtime
        with _lp.open("r", encoding="utf-8", errors="replace") as _f:
            _lines = _f.readlines()[-60:]
        for _ln in reversed(_lines):
            if "prompt eval time" in _ln or "        eval time" in _ln:
                # newest completed task already passed prefill → decode phase
                return None
            _m = _re.search(
                r"prompt processing, n_tokens =\s*(\d+), progress = ([\d.]+),"
                r" t =\s*[\d.]+ s / ([\d.]+) tokens per second",
                _ln,
            )
            if _m:
                _n = int(_m.group(1))
                _prog = float(_m.group(2))
                _tps = float(_m.group(3))
                if _prog <= 0.0 or _prog >= 0.999 or _tps <= 1.0:
                    return None
                _total = _n / _prog
                if _total < 3000:
                    return None
                _age = _time.time() - _mtime
                if _age > 45.0:
                    # log silent for 45s+ → not actively prefilling
                    return None
                _est_n = min(_total, _n + max(0.0, _age) * _tps)
                return {"n": int(_est_n),
                        "total": int(_total),
                        "progress": min(0.995, _est_n / _total),
                        "tps": _tps}
        return None
    except Exception:
        return None


async def _await_with_hb(
    coro_factory,
    *,
    timeout: float,
    emit_fn,
    label: str = "Model",
    interval: float = 10.0,
):


    _task = asyncio.create_task(coro_factory())
    _t0 = time.monotonic()
    try:
        while True:
            _el = int(time.monotonic() - _t0)
            try:
                await emit_fn({"type": "status", "content": f"⏳ {label} … ({_el}s)"})
            except Exception:
                pass
            if _el >= timeout:
                raise asyncio.TimeoutError()
            _done, _pending = await asyncio.wait({_task}, timeout=interval)
            if _done:
                return _task.result()
    finally:
        if not _task.done():
            _task.cancel()


def _park_on_disconnect(
    *,
    ctx,
    loop_items=None,
    di=None,
    done_tasks=None,
    written_files=None,
    explore_ctx="",
    ws_str="",
    n_items=0,
):
    if ctx.chat_id is None:
        return
    try:
        _rem: list = []
        if isinstance(loop_items, list) and isinstance(di, int) and 0 <= di < len(loop_items):
            _rem = [{"title": str(t)} for t in loop_items[di:]]
        if _rem:
            _write_resume_block(
                chat_id=ctx.chat_id,
                workspace=str(ws_str or ""),
                chunks_total=int(n_items or 0),
                chunks_done=list(done_tasks or []),
                chunks_remaining=_rem,
                written_files=list(written_files or []),
                last_summary=" | ".join(str(t) for t in (done_tasks or [])),
                plan_msgs=[],
                explore_ctx=str(explore_ctx or ""),
                halt_reason="disconnect",
            )
        _save_chat_context(ctx.chat_id, {
            **(_load_chat_context(ctx.chat_id) or {}),
            "last_run": {
                "task": str(getattr(ctx, "user_input", ""))[:8000],
                "stop_reason": "disconnect",
                "written_files": sorted(set(written_files or []))[:50],
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "success": False,
            },
        })
        logger.warning(
            "[DISCONNECT-PARK] Run parked chat=%s chunks_remaining=%d written_files=%d halt_reason=disconnect",
            ctx.chat_id, len(_rem), len(list(written_files or [])),
        )
        try:
            from infra.notify import notify
            notify(
                "HiveMind — Run interrupted (browser closed)",
                f"chat={ctx.chat_id}, {len(list(written_files or []))} file(s) written. "
                "Send a message to continue.",
                dedup_sig=f"interrupt:{ctx.chat_id}",
            )
        except Exception:
            pass
        logger.warning(
            "\n" + "=" * 62 + "\n"
            + "  BROWSER CLOSED — RUN PARKED\n"
            + f"  chat={ctx.chat_id} | files_written={len(list(written_files or []))} | chunks_remaining={len(_rem)}\n"
            + "  Resume: open the chat and send a message to continue.\n"
            + "=" * 62
        )
    except Exception as _park_err:
        logger.warning("[DISCONNECT-PARK] Park failed: %s", _park_err)


def _build_touched_context(msgs: list, touched_paths: set) -> str:
    """Extract file contents for touched files from Pre-Explore messages via forward-pass."""
    _path_contents: dict[str, str] = {}
    _pending_path = ""
    for _m in msgs:
        if _m.get("role") == "assistant" and "tool_calls" in _m:
            _pending_path = ""
            for _tc in _m.get("tool_calls", []):
                _args = _parse_tool_args(_tc.get("function", {}).get("arguments", {}))
                _tp = _args.get("path", "").replace("\\", "/").lower()
                for _t in touched_paths:
                    if _t in _tp or _tp in _t:
                        _pending_path = _t
                        break
                if _pending_path:
                    break
        elif _m.get("role") == "tool" and _m.get("name") == "read_file" and _pending_path:
            _content = str(_m.get("content", ""))
            if _content.strip():
                _path_contents[_pending_path] = _content[:3000]
            _pending_path = ""
    if not _path_contents:
        return ""
    _blocks = []
    for _p in sorted(_path_contents.keys()):
        _blocks.append(f"### {_p}\n```\n{_path_contents[_p]}\n```")
    return "\n\n## Relevant Files (touched by task)\n\n" + "\n\n".join(_blocks)


def _drain_thinking_rescue(duo_loop) -> list:


    if duo_loop is None:
        return []
    _events = list(getattr(duo_loop, "_pending_events", []) or [])
    duo_loop._pending_events.clear()
    _th = getattr(duo_loop, "_dr_thinking_parts", []) or []
    if _th:
        logger.warning(
            "[DUO] Tool round aborted — %d thinking chunks (%d chars) kept in memory",
            len(_th), sum(len(t) for t in _th),
        )
    return _events


def _build_dtool_base(system_content, explore_history, plan_content, bridge_msg):
    """Build the initial coder tool-loop message list.

    Guarantees exactly 1 system message at index 0.
    """
    if plan_content:
        logger.warning(
            "[PLAN-INJECT] bridge branch: plan injected into coder context (%d chars)",
            len(plan_content),
        )
    else:
        logger.warning("[PLAN-INJECT] bridge branch: NO plan present (empty/None)")
    return [
        {"role": "system", "content": system_content},
        *explore_history,
        *([{"role": "assistant", "content": f"[IMPLEMENTATION PLAN]\n{plan_content}"}]
          if plan_content else []),
        bridge_msg,
    ]


def _resolve_fallback_model(preferred: str, exclude_mdl: str = "", num_ctx: int = 8192) -> str | None:
    """Robust fallback-model resolver (2026-09-07).

    Live finding: duo_coder_fallback_model pointed at a non-installed model
    (qwen3.5:4b-ud was missing) → the planner AND coder fallback both hit a
    dead end and the run ended although other small models were available.
    This checks preferred for existence + can_fit first, then falls back
    deterministically to the first installed, loadable candidate model.
    """
    try:
        from backend.llama_models import resolve_model_path as _resolve_path, list_available_models as _list_avail
        from backend.llama_server_manager import manager as _lsm_fb
    except Exception:
        return preferred
    try:
        _avail = set(_list_avail() or [])
    except Exception:
        _avail = set()

    _cands: list[str] = []
    if preferred and preferred != exclude_mdl:
        _cands.append(preferred)
    for _m in _FALLBACK_MODEL_PREFERENCE:
        if _m not in _cands and _m != exclude_mdl:
            _cands.append(_m)
    for _m in _cands:
        try:
            if not _resolve_path(_m):
                continue
        except Exception:
            continue
        try:
            if not _lsm_fb.can_fit(_m, num_ctx).ok:
                continue
        except Exception:
            pass
        return _m
    if _avail:
        try:
            from backend.llama_vram_table import vram_of as _vram_of_fb
            for _m in sorted(_avail, key=lambda x: _vram_of_fb(x)):
                if _m == exclude_mdl or _m in _cands:
                    continue
                try:
                    if _resolve_path(_m) and _lsm_fb.can_fit(_m, num_ctx).ok:
                        return _m
                except Exception:
                    pass
        except Exception:
            pass
    return None


def _inject_plan_into_coder_msgs(dtool_msgs, plan_result, *, chunking: bool, is_first_outer_round: bool):


    if (
        plan_result is not None
        and getattr(plan_result, "plan_content", None)
    ):
        # PLAN-INJECT (2026-09-08): also injected in chunked mode — every
        # fresh coder context gets the planner's detailed plan, not just the
        # subtask title list from build_chunk_context.
        logger.warning(
            "[PLAN-INJECT] non-bridge branch: plan injected into coder context (%d chars, first_outer_round=%s)",
            len(plan_result.plan_content), is_first_outer_round,
        )
        dtool_msgs.append({
            "role": "assistant",
            "content": f"[IMPLEMENTATION PLAN]\n{plan_result.plan_content}",
        })
        return True
    logger.warning(
        "[PLAN-INJECT] non-bridge branch: injection skipped "
        "(plan=%s chunking=%s first_round=%s)",
        bool(plan_result is not None and getattr(plan_result, "plan_content", None)),
        chunking, is_first_outer_round,
    )
    return False


def _briefing_limit_tokens(settings: dict | None = None) -> int:
    try:
        val = int((settings or {}).get("duo_coder_briefing_think_tokens",
                                       BRIEFING_MAX_TOKENS_DEFAULT))
    except (TypeError, ValueError):
        val = BRIEFING_MAX_TOKENS_DEFAULT
    return max(-1, min(200_000, val))


def _est_tokens(text: str, model: str = "") -> int:
    chars = len(str(text or ""))
    if not chars:
        return 0
    try:
        from core.budgets import cpt_for as _cpt_for
        cpt = float(_cpt_for(model) or 0)
    except (ImportError, AttributeError, TypeError, ValueError):
        cpt = 0.0
    if cpt <= 0:
        cpt = 3.92
    return int(chars / cpt)


def coder_briefing_text(thinking: str, plan_content: str = "",
                        subtasks: list | None = None,
                        settings: dict | None = None,
                        model: str = "") -> tuple[str, str, int]:
    """(text_for_coder, mode, thinking_tokens) for the plan briefing.

    mode is "full" (thinking injected), "plan-only" (thinking replaced by the
    plan) or "off" (nothing to inject). Never raises.
    """
    think = str(thinking or "").strip()
    if not think:
        return "", BRIEFING_OFF, 0
    tokens = _est_tokens(think, model)
    limit = _briefing_limit_tokens(settings)
    if limit < 0 or tokens <= limit:
        return think, BRIEFING_FULL, tokens
    plan = str(plan_content or "").strip()
    if not plan and subtasks:
        plan = "\n".join(f"- {str(s).strip()}" for s in subtasks if str(s).strip())
    note = (f"[Planner reasoning dropped: ~{tokens} tokens is over the "
            f"{limit}-token briefing limit - implement the PLAN, not the reasoning.]")
    body = (plan + "\n\n" + note) if plan else note
    return body, BRIEFING_PLAN_ONLY, tokens


def _build_plan_anchor_text(subtasks: list, plan_tracker, plan_content: str = "") -> str:
    # FULL PLAN (2026-10-04, user decision: "600 zu wenig, der fertige Plan
    # soll als Ganzes durchgegeben werden"): the anchor is the ONLY part of
    # the plan that survives compression (verified in
    # tests/test_compression_plan_survival.py), so the COMPLETE plan body
    # rides along - no truncation. The anchor is re-injected verbatim, so
    # every visual detail the planner wrote stays with the coder.
    # Trade-off, accepted: a large plan keeps the compressed context large
    # (planner max_tokens=8000 -> worst case ~30KB plan text).
    _base = ""
    if subtasks:
        _base = ", ".join(str(t) for t in subtasks)
    elif plan_tracker is not None and getattr(plan_tracker, "total", 0) > 0:
        _anchor_steps = []
        for _aps in getattr(plan_tracker, "_plan", None).steps:
            _aps_paths = ", ".join(getattr(_aps, "expected_paths", []) or []) or "?"
            _anchor_steps.append(
                f"step {getattr(_aps, 'id', '?')}: {getattr(_aps, 'intent', '') or ''} → {_aps_paths}"
            )
        _base = "; ".join(_anchor_steps)
    _plan = str(plan_content or "").strip()
    if _plan:
        _base = (_base + "\nPLAN:\n" + _plan) if _base else ("PLAN:\n" + _plan)
    return _base


def _strip_stale_ctx_notices(msgs: list) -> list:
    """COMPRESS-CLEANUP-FIX (2026-08-31): removes stale CTX warning user
    messages from the message list.

    Live finding: after a compression, the "[RUNTIME NOTICE] [CTX CRITICAL:
    ~85% full] ... Stop reading new files. Complete current edits and call
    task_complete." message injected before the compression stayed at the
    tail. The context had just been shrunk to ~15% — the warning contradicted
    the fresh state and pushed the coder to a premature task_complete
    (-> loop_detected stop a few rounds after the compression).

    ONLY user/system messages with the CTX markers are removed; tool/coder
    messages stay untouched.
    """
    _markers = ("[RUNTIME NOTICE]", "[CTX CRITICAL:", "[CTX: ~", "[CTX:")
    return [
        m for m in msgs
        if not (
            m.get("role") in ("user", "system")
            and any(str(m.get("content") or "").startswith(_mk) for _mk in _markers)
        )
    ]


def _apply_thinking_kwargs(payload, profile, thinking, coder_tool_think):


    if profile.get("preserve_thinking") and thinking:
        payload.setdefault("chat_template_kwargs", {})["preserve_thinking"] = True
    if not coder_tool_think:
        payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
    return payload


async def _auto_commit_chunk(user_input: str, subtask: str, workspace: str,
                             git_enabled: bool, files: list[str] | None = None,
                             commit_allowed: bool = True) -> str:


    if not git_enabled or not workspace:
        return ""
    # COMMIT-GATE (2026-10-14, security fix): a run without an explicit
    # workspace (and without a chat session carrying one) must never commit
    # anywhere - the gravSandbox incident: a stray API run landed in
    # "last used" and swept 1186 files into a commit.
    if not commit_allowed:
        logger.warning(
            "[AUTO-COMMIT] skipped: run has no explicit workspace and no chat "
            "session workspace - committing is disabled for this run")
        return "ℹ️ auto-commit skipped: run has no explicit workspace (commit-gated)"
    try:
        from hive_functions.git_tools import auto_commit_block_reason as _acb_reason
        _block = _acb_reason(workspace)
    except Exception:
        _block = ""
    if _block:
        logger.info("[AUTO-COMMIT] skipped: %s", _block)
        return f"ℹ️ auto-commit skipped: {_block}"
    try:
        from hive_functions.git_tools import (
            exec_git_commit, exec_git_squash_checkpoints,
        )
        try:
            from settings import load_settings as _ls
            _prefix = (str((_ls() or {}).get("git_commit_prefix", "") or "").strip()
                       or "hivemind:")
        except Exception:
            _prefix = "hivemind:"
        _label = (subtask or user_input or "workspace")[:60]
        _message = f"{_prefix} {_label}"

        _fold_out = await exec_git_squash_checkpoints(_message, workspace)
        if _fold_out.startswith(("✅", "❌")):
            return "" if _fold_out.startswith("ℹ️") else _fold_out

        _out = await exec_git_commit(message=_message, workspace=workspace,
                                     files=files or None, allow_add_all=False)
        if _out.startswith(("ℹ️", "⚠️")):
            return ""
        return _out
    except Exception as _ac_err:
        logger.warning("[AUTO-COMMIT] failed: %s", _ac_err)
        return ""


def _git_checkpoints_enabled(ctx) -> bool:

    try:
        from settings import load_settings as _ls
        return bool((_ls() or {}).get("duo_git_checkpoints", False))
    except Exception:
        return True


async def _git_checkpoint_at_chunk_start(ctx, _ws_str: str, _di: int,
                                         _n_items: int, subtask,
                                         run_files: list[str] | None = None) -> str:


    if not _ws_str or not _git_checkpoints_enabled(ctx):
        return ""
    # COMMIT-GATE (2026-10-14, security fix): chunk-start checkpoints run
    # `git add -A` by design - for a run without an explicit/chat workspace
    # that would sweep foreign (owner) changes into a run commit.
    if not getattr(ctx, "workspace_commit_allowed", True):
        logger.info(
            "[GIT-CHECKPOINT] skipped: run has no explicit/chat workspace "
            "(commit-gated) - a checkpoint here would sweep foreign changes")
        return ""
    try:
        from hive_functions.git_tools import exec_git_checkpoint as _gck
        if ctx.duo_config.git_autocommit:
            _label = f"chunk {_di + 1}/{_n_items}: {str(subtask)[:40] if subtask else 'no subtask'}"
        else:
            if getattr(ctx, "_git_session_cp_done", False):
                return ""
            _label = "session start"
        # RUN-SCOPED (2026-10-14): the checkpoint stages only the files THIS
        # run wrote so far - never `add -A` on the owner's dirty repo.
        _out = await _gck(_label, _ws_str,
                          files=list(run_files or []) or None)
        if _out and not ctx.duo_config.git_autocommit \
                and not _out.startswith("ℹ️"):
            ctx._git_session_cp_done = True
        return _out
    except Exception as _ck_err:
        logger.debug("[GIT-CHECKPOINT] skipped: %s", _ck_err)
        return ""


def _ld_setter(src_line: int = 0) -> None:
    import inspect as _insp
    logger.warning("[LD-SET] _loop_detected set at line %d (caller %d)",
                   src_line, _insp.currentframe().f_back.f_lineno)


def loop_detect_file_snapshot(ws_str: str, explore_ctx, pre_explore_msgs, written_files, contracts_raw) -> dict | None:
    try:
        from hive_functions.chunking import (
            build_file_signature as _bfs,
            normalize_resume_candidate_path as _nrcp,
        )
        _cands: set = set()
        for _wf in written_files or []:
            _cands.add(str(_wf))
        try:
            _cands.update(
                (_explore_extract_files(explore_ctx, pre_explore_msgs) or {}).keys()
            )
        except Exception:
            pass
        for _ct in (contracts_raw or []):
            if not isinstance(_ct, dict):
                continue
            for _e in (_ct.get("exports") or []):
                if isinstance(_e, dict):
                    _p = str(_e.get("path") or _e.get("file") or "")
                    if _p:
                        _cands.add(_p)
                elif isinstance(_e, str) and _e.strip():
                    _cands.add(_e.strip())
        _out: dict = {}
        for _cp in _cands:
            _np = _nrcp(_cp, Path(ws_str))
            if not _np:
                continue
            _sig = _bfs(_np)
            if _sig:
                _out[_np] = _sig
        return _out
    except Exception as _ld_err:
        logger.debug("[LOOP-DETECT] File snapshot failed: %s", _ld_err)
        return None


async def warm_workers_after_run(slots: list, coder_mdl: str) -> None:
    try:
        from backend.llama_server_manager import manager as _lsm_aw
        try:
            await _lsm_aw.evict(coder_mdl)
        except Exception:
            pass
        for _wsk in slots:
            try:
                await _lsm_aw.ensure_loaded(
                    str(_wsk.get("model", "")),
                    num_ctx=int(_wsk.get("num_ctx") or _wsk.get("ctx") or 16384),
                    n_parallel=int(_wsk.get("n_parallel", 1) or 1),
                )
            except Exception as _err:
                if isinstance(_err, (
                    GeneratorExit,
                    asyncio.CancelledError,
                    KeyboardInterrupt,
                    SystemExit,
                )):
                    raise
                logger.debug(
                    "[DUO] Suppressed error at after-run worker warmup: %s",
                    _err, exc_info=True
                )
    except Exception:
        pass


async def unpin_duo_models(ctx, pinned: list, ka: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as _uc:
        for _um in pinned:
            try:
                _unpin_ctx = ctx.get_num_ctx(_um)
                _unpin_payload: dict = {"model": _um, "keep_alive": ka, "stream": False}
                # rnj-1:8b default=32768 → ~2GB extra KV-Cache → VRAM-Spike auf 8GB GPU
                if _unpin_ctx:
                    _unpin_payload["options"] = {"num_ctx": _unpin_ctx, "num_predict": 0}
                await _bk_load(_um, keep_alive=ka, num_ctx=_unpin_ctx)
            except Exception as _err:
                if isinstance(_err, (
                    GeneratorExit,
                    asyncio.CancelledError,
                    KeyboardInterrupt,
                    SystemExit,
                )):
                    raise
                logger.debug(
                    "[DUO] Suppressed error at unpin keep-alive: %s",
                    _err, exc_info=True
                )


def calib_est_tokens(real_prompt_tokens: list, est_at_real: list, raw_est: int) -> int:
    """REAL-CALIBRATED (2026-09-20): scale the chars/4 estimate
    with the last measured prompt_tokens ratio — the raw
    estimate ran ~9% under the real prompt on some models."""
    if real_prompt_tokens[0] > 0 and est_at_real[0] > 0:
        return int(real_prompt_tokens[0] * (raw_est / max(1, est_at_real[0])))
    return raw_est


async def keepalive_vision(vis_mdl: str) -> None:
    try:
        from backend.llama_server_manager import manager as _kvm
        await _kvm.touch_if_loaded(vis_mdl)
    except Exception:
        pass


async def emit_chunk_planner_heartbeat(ctx, hb_queue, hb_stop, planner_model: str) -> None:
    _elapsed = 0
    await asyncio.sleep(1.0)
    while not hb_stop.is_set() and not ctx.aborted():
        if hb_stop.is_set() or ctx.aborted():
            break
        _elapsed += 1
        try:
            # put_nowait: never blocks; raises QueueFull if maxsize
            # hit (slow consumer) — silently drop, keep looping.
            hb_queue.put_nowait({
                "type": "planner_thinking",
                "elapsed": _elapsed,
                "model": planner_model,
            })
        except asyncio.QueueFull:
            pass  # consumer can't keep up — drop heartbeat
        except Exception:
            break
        await asyncio.sleep(1.0)


def write_pre_loop_resume(ctx, ws_str: str, explore_ctx, done_tasks: list, written_files: list, halt_reason: str = "hard_stop") -> None:
    try:
        if ctx.chat_id and not _load_resume_block(ctx.chat_id):
            _write_resume_block(
                chat_id=ctx.chat_id,
                workspace=ws_str,
                chunks_total=1,
                chunks_done=done_tasks,
                chunks_remaining=[{"title": ctx.user_input}],
                written_files=written_files,
                last_summary=" | ".join(done_tasks),
                plan_msgs=[],
                explore_ctx=explore_ctx,
                halt_reason=halt_reason,
            )
    except Exception as _rl_err:
        if isinstance(_rl_err, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        logger.warning("[PRE-LOOP-RESUME] Write failed: %s", _rl_err)


def halt_with_resume(ctx, ws_str: str, explore_ctx, done_tasks: list, written_files: list, di: int, loop_items: list, n_items: int, halt_reason: str) -> None:
    _rem_halt = [{"title": str(t)} for t in loop_items[di:]]
    if ctx.chat_id and _rem_halt:
        _write_resume_block(
            chat_id=ctx.chat_id,
            workspace=ws_str,
            chunks_total=n_items,
            chunks_done=done_tasks,
            chunks_remaining=_rem_halt,
            written_files=written_files,
            last_summary=" | ".join(done_tasks),
            plan_msgs=[],
            explore_ctx=explore_ctx,
            halt_reason=halt_reason,
        )


async def plan_emit_fn(plan_event_q, event: dict) -> None:
    await plan_event_q.put(event)


def plan_aborted_fn(ctx) -> bool:
    return ctx.aborted()


async def plan_heartbeat_fn(plan_event_q, planner_hb_queue) -> None:
    # Drain planner heartbeat queue into the event bridge
    while not planner_hb_queue.empty():
        try:
            _hb = planner_hb_queue.get_nowait()
            await plan_event_q.put(_hb)
        except asyncio.QueueFull:
            pass


async def inloop_plan_emit_fn(inloop_event_q, event: dict) -> None:
    try:
        inloop_event_q.put_nowait(event)
    except asyncio.QueueFull:
        pass


async def evict_model_handler(ctx, model: str) -> None:
    if not ctx.duo_config.evict_on_pause:
        return
    try:
        from backend.llama_server_manager import manager as _lsm_pause
        await _lsm_pause.evict(model)
        await ctx.emit({"type": "status", "content": "💤 Model unloaded from VRAM during pause."})
    except Exception as _evict_err:
        logger.warning(f"evict_on_pause failed: {_evict_err}")
