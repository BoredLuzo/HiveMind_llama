# -*- coding: utf-8 -*-
"""Shared tool-loop guards (no-progress detection).

A model that repeats the SAME tool call with the SAME arguments is not making
progress; small local models do this often (re-read the same file, re-run the
same command). The guard nudges once, then lets the caller's round limit
handle a model that still refuses to move.
"""
from __future__ import annotations


def tool_signatures(msg: dict) -> list[str]:
    """Stable signatures for one assistant message's tool calls."""
    out: list[str] = []
    for _tc in (msg or {}).get("tool_calls") or []:
        _fn = (_tc or {}).get("function") or {}
        _name = str(_fn.get("name") or "")
        _args = _fn.get("arguments", "")
        if not isinstance(_args, str):
            import json as _json
            try:
                _args = _json.dumps(_args, sort_keys=True, ensure_ascii=False)
            except (TypeError, ValueError):
                _args = str(_args)
        if _name:
            out.append(f"{_name}:{_args}")
    return out


def repeat_guard():
    """on_after_round callback: nudge an identical repeated tool call once."""
    _counts: dict[str, int] = {}
    _nudged: set[str] = set()

    async def _after_round(msg, msgs, _round, _state):
        for _sig in tool_signatures(msg):
            _counts[_sig] = _counts.get(_sig, 0) + 1
            if _counts[_sig] >= 2 and _sig not in _nudged:
                _nudged.add(_sig)
                msgs.append({"role": "user", "content": (
                    "You already called this tool with the same arguments "
                    "earlier in this run. If its result is still in context, "
                    "use it instead of repeating; if it was compressed away "
                    "or you need fresh verification, re-running is fine.")})
        return True

    return _after_round


def _release_compacted_read_path(head_line: str) -> None:
    """Drop a compacted read_file header path from read-dedup / read-guard sets."""
    import re as _re_hdr
    m = _re_hdr.match(r"^(?:===\s*)?\[(.+?)\s+(?:total lines:|lines\s+\d+)", head_line.strip())
    if not m:
        return
    raw_p = m.group(1).strip()
    if not raw_p:
        return
    try:
        from context.compression import _drop_read_guard_paths
        _drop_read_guard_paths([raw_p])
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    try:
        from tools.runner import _read_signatures as _rs_map
        norm = raw_p.replace("\\", "/").rstrip("/").lower()
        for k in [k for k in list(_rs_map) if k.rstrip("/") == norm or k.rstrip("/").endswith("/" + norm)]:
            _rs_map.pop(k, None)
    except (ImportError, AttributeError, TypeError):
        pass


def make_tool_context_compactor(num_ctx: int = 8192, keep_recent_tools: int = 6):
    """Deterministic sliding tool-context compactor for ToolLoop (Direct / Tool-Agent).

    - Proactive calls (``round_num >= 0``) leave ``messages`` untouched while
      estimated tokens stay below 72% of ``num_ctx`` (preserving KV prefix cache).
    - Overflow retries (``round_num < 0``) or high-water rounds compact older
      tool outputs to 1-line summary stubs while keeping the newest tool outputs
      verbatim; if still over budget after stubbing (e.g. 200+ tool rounds),
      oldest middle tool-round groups are folded into a single progress note.
    """
    import json as _json
    from utils.token import estimate_ctx_tokens as _est_tok

    ctx_cap = max(2048, int(num_ctx or 8192))
    trigger_tok = max(1024, int(ctx_cap * 0.72))
    target_tok = max(768, int(ctx_cap * 0.56))

    async def _compact(messages: list[dict], round_num: int) -> list[dict]:
        if not messages or len(messages) < 4:
            return messages
        is_overflow = int(round_num) < 0
        est_before = int(_est_tok(messages))
        if not is_overflow and est_before < trigger_tok:
            return messages

        keep_n = min(3, max(1, keep_recent_tools)) if is_overflow else max(2, keep_recent_tools)
        tool_indices = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "tool"]
        protected_tools = set(tool_indices[-keep_n:]) if tool_indices else set()
        last_asst_idx = max(
            (i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "assistant"),
            default=-1,
        )

        out: list[dict] = []
        changed = False
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                out.append(msg)
                continue
            role = msg.get("role")
            if role == "tool" and idx not in protected_tools:
                content = str(msg.get("content") or "")
                if len(content) > 260 and not content.startswith("[Compacted tool output"):
                    first_ln = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")[:140]
                    tname = str(msg.get("name") or "tool")
                    if tname == "read_file":
                        _release_compacted_read_path(first_ln)
                    stub = (
                        f"[Compacted tool output ({tname}, {len(content)} chars): "
                        f"{first_ln} — call {tname} again if exact details are needed]"
                    )
                    out.append({**msg, "content": stub})
                    changed = True
                    continue
            elif role == "assistant" and idx != last_asst_idx and msg.get("tool_calls"):
                new_tcs = []
                tc_changed = False
                for tc in msg.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    raw_a = fn.get("arguments", "")
                    raw_s = raw_a if isinstance(raw_a, str) else _json.dumps(raw_a or {}, ensure_ascii=False)
                    if len(raw_s) > 1600:
                        try:
                            parsed_a = _json.loads(raw_s) if isinstance(raw_a, str) else dict(raw_a or {})
                        except (_json.JSONDecodeError, ValueError, TypeError):
                            parsed_a = {}
                        compact_a = {
                            k: (f"[{len(v)} chars compacted]" if isinstance(v, str) and len(v) > 200 else v)
                            for k, v in (parsed_a.items() if isinstance(parsed_a, dict) else ())
                        }
                        new_tcs.append({
                            **tc,
                            "function": {**fn, "arguments": _json.dumps(compact_a, ensure_ascii=False)},
                        })
                        tc_changed = True
                    else:
                        new_tcs.append(tc)
                if tc_changed:
                    out.append({**msg, "tool_calls": new_tcs})
                    changed = True
                    continue
            out.append(msg)

        est_mid = int(_est_tok(out))
        prev_chars = sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
        mid_chars = sum(len(str(m.get("content") or "")) for m in out if isinstance(m, dict))
        need_group_drop = est_mid > target_tok or (is_overflow and mid_chars >= int(prev_chars * 0.90))
        if not need_group_drop:
            return out if changed else messages

        # Group older (assistant_with_tool_calls + following tool_results) blocks
        groups: list[tuple[int, int, list[str]]] = []
        i = 0
        n = len(out)
        while i < n:
            m = out[i]
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
                j = i + 1
                labels: list[str] = []
                for tc in m.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    tn = str(fn.get("name") or "")
                    if tn:
                        labels.append(tn)
                while j < n and isinstance(out[j], dict) and out[j].get("role") == "tool":
                    j += 1
                if j > i + 1:
                    groups.append((i, j, labels))
                    i = j
                    continue
            i += 1

        if len(groups) <= 3:
            return out if changed else messages

        droppable = groups[:-3]
        drop_spans: list[tuple[int, int]] = []
        dropped_rounds = 0
        est_work = est_mid
        for gs, ge, _ in droppable:
            span_tok = int(_est_tok(out[gs:ge]))
            drop_spans.append((gs, ge))
            dropped_rounds += 1
            est_work -= span_tok
            if est_work <= target_tok and (not is_overflow or dropped_rounds >= 1):
                break

        if not drop_spans:
            return out if changed else messages

        drop_idx: set[int] = set()
        for gs, ge in drop_spans:
            drop_idx.update(range(gs, ge))

        note = f"[Compacted {dropped_rounds} earlier tool round(s) to fit context window; recent tool outputs remain intact.]"
        pruned: list[dict] = []
        note_inserted = False
        first_drop = drop_spans[0][0]
        for idx, m in enumerate(out):
            if idx == first_drop and not note_inserted:
                note_inserted = True
                if pruned and isinstance(pruned[-1], dict) and pruned[-1].get("role") == "user" and isinstance(pruned[-1].get("content"), str):
                    pruned[-1] = {**pruned[-1], "content": pruned[-1]["content"].rstrip() + "\n\n" + note}
                else:
                    pruned.append({"role": "user", "content": note})
            if idx in drop_idx:
                continue
            pruned.append(m)
        return pruned

    return _compact
