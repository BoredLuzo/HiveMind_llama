# -*- coding: utf-8 -*-


from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

_LADDER_DEFAULT = ["lfm2.5:2.6b", "qwen3.5:0.8b-ud"]
_ALLOWED_TOOLS = ("read_file", "list_dir", "search_code")

_last_block_ts = 0.0
_active = False        # single-flight


def _cfg() -> dict:
    try:
        from settings import load_settings
        return load_settings() or {}
    except Exception:
        return {}


def _s(key: str, default):
    return _cfg().get(key, default)


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
            return Path(p).stat().st_size / (1024 ** 3)
    except Exception:
        pass
    return None


async def pick_delegate_model() -> tuple[str, int]:

    cfg = _cfg()
    ladder = [m for m in (cfg.get("subagent_lite_model_ladder") or _LADDER_DEFAULT) if m]
    min_ram = float(_s("subagent_lite_min_free_ram_gb", 5.0))
    margin = int(_s("subagent_lite_safety_margin_mib", 256))
    ctx_default = int(_s("subagent_lite_ctx_default", 8192))

    ram = _available_ram_gb()
    biggest = max((_gguf_size_gb(m) or 0.0) for m in ladder) if ladder else 0.0
    if ram is not None and ram < min_ram:
        raise GateBlocked(f"ram {ram:.1f}GB < {min_ram}GB")
    if ram is not None and biggest and ram < biggest + 0.5:
        raise GateBlocked(f"ram {ram:.1f}GB < file size {biggest:.1f}GB+0.5")

    from backend.llama_server_manager import manager
    if any(getattr(s, "_loading", False) for s in manager._slots):
        raise GateBlocked("another load is already running")

    for model in ladder:
        fit = manager.can_fit(model, ctx_default, safety_margin_mib=margin)
        if getattr(fit, "ok", False):
            return model, ctx_default
    raise GateBlocked("vram: kein Leitern-Modell passt")


async def run_research(task: str, workspace_lock: str | None) -> str:

    global _active, _last_block_ts

    if not bool(_s("subagent_lite_enabled", True)):
        return "Subagent disabled — researching inline."
    if _active:
        return "Subagent already busy — researching inline."
    now = time.time()
    if now - _last_block_ts < float(_s("subagent_lite_cooldown_s", 60)):
        return "Subagent in cooldown (recently blocked) — researching inline."

    try:
        model, ctx = await pick_delegate_model()
    except GateBlocked as gb:
        _last_block_ts = time.time()
        return (f"Subagent not available ({gb.reason}) — researching inline.")

    _active = True
    try:
        return await asyncio.wait_for(
            _run_sub_loop(task, workspace_lock, model, ctx),
            timeout=float(_s("subagent_lite_timeout_s", 120)),
        )
    except asyncio.TimeoutError:
        return ("Subagent exceeded the time limit — partial results "
                "discarded. Research inline.")
    except Exception as e:
        return f"Subagent-Fehler ({type(e).__name__}) — recherchiere inline."
    finally:
        _active = False


# ── Sub-Loop ──────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a research delegate. Use the provided read-only tools "
    "(read_file, list_dir, search_code) to investigate the task, then reply "
    "with a COMPACT summary (max ~500 tokens). "
    "No code changes - research only."
)


def _tool_schemas() -> list[dict]:
    """OpenAI-Schemas der Read-Only-Tools aus definitions.py filtern."""
    try:
        from tools.definitions import _INLINE_CODING_TOOLS
        want = set(_ALLOWED_TOOLS)
        return [t for t in _INLINE_CODING_TOOLS
                if t.get("function", {}).get("name") in want]
    except Exception:
        # Minimal-Fallback (OpenAI-Format)
        out = []
        for n in _ALLOWED_TOOLS:
            out.append({"type": "function", "function": {
                "name": n,
                "description": f"read-only tool: {n}",
                "parameters": {"type": "object", "properties": {}},
            }})
        return out


async def _call_model(port: int, model: str, messages: list[dict]) -> dict:
    """Non-stream Chat-Call mit Tools. Returns raw assistant message dict."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0.6,
        "max_tokens": int(_s("subagent_lite_max_tokens", 700)),
        "tools": _tool_schemas(),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=4, read=60,
                                                          write=10, pool=10)) as c:
        r = await c.post(f"http://127.0.0.1:{port}/v1/chat/completions",
                         json=payload)
        r.raise_for_status()
        data = r.json()
    return (data.get("choices") or [{}])[0].get("message") or {}


async def _run_sub_loop(task: str, workspace_lock: str | None,
                        model: str, ctx: int) -> str:
    from backend.llama_server_manager import manager

    max_tools = int(_s("subagent_lite_max_tools", 12))
    port = await manager.ensure_loaded(model, num_ctx=ctx, pin=True)

    from tools import runner as _tr
    _tok_read = _tr._files_read_in_run.set(set())
    _tok_seen = _tr._files_seen_in_run.set(set())
    _tok_written = _tr._files_written_in_run.set(set())
    _tok_inctx = _tr._files_in_context.set(set())

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Aufgabe:\n{task}\n\n"
         "Recherchiere mit den Werkzeugen und liefere die Zusammenfassung."},
    ]
    summary = ""
    from tools.runner import _run_inline_tool
    try:
        for _round in range(max_tools + 1):
            msg = await _call_model(port, model, messages)
            tcs = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()
            if not tcs or _round >= max_tools:
                summary = content or "(Delegat lieferte keinen Inhalt)"
                break
            messages.append(msg)
            for tc in tcs:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {}
                tc_id = tc.get("id") or f"call_{_round}"
                if name not in _ALLOWED_TOOLS:
                    result = f"[blocked] '{name}' ist im Subagent nicht erlaubt (read-only)."
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
        try:
            await manager.evict(model)
        except Exception:
            pass
        await asyncio.sleep(1.5)

    return f"[subagent summary | {model}]\n{summary}"
