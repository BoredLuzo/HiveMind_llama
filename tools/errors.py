# -*- coding: utf-8 -*-
"""Tool error formatting and parsing (unified format A + B)."""
from __future__ import annotations

import json
import re


def tool_error_response(
    code: str,
    message: str,
    *,
    tool: str = "",
    mode: str = "",
    details: dict | None = None,
) -> str:
    """
    Format a tool error as a string that gets injected into the LLM context.
    Produces Format B (text): [TOOL_ERROR:CODE] tool (mode): message
    With optional JSON meta block for structured consumers.
    """
    base = f"[TOOL_ERROR:{code}]"
    if tool:
        base += f" {tool}"
        if mode:
            base += f" ({mode})"
    base += f": {message}"
    if details:
        try:
            base += f"\n[TOOL_ERROR_META] {json.dumps(details, ensure_ascii=False)}"
        except Exception:
            pass
    return base


def parse_tool_error(result: str) -> dict | None:
    """
    Extract structured error info from a tool result string.
    Handles BOTH Format A (JSON) and Format B (text).
    Returns dict with keys: code, tool, mode, message
    """
    if not result or not isinstance(result, str):
        return None
    txt = result

    # ── Format A: [TOOL_ERROR] {"error":{...}} ──
    prefix_a = "[TOOL_ERROR] "
    if txt.startswith(prefix_a):
        try:
            payload = json.loads(txt[len(prefix_a):].strip())
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            if isinstance(err, dict):
                return {
                    "code": str(err.get("code", "")),
                    "tool": str(err.get("tool", "") or ""),
                    "mode": str(err.get("mode", "") or ""),
                    "message": str(err.get("message", "") or "")[:500],
                }
        except (json.JSONDecodeError, AttributeError):
            pass
        return None

    # ── Format B: [TOOL_ERROR:CODE] tool (mode): message ──
    m = re.search(r"\[TOOL_ERROR:([^\]]+)\]", txt)
    if not m:
        return None
    code_part = m.group(1)
    code = code_part.strip()
    tool = ""
    mode = ""
    msg = ""
    rest = txt[txt.index("]") + 1:].strip()
    if ":" in rest:
        prefix_b, msg = rest.split(":", 1)
        prefix_b = prefix_b.strip()
        msg = msg.strip()
        # extract META block from the message (stays in the string, but not in 'message').
        _meta_i = msg.find("\n[TOOL_ERROR_META] ")
        if _meta_i >= 0:
            msg = msg[:_meta_i]
        paren_m = re.match(r"(\S+)(?:\s*\((\S+)\))?", prefix_b)
        if paren_m:
            tool = paren_m.group(1)
            mode = paren_m.group(2) or ""
        # READ-REQUIRED/BLOCKED-Format (tools/runner.py ~:467):
        #   "[TOOL_ERROR: READ_REQUIRED]\nAction: edit_file BLOCKED on '<path>'"
        # parse_tool_error las tool="Action" -> tool_call_failed(txt, "edit_file")
        if tool == "Action":
            _m2 = re.match(r"(edit_file|patch_file|write_file|write_file_append|replace_lines|read_file|search_code|find_files|list_dir|run_bash|run_tests|run_python|task_complete|git_commit)\b", msg)
            if _m2:
                tool = _m2.group(1)
    return {
        "code": code,
        "tool": tool,
        "mode": mode,
        "message": msg[:500],
    }


def tool_call_failed(result: str, tool_name: str | None = None) -> bool:
    """
    Check if a tool result indicates an error.
    Optionally match against a specific tool name.
    Handles both Format A and Format B.
    """
    txt = str(result or "")
    if tool_name and txt.startswith(f"[{tool_name} error"):
        return True
    if txt.startswith("[TOOL_ERROR") or "TOOL_ERROR" in txt[:80]:
        if tool_name:
            err = parse_tool_error(txt)
            if err and err.get("tool", "") not in ("", tool_name):
                return False
        return True
    return False


def tool_error_has_code(result: str, code: str, tool_name: str | None = None) -> bool:
    """
    Check if a tool error has a specific error code.
    Optionally match against a specific tool name.
    Handles both Format A and Format B.
    """
    err = parse_tool_error(result)
    if not err:
        txt = str(result or "")
        return f"[TOOL_ERROR:{code}" in txt
    if tool_name and err.get("tool", "") != tool_name:
        return False
    return err.get("code", "") == code
