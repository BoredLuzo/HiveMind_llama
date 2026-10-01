# -*- coding: utf-8 -*-
"""Helper-Funktionen und Konstanten aus duo_runner.py extrahiert."""
from __future__ import annotations
import asyncio
import json as _json
import logging
import re
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_VRAM_BUDGET_GB = 7.5

_READ_ONLY_KEYWORDS = (
    "read-only", "readonly", "no write", "do not write", "don't write",
    "do not modify", "don't modify",
)

# A read-only phrase alone is NOT enough: the same wording is frequently used as
# a *content constraint* inside a real implementation request (observed live:
# "## CRITICAL – MAZE DATA (GROUND TRUTH – DO NOT MODIFY)" inside a
# "Create a fully playable Pac-Man game …" task). Those tasks disabled the tool
# round, and a tool-finetuned model (hermes v12) then free-styled textual tool
# calls ([TOOL_CALL] …, edit_file<argkey>…) that nothing ever executes.
# Implementation-intent verbs therefore override the read-only classification.
_IMPL_OVERRIDE_RE = re.compile(
    r"\b(?:create|build|write|implement(?:ed|ation)?|fix(?:es|ing)?|"
    r"generate|develop|refactor|produce|patch)\b",
    re.IGNORECASE,
)

# Verbs negated directly before/after ("don't change anything",
# "fix nothing") must not count as implementation intent.
_NEGATOR_RE = re.compile(
    r"(?:never|not\b|no\b|don'?t|dont)",
    re.IGNORECASE,
)


def _impl_negated(low: str, s: int, e: int) -> bool:
    _before = low[max(0, s - 28):s]
    _after = low[e:e + 28]
    if _NEGATOR_RE.search(_before):
        return True
    return bool(_NEGATOR_RE.search(_after))


def is_read_only_request(text: str) -> bool:
    """True only when a read-only phrase is present AND there is no
    (non-negated) implementation intent in the request."""
    if not text:
        return False
    _low = text.lower()
    if not any(_k in _low for _k in _READ_ONLY_KEYWORDS):
        return False
    _scan = _low
    for _k in _READ_ONLY_KEYWORDS:
        _scan = _scan.replace(_k, " ")
    _pos = 0
    while True:
        _m = _IMPL_OVERRIDE_RE.search(_scan, _pos)
        if not _m:
            return True
        if _impl_negated(_scan, _m.start(), _m.end()):
            _pos = _m.end()
            continue
        return False

RE_THINK_CLEANUP = re.compile(r"<think[^>]*>[\s\S]*?</think(?:ing)?>", re.DOTALL)

_RE_THINK_TOOL = re.compile(
    r'<think>.*?"name"\s*:\s*"([a-z_]+)".*?</think>',
    re.DOTALL | re.IGNORECASE
)


def _preprocess_think_blocks(text: str) -> str:
    # Malformed JSON is silently discarded — would corrupt the tool-call stream otherwise.
    _m = _RE_THINK_TOOL.search(text)
    if _m:
        _raw_json = _m.group(0)
        # Extract just the JSON part from between <think> and </think>
        _inner = re.search(r'<think>(.*?)</think>', _raw_json, re.DOTALL)
        if _inner:
            try:
                _parsed = _json.loads(_inner.group(1).strip())
                if isinstance(_parsed, dict) and "name" in _parsed and isinstance(_parsed.get("name"), str):
                    # Valid tool-call JSON — inject it into content stream
                    inner = re.sub(r'<think>(.*?)</think>', r'\1', text, flags=re.DOTALL).strip()
                    rest = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
                    return (inner + "\n" + rest).strip()
            except (_json.JSONDecodeError, ValueError) as _je:
                logging.getLogger(__name__).warning(
                    "[THINK-BLOCK] Malformed JSON in <think> block discarded: %s — %s",
                    _inner.group(1).strip()[:80], _je
                )
        # Fallback: no valid JSON found, strip think blocks normally
        return RE_THINK_CLEANUP.sub("", text)
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

# ── Regexes ────────────────────────────────────────────────────────────

_RE_WIN_PATH = re.compile(r'[A-Za-z]:\\[^\s\n\'"<>|?*]+')
_RE_UNIX_PATH = re.compile(r'/(?:home|usr|var|opt|tmp|mnt)/[^\s\n\'"<>|?*]+')
_RE_FILE_EXT = re.compile(r'[A-Za-z]:\\[^\s\n\'"<>|?*]+|/[^\s\n\'"<>|?*]+\.\w{1,8}')
_RE_CRITIC_APPROVED = re.compile(r"approved=(true|false)", re.IGNORECASE)
_RE_CRITIC_VERDICT = re.compile(r"verdict=(\S+)")
_RE_CRITIC_ISSUES = re.compile(r"issues=\[([^\]]*)\]")

# ── Thinking Budget ────────────────────────────────────────────────────

_model_cap_overrides: dict = {}

def update_model_capability_overrides(overrides: dict | None = None):
    _model_cap_overrides.clear()
    _model_cap_overrides.update(overrides or {})


def _inject_no_think_directive(msgs: list[dict]) -> list[dict]:
    return [
        {**m, "content": m["content"] if "<|think_off|>" in m["content"]
         else "<|think_off|>" + m["content"].replace("<|think_on|>", "")}
        if m.get("role") == "system" else m
        for m in msgs
    ]


def parse_sse_delta(
    delta: dict,
    *,
    tool_calls_acc: dict[int, dict] | None = None,
    thinking_keys: tuple[str, ...] = (),
) -> tuple[str, str]:
    content = delta.get("content") or ""
    thinking = ""
    for key in thinking_keys:
        thinking = delta.get(key) or ""
        if thinking:
            break

    if tool_calls_acc is not None:
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            for tc_delta in tool_calls:
                tc_idx = tc_delta.get("index", 0)
                if tc_idx not in tool_calls_acc:
                    tool_calls_acc[tc_idx] = {
                        "id": tc_delta.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                acc = tool_calls_acc[tc_idx]
                if tc_delta.get("id"):
                    acc["id"] = tc_delta["id"]
                tc_fn = tc_delta.get("function", {})
                if tc_fn.get("name"):
                    acc["function"]["name"] = tc_fn["name"]
                if tc_fn.get("arguments"):
                    acc["function"]["arguments"] += tc_fn["arguments"]

    return content, thinking


def _build_dropped_tool_retry(names: list[str]) -> str:


    _names = [n for n in (names or []) if n]
    _label = ", ".join(_names[:3]) or "tool call"
    if any(n == "write_file" for n in _names):
        return (
            "Your tool-call arguments were malformed or truncated and were DROPPED "
            f"('{_label}'). This usually means the JSON output hit the token limit "
            "mid-call. Write large files in STAGES: call write_file with ONLY the "
            "first part (within your OUTPUT-BUDGET), then write_file_append for "
            "each following chunk. NEVER put a whole large file in a single write_file call."
        )
    if any(n == "edit_file" for n in _names):
        return (
            "Your tool-call arguments were malformed or truncated and were DROPPED "
            f"('{_label}'). Keep SEARCH/REPLACE blocks SMALL (max ~30 lines) and "
            "COMPLETE — a truncated JSON payload is dropped. Use multiple blocks per "
            "call if needed, but never an oversized one."
        )
    return (
        "Your tool-call arguments were malformed or truncated and were DROPPED "
        f"('{_label}'). Re-issue the tool call with VALID, COMPLETE JSON arguments "
        "— keep them small enough to finish within the output token limit."
    )


def _get_thinking_profile(model_name: str, settings_dict: dict | None = None) -> dict:
    _s = settings_dict or {}
    _model_profiles = _s.get("_model_profiles", {})
    _profile = _model_profiles.get(model_name, {})
    if not _profile:
        _base = model_name.split(":")[0]
        for k, v in _model_profiles.items():
            if k.startswith(_base):
                _profile = v
                break
    return _profile or {"thinking_enabled": False, "thinking_budget": 0, "max_tokens": 4096}


def _calculate_thinking_tokens(
    model_name: str,
    settings_dict: dict | None = None,
    input_tokens: int = 0,
    available_ctx: int = 8192,
    agent_name: str = "",
) -> int:
    _s = settings_dict or {}
    base_budget = None
    if agent_name:
        agent_cfg = _s.get("agents", {}).get(agent_name, {})
        if "thinking_budget" in agent_cfg:
            base_budget = agent_cfg["thinking_budget"]
    if base_budget is None:
        profile = _get_thinking_profile(model_name, _s)
        if not profile.get("thinking_enabled"):
            return 0
        base_budget = profile.get("thinking_budget", 2500)
    if input_tokens > 0 and input_tokens < base_budget * 0.2:
        adaptive_budget = max(200, int(base_budget * 0.3))
    else:
        adaptive_budget = base_budget
    available = max(0, available_ctx - input_tokens)
    _max_thinking_share = int(available * 0.40)
    adaptive_budget = min(adaptive_budget, _max_thinking_share)
    return max(0, adaptive_budget)


def _apply_thinking_fields(payload: dict, thinking_on: bool, thinking_budget: int) -> dict:


    if thinking_on:
        payload["thinking"] = True
        payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = True
        if thinking_budget > 0:
            payload["thinking_budget"] = max(64, thinking_budget)
    else:
        payload["thinking"] = False
        payload["thinking_budget"] = 0
        payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
    return payload


def _compress_fail_streak_update(streak: int, success: bool, stop_limit: int = 3) -> tuple[int, bool]:
    """Consecutive-compression-failure tracking for the tool loop.

    success=True resets the streak; success=False increments it.
    Returns (new_streak, stop) — stop=True when the streak reaches stop_limit,
    i.e. the 400->compress->fail->restore cycle must abort the run instead of
    looping until the (24h) run deadline.
    """
    _new_streak = 0 if success else streak + 1
    return _new_streak, _new_streak >= stop_limit


_EXPLORE_SIZE_TOLERANCE: dict[str, int] = {
    "qwen3.5":     1,   # 2B/4B/9B-Basis — live belegt
    "granite-4.1": 1,
    "qwen3.6":     1,
    "ling-3.0-tiny": 1,  # 1.3B-active MoE (7.9B total) — small family, same read prewarm as qwen3.5
}


def _explore_size_tolerance(model_name: str) -> int:
    return _EXPLORE_SIZE_TOLERANCE.get(str(model_name or "").split(":")[0], 0)


def _collect_new_explore_paths(tool_calls: list, seen: set[str]) -> list[str]:


    new_paths: list[str] = []
    for _tc in tool_calls or []:
        _fn = ((_tc.get("function") or {}).get("name") or "").strip()
        if _fn not in ("read_file", "list_dir", "find_files"):
            continue
        _args = (_tc.get("function") or {}).get("arguments") or {}
        if isinstance(_args, str):
            try:
                import json as _json
                _args = _json.loads(_args)
            except Exception:
                _args = {}
        _p = str((_args or {}).get("path") or "").strip()
        if not _p:
            continue
        _norm = _p.replace("\\", "/").lower()
        if _fn == "read_file":
            _norm = f"{_norm}#rng:{_read_range_key(_args)}"
        if _norm not in seen:
            seen.add(_norm)
            new_paths.append(_p)
    return new_paths


def _read_range_key(args) -> str:


    _sl = (args or {}).get("start_line")
    _el = (args or {}).get("end_line")
    if _sl is None and _el is None:
        return "full"
    return f"{_sl}-{_el}"


def read_loop_key(path: str, start_line, end_line, result: str) -> str | None:


    txt = str(result or "")
    if txt.startswith("[SKIP:"):
        return None
    try:
        from tools.errors import parse_tool_error
        _err = parse_tool_error(txt)
    except Exception:
        _err = None
    if _err:
        return f"{path}#err:{_err.get('code') or 'UNKNOWN'}"
    if start_line is not None or end_line is not None:
        return f"{path}#rng:{start_line}-{end_line}"
    return f"{path}#rng:full"


def _should_escalate_pass_files(
    important_task: bool,
    until_finished: bool,
    n_explore_files: int,
    total_explore_chars: int,
    effective_mode: str,
    max_files: int = 25,
    max_total_chars: int = 200_000,
) -> bool:


    if effective_mode != "touched":
        return False
    if not (important_task or until_finished):
        return False
    if not (1 <= n_explore_files <= max_files):
        return False
    if total_explore_chars > max_total_chars:
        return False
    return True


def _sum_edit_lines(file_changes: dict) -> int:


    total = 0
    for _vd in (file_changes or {}).values():
        _op = str(_vd.get("op", "") or "")
        if _op == "write":
            total += int(_vd.get("lines", 0) or 0)
        elif _op in ("created", "rewrote"):
            total += int(_vd.get("lines_added", 0) or 0) + int(_vd.get("lines_removed", 0) or 0)
        elif _op == "edited":
            total += int(_vd.get("lines_added", 0) or 0)
    return total


# ── Budget / Timeout ───────────────────────────────────────────────────

def _resolve_tool_budget(max_tool_rounds_cfg: int, until_finished: bool,
                          settings: dict | None = None, profile: str = "balanced") -> int:
    _s = settings or {}
    if until_finished:
        return max(12, min(999999, int(_s.get("duo_until_finished_cap", 999999))))
    _factor = {"fast": 0.55, "balanced": 0.70, "critical": 1.0}.get(profile, 0.70)
    _scaled = max(4, int(round(max_tool_rounds_cfg * _factor)))
    _hard_cap = int(_s.get("duo_max_tool_rounds_runtime_cap", 300))
    return max(4, min(_scaled, _hard_cap))


def _resolve_tool_read_timeout_seconds(settings: dict | None = None,
                                        profile: str = "balanced",
                                        until_finished: bool = False) -> float:
    _s = settings or {}
    try:
        _base = float(_s.get("duo_read_timeout", 300))
    except Exception:
        _base = 300.0
    _scale = {"fast": 0.55, "balanced": 1.0, "critical": 1.35}.get(
        str(profile or "balanced").strip().lower(), 1.0)
    if until_finished:
        _scale = max(_scale, 1.2)
    return max(45.0, min(1800.0, _base * _scale))


# ── Prompt builder ─────────────────────────────────────────────────────

def _build_duo_coder_sys(ctx, has_plan: bool, has_subtasks: bool, has_explore_ctx: bool) -> str:
    from hive_functions.prompts import PROMPTS
    parts = [
        PROMPTS.get("duo_coder_execution" if has_plan else "duo_coder_autonomous", "")
    ]
    parts.append(
        ctx.get_effective_prompt_with_override("duo_coder", ctx.active_preset, ctx.use_learned)
        or PROMPTS["duo_coder_base"]
    )
    try:
        _ws_available = bool(
            (ctx.settings or {}).get("duo_websearch_enabled", False)
            and ctx.websearch_available
        )
        if _ws_available:
            parts.append(
                "WEB SEARCH IS AVAILABLE: use web_search / web_fetch for anything "
                "you are not 100% certain about — unknown APIs, library signatures, "
                "framework behavior, config options, error messages. PREFER LOOKUP "
                "OVER GUESSING: a wrong API assumption breaks the build, a search "
                "takes 2 seconds. When in doubt: search."
            )
    except Exception:
        pass
    if has_subtasks:
        parts.append(PROMPTS.get("duo_coder_chunking", ""))
    else:
        parts.append(PROMPTS.get("duo_coder_no_chunk", ""))
    if has_explore_ctx:
        parts.append(PROMPTS.get("duo_coder_explored", ""))
    else:
        parts.append(PROMPTS.get("duo_coder_unexplored", ""))
    if ctx.duo_config.test_feedback_chunk or ctx.duo_config.test_feedback_final:
        parts.append(PROMPTS.get("duo_coder_auto_test", ""))
    if ctx.duo_config.until_finished:
        parts.append(PROMPTS.get("duo_coder_until_finished", ""))
    sys_prompt = "\n\n".join(p for p in parts if p)
    # RUNTIME-ENV LINE (2026-09-30): DUO_CODER_BASE promises an OS/runtime
    # note "appended to this system prompt" and a workspace root "stated in
    # your context" — neither existed, so small models guessed the
    # environment Linux-container style (live: Sharp ran `ls /root` and
    # `cd C:\workspace\tetris` before its first write, minutes at ~11 t/s).
    # Built ONCE per run here, never mutated mid-run (prefix-cache safe).
    try:
        _ws_root = str(getattr(ctx, "workspace", "") or "").strip()
        _env_line = ("RUNTIME: This machine is Windows — there is NO Linux "
                     "container; never look in /workspace or /root. "
                     + (f"Workspace root: {_ws_root.replace(chr(92), '/')} — "
                        if _ws_root else "")
                     + "use relative paths in tool calls.")
        # core marker, not the whole line: a custom prompt may already carry
        # the same info in a slightly different wording
        if "RUNTIME: This machine is Windows" not in sys_prompt:
            sys_prompt = sys_prompt.rstrip() + "\n\n" + _env_line
    except Exception:
        pass
    return sys_prompt


# ── Pre-Explore ────────────────────────────────────────────────────────

async def _run_parallel_pre_explore(
    *, exec_mdl, worker_slots, partitions, xtools, xopts, xport,
    workspace, tree_ctx, user_input, xctx_chars_limit, xmsg_hard_cap,
    aborted_fn, step_skipped_fn, emit_fn, websearch_enabled=False,
    searched_queries=None, preload_fn=None, parallel_enabled=True,
    chat_id="", settings_dict=None, websearch_fn=None,
    thinking_override: bool | None = None,
    llm_read_timeout: float | None = None,
) -> tuple:
    from hive_functions.pre_explore import run_pre_explore
    if llm_read_timeout is None:
        try:
            llm_read_timeout = float((xopts or {}).get("_llm_read_timeout_s", 300.0) or 300.0)
        except Exception:
            llm_read_timeout = 300.0
    parallel_on = bool(
        (settings_dict or {}).get("duo_parallel_preexplore", False)
    ) if parallel_enabled else False
    explore_ctx, results, contracts, _msgs = await run_pre_explore(
        model=exec_mdl, port=xport, partitions=partitions,
        workspace=workspace, tree_ctx=tree_ctx, user_input=user_input,
        worker_slots=worker_slots, parallel=parallel_on,
        msg_cap=14,
        max_tool_rounds=int(xopts.get("_preexplore_max_tools", 20)),
        pctx=int(xopts.get("num_ctx", 0)),
        websearch_fn=websearch_fn,
        aborted_fn=aborted_fn, emit_fn=emit_fn,
        thinking_override=thinking_override,
        llm_read_timeout=llm_read_timeout,
    )
    if chat_id and explore_ctx:
        from context.resume import _clear_resume_block
        _clear_resume_block(chat_id)
    return explore_ctx, results, contracts, _msgs


# ── Symbol Hints ───────────────────────────────────────────────────────

def _extract_symbol_candidates(text: str, max_symbols: int = 6) -> list[str]:
    _re_symbol_candidate = re.compile(r'\b([\w_]{3,60})\b')
    symbols = _re_symbol_candidate.findall(text or "")
    seen = set()
    ranked = []
    for s in symbols:
        if s in seen or s.lower() in {"the", "and", "for", "with", "from", "that", "this",
            "not", "are", "will", "can", "has", "was", "all", "but", "its", "new", "use",
            "one", "two", "any", "may", "set", "get", "put", "add", "run", "let", "see"}:
            continue
        seen.add(s)
        ranked.append((s, text.count(s)))
    ranked.sort(key=lambda x: -x[1])
    return [t for _, t in ranked[: max(1, int(max_symbols or 6))]]


async def _build_symbol_reference_hints(
    query: str, workspace_path: str, *, top_k: int = 2, max_items: int = 120,
) -> list[dict]:
    root = Path(workspace_path or ".")
    if not root.exists():
        return []
    symbols = _extract_symbol_candidates(query, max_symbols=max(4, int(top_k) * 3))
    if not symbols:
        return []
    out: list[dict] = []
    for sym in symbols:
        if len(out) >= max(1, int(top_k or 2)):
            break
        try:
            from hive_functions.hivemind_feature.ast_tools import find_references_report
            report = await asyncio.to_thread(find_references_report, root, sym, max_items)
        except Exception:
            continue
        low = str(report or "").lower()
        if "[find_references error:" in low or "(no references found)" in low:
            continue
        lines = [ln.strip() for ln in str(report).splitlines() if ln.startswith("def ") or ln.startswith("use ")]
        if not lines:
            continue
        out.append({"symbol": sym, "matches": [ln[:180] for ln in lines[:3]]})
    return out

# -- Run-Config-Helfer (aus server.py extrahiert) -----------------------------

_DUO_RUNTIME_PROFILES = {"fast", "balanced", "critical"}


def _resolve_duo_runtime_profile(
    profile_override: str | None,
    *,
    important_task: bool,
    until_finished: bool,
    lock_override: bool = False,
) -> str:
    """Resolve effective runtime profile with escalation for critical work."""
    from core import state as _st
    raw = str(profile_override or _st.settings.get("duo_runtime_profile", "balanced")).strip().lower()
    if lock_override and raw in _DUO_RUNTIME_PROFILES:
        return raw
    if until_finished or important_task:
        return "critical"
    return raw if raw in _DUO_RUNTIME_PROFILES else "balanced"


def _resolve_duo_run_timeout_seconds(profile: str) -> float:
    """Resolve wall-clock timeout for one Duo/Agentic run."""
    from core import state as _st
    try:
        _balanced = float(_st.settings.get("duo_run_timeout_seconds", 420))
    except Exception:
        _balanced = 420.0
    try:
        _critical = float(_st.settings.get("duo_run_timeout_critical_seconds", 900))
    except Exception:
        _critical = 900.0

    if profile == "critical":
        return max(120.0, _critical)
    if profile == "fast":
        return max(90.0, min(_balanced, 300.0))
    return max(120.0, _balanced)


def _bucket_stop_reason(stop_reason: str) -> str:
    sr = str(stop_reason or "unknown").strip().lower()
    if sr in {"completed", "aborted", "memory_list", "memory_forget", "memory_write", "self_query_completed"}:
        return "none"
    if sr in {"request_timeout", "request_error", "stream_incomplete", "server_error_event"}:
        return "transport_error"
    if sr in {"hard_stop", "timeout", "error"}:
        return "runtime_load_error"
    if sr in ("max_tool_rounds", "verification_required_after_write"):
        return "tool_error"
    return "unknown_error"


def build_image_desc_block(desc) -> str:
    """[IMAGE DESCRIPTION] block for the duo coder/planner context (2026-10-01).

    The vision-agent/prepro pipeline turns attached images into a text
    description; the agentic coder previously saw it ONLY in the
    non-chunking round-0 user input (and the planner never did). This block
    is meant for the coder SYSTEM prompt (constant per run, survives
    compression) and the planner task. Empty desc -> empty block.
    """
    _d = str(desc or "").strip()
    if not _d:
        return ""
    return "\n\n[IMAGE DESCRIPTION]:\n" + _d


def attach_images_to_last_user(msgs, images):
    """Convert the LAST user message to OpenAI content parts carrying the
    attached images (2026-10-01).

    Wire format matches llama_client._convert_messages: image_url parts
    first (raw base64 wrapped as a jpeg data URL — llama-server sniffs the
    real mime), then the text part. No-op when images is empty or no user
    message exists; callers pass this ONLY after resolve_image_plan() said 'raw'.
    """
    _imgs = [str(b).strip() for b in (images or []) if str(b or "").strip()]
    if not _imgs or not msgs:
        return msgs
    for _m in reversed(msgs):
        if isinstance(_m, dict) and _m.get("role") == "user":
            _txt = _m.get("content")
            if not isinstance(_txt, str):
                return msgs  # already parts / unexpected shape: leave untouched
            _parts = [{"type": "image_url",
                       "image_url": {"url": "data:image/jpeg;base64," + _b}}
                      for _b in _imgs]
            _parts.append({"type": "text", "text": _txt})
            _m["content"] = _parts
            return msgs
    return msgs



def resolve_image_plan(planner_mdl, coder_mdl, settings, ctx) -> dict:
    """Pure decision: how the agentic duo run handles attached images
    (2026-10-01, replaces the duo_coder_raw_images one-switch approach).

    Returns {"planner": "raw"|"description"|"none",
             "coder":   "raw"|"description"|"none",
             "warnings": [str], "mode": "direct"|"preprocess"|None}.

    duo_image_mode None (never set, or a preset carried None back) is
    DERIVED: an active vision_model.json (ctx.vision_cfg enabled+model)
    means "preprocess" (text description to both roles), otherwise
    "direct". In direct mode raw parts go to whichever role is checked AND
    vision-capable (registry lookup, no name matching); a checked
    non-multimodal role gets a warning and no image. No images attached:
    everything none, no warnings.
    """
    warnings: list = []
    images = [str(b or "").strip() for b in (getattr(ctx, "images", None) or [])
              if str(b or "").strip()]
    if not images:
        return {"planner": "none", "coder": "none", "warnings": [], "mode": None}
    mode = settings.get("duo_image_mode")
    if mode is None:
        _vc = getattr(ctx, "vision_cfg", None) or {}
        mode = "preprocess" if (_vc.get("enabled") and _vc.get("model")) else "direct"
        warnings.append(f"duo_image_mode not set — derived '{mode}' from the vision config")
    plan = {"planner": "none", "coder": "none"}
    if mode == "preprocess":
        if str(getattr(ctx, "image_description", "") or "").strip():
            plan["planner"] = plan["coder"] = "description"
        else:
            warnings.append("preprocess mode but no image description available — "
                            "enable the vision model in image settings")
        return {"planner": plan["planner"], "coder": plan["coder"],
                "warnings": warnings, "mode": mode}
    for role, mdl, key in (("planner", planner_mdl, "duo_image_to_planner"),
                           ("coder", coder_mdl, "duo_image_to_coder")):
        if bool(settings.get(key, False)):
            from core.model_sampling import _model_profile
            if (_model_profile(str(mdl or "")) or {}).get("vision", False):
                plan[role] = "raw"
            else:
                warnings.append(f"{role} '{mdl}' is not multimodal — image skipped for it")
    if "raw" not in plan.values() and not any("not multimodal" in w for w in warnings):
        warnings.append("no target selected, image will not be used")
    return {"planner": plan["planner"], "coder": plan["coder"],
            "warnings": warnings, "mode": mode}


def duo_gate_status_text(plan: dict) -> str:
    """Status line for the duo image plan (2026-10-01, chat_run gate).

    Claims ONLY what resolve_image_plan decided — the attach-time projector
    check (C4) has the final word and may still skip a raw role.
    """
    _w = plan.get("warnings") or []
    return ("[Image] duo plan — planner: " + str(plan.get("planner", "none"))
            + ", coder: " + str(plan.get("coder", "none"))
            + ((" | " + " | ".join(_w)) if _w else ""))


def _build_duo_image_plan(planner_mdl, coder_mdl, settings, images,
                          image_description, vision_cfg) -> dict:
    """chat_run gate wrapper: plain values -> resolve_image_plan ctx."""
    from types import SimpleNamespace
    return resolve_image_plan(planner_mdl, coder_mdl, settings,
                              SimpleNamespace(images=images,
                                              image_description=image_description,
                                              vision_cfg=vision_cfg or {}))
