"""Pre-Explore: LLM-Client, Prompts, Usage-Tracking (Teil von hive_functions/pre_explore)."""

from __future__ import annotations

from core.duo_helpers import _apply_thinking_fields
import asyncio
import httpx
import json
import re

from .contracts import logger

async def _get_llm_client() -> httpx.AsyncClient:
    global _llm_client, _llm_client_lock
    if _llm_client_lock is None:
        _llm_client_lock = asyncio.Lock()
    async with _llm_client_lock:
        if _llm_client is None:
            _llm_client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                timeout=httpx.Timeout(connect=5.0, read=300.0, write=5.0, pool=2.0),
            )
        return _llm_client


def _fallback_explore_prompt(label, workspace, paths, tree_ctx=""):
    path_list = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(paths))
    tree_hint = "\nWorkspace tree provided. Use read_file DIRECTLY.\n" if tree_ctx else ""
    return (
        f"Explore partition '{label}' in {workspace}.\n{tree_hint}"
        f"Read ALL files below. Then write CONTRACT in TOML:\n\nFiles:\n{path_list}\n\n"
        "```toml\n[contract]\n"
        f'partition = "{label}"\n'
        'entry_points = ["main.py"]\nfiles_read = ["..."]\nexports = ["..."]\n'
        'imports_needed = ["..."]\ntouched_by_task = true/false\n'
        "complexity_score = 0.0-1.0\n"
        'hint = "key insight"\n\n'
        '[[plan]]\nstep = 1\nfile = "..."\naction = "..."\n```\n'
        "IMPORTANT: Read ALL files before writing CONTRACT.\n"
    )


def _needs_no_think(model_name: str) -> bool:


    lo = model_name.lower()
    return any(x in lo for x in ("qwen3", "bonsai"))


def _is_thinking_model(model_name: str) -> bool:

    lo = model_name.lower()
    return any(x in lo for x in ("qwen3.6", "bonsai"))


def _resolve_worker_thinking(thinking_override: bool | None, model_name: str) -> bool:
    """Worker-Thinking-Entscheidung: expliziter UI-Override gewinnt 1:1,
    sonst Modell-Name-Heuristik (_is_thinking_model)."""
    if thinking_override is not None:
        return bool(thinking_override)
    return _is_thinking_model(model_name)


def _compress_msgs(msgs: list[dict], keep_system: bool = True, keep_tail: int = 6) -> list[dict]:
    """
    When msgs grows large, collapse old file-read results into a single
    'files already read' summary note. Keeps the system prompt + last N messages.
    
    This is the key mechanism preventing ctx overflow on small-ctx workers:
    instead of truncating arbitrarily, we explicitly summarise what was read.
    """
    if len(msgs) <= keep_tail + 1:
        return msgs

    system = [m for m in msgs if m["role"] == "system"] if keep_system else []
    tail   = [m for m in msgs if m["role"] != "system"][-keep_tail:]

    # M4 FIX: Cap tail total chars to prevent overflow from very large file-read messages.
    # Each tail message can contain up to MAX_FILE_CHARS (20000+), so 6 tail msgs = 120k chars.
    # For small-ctx workers (8k ctx → COMPRESS_CHAR_THRESHOLD ~24k), this is 5x the budget.
    # Solution: trim tail messages to fit within a reasonable budget.
    _TAIL_CHAR_BUDGET = 24000  # ~6000 tokens at chars/4
    _tail_chars = sum(len(m.get("content") or "") for m in tail)
    if _tail_chars > _TAIL_CHAR_BUDGET:
        _trimmed_tail: list[dict] = []
        _remaining_budget = _TAIL_CHAR_BUDGET
        for _tm in reversed(tail):
            _tc = str(_tm.get("content") or "")
            if len(_tc) <= _remaining_budget:
                _trimmed_tail.insert(0, _tm)
                _remaining_budget -= len(_tc)
            else:
                _trimmed_tail.insert(0, {
                    **_tm,
                    "content": _tc[:_remaining_budget] + "\n… [tail trimmed to fit context budget]",
                })
                break
        tail = _trimmed_tail

    # Count files already read from the dropped messages
    dropped = [m for m in msgs if m["role"] != "system"][:-keep_tail]
    read_paths: list[str] = []
    for m in dropped:
        content = m.get("content") or ""
        # Match [read_file: path] markers — tolerate quotes, backslashes and extra whitespace
        # M10 FIX: post-strip quotes from path + normalize backslashes
        for match in re.finditer(r"\[read_file:\s*([^\]\r\n]+?)\s*\]", content):
            p = match.group(1).strip().strip("\"'").replace("\\", "/")
            if p and p not in read_paths:
                read_paths.append(p)

    if read_paths:
        summary_note = {
            "role": "user",
            "content": (
                f"[CONTEXT COMPRESSED — {len(dropped)} old messages dropped]\n"
                f"Files already read and processed ({len(read_paths)}): "
                f"{', '.join(read_paths)}\n"
                "Their content has been analysed. Continue with remaining files."
            ),
        }
        return system + [summary_note] + tail

    # No paths extracted (edge case) — still trim, just without the summary note
    return system + tail


def _reset_pre_explore_usage() -> None:
    global _pre_explore_usage
    _pre_explore_usage = {"completion_tokens": 0, "prompt_tokens": 0, "cached_tokens": 0}


def _snapshot_pre_explore_usage() -> dict:
    return dict(_pre_explore_usage)


def _usage_add(_u: dict) -> None:


    if not _u or not _u.get("completion_tokens"):
        return
    try:
        _cached = int((_u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    except Exception:
        _cached = 0
    _pre_explore_usage["completion_tokens"] += int(_u["completion_tokens"])
    _pre_explore_usage["prompt_tokens"] += int(_u.get("prompt_tokens") or 0)
    _pre_explore_usage["cached_tokens"] += _cached


async def _llm(
    model, port, messages, *,
    stream=False, temp=0.7, max_tok=1024,
    top_p=1.0, top_k=20, min_p=0.0,
    penalty=1.0, presence_penalty=1.5,
    msg_cap=0, emit_tok=None,
    tools=None, thinking=False, thinking_budget=0,
    no_think=False,
    read_timeout: float = 300.0,
) -> dict:
    """
    Returns: {"content": str, "tool_calls": list[{"name": str, "args": dict}]}
    emit_tok receives only content tokens (no thinking, no tool JSON).
    """
    ms = list(messages[-msg_cap:] if msg_cap > 0 else messages)

    payload = {
        "model": model, "messages": ms, "stream": stream,
        "temperature": temp, "max_tokens": max_tok, "n_predict": max_tok,
        "repeat_penalty": penalty, "repeat_last_n": 128, "frequency_penalty": 0.1,
        "top_p": top_p, "top_k": top_k, "min_p": min_p,
        "presence_penalty": presence_penalty,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "required"
    _apply_thinking_fields(payload, thinking, thinking_budget)

    for attempt in range(30):
        content = ""
        tool_calls = []
        try:
            _c = await _get_llm_client()
            _req_timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=2.0)
            if stream:
                async with _c.stream(
                    "POST", f"http://127.0.0.1:{port}/v1/chat/completions",
                    json=payload, timeout=_req_timeout,
                ) as r:
                    if r.status_code in (404, 503) and attempt < 29:
                        await asyncio.sleep(min(2.5, 0.4 * (attempt + 1)))
                        continue
                    if r.status_code != 200:
                        logger.warning("LLM stream HTTP %d port=%d", r.status_code, port)
                        return {"content": "", "tool_calls": []}
                    async for ln in r.aiter_lines():
                        ln = ln.strip()
                        if not ln or ln == "data: [DONE]":
                            continue
                        if ln.startswith("data: "):
                            ln = ln[6:]
                        try:
                            _chunk = json.loads(ln)
                        except json.JSONDecodeError:
                            continue
                        _u = _chunk.get("usage") or {}
                        _usage_add(_u)
                        d = (_chunk.get("choices") or [{}])[0].get("delta", {})
                        t = d.get("content", "")
                        if t:
                            content += t
                            if emit_tok:
                                await emit_tok(t)
            else:
                r = await _c.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json=payload, timeout=_req_timeout,
                )
                if r.status_code in (404, 503) and attempt < 29:
                    await asyncio.sleep(min(2.5, 0.4 * (attempt + 1)))
                    continue
                if r.status_code != 200:
                    logger.warning("LLM HTTP %d port=%d: %s", r.status_code, port, r.text[:200])
                    return {"content": "", "tool_calls": []}
                _rd = r.json()
                _msg = _rd["choices"][0]["message"]
                _usage_add(_rd.get("usage") or {})
                content = _msg.get("content") or ""
                for tc in (_msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args_parsed = json.loads(args_raw) if isinstance(args_raw, str) else args_raw or {}
                    except Exception:
                        args_parsed = {}
                    tool_calls.append({"name": fn.get("name", ""), "args": args_parsed})
            return {"content": content, "tool_calls": tool_calls}

        except httpx.ConnectError:
            logger.warning("LLM ConnectError port=%d attempt=%d", port, attempt + 1)
            await asyncio.sleep(min(2.0, 0.3 * (attempt + 1)))
        except httpx.ReadTimeout:
            logger.warning("LLM ReadTimeout port=%d attempt=%d", port, attempt + 1)
            await asyncio.sleep(min(2.0, 0.3 * (attempt + 1)))
        except Exception as e:
            logger.error("LLM error port=%d attempt=%d: %s", port, attempt + 1, e)
            await asyncio.sleep(min(1.5, 0.2 * (attempt + 1)))

    logger.error("LLM FAILED after 30 attempts port=%d model=%s", port, model)
    return {"content": "", "tool_calls": []}
