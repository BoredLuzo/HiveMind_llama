# -*- coding: utf-8 -*-


from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger("hivemind.subagent_lite")

_LADDER_DEFAULT = ["lfm3:1.7b-qad", "lfm2.5:2.6b", "qwen3.5:0.8b-ud"]
_ALLOWED_TOOLS = ("read_file", "list_dir", "search_code")

_last_block_ts = 0.0
_active = False        # single-flight
_LAST_SUB_FINISH_REASON: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_sub_last_finish_reason", default=""
)
_SUB_CFG_CV: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_sub_cfg_cv", default=None
)


def _cfg() -> dict:
    cached = _SUB_CFG_CV.get()
    if cached is not None:
        return cached
    data: dict = {}
    try:
        from settings import load_settings
        data = load_settings() or {}
    except Exception:
        data = {}
    try:
        import core.state as _st
        if isinstance(_st.settings, dict) and _st.settings:
            data.update(_st.settings)
    except (ImportError, AttributeError):
        return data
    return data


def _s(key: str, default, cfg: dict | None = None):
    return (cfg if cfg is not None else _cfg()).get(key, default)


class GateBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _available_ram_gb() -> float | None:
    try:
        from backend.llama_server_manager import _available_ram_gb as f
        return float(f())
    except Exception:
        return None


def _gguf_size_gb(model: str) -> float | None:
    try:
        from backend.llama_server_manager import resolve_model_path
        p = resolve_model_path(model)
        if p and Path(p).exists():
            from backend.gguf_meta import _total_gguf_size_bytes
            _gp = Path(p)
            return _total_gguf_size_bytes(_gp, _gp.stat().st_size) / (1024 ** 3)
    except Exception:
        pass
    return None


def _same_model(a: str | None, b: str | None) -> bool:
    sa = str(a or "").strip().lower()
    sb = str(b or "").strip().lower()
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    try:
        from backend.llama_manager_utils import _nm
        return _nm(sa) == _nm(sb)
    except ImportError:
        return False


def _slot_can_open_for_subagent(slot) -> bool:
    """True if *slot* is free or idle-evictable without disturbing pinned/active slots."""
    if getattr(slot, "_loading", False):
        return False
    if not getattr(slot, "is_running", False):
        return True
    if getattr(slot, "pinned", False):
        return False
    in_flight_fn = getattr(slot, "in_flight_active", None)
    if callable(in_flight_fn):
        if in_flight_fn():
            return False
    elif int(getattr(slot, "in_flight", 0) or 0) > 0:
        return False
    return True


def _resolve_subagent_ladder(cfg: dict) -> list[str]:
    """Return explicit subagent_lite_model_ladder, else installed small tool-capable models."""
    explicit = [m for m in (cfg.get("subagent_lite_model_ladder") or []) if m]
    if explicit:
        return explicit
    try:
        from routing.model_automap import pick_installed_small_tool_models
        discovered = pick_installed_small_tool_models()
        if discovered:
            return discovered
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as _e:
        logger.debug("subagent_lite dynamic ladder resolution skipped: %s", _e)
    if "subagent_lite_model_ladder" in cfg and cfg.get("subagent_lite_model_ladder") == []:
        return []
    return list(_LADDER_DEFAULT)


async def pick_delegate_model(cfg: dict | None = None) -> tuple[str, int]:

    cfg = cfg if cfg is not None else _cfg()
    ladder = _resolve_subagent_ladder(cfg)
    min_ram = float(_s("subagent_lite_min_free_ram_gb", 5.0, cfg))
    margin = int(_s("subagent_lite_safety_margin_mib", 256, cfg))
    ctx_default = int(_s("subagent_lite_ctx_default", 8192, cfg))

    ram = _available_ram_gb()
    if ram is not None and ram < min_ram:
        raise GateBlocked(f"ram {ram:.1f}GB < {min_ram}GB")

    from backend.llama_server_manager import manager
    slots = list(getattr(manager, "_slots", []) or [])
    if any(getattr(s, "_loading", False) for s in slots):
        raise GateBlocked("another load is already running")

    has_free_slot = any(
        not getattr(s, "is_running", False) and not getattr(s, "_loading", False)
        for s in slots
    )
    if slots and not has_free_slot and not any(_slot_can_open_for_subagent(s) for s in slots):
        raise GateBlocked("no free slot available for subagent")

    loaded_models = [
        str(getattr(s, "model", "") or "")
        for s in slots
        if getattr(s, "model", None) and (getattr(s, "is_running", False) or getattr(s, "_loading", False))
    ]

    last_reason = "vram: no ladder model fits"
    for model in ladder:
        # Never hijack/evict a model already running in another active slot (e.g. coder):
        # pick a smaller separate ladder model so subagent gets its own dedicated slot.
        if any(_same_model(model, lm) for lm in loaded_models):
            last_reason = f"slot busy: {model} already loaded"
            continue
        file_gb = _gguf_size_gb(model)
        if ram is not None and file_gb and ram < file_gb + 0.5:
            last_reason = f"ram {ram:.1f}GB < file size {file_gb:.1f}GB+0.5"
            continue
        fit = manager.can_fit(model, ctx_default, safety_margin_mib=margin)
        if getattr(fit, "ok", False):
            return model, ctx_default
        last_reason = "vram: no ladder model fits"
    raise GateBlocked(last_reason)


async def run_research(task: str, workspace_lock: str | None) -> str:

    global _active, _last_block_ts

    cfg = _cfg()
    if not bool(_s("subagent_lite_enabled", False, cfg)):
        return "Subagent disabled — researching inline."
    if _active:
        return "Subagent already busy — researching inline."
    _active = True  # H-audit: claim BEFORE the first await (TOCTOU)
    # LEAK-FIX (audit 2026-11-03): the cooldown and GateBlocked early returns
    # left _active True forever (the finally only covered the later try), so
    # the first blocked attempt disabled the subagent until process restart.
    _cfg_tok = _SUB_CFG_CV.set(cfg)
    try:
        now = time.time()
        if now - _last_block_ts < float(_s("subagent_lite_cooldown_s", 60, cfg)):
            return "Subagent in cooldown (recently blocked) — researching inline."

        try:
            model, ctx = await pick_delegate_model()
        except GateBlocked as gb:
            _last_block_ts = time.time()
            return (f"Subagent not available ({gb.reason}) — researching inline.")

        try:
            return await asyncio.wait_for(
                _run_sub_loop(task, workspace_lock, model, ctx),
                timeout=float(_s("subagent_lite_timeout_s", 120, cfg)),
            )
        except asyncio.TimeoutError:
            return ("Subagent exceeded the time limit — partial results "
                    "discarded. Research inline.")
        except Exception as e:
            return f"Subagent error ({type(e).__name__}) — research inline."
    finally:
        _SUB_CFG_CV.reset(_cfg_tok)
        _active = False


# ── Sub-Loop ──────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a research delegate. Use the provided read-only tools "
    "(read_file, list_dir, search_code) to investigate the task, then reply "
    "with a COMPACT summary (max ~500 tokens). "
    "No code changes - research only."
)


def build_tool_schemas(
    allowed_tools: tuple[str, ...] | list[str],
    fallback_prefix: str = "read-only tool",
) -> list[dict]:
    """Filter OpenAI tool schemas from definitions.py with active runtime budgets."""
    try:
        from tools.definitions import _INLINE_CODING_TOOLS, _apply_budget_texts
        try:
            from core.budgets import active as _b_active
            _src = _apply_budget_texts(list(_INLINE_CODING_TOOLS), _b_active())
        except (ImportError, AttributeError, TypeError):
            _src = list(_INLINE_CODING_TOOLS)
        want = set(allowed_tools)
        return [t for t in _src
                if t.get("function", {}).get("name") in want]
    except Exception:
        return [
            {"type": "function", "function": {
                "name": n,
                "description": f"{fallback_prefix}: {n}",
                "parameters": {"type": "object", "properties": {}},
            }}
            for n in allowed_tools
        ]


def _tool_schemas() -> list[dict]:
    """Filter the read-only tool OpenAI schemas from definitions.py."""
    return build_tool_schemas(_ALLOWED_TOOLS, "read-only tool")


async def _call_model_with_meta(
    port: int, model: str, messages: list[dict], cfg: dict | None = None
) -> tuple[dict, str]:
    """Non-stream Chat-Call mit Tools. Returns (assistant message dict, finish_reason)."""
    from core.llm_transport import post_chat_json

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0.6,
        "max_tokens": int(_s("subagent_lite_max_tokens", 700, cfg)),
        "tools": _tool_schemas(),
    }
    res = await post_chat_json(
        port,
        payload,
        timeout=httpx.Timeout(connect=4, read=60, write=10, pool=10),
        max_attempts=2,
        log_label="subagent_lite",
    )
    return res.message, str(getattr(res, "finish_reason", "") or "")


async def _call_model(
    port: int, model: str, messages: list[dict], cfg: dict | None = None
) -> dict:
    """Non-stream Chat-Call mit Tools. Returns raw assistant message dict."""
    if cfg is not None:
        msg, finish_reason = await _call_model_with_meta(port, model, messages, cfg=cfg)
    else:
        msg, finish_reason = await _call_model_with_meta(port, model, messages)
    _LAST_SUB_FINISH_REASON.set(finish_reason)
    return msg


async def _run_sub_loop(task: str, workspace_lock: str | None,
                        model: str, ctx: int, cfg: dict | None = None) -> str:
    from backend.llama_server_manager import manager
    from core.toolcall_sanitize import sanitize_invalid_tool_call_history

    cfg = cfg if cfg is not None else _cfg()
    max_tools = int(_s("subagent_lite_max_tools", 12, cfg))
    was_loaded_before = any(
        _same_model(getattr(s, "model", ""), model) and getattr(s, "is_running", False)
        for s in (getattr(manager, "_slots", []) or [])
    )

    from tools import runner as _tr
    _tok_read = _tr._files_read_in_run.set(set())
    _tok_seen = _tr._files_seen_in_run.set(set())
    _tok_written = _tr._files_written_in_run.set(set())
    _tok_inctx = _tr._files_in_context.set(set())

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Task:\n{task}\n\n"
         "Research with the tools and provide the summary."},
    ]
    summary = ""
    from tools.runner import _run_inline_tool
    try:
        port = await manager.ensure_loaded(model, num_ctx=ctx, pin=True)
        for _round in range(max_tools + 1):
            sanitize_invalid_tool_call_history(messages)
            _LAST_SUB_FINISH_REASON.set("")
            msg = await _call_model(port, model, messages)
            finish_reason = _LAST_SUB_FINISH_REASON.get("")
            tcs = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()
            _fr = finish_reason.strip().lower()
            if _fr == "length":
                if tcs:
                    if _round >= max_tools:
                        summary = content or "(delegate truncated before summary)"
                        break
                    messages.append({
                        "role": "user",
                        "content": (
                            "[TRUNCATED] Your previous tool call was cut off by "
                            "max_tokens and was not executed. Issue a shorter "
                            "read-only tool call or return your summary now."
                        ),
                    })
                    continue
                summary = (
                    f"{content}\n[truncated: max_tokens reached]"
                    if content
                    else "(delegate returned no content — truncated)"
                )
                break
            if not tcs or _round >= max_tools:
                summary = content or "(delegate returned no content)"
                break
            messages.append(msg)
            for tc in tcs:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                tc_id = tc.get("id") or f"call_{_round}_{len(messages)}"  # H-audit: unique per call
                args_invalid = False
                args: dict = {}
                if isinstance(raw_args, dict):
                    args = raw_args
                elif isinstance(raw_args, str):
                    stripped = raw_args.strip()
                    if not stripped:
                        if isinstance(fn, dict):
                            fn["arguments"] = "{}"
                        args = {}
                    else:
                        try:
                            parsed = json.loads(stripped)
                            if isinstance(parsed, dict):
                                args = parsed
                            else:
                                args_invalid = True
                        except (ValueError, TypeError):
                            args_invalid = True
                elif raw_args is None:
                    if isinstance(fn, dict):
                        fn["arguments"] = "{}"
                    args = {}
                else:
                    args_invalid = True

                if args_invalid:
                    result = "[TOOL_ERROR:INVALID_JSON] Tool arguments must be a valid JSON object."
                elif name not in _ALLOWED_TOOLS:
                    result = f"[blocked] '{name}' is not allowed in the subagent (read-only)."
                else:
                    result = await _run_inline_tool(
                        name, dict(args), workspace_lock=workspace_lock)
                    result = str(result)[:8000]
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                 "content": result})
    finally:
        _tr._files_read_in_run.reset(_tok_read)
        _tr._files_seen_in_run.reset(_tok_seen)
        _tr._files_written_in_run.reset(_tok_written)
        _tr._files_in_context.reset(_tok_inctx)
        if not was_loaded_before:
            _evict_task = asyncio.ensure_future(manager.evict(model))
            _evict_task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
            try:
                await asyncio.shield(_evict_task)
                await asyncio.sleep(1.5)
            except Exception:
                pass

    return f"[subagent summary | {model}]\n{summary}"
