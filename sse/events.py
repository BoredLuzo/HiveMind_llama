"""SSE-Event-Builder (aus server.py extrahiert)."""

import json
import re


def make_tool_call_event(name: str, args: dict) -> dict:
    """Build a structured tool_call SSE event — shown as a chip in the UI."""
    """Build a structured tool_call SSE event — shown as a chip in the UI.
    label  = the single most informative arg (path / cmd / query / url).
    detail = optional secondary info (line count, truncated cmd tail, …).
    extra  = dict of additional k/v pairs shown in the expanded info tab.
    """
    label = ""
    detail = ""
    extra: dict = {}

    if name in ("patch_file", "read_file", "edit_file"):
        label = str(args.get("path", "") or args.get("file", "")).strip()
        if name == "patch_file":
            old = str(args.get("old_str", ""))
            new = str(args.get("new_str", ""))
            old_lines = old.count("\n") + 1 if old.strip() else 0
            new_lines = new.count("\n") + 1 if new.strip() else 0
            detail = old.strip().splitlines()[0][:50] if old.strip() else ""
            if old_lines:
                extra["old_lines"] = old_lines
            if new_lines:
                extra["new_lines"] = new_lines
            extra["net"] = f"{new_lines - old_lines:+d} lines" if old_lines and new_lines else ""
        elif name == "read_file":
            start = args.get("start_line")
            end   = args.get("end_line")
            if start is not None or end is not None:
                extra["range"] = f"L{start or 1}–{end or '∞'}"
        elif name == "edit_file":
            extra["mode"] = str(args.get("mode", "")).strip() or "replace"
            _edits = str(args.get("edits", "") or "")
            if _edits:
                from utils.patterns import _RE_SEARCH_REPLACE_BLOCK, _RE_SEARCH_REPLACE_BLOCK_LENIENT
                _eb = _RE_SEARCH_REPLACE_BLOCK.findall(_edits) or _RE_SEARCH_REPLACE_BLOCK_LENIENT.findall(_edits)
                if _eb:
                    _old, _new = _eb[0]
                    _old_lines = _old.count("\n") + 1 if _old.strip() else 0
                    _new_lines = _new.count("\n") + 1 if _new.strip() else 0
                    if _old.strip():
                        detail = _old.strip().splitlines()[0][:50]
                    if len(_eb) > 1:
                        extra["blocks"] = len(_eb)
                    if _old_lines and _new_lines:
                        extra["net"] = f"{_new_lines - _old_lines:+d} lines"

    elif name == "run_bash":
        cmd = str(args.get("cmd", "")).strip()
        label = cmd[:300] + ("…" if len(cmd) > 300 else "")
        extra["cmd"] = cmd  # full command (no truncation) for expanded view
        timeout = args.get("timeout")
        if timeout:
            extra["timeout"] = f"{timeout}s"

    elif name == "web_search":
        label = str(args.get("query", "")).strip()[:300]
        extra["query"] = str(args.get("query", "")).strip()
        num = args.get("num_results") or args.get("n")
        if num:
            extra["num_results"] = num

    elif name == "web_fetch":
        url = str(args.get("url", "")).strip()
        label = url[:300] + ("…" if len(url) > 300 else "")
        extra["url"] = url  # full URL for expanded view

    elif name == "find_files":
        label = str(args.get("pattern", "") or args.get("glob", "")).strip()
        path  = str(args.get("path", "") or args.get("dir", "")).strip()
        if path and path != ".":
            detail = path
            extra["search_root"] = path
        extra["pattern"] = label

    elif name == "get_signatures":
        label = str(args.get("path", "")).strip()
        if args.get("max_items"):
            extra["max_items"] = args.get("max_items")

    elif name == "find_references":
        label = str(args.get("symbol", "")).strip()
        path = str(args.get("path", "") or "").strip()
        if path:
            detail = path
            extra["path"] = path
        if args.get("max_items"):
            extra["max_items"] = args.get("max_items")

    elif name == "list_dir":
        label = str(args.get("path", "")).strip() or "."

    elif name == "search_code":
        label = str(args.get("pattern", "")).strip()
        path = str(args.get("path", "") or "").strip()
        if path and path != ".":
            detail = path
            extra["search_root"] = path

    elif name in ("write_file", "write_file_append"):
        label = str(args.get("path", "")).strip()
        content = str(args.get("content", "") or "")
        lines = content.count("\n") + 1 if content.strip() else 0
        if lines:
            detail = f"{lines} lines"
            extra["lines"] = lines

    elif name == "replace_lines":
        label = str(args.get("path", "")).strip()
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None and end is not None:
            detail = f"L{start}–{end}"

    elif name == "edit_ast":
        label = str(args.get("target_name", "")).strip()
        path = str(args.get("path", "")).strip()
        ttype = str(args.get("target_type", "")).strip()
        if path:
            detail = path
            extra["path"] = path
        if ttype:
            extra["target_type"] = ttype

    elif name == "git_status":
        label = str(args.get("cmd", "")).strip()

    elif name == "git_commit":
        label = str(args.get("message", "")).strip()
        ws = str(args.get("workspace", "") or "").strip()
        if ws:
            extra["workspace"] = ws

    elif name == "run_python":
        code = str(args.get("code", "")).strip()
        label = code[:300] + ("…" if len(code) > 300 else "")
        extra["code"] = code

    elif name == "install_package":
        manager = str(args.get("manager", "")).strip()
        packages = str(args.get("packages", "")).strip()
        label = f"{manager}: {packages}"
        if args.get("dev"):
            extra["dev"] = True

    elif name == "task_complete":
        status = str(args.get("status", "")).strip()
        build_status = ""
        completed = []
        blockers = []
        try:
            sj = json.loads(status)
            build_status = str(sj.get("build_status") or "").strip()
            completed = sj.get("completed") or []
            blockers = sj.get("blockers") or []
        except Exception:
            _m = re.search(r'"(?:build_status|status)"\s*:\s*"(\w+)"', status)
            if _m:
                build_status = _m.group(1)
        label = build_status or "done"
        if completed:
            extra["completed"] = f"{len(completed)} items"
        if blockers:
            extra["blockers"] = f"{len(blockers)} items"

    elif name == "ask_user":
        label = str(args.get("question", "")).strip()[:300]

    elif name == "browser":
        label = str(args.get("action", "")).strip()
        url = str(args.get("url", "") or "").strip()
        sel = str(args.get("selector", "") or "").strip()
        if url:
            extra["url"] = url
        if sel:
            extra["selector"] = sel

    elif name == "start_background":
        cmd = str(args.get("cmd", "")).strip()
        label = cmd[:300] + ("…" if len(cmd) > 300 else "")
        extra["cmd"] = cmd

    elif name == "get_background_output":
        label = str(args.get("handle", "") or "").strip() or "list all"

    elif name == "stop_background":
        label = str(args.get("handle", "")).strip()

    elif name == "undo_last":
        label = str(args.get("path", "")).strip() or "all files"

    elif name == "run_tests":
        lang = str(args.get("lang_override", "") or "").strip()
        label = lang or "auto"
        if args.get("timeout"):
            extra["timeout"] = f"{args.get('timeout')}s"

    else:
        # Generic: expose all args in extra
        first_val = next(iter(args.values()), "") if args else ""
        label = str(first_val).strip()[:300]
        for k, v in args.items():
            extra[k] = str(v)[:120]

    # Remove empty extra values to keep payload lean
    extra = {k: v for k, v in extra.items() if v not in (None, "", 0)}

    return {"type": "tool_call", "name": name, "label": label, "detail": detail, "extra": extra}


def make_tool_result_event(name: str, result: str) -> dict:
    """Build a tool_result SSE event shown as collapsible output under the chip."""
    """Build a tool_result SSE event shown as collapsible output under the chip.
    ok=True  → green tint   (success / content returned)
    ok=False → red tint     (error / TOOL_ERROR prefix)
    text     → smart-trimmed preview of the result
    full     → full result for the expandable section (capped at 4000 chars)
    """
    ok = not (
        result.startswith("[TOOL_ERROR")
        or result.startswith("[patch_file error")
        or result.startswith("[edit_file error")
        or result.startswith("[run_bash error")
        or result.startswith("[JSON parse error")
        or "TOOL_ERROR" in result[:60]
    )

    # Per-tool preview: what the user actually cares about
    if name == "run_bash":
        # Last N lines of bash output — errors are at the bottom
        lines = result.strip().splitlines()
        preview_lines = lines[-12:] if len(lines) > 12 else lines
        text = "\n".join(preview_lines)
    elif name == "read_file":
        # First few lines of file content
        lines = result.strip().splitlines()
        preview_lines = lines[:8]
        text = "\n".join(preview_lines)
        if len(lines) > 8:
            text += f"\n… ({len(lines)} lines total)"
    elif name in ("write_file_append", "patch_file", "edit_file"):
        # These return short status strings already
        text = result.strip()[:200]
    elif name == "web_search":
        # First ~300 chars of search results
        text = result.strip()[:300]
        if len(result) > 300:
            text += "…"
    elif name == "web_fetch":
        lines = result.strip().splitlines()
        preview_lines = lines[:10]
        text = "\n".join(preview_lines)
        if len(lines) > 10:
            text += f"\n… ({len(lines)} lines)"
    elif name == "find_files":
        text = result.strip()[:600]  # more room for find_files with many matches
    else:
        text = result.strip()[:300]

    # BUG-3 FIX: full must also be stripped so frontend _trShort check
    # (text === full) works correctly for short status strings like
    # "✅ Written: src/main.py". Unstripped full never equals stripped text.
    _full = result.strip()[:4000]

    return {
        "type": "tool_result",
        "name": name,
        "ok": ok,
        "text": text,
        "full": _full,
    }


