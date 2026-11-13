# -*- coding: utf-8 -*-
"""Helper-Funktionen und Konstanten aus duo_runner.py extrahiert."""
from __future__ import annotations
import asyncio
import json as _json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("hivemind.duo_helpers")

from utils.token import CHARS_PER_TOKEN as _CHARS_PER_TOKEN

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_VRAM_BUDGET_GB = 7.5

# PRE-SEED (2026-11-09): small workspaces don't need a read_file round. The
# coder gets the complete files in its first user message, exactly like the
# one-shot harnesses it competes with; the read guard is pre-populated so
# edit_file is allowed immediately. Only top-level, small, text-like files —
# never a recursive scan of a real repo.
_PRESEED_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".toml",
    ".yaml", ".yml", ".md", ".txt", ".html", ".css", ".sh",
})


def _duo_context_budget_chars() -> int:
    """duo_context_budget_chars (D2): upper cap for the pre-seed block.
    Settings read is best-effort — a broken settings object falls back to
    the previous fixed 8000."""
    try:
        from core import state as _st
        return max(1000, int(_st.settings.get("duo_context_budget_chars", 8000)))
    except (ImportError, AttributeError, TypeError, ValueError):
        return 8000


def build_preseed_context(workspace: str, ctx_tokens: int = 0,
                          max_files: int = 4, query: str = "",
                          budget_chars: int | None = None) -> tuple[str, list[str]]:
    """Return (prompt_block, relative_paths) for a tiny workspace.

    Empty block/paths when the workspace has no top-level code files, or the
    combined content exceeds the character cap. The cap scales with the coder
    context and never exceeds duo_context_budget_chars (default 8 KB).

    Up to `max_files` top-level files are inlined. D2 (2026-11-09): when the
    workspace has MORE than `max_files` such files, the workspace index
    (context/repo_index) selects the `max_files` files most relevant to
    `query` (the task text) instead of refusing — but only when the query
    actually matches something; an empty query keeps the old no-preseed
    behavior for big workspaces.
    """
    try:
        root = Path(str(workspace or ""))
        if not root.is_dir():
            return "", []
        files = [
            p for p in sorted(root.iterdir(), key=lambda x: x.name.lower())
            if p.is_file()
            and p.suffix.lower() in _PRESEED_EXTS
            and not p.name.startswith(".")
        ]
        if not files:
            return "", []
        if len(files) > int(max_files):
            if not str(query or "").strip():
                return "", []
            try:
                from context.repo_index import search_workspace as _idx_search
                _picked = _idx_search(str(root), str(query), limit=int(max_files))
            except (ImportError, OSError):
                _picked = []
            if not _picked:
                return "", []
            _want = {name for name, _score in _picked}
            files = [p for p in files if p.name in _want]
        cap = 4000
        _budget = int(budget_chars) if budget_chars is not None \
            else _duo_context_budget_chars()
        if int(ctx_tokens or 0) > 0:
            cap = min(_budget, max(2000, int(ctx_tokens) // 4))
        if cap > _budget:
            cap = _budget
        parts: list[str] = []
        names: list[str] = []
        total = 0
        for path in files:
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return "", []
            if "\x00" in body:
                return "", []
            total += len(body)
            if total > cap:
                return "", []
            names.append(path.name)
            lang = path.suffix.lstrip(".").lower()
            parts.append(f"### FILE: {path.name}\n```{lang}\n{body.rstrip()}\n```")
        if not parts:
            return "", []
        block = (
            "[PRE-SEEDED WORKSPACE FILES — already read, do not call read_file "
            "for them]\n"
            "These are the complete current contents. Edit directly with "
            "edit_file/write_file; preserve all existing top-level symbols.\n\n"
            + "\n\n".join(parts)
        )
        return block, names
    except (OSError, ValueError, TypeError):
        return "", []


def _duo_deep_retrieval_cfg(settings_dict: dict | None = None) -> tuple:
    """(enabled, max_hints, budget_chars, index_max_files, index_max_depth)
    from the D2 deep-retrieval knobs; best-effort — a broken settings object
    falls back to the shipped defaults."""
    _s = settings_dict
    if _s is None:
        try:
            from core import state as _st
            _s = _st.settings
        except (ImportError, AttributeError):
            _s = {}

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int((_s or {}).get(key, default))))
        except (TypeError, ValueError):
            return default

    enabled = bool((_s or {}).get("duo_deep_retrieval", True))
    max_hints = _int("duo_deep_retrieval_max_files", 5, 1, 32)
    budget = _int("duo_deep_retrieval_max_chars", 3000, 300, 20000)
    idx_files = _int("duo_index_max_files", 2000, 1, 50000)
    idx_depth = _int("duo_index_max_depth", 6, 1, 24)
    return enabled, max_hints, budget, idx_files, idx_depth


def build_deep_retrieval_context(workspace: str, query: str = "",
                                 settings_dict: dict | None = None
                                 ) -> tuple[str, list[str]]:
    """D2 deep retrieval (2026-11-11): rank nested repo files into a compact
    hint block for the coder's first message.

    Additive-only companion to build_preseed_context: the caller fires it
    ONLY when the tiny-workspace pre-seed produced nothing, so the existing
    pre-seed bytes are untouched. The block carries signatures (path + size
    + top symbols), never full contents. Returns ("", []) for an empty
    query, a no-match query, a disabled knob or any error — no arbitrary
    picks. The index walk is bounded (heavy/hidden dirs pruned, depth +
    max_files caps); callers run this off the event loop (asyncio.to_thread)
    because the FIRST refresh of a large workspace reads up to
    duo_index_max_files files.
    """
    if not str(query or "").strip():
        return "", []
    try:
        enabled, max_hints, budget, idx_files, idx_depth = \
            _duo_deep_retrieval_cfg(settings_dict)
        if not enabled:
            return "", []
        from context.repo_index import deep_context_block as _dcb
        return _dcb(str(workspace or ""), str(query), max_hints=max_hints,
                    budget_chars=budget, max_files=idx_files,
                    max_depth=idx_depth)
    except (ImportError, OSError, ValueError, TypeError):
        return "", []

# READ-ONLY-CODER-NOTE (moved from duo_runner 2026-11-08): duo_round_input
# needs it too; keeping it here removes the extracted-module back-edge.
_READONLY_CODER_NOTE = (
    "[READ-ONLY REQUEST — file tools are DISABLED]\n"
    "Answer in plain text from the provided codebase context. If you would call a "
    "tool, describe the outcome in text instead.\n"
    "IMPORTANT: textual tool-call syntax will NOT be executed — never output "
    "[TOOL_CALL] … [END_TOOL_CALL], edit_file<argkey>…, <function=…> or bare tool JSON."
)

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
    _mp_overrides = (settings_dict or {}).get("_model_profiles", {})
    _base = (model_name or "").split(":")[0]
    _ov = _mp_overrides.get(model_name) or (
        next((v for k, v in _mp_overrides.items()
              if k == _base or k.startswith(_base + ":")), {})
        if _base else {}
    )
    if _ov and _ov.get("thinking_enabled") is not None:
        return dict(_ov)

    from model_configs.models_registry import get_profile, get_capabilities, get_thinking_budget
    from core.model_sampling import _model_profile

    prof = get_profile(model_name) or {}
    caps = get_capabilities(model_name) or {}
    mp = _model_profile(model_name) or {}
    thinking = bool(
        caps.get("thinking") or prof.get("thinking")
        or prof.get("reasoning") in ("on", True) or mp.get("thinking")
    )
    _gtb = get_thinking_budget(model_name)
    raw_tb = _gtb if _gtb is not None else prof.get("thinking_budget")
    try:
        budget = int(raw_tb) if raw_tb is not None else 0
    except (ValueError, TypeError):
        budget = 0
    if thinking and budget <= 0:
        budget = 2500
    try:
        max_tokens = int(prof.get("max_tokens") or 4096)
    except (ValueError, TypeError):
        max_tokens = 4096
    return {"thinking_enabled": thinking, "thinking_budget": budget, "max_tokens": max_tokens}


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
            tb = agent_cfg.get("thinking_budget")
            if tb is not None:
                try:
                    val = int(tb)
                    if val > 0:
                        base_budget = val
                except (ValueError, TypeError):
                    pass

    profile = _get_thinking_profile(model_name, _s)
    if base_budget is None:
        if not profile.get("thinking_enabled"):
            return 0
        base_budget = profile.get("thinking_budget", 2500)
        if base_budget <= 0:
            base_budget = 2500

    if input_tokens > 0 and input_tokens < base_budget * 0.2:
        adaptive_budget = max(200, int(base_budget * 0.3))
    else:
        adaptive_budget = base_budget

    available = max(0, available_ctx - input_tokens)
    _max_thinking_share = int(available * 0.40)
    if _max_thinking_share < 64 or adaptive_budget <= 0:
        return 0
    return min(max(64, int(adaptive_budget)), _max_thinking_share)


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
                                        until_finished: bool = False,
                                        num_ctx: int | None = None,
                                        model: str | None = None) -> float:
    _s = settings or {}
    try:
        _base = float(_s.get("duo_read_timeout", 300))
    except Exception:
        _base = 300.0
    _scale = {"fast": 0.55, "balanced": 1.0, "critical": 1.35}.get(
        str(profile or "balanced").strip().lower(), 1.0)
    if until_finished:
        _scale = max(_scale, 1.2)
    _timeout = _base * _scale
    # PREFILL-AWARE (2026-10-27, live slow run): a cold prefill costs
    # prompt_tokens / prefill_tps seconds. Measured on an 8 GB RX 6600:
    # decode 37-48 t/s is fine, prefill falls to ~90 t/s at 24k tokens, so a
    # 38k prompt needs ~420 s - exactly the rounds that looked "slow" and one
    # of which ended as a timeout while the server was working. The read
    # timeout has to cover the prefill of the configured context.
    if num_ctx:
        try:
            from core.budgets import prefill_tps_for as _ptps
            _rate = max(40.0, min(2000.0, float(_ptps(model or "") or 0)))
        except (ImportError, TypeError, ValueError):
            _rate = 100.0
        _prefill_s = float(num_ctx) / _rate
        _timeout = max(_timeout, _prefill_s * 1.6 + 60.0)
    return max(45.0, min(3600.0, _timeout))


# ── Prompt builder ─────────────────────────────────────────────────────

def should_compress_after_truncated_drop(finish_reason, dropped_names) -> bool:
    """True when a dropped tool call was an edit_file cut at the output limit.

    A truncated edit_file cannot be salvaged: applying half an old_text/new_text
    pair corrupts the file. Dropping it is correct, but retrying it with the
    SAME clamped budget truncates again by construction (live 2026-10-31: a
    2404-token budget against a ~16k request). The caller compresses first.
    write_file keeps its own salvage + append flow and must not be diverted.
    """
    if str(finish_reason or "") != "length":
        return False
    return any(str(n or "") == "edit_file" for n in (dropped_names or []))


def inloop_plan_should_run(agentic_thinking: bool, thinking_per_chunk: bool,
                           aborted: bool, planner_enabled: bool,
                           has_plan: bool,
                           chunking_enabled: bool = True) -> bool:
    """PLAN-DUP-GUARD (2026-11-03): may the coder's tool round run the
    in-loop planner (run_inloop_planner with make_thinking_planner_sys —
    the "## Plan: / ### Open Tasks" markdown template)?

    PLANNER-OFF (2026-11-04): when the planner is completely off (planner
    toggle off AND chunking off), the coder gets only the user prompt. No
    in-loop plan, no planner-format text, no plan talk.

    Otherwise the in-loop plan is the rescue path for agentic runs whose
    planner phase was skipped or produced nothing parseable. When the run
    ALREADY carries a planner plan — planner phase ran (chunking or planner
    toggle) or a resume delivered subtasks — a second plan must NOT be
    requested: the second plan otherwise arrives as visible answer text
    (token event, "**Plan:**"), doubling the planning wait on big prompts.
    """
    if not planner_enabled and not chunking_enabled:
        return False
    if aborted or planner_enabled or has_plan:
        return False
    return bool(agentic_thinking or thinking_per_chunk)


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
    try:
        if (str((ctx.settings or {}).get("source") or "").startswith("telegram")
                and not bool((ctx.settings or {}).get("phone_restricted"))):
            parts.append(
                "FILE DELIVERY: when the task asks for a deliverable, write the "
                "file, call send_file with its workspace path exactly once, and "
                "then finish with task_complete. Delivery happens automatically "
                "after the final verification. Do not keep searching after the "
                "deliverable exists."
            )
    except (AttributeError, TypeError, ValueError):
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
        if sys.platform == "win32":
            _env_line = ("RUNTIME: This machine is Windows — there is NO Linux "
                         "container; never look in /workspace or /root. "
                         + (f"Workspace root: {_ws_root.replace(chr(92), '/')} — "
                            if _ws_root else "")
                         + "use relative paths in tool calls.")
        else:
            _env_line = ("RUNTIME: This machine is Linux. Commands run in the "
                         "workspace environment; use relative paths in tool "
                         "calls. "
                         + (f"Workspace root: {_ws_root} — " if _ws_root else "")
                         + "do not assume Windows paths.")
        # SHELL-TOOLS (2026-10-21): models are trained on GNU coreutils and
        # write grep/sed/awk/find. Say plainly whether this box has them.
        try:
            from infra import shell_env as _shell_env
            _sh = _shell_env.runtime_hint()
            if _sh:
                _env_line += " " + _sh
        except (ImportError, AttributeError, TypeError):
            pass
        # core marker, not the whole line: a custom prompt may already carry
        # the same info in a slightly different wording
        if "RUNTIME: This machine is Windows" not in sys_prompt:
            sys_prompt = sys_prompt.rstrip() + "\n\n" + _env_line
    except Exception:
        pass
    # EDITOR CONTEXT (2026-10-21): open file/selection from the IDE. Appended
    # last so the runtime line stays adjacent to the rest of the environment
    # info; prefix-cache-safe because it is built once per run.
    try:
        from infra import editor_context as _ec_mod
        _ec_block = _ec_mod.context_block_for(getattr(ctx, "workspace", "") or None)
        if _ec_block:
            sys_prompt = sys_prompt.rstrip() + "\n\n" + _ec_block
    except (ImportError, AttributeError, TypeError, ValueError):
        pass
    return sys_prompt


# ── Pre-Explore ────────────────────────────────────────────────────────

def resolve_coder_tool_mode(settings: dict | None, read_only: bool = False) -> str:
    """Tool mode for the coder round: readonly > core (default) > full.

    CORE (2026-11-08): the coder defaults to the token-saving core set
    (read/edit/run/tests). `duo_coder_tool_tier=full` opts into the previous
    behaviour with git/web/MCP/browser/LSP tools.
    """
    if read_only:
        return "duo_readonly"
    tier = str((settings or {}).get("duo_coder_tool_tier", "core") or "core")
    _t = tier.strip().lower()
    if _t == "full":
        return "duo_full"
    if _t == "lean":
        return "duo_lean"
    if _t == "mcp":
        return "duo_mcp"
    if _t in ("readonly", "research"):
        return "duo_readonly"
    return "duo_core"


async def _run_parallel_pre_explore(
    *, exec_mdl, worker_slots, partitions, xopts, xport,
    workspace, tree_ctx, user_input,
    aborted_fn, emit_fn,
    parallel_enabled=True, chat_id="", settings_dict=None, websearch_fn=None,
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
    if sr in {"completed", "aborted", "graceful_stop", "memory_list",
              "memory_forget", "memory_write", "self_query_completed"}:
        return "none"
    if sr in {"request_timeout", "request_error", "stream_incomplete", "server_error_event"}:
        return "transport_error"
    if sr == "timeout":
        return "timeout"
    if sr in {"hard_stop", "error"}:
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


def direct_vision_should_drop(slot_vision_active, slot_model: str = "",
                              requested_model: str = "") -> bool:
    """Direct-mode image drop decision (2026-10-04) - pure.

    Warn/drop ONLY when the loaded slot actually belongs to the requested
    model AND /props really reported vision:false. Every other state trusts
    the plan: no slot yet (the client loads after this check - chat_stream's
    backstop reads the truth post-load), another model still loaded (a
    pinned planner without a projector must NOT cause a false drop on a
    healthy run), /props unknown (None).
    Pinned by tests/test_vision_gate.py (4 cases).
    """
    if slot_model and requested_model and slot_model != requested_model:
        return False
    return slot_vision_active is False


def build_steer_user_message(text: str, images=None) -> dict:
    """Mid-run steer message builder (2026-10-04, user decision: steering
    with screenshots). Pure: images (list of base64/data-URL strings) become
    image_url parts FIRST, the text follows as a text part - exactly the
    wire shape llama-server expects. No images -> plain string content.
    Used by all steering insertion sites so the wire format is identical.
    """
    # STEER FRAME (2026-10-07, user feedback: force-steer placement felt wrong): a bare
    # "[USER STEER]" tag read as background chatter - small models
    # ignored it. The user's mid-run words are the highest-priority
    # instruction on the table, so the frame says exactly that.
    _txt = ("[OWNER STEER - highest priority, arrived mid-run]\n" + text.strip()
            + "\nIncorporate this immediately: it overrides your current "
              "approach. Act on it with tools in this round if applicable.")
    _imgs = [str(b or "").strip() for b in (images or []) if str(b or "").strip()]
    if not _imgs:
        return {"role": "user", "content": _txt}
    parts = [{"type": "image_url", "image_url": {"url":
              b if b.startswith("data:") else "data:image/jpeg;base64," + b}}
             for b in _imgs]
    parts.append({"type": "text", "text": _txt})
    return {"role": "user", "content": parts}


def resolve_duo_role_models(settings) -> tuple:
    """The duo role models from ONE place (2026-10-04).

    Two live resolution paths exist: the AGENTS panel writes
    agents.duo_planner.model, the CODE panel's planner dropdown writes
    duo_planner_model. Readers that checked only one of them produced the
    live bug: the image status line claimed "planner '' is not multimodal"
    while the runner used the configured model. Order: agent card first
    (most specific), then the flat settings key.
    Returns (planner_model, coder_model) - either may be "" when unset.
    """
    _agents = settings.get("agents") or {}
    _planner = str((((_agents.get("duo_planner") or {}).get("model")))
                   or settings.get("duo_planner_model") or "").strip()
    _coder = str((((_agents.get("duo_coder") or {}).get("model")))
                 or settings.get("duo_coder_model") or "").strip()
    return _planner, _coder


def count_image_parts(messages) -> int:
    """Number of image_url content parts actually carried by the messages
    (2026-10-04: make 'did the model get the image' measurable
    instead of arguable). Pure; returns 0 for plain-string contents."""
    _n = 0
    for _m in messages or []:
        if not isinstance(_m, dict):
            continue
        _c = _m.get("content")
        if isinstance(_c, list):
            _n += sum(1 for _p in _c
                      if isinstance(_p, dict) and _p.get("type") == "image_url")
    return _n


# VISION TRUTH (2026-10-04): the coder's raw-image note as ONE constant so
# the vision gate can retract exactly this text from an already-built
# system prompt when the post-load /props check says the projector dropped.
CODER_VISION_NOTE = (
    "\n\n[VISION]: the user's attached image is included in this "
    "conversation - you can see it. Reference concrete visual "
    "details (layout, texts, colors, shapes) in your work.")


CPU_CTX_WARN_TOKENS = 32768


def cpu_prefill_warning(ctx_tokens, backend: str = "") -> str:
    """Warn when a CPU-only install is configured with a large context.

    TIME-NOT-SIZE: without GPU offload every prompt token is processed on the CPU
    (a 2B model can need many minutes for one write_file),
    so n_ctx stops being a memory setting and becomes a *time* setting - the
    first prompt alone can take many minutes and each re-prefill repeats it. The
    failure looks like a hang, not like a wrong setting, so say it once with the
    real number. Returns "" when there is nothing to warn about.
    """
    try:
        _ctx = int(ctx_tokens or 0)
    except (TypeError, ValueError):
        return ""
    if str(backend or "").strip().lower() != "cpu":
        return ""
    if _ctx <= CPU_CTX_WARN_TOKENS:
        return ""
    return (f"CPU inference with ctx={_ctx}: there is no GPU offload, so the "
            f"first prompt alone can take many minutes and every re-prefill "
            f"repeats it. 8k-16k is the practical range on a laptop CPU; raise "
            f"it only when the model runs on a GPU.")


def vision_note_for_coder(img_plan: dict, coder_slot_vision) -> str:
    """The [VISION] note, but only when the coder slot is CONFIRMED to run
    with its projector (slot.vision_active is True, set from
    /props.modalities.vision after a load).

    STRICT-VISION (a note may only claim vision when the projector is
    loaded): False (projector missing) and None (unproven - slot not loaded
    yet, /props unreachable) both keep the note out. A note that claims "you
    can see it" while the projector is absent makes the model invent visual
    details; saying nothing is the honest default. The post-load gate adds the
    note once /props confirms vision."""
    if (img_plan or {}).get("coder") != "raw":
        return ""
    return CODER_VISION_NOTE if coder_slot_vision is True else ""


def effective_image_plan(plan, *, planner_slot_vision=None, coder_slot_vision=None,
                         has_description: bool = False):
    """Downgrade image-plan entries the LOADED server cannot honor (2026-10-04).

    resolve_image_plan() decides from settings + the model registry BEFORE the
    load; this runs AFTER ensure_loaded and checks the slot's /props-derived
    vision truth. slot vision values: True (server runs with projector),
    False (server runs WITHOUT one), None (/props unavailable - trust the
    plan, no false fallback). 'raw' + vision False falls back to the image
    description when one exists, else to 'none'.
    Returns (new_plan, warnings) - warnings are UI-ready strings.
    """
    plan = dict(plan)
    warnings: list = []
    for role, actual in (("planner", planner_slot_vision),
                         ("coder", coder_slot_vision)):
        if plan.get(role) != "raw" or actual is not False:
            continue
        if has_description:
            plan[role] = "description"
            warnings.append(
                f"{role} runs without a vision projector (server reports vision:false) "
                f"- falling back to the image description.")
        else:
            plan[role] = "none"
            warnings.append(
                f"{role} runs without a vision projector (server reports vision:false) "
                f"and no image description exists - image skipped for it.")
    return plan, warnings



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
    _coder_off = bool(settings.get("duo_coder_images_off", False))
    for role, mdl, key in (("planner", planner_mdl, "duo_image_to_planner"),
                           ("coder", coder_mdl, "duo_image_to_coder")):
        if role == "coder" and _coder_off:
            # OWNER TOGGLE (2026-10-07): "coder takes NO images" - wins over
            # the per-role checkbox, no projector load, no raw attach.
            if bool(settings.get(key, False)):
                warnings.append("coder images OFF (toggle) — image skipped for the coder")
            continue
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


def should_evict_vision_after_prepro(mode, vision_model, duo_models) -> bool:
    """Pure guard for the chat_run prepro eviction (2026-10-02): the vision
    model is evicted after preprocessing ONLY in code_duo runs and ONLY when
    it is not one of the duo models (planner/coder). Normal chat NEVER evicts
    the vision model per image."""
    return str(mode) == "code_duo" and bool(vision_model) and \
        str(vision_model) not in {str(m) for m in (duo_models or [])}


def _log_msgsig_drift(messages, prev_sig_ref, *, round_no, chunk_no) -> None:
    """DEBUG-only message-shape diagnostics (moved from duo_runner).

    Logs the role dump and, when the message signature changed below 10k
    tokens, a [MSGSIG-CHANGE] warning that explains a prompt-cache miss.
    `prev_sig_ref` is a one-element list updated in place.
    """
    logger = logging.getLogger("hivemind.duo")
    logger.debug(
        "[ROLE-DUMP] round=%d system_count=%d roles=%s",
        chunk_no,
        sum(1 for m in messages if m.get("role") == "system"),
        _json.dumps([m.get("role") for m in messages]),
    )
    try:
        _sig_now = [(m.get("role", ""), len(str(m.get("content", "") or "")))
                    for m in messages]
        _prev_sig = prev_sig_ref[0] if prev_sig_ref else None
        _changed_early = []
        if _prev_sig:
            _cum_chars = 0
            for _mi in range(max(len(_sig_now), len(_prev_sig))):
                if _mi >= len(_prev_sig):
                    break
                _s_cur = _sig_now[_mi] if _mi < len(_sig_now) else None
                _s_prev = _prev_sig[_mi]
                if _s_cur != _s_prev:
                    if _cum_chars / _CHARS_PER_TOKEN < 10000:
                        _changed_early.append(
                            (_mi, int(_cum_chars / _CHARS_PER_TOKEN),
                             _s_prev, _s_cur))
                    break
                if _s_cur:
                    _cum_chars += _s_cur[1]
        prev_sig_ref[0] = _sig_now
        if _changed_early:
            logger.warning(
                "[MSGSIG-CHANGE] round=%d change below 10k tokens: %s (msgs=%d)",
                round_no, _changed_early[:5], len(_sig_now),
            )
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as _sig_err:
        logger.debug("[MSGSIG-CHANGE] Signature error: %s", _sig_err)
