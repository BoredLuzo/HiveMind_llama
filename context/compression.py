"""Kontext-Kompression (aus server.py extrahiert)."""
from __future__ import annotations

import logging
import httpx
import re
import time
from typing import Callable
from hive_functions.memory import ToolContextLRU, _recall_marker
from utils.token import estimate_ctx_tokens as _estimate_ctx_tokens
from utils.patterns import _RE_PATH_KEY

logger = logging.getLogger("hivemind.compression")

# bzw. "[path lines X-Y / N]".
_RE_READ_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\]'\"]+")

_ERROR_MARKERS = ("[TOOL_ERROR", "Error:", "Traceback", "FAILED", "[stderr]", "SyntaxError", "TypeError")


def _weighted_ttl(content: str, base_ttl: int) -> int:
    if any(m in content for m in _ERROR_MARKERS):
        return base_ttl * 2
    return base_ttl


_settings: dict = {}
_registry_get: Callable | None = None
_pipeline_chat_stream: Callable | None = None

def init_compression(settings_obj=None, registry_get_fn=None,
                     pipeline_chat_stream_fn=None):
    global _settings, _registry_get, _pipeline_chat_stream
    if settings_obj is not None:
        _settings = settings_obj
    if registry_get_fn:
        _registry_get = registry_get_fn
    if pipeline_chat_stream_fn:
        _pipeline_chat_stream = pipeline_chat_stream_fn

def evict_stale_reads_for_path(
    *,
    messages: list[dict],
    lru: ToolContextLRU,
    path: str,
    exclude_idx: int | None = None,
) -> int:
    """
    LRU-A: immediately evict read_file outputs for a path that was just
    edited/written - the cached content is stale and misleading. Replaces the
    tool-message payload in-place with a recall marker (keeps message indices
    stable) and marks the LRU entries evicted.

    LRU-B: when a NEW read_file for the same path is registered, *exclude_idx*
    keeps that newest copy alive while the older full duplicates are evicted.

    Returns the number of tool messages evicted.
    """
    if not messages or not path:
        return 0
    evicted = 0
    # oldest first -> keep the newest read of the path alive
    for entry in lru.alive_by_path(path, kind="read_file"):
        idx = int(entry.get("idx", -1))
        if idx < 0 or idx >= len(messages):
            continue
        if exclude_idx is not None and idx == int(exclude_idx):
            continue
        msg = messages[idx]
        if msg.get("role") != "tool" or msg.get("name") != "read_file":
            continue
        old = str(msg.get("content", ""))
        if not old or old.startswith("[System: Content of"):
            lru.mark_evicted(idx)
            continue
        messages[idx] = {
            **msg,
            "content": _recall_marker(entry.get("path", "") or path),
        }
        lru.mark_evicted(idx)
        evicted += 1
    return evicted


def _evict_stale_tool_outputs(
    *,
    messages: list[dict],
    lru: ToolContextLRU,
    target_token_budget: int,
    hard_floor_tokens: int,
) -> int:
    """
    Phase 2 semantic eviction:
    - keeps conversational turns intact
    - evicts lowest-TTL tool outputs first
    - replaces evicted payloads with strict recall markers
    """
    if not messages:
        return 0

    est = _estimate_ctx_tokens(messages)
    if est <= target_token_budget:
        return 0

    evicted = 0
    for entry in lru.candidates():
        if est <= target_token_budget:
            break
        idx = int(entry.get("idx", -1))
        if idx < 0 or idx >= len(messages):
            continue
        msg = messages[idx]
        if msg.get("role") != "tool":
            continue
        old = str(msg.get("content", ""))
        if not old:
            continue
        if old.startswith("[System: Content of"):
            continue

        p = entry.get("path", "") or msg.get("name", "tool output")
        placeholder = _recall_marker(p)
        messages[idx] = {
            **msg,
            "content": placeholder,
        }
        lru.mark_evicted(idx)
        # P1-4 FIX: Incremental token delta instead of O(n) full re-scan.
        # Previously: est = _estimate_ctx_tokens(messages) — O(n) per eviction.
        # Now: adjust by the delta of old vs placeholder content.
        est -= (len(old) // 3) - (len(placeholder) // 3)
        evicted += 1
        if est <= hard_floor_tokens:
            break

    return evicted


def _validate_compression_summary(summary: str,
                                   written_files: list[str],
                                   done_tasks: list[str],
                                   known_partitions: list[str] = None,
                                   plan_anchor: str = "") -> bool:
    if known_partitions is None:
        known_partitions = []
    if len(summary.strip()) < 80:
        return False
    if written_files:
        if not any(f.split("/")[-1] in summary
                   for f in written_files):
            return False
    hallucination_phrases = [
        "task complete", "all done", "successfully completed",
        "finished all", "implementation complete"
    ]
    if not written_files:
        if any(p in summary.lower() for p in hallucination_phrases):
            return False
    if known_partitions:
        _present = [p for p in known_partitions if p in summary]
        if len(_present) < len(known_partitions) // 2:
            _missing = [p for p in known_partitions if p not in summary]
            logger.warning(
                "[COMPRESSION] Summary dropped %d/%d partition labels. Missing: %s",
                len(_missing), len(known_partitions), ", ".join(_missing[:5]))
            return False
    if plan_anchor:
        # Mindesttreffer 2: erzwingt echte inhaltliche Uebernahme, toleriert
        import re as _re_plan
        _plan_words = list(dict.fromkeys(
            w.lower() for w in _re_plan.findall(
                r"[A-Za-z\xc4\xd6\xdc\xe4\xf6\xfc0-9_]{4,}", plan_anchor)
        ))
        if _plan_words:
            _hit = sum(1 for w in _plan_words if w in summary.lower())
            if _hit < max(2, int(len(_plan_words) * 0.25)):
                logger.warning(
                    "[COMPRESSION] Summary dropped plan anchor (%d/%d words).",
                    _hit, len(_plan_words))
                return False
    return True

    

async def _compress_tool_context(
    messages: list,
    model: str,
    port: int,
    client,
    system_prompt: str,
    original_task: str,
    written_files: list,
    done_tasks: list,
    goal_pin: dict | None = None,
    keep_recent_msgs: int = 12,
    plan_state: str = "",
    plan_anchor_text: str = "",
    last_test_status: str = "",
    explore_ctx: str = "",
    tool_rounds: int = 0,
    max_tool_rounds: int = 0,
) -> list:


    _keep_recent = max(0, int(keep_recent_msgs or 0))
    _history_msgs = [m for m in messages if m.get("role") != "system"]
    if goal_pin:
        _history_msgs = [
            m for m in _history_msgs
            if not (m.get("role") == goal_pin.get("role") and m.get("content") == goal_pin.get("content"))
        ]
    if _keep_recent > 0 and len(_history_msgs) > _keep_recent:
        _older_msgs = _history_msgs[:-_keep_recent]
        _recent_tail_msgs = _history_msgs[-_keep_recent:]
    else:
        _older_msgs = _history_msgs
        _recent_tail_msgs = []

    _condensed_files: set = set()
    for _om in _older_msgs:
        if _om.get("role") != "tool" or _om.get("name") != "read_file":
            continue
        _omc = _om.get("content", "")
        if isinstance(_omc, list):
            _omc = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in _omc
            )
        for _rm in _RE_READ_PATH.finditer(_omc):
            _condensed_files.add(_rm.group())

    _history_text = []
    for m in _older_msgs:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            # Tool-result messages
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        if role in ("assistant", "tool") and content:
            _limit = 1500 if role == "tool" else 800
            _history_text.append(f"[{role.upper()}]: {content[:_limit]}")

    if not _history_text:
        return messages, set(), {}

    _compress_prompt = (
        f"Summarize the following coding session for context compression.\n\n"
        f"ORIGINAL TASK: {original_task}\n\n"
        f"ALREADY WRITTEN FILES: {', '.join(written_files) if written_files else 'none'}\n"
        f"COMPLETED SUBTASKS: {', '.join(done_tasks) if done_tasks else 'none'}\n"
        + (f"Current plan state: {plan_state}\n" if plan_state else "")
        + (f"LAST TEST RUN: {last_test_status}\n" if last_test_status else "")
        + (f"PROGRESS: round {tool_rounds} of ~{max_tool_rounds}\n" if max_tool_rounds else "")
        + "\n"
        f"SESSION HISTORY:\n" + "\n".join(_history_text) + "\n\n"
        f"Write a compact summary (max 400 words) covering:\n"
        f"1. What files were read and their key contents/structure\n"
        f"2. What was discovered (classes, methods, patterns, dependencies)\n"
        f"3. What was already implemented/written\n"
        f"4. What still needs to be done\n"
        f"5. Any important constraints or issues found\n"
        f"\n"
        f"Your summary MUST include these three sections, in this order:\n"
        f"1. KEY DECISIONS: The 3 most important choices made so far. If none yet, write 'none'.\n"
        f"2. CURRENT BLOCKER: If any test or build is currently failing, copy the exact error message (max 3 lines). If nothing is failing, write 'none'.\n"
        f"3. PROGRESS: X/Y files written, last tool round was {tool_rounds}.\n"
        f"\n"
        f"Be dense and technical. This replaces the full history."
    )

    if explore_ctx:
        import re as _re_anchor
        _partitions = _re_anchor.findall(r'partition\s*=\s*"([^"]+)"', explore_ctx)
        if _partitions:
            _anchor = "\n\n## NON-COMPRESSIBLE CONTRACT ANCHORS\n"
            _anchor += "These partition labels and their key fields MUST appear verbatim in your summary:\n"
            for _p in list(dict.fromkeys(_partitions))[:12]:
                _anchor += f"- partition: {_p}\n"
            _anchor += (
                "Also preserve for each partition: exports, imports_internal, "
                "imports_external, touched_by_task, complexity_score.\n"
                "| PRESERVE THESE PARTITIONS EXACTLY AS LISTED ABOVE |"
            )
            _compress_prompt += _anchor

    if plan_anchor_text:
        _compress_prompt += (
            "\n\n## NON-COMPRESSIBLE PLAN ANCHOR\n"
            "The following plan must survive compression. Preserve its step "
            "intents and target files as verbatim as possible:\n"
            f"| PLAN: {plan_anchor_text} |"
        )

    _compress_usage: dict = {}
    try:
        _c_gen_t0 = time.monotonic()  # GEN-TIME: Compression-POST-Dauer
        _resp = await client.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={
                "model":          model,
                "messages":       [{"role": "user", "content": _compress_prompt}],
                "stream":         False,
                "temperature":    0.1,
                "max_tokens":     800,
                "thinking": False, "thinking_budget": 0,
            },
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
        _data = _resp.json()
        _u = _data.get("usage") or {}
        if _u.get("completion_tokens"):
            # TOKEN-TRACKER (2026-08-25): cached_tokens aus prompt_tokens_details.
            try:
                _cu_cached = int((_u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            except Exception:
                _cu_cached = 0
            _compress_usage = {"completion_tokens": int(_u["completion_tokens"]),
                               "prompt_tokens": int(_u.get("prompt_tokens") or 0),
                               "cached_tokens": _cu_cached,
                               "gen_ms": int((time.monotonic() - _c_gen_t0) * 1000)}
        if "choices" in _data:
            _summary = _data["choices"][0].get("message", {}).get("content", "").strip()
        else:
            _summary = _data.get("message", {}).get("content", "").strip()
    except Exception as _compress_err:
        logger.warning(
            "Context compression failed (%s: %s) - using fallback summary",
            type(_compress_err).__name__, _compress_err
        )
        _fallback_files = ", ".join(written_files[:10]) if written_files else "none"
        _fallback_done  = ", ".join(done_tasks[:5])    if done_tasks    else "none"
        _tool_results = [
            (m.get("content", "") or "")[:200]
            + ("..." if len(m.get("content", "") or "") > 200 else "")
            for m in messages
            if m.get("role") == "tool"
        ][-3:]
        import re as _re_fb
        _fb_parts = _re_fb.findall(r'partition\s*=\s*"([^"]+)"', explore_ctx or "")
        _summary = (
            f"## State Reconstruction (compression model unavailable)\n\n"
            f"**Original Task:** {original_task[:300]}\n\n"
            f"**Written Files:** {_fallback_files}\n"
            f"**Completed Subtasks:** {_fallback_done}\n"
        )
        if _fb_parts:
            _summary += "\n**Preserved Partitions:**\n"
            for _p in list(dict.fromkeys(_fb_parts))[:12]:
                _summary += f"- partition: {_p}\n"
        _summary += (
            "\nContinue implementing — do NOT re-read already written files."
        )

    # Neue komprimierte Message-Liste
    _explored_paths: list[str] = []
    for _m in messages:
        _mc = _m.get("content", "")
        if isinstance(_mc, list):
            _mc = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in _mc)
        if _mc:
            _explored_paths += _RE_PATH_KEY.findall(_mc)  # BUG-7 FIX: compiled constant
    _explored_str = "\n".join(f"  - {p}" for p in dict.fromkeys(_explored_paths)) if _explored_paths else "  (none recorded)"

    _system_msg = {"role": "system", "content": system_prompt}
    # PLAN-REINJECT-FIX (2026-08-31): the plan anchor is written not only into
    # the compression prompt, but also into the RESULTING summary message.
    # Previously the plan was completely lost on an LLM compression error
    # (fallback summary) — the coder no longer knew which subtask was running
    # and what was coming next (observed live: CTX-COMPRESS -> 3 tool calls
    # -> loop_detected stop). The anchor is cheap (a few hundred tokens) and
    # guarantees the plan ALWAYS survives compression.
    _plan_reinject = ""
    if plan_anchor_text:
        _plan_reinject = (
            f"[PLAN - must continue, do NOT deviate from these steps]\n{plan_anchor_text}\n\n"
        )
    _summary_msg = {
        "role": "user",
        "content": (
            f"TASK: {original_task}\n\n"
            + _plan_reinject
            + (f"[PLAN STATE: {plan_state}]\n\n" if plan_state else "")
            + (f"[LAST TEST: {last_test_status[:120]}]\n" if last_test_status else "") +
            f"[CONTEXT SUMMARY — previous tool calls compressed]\n{_summary}\n"
            f"[END SUMMARY]\n\n"
            f"[RECENT CONTEXT KEPT RAW: {len(_recent_tail_msgs)} latest messages]\n\n"
            f"ALREADY EXPLORED (do NOT read/list these again):\n{_explored_str}\n\n"
            f"[FILES ALREADY WRITTEN — do not re-read or re-write unless fixing a bug]:\n"
            + ("\n".join(f"  - {f}" for f in written_files) if written_files else "  (none yet)") + "\n\n"
            + "Continue implementing directly — exploration phase is complete."
        )
    }
    # BUG-F FIX: goal_pin (role:user) followed by _summary_msg (role:user) = consecutive user turns
    if goal_pin:
        _summary_msg = {
            **_summary_msg,
            "content": goal_pin.get("content", "") + "\n\n" + _summary_msg["content"]
        }
    _compressed = [_system_msg, _summary_msg]
    _compressed.extend(_recent_tail_msgs)
    return _compressed, _condensed_files, _compress_usage


# -- Chat Session Compression ----------------------------------
# Difference to _compress_tool_context (code focus):

SESSION_COMPRESS_THRESHOLD = 20  # messages (= 10 turns) → compress
SESSION_COMPRESS_KEEP      = 6


async def _compress_chat_session(sess_msgs: list) -> list:


    _threshold = int(_settings.get("session_compress_threshold", SESSION_COMPRESS_THRESHOLD))
    if len(sess_msgs) <= _threshold:
        return sess_msgs

    # Split: old history (to compress) + fresh history (keep)
    _old_msgs  = sess_msgs[:-SESSION_COMPRESS_KEEP]
    _keep_msgs = sess_msgs[-SESSION_COMPRESS_KEEP:]

    _history_lines = []
    for m in _old_msgs:
        role    = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        if role in ("user", "assistant") and content:
            label = "USER" if role == "user" else "ASSISTANT"
            _history_lines.append(f"[{label}]: {str(content)[:600]}")

    if not _history_lines:
        return sess_msgs

    _compress_prompt = (
        "You are compressing a conversation history for a chat system. "
        "Create a compact but information-dense summary that preserves what matters for future responses.\n\n"
        "CONVERSATION HISTORY TO COMPRESS:\n"
        + "\n".join(_history_lines)
        + "\n\n"
        "Write a compact summary (max 300 words) covering:\n"
        "1. Main topics discussed (most recent first)\n"
        "2. User preferences, style, or constraints they expressed\n"
        "3. Decisions made or conclusions reached\n"
        "4. Ongoing tasks or open questions the user has\n"
        "5. Any important context about the user or their project\n\n"
        "Be dense and factual. Use bullet points. "
        "This replaces older messages — the most recent messages are kept separately."
    )

    _summary = ""
    try:
        _sum_model = _registry_get("direct") or _registry_get("analyst") or ""
        if _sum_model:
            _parts: list[str] = []
            async for tok in _pipeline_chat_stream(
                _sum_model,
                [{"role": "user", "content": _compress_prompt}],
                0.1, 400
            ):
                _parts.append(tok)
            _summary = "".join(_parts).strip()
    except Exception as _sess_compress_err:
        logger.warning(
            "Session compression failed (%s: %s) - using plaintext fallback",
            type(_sess_compress_err).__name__, _sess_compress_err
        )

    if not _summary:
        _summary = "Previous conversation covered: " + "; ".join(
            l[:120] for l in _history_lines[::2][:6]
        )

    # Komprimierte Session: Summary als system-Message + frische Messages
    _compressed_block = {
        "role":    "system",
        "content": (
            f"[CONVERSATION SUMMARY — {len(_old_msgs)} older messages compressed]\n"
            f"{_summary}\n"
            f"[END SUMMARY — current conversation continues below]"
        )
    }
    return [_compressed_block] + _keep_msgs

