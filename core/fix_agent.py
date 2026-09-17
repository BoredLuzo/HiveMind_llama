# -*- coding: utf-8 -*-
"""WEDGE-HANDOFF fix agent (2026-09-17): a fresh-context delegate that repairs
ONE wedged edit and returns control.

Pattern follows core/subagent_lite.py (fresh messages list, own read/write
guard ContextVars, single-flight, budgeted rounds), with the differences that
matter for a REPAIR role:

- Model: the CODER model by default (fresh context, same weights — a model
  switch mid-wedge is exactly the confusion the no-silent-fallback decision
  removed; duo_wedge_handoff_model exists only as an explicit escape hatch).
- Port: reuses the coder's warm port when given (no VRAM churn, no evict).
  The model is NEVER evicted here — the coder keeps running on it.
- Tools: read + write family via the "fix_agent" tool-mode allowlist
  (definitions.py). Deliberately NO run_bash/git/ask_user: minimal-invasive
  repair only. All handler-level guards (noop, shrink, block-sniff,
  READ_REQUIRED) apply unchanged because they live in the handlers/funnel.
- Verdict contract: the final reply must end with
  "VERDICT: FIXED | NO_FIX_NEEDED | FAILED — <reason>".
  NO_FIX_NEEDED is a first-class success outcome: sometimes the file was
  already correct and the coder's mental model was wrong — an empty diff is
  then the RIGHT answer, not a failure.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

_ALLOWED_TOOLS = ("read_file", "get_signatures", "list_dir", "find_files",
                  "search_code", "edit_file", "write_file",
                  "write_file_append", "undo_last", "get_datetime")

_active = False  # single-flight: no nested/concurrent handoffs


def _s(key: str, default):
    try:
        from settings import load_settings
        return (load_settings() or {}).get(key, default)
    except Exception:
        return default


_SYSTEM_PROMPT = (
    "You are an edit-repair delegate. A coding agent failed the same edit on "
    "one file repeatedly. You get a compact handover: the file, the intent, "
    "and the last failed attempts. Repair the edit with a MINIMAL change: "
    "read_file the file fresh, copy old_text VERBATIM from your own read, "
    "never reformat unrelated code, preserve line endings. If you conclude "
    "the file already satisfies the intent, change nothing.\n"
    "End your final reply with EXACTLY one verdict line:\n"
    "VERDICT: FIXED — <what you changed>\n"
    "VERDICT: NO_FIX_NEEDED — <why the file already satisfies the intent>\n"
    "VERDICT: FAILED — <what blocks you>"
)

_RE_VERDICT = re.compile(
    r"^\s*verdict:\s*(FIXED|NO[ \-_]FIX[ \-_]NEEDED|FAILED)\b[\s\u2014\u2013\-:]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_verdict(text: str) -> tuple[str, str]:
    """Extract the LAST verdict line. Returns (status, reason) with status in
    fixed | no_fix_needed | failed | unknown. Tolerates hyphen/space variants
    (models mangle NO_FIX_NEEDED) and missing separators."""
    status, reason = "unknown", ""
    for m in _RE_VERDICT.finditer(str(text or "")):
        status = m.group(1).lower().replace("-", "_").replace(" ", "_")
        reason = (m.group(2) or "").strip()
    return status, reason


def result_from_verdict(text: str, changed: bool) -> dict:
    """Map (final reply, file-actually-changed) to the handoff result.
    Integrity check: a claimed FIXED with an unchanged file is a failure,
    not a success."""
    status, reason = parse_verdict(text)
    reason = reason[:400]
    if status == "fixed":
        if changed:
            return {"status": "resolved", "summary": reason or "edit applied", "changed": True}
        return {"status": "failed", "summary": "claimed FIXED but the file is unchanged", "changed": False}
    if status == "no_fix_needed":
        return {"status": "no_fix_needed", "summary": reason or "file already correct", "changed": changed}
    if status == "unknown" and changed:
        return {"status": "resolved", "summary": reason or "file changed (no verdict line)", "changed": True}
    if status == "unknown":
        return {"status": "failed", "summary": reason or "no verdict line returned", "changed": False}
    return {"status": "failed", "summary": reason or "FAILED verdict", "changed": changed}


def _tool_schemas() -> list[dict]:
    try:
        from tools.definitions import _INLINE_CODING_TOOLS
        want = set(_ALLOWED_TOOLS)
        return [t for t in _INLINE_CODING_TOOLS
                if t.get("function", {}).get("name") in want]
    except Exception:
        return [{"type": "function", "function": {"name": n, "description": f"fix-agent tool: {n}",
                                                  "parameters": {"type": "object", "properties": {}}}}
                for n in _ALLOWED_TOOLS]


async def _post_chat(port: int, model: str, messages: list[dict]) -> dict:
    """Non-stream chat call with tools. Returns the raw assistant message."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0.4,  # repair task: low temperature, deterministic edits
        "max_tokens": int(_s("duo_wedge_handoff_max_tokens", 1200)),
        "tools": _tool_schemas(),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=4, read=90,
                                                       write=10, pool=10)) as c:
        r = await c.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
    return (data.get("choices") or [{}])[0].get("message") or {}


async def _resolve_port(model: str, num_ctx: int, port: int | None) -> int:
    """Prefer the coder's warm port; fall back to ensure_loaded only when it
    is unreachable. NEVER evicts — the coder keeps these weights loaded."""
    from backend.llama_server_manager import manager
    if port:
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"http://127.0.0.1:{port}/health")
                if r.status_code < 500:
                    return int(port)
        except Exception:
            pass
    return await manager.ensure_loaded(model, num_ctx=num_ctx, pin=True)


def _stat_sig(p: Path):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


async def run_fix_agent(*, path: str, handover: str, model: str,
                        workspace_lock: str, port: int | None = None,
                        num_ctx: int = 8192, max_rounds: int = 10,
                        timeout_s: float = 300.0) -> dict:
    """Run the repair delegate. Returns
    {"status": "resolved"|"no_fix_needed"|"failed", "summary": str, "changed": bool}."""
    global _active
    if _active:
        return {"status": "failed", "summary": "fix agent already running (single-flight)",
                "changed": False}
    _active = True
    try:
        return await asyncio.wait_for(
            _run_fix_loop(path=path, handover=handover, model=model,
                          workspace_lock=workspace_lock, port=port,
                          num_ctx=num_ctx, max_rounds=max_rounds),
            timeout=float(timeout_s))
    except asyncio.TimeoutError:
        return {"status": "failed",
                "summary": f"fix agent timeout after {float(timeout_s):.0f}s",
                "changed": False}
    except Exception as e:
        return {"status": "failed", "summary": f"fix agent error ({type(e).__name__}: {str(e)[:150]})",
                "changed": False}
    finally:
        _active = False


async def _run_fix_loop(*, path: str, handover: str, model: str,
                        workspace_lock: str, port: int | None,
                        num_ctx: int, max_rounds: int) -> dict:
    from tools import runner as _tr
    from tools.runner import _run_inline_tool

    target = Path(path)
    if not target.is_absolute():
        target = Path(workspace_lock or ".") / target
    before = _stat_sig(target)

    # Fresh guard scope (same hygiene as subagent_lite): the fix agent gets
    # its own read/seen/written sets so the read-before-write guard and the
    # stale-signature gate track ITS reads, not the coder's.
    _tok_read = _tr._files_read_in_run.set(set())
    _tok_seen = _tr._files_seen_in_run.set(set())
    _tok_written = _tr._files_written_in_run.set(set())
    _tok_inctx = _tr._files_in_context.set(set())

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": handover},
    ]
    try:
        final_text = ""
        for rnd in range(max_rounds + 1):
            live_port = await _resolve_port(model, num_ctx, port)
            msg = await _post_chat(live_port, model, messages)
            tcs = msg.get("tool_calls") or []
            final_text = (msg.get("content") or "").strip()
            if not tcs or rnd >= max_rounds:
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
                if name not in _ALLOWED_TOOLS:
                    result = f"[blocked] '{name}' is not allowed in the fix agent (minimal repair only)."
                else:
                    result = await _run_inline_tool(
                        name, dict(args), workspace_lock=workspace_lock,
                        tool_mode="fix_agent", include_websearch=False)
                    result = str(result)[:8000]
                messages.append({"role": "tool", "tool_call_id": tc.get("id") or f"fix_{rnd}",
                                 "content": result})
        changed = before is not None and _stat_sig(target) != before
        return result_from_verdict(final_text, changed)
    finally:
        _tr._files_read_in_run.reset(_tok_read)
        _tr._files_seen_in_run.reset(_tok_seen)
        _tr._files_written_in_run.reset(_tok_written)
        _tr._files_in_context.reset(_tok_inctx)
