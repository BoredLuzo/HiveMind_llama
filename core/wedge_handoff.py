# -*- coding: utf-8 -*-
"""WEDGE-HANDOFF (2026-09-17): fresh-agent handoff for wedging edits.

A "wedge" is a coding agent repeating a failing edit on the SAME file without
progress. Detection is deliberately precise (no gut-feeling heuristics):

  (a) STREAK   — N consecutive failed edit-family calls on the same
                 normalized path (default N=4, settings key
                 duo_wedge_handoff_streak_threshold). The streak resets ONLY
                 on a SUCCESSFUL write to that path. A fresh read_file alone
                 never resets: re-read + retry of the same wrong edit IS the
                 wedge pattern.
  (b) REPEAT   — the exact same call (tool + md5 of serialized args) fails a
                 SECOND time within the current streak (not necessarily
                 consecutively — "twice somewhere in the streak, no
                 successful write in between"). A byte-identical retry of an
                 already-failed call is a certain wedge: no missing
                 information can arrive by repeating it.

This module holds only pure detection/format logic (importable in tests
without the llama backend). The executor (core/tool_executor.py) feeds
failures/successes; duo_runner enforces the handoff POLICY (budget, timeout)
and runs the fix agent (core/fix_agent.py).
"""
from __future__ import annotations

import hashlib
import os

# The write/edit tools whose repetition defines a wedge. replace_lines /
# edit_ast are legacy (not advertised) and excluded on purpose.
EDIT_FAMILY_TOOLS = ("edit_file", "patch_file", "write_file", "write_file_append")

_HISTORY_PER_PATH = 3        # handover carries the LAST 3 failed attempts
_RESULT_CAP_CHARS = 220      # per-attempt error snippet cap in the state
_MAX_SENTINEL = 99           # streak cap for logging sanity


def new_wedge_state(threshold: int = 4) -> dict:
    """Fresh per-chunk detection state (duo_runner creates one per chunk and
    passes the SAME dict into every ToolRoundState, like attempts_per_file)."""
    return {
        "threshold": max(2, int(threshold)),
        "streaks": {},      # norm_path -> consecutive failure count
        "fail_hashes": {},  # norm_path -> set(md5) of failed calls in streak
        "history": {},      # norm_path -> list of {"tool","result"} (last 3)
    }


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path or ""))).replace("\\", "/")


def _args_hash(tool: str, raw_args) -> str:
    return hashlib.md5((str(tool) + "|" + str(raw_args or "")).encode("utf-8", "replace")).hexdigest()


def note_edit_failure(ws: dict, tool: str, path: str, raw_args, result: str) -> bool:
    """Record a failed edit-family call. Returns True when this exact call
    (tool + args hash) had ALREADY failed earlier in the current streak —
    the immediate wedge signal (rule b)."""
    key = _norm(path)
    h = _args_hash(tool, raw_args)
    seen = h in ws["fail_hashes"].get(key, set())
    ws["streaks"][key] = min(_MAX_SENTINEL, ws["streaks"].get(key, 0) + 1)
    ws["fail_hashes"].setdefault(key, set()).add(h)
    hist = ws["history"].setdefault(key, [])
    hist.append({"tool": str(tool), "result": str(result or "")[:_RESULT_CAP_CHARS]})
    del hist[:-_HISTORY_PER_PATH]
    return seen


def note_edit_success(ws: dict, path: str) -> None:
    """Successful write on the path: full reset of streak, hashes, history."""
    key = _norm(path)
    ws["streaks"].pop(key, None)
    ws["fail_hashes"].pop(key, None)
    ws["history"].pop(key, None)


def detect_wedge(ws: dict, path: str, repeat_failure: bool) -> bool:
    """Wedge decision: identical-call repeat OR streak >= threshold."""
    if repeat_failure:
        return True
    return ws["streaks"].get(_norm(path), 0) >= int(ws.get("threshold", 4))


def streak_of(ws: dict, path: str) -> int:
    return ws["streaks"].get(_norm(path), 0)


def attempts_of(ws: dict, path: str) -> list:
    return list(ws["history"].get(_norm(path), []))


def build_handover(*, path: str, intent: str, attempts: list,
                   streak: int, threshold: int) -> str:
    """Compact handover for the fix agent. Deliberately NO file content: the
    stale read basis is the disease — the fix agent must read_file fresh."""
    att = attempts[-_HISTORY_PER_PATH:]
    lines = [
        "[WEDGE-HANDOFF] Repair delegation for ONE wedged edit.",
        "",
        f"File: {path}",
        f"Intent: {intent.strip() or '(unspecified — infer it from the file and the failed attempts)'}",
        "",
        f"The coder failed this edit {int(streak)}x in a row (threshold {int(threshold)}).",
    ]
    if att:
        lines.append("Failed attempts (last 3):")
        for i, a in enumerate(att, 1):
            res = str(a.get("result", "")).replace("\n", " ")[:200]
            lines.append(f"  {i}) {a.get('tool', '?')} => {res}")
    else:
        lines.append("Failed attempts: (none recorded)")
    lines += [
        "",
        "Rules:",
        "- read_file the target FRESH. Do NOT copy old_text from the failed",
        "  attempts above — those are exactly the texts that did not match.",
        "- Minimal fix: change only what the intent requires. No reformatting.",
        "- Preserve the file's line endings (CRLF stays CRLF).",
        "- old_text must be copied VERBATIM from YOUR OWN read_file and appear",
        "  exactly once in the file.",
        "- Do not run the project, do not commit — repair the edit only.",
        "",
        "End your final reply with exactly one line:",
        "VERDICT: FIXED — <what you changed>",
        "VERDICT: NO_FIX_NEEDED — <why the file already satisfies the intent>",
        "VERDICT: FAILED — <what blocks you>",
    ]
    return "\n".join(lines)


def build_resolved_notice(path: str, summary: str, no_fix: bool = False) -> str:
    summary = str(summary or "").strip()[:400] or "(no summary returned)"
    if no_fix:
        return (
            f"[WEDGE-HANDOFF-NO_FIX_NEEDED] {path}: a fresh-context repair agent "
            f"verified the file already satisfies the intent: {summary} "
            "Re-examine what the edit was supposed to achieve before editing "
            "this file again."
        )
    return (
        f"[WEDGE-HANDOFF-RESOLVED] {path} was repaired by a fresh-context "
        f"agent: {summary} The file changed on disk — read_file it again "
        "before any further edit on it."
    )


def build_escalated_notice(path: str, reason: str) -> str:
    reason = str(reason or "").strip()[:400] or "fix agent failed"
    return (
        f"[WEDGE-HANDOFF-FAILED] {path}: the repair agent could not fix the "
        f"wedged edit: {reason} The run is stopping — the handover is "
        "preserved for human review."
    )


def invalidate_read_signature(path: str, workspace: str) -> bool:
    """Drop the coder's read signature for the path so the next edit after a
    fix-agent change fails CLEARLY with the stale note (forcing a fresh
    read_file) instead of fuzzy-matching a stale basis."""
    try:
        from pathlib import Path as _P
        from tools import runner as _tr
        from utils.file import normalize_tool_path as _ntp
        fp = _P(path)
        if not fp.is_absolute():
            fp = _P(workspace) / fp
        fp = fp.resolve()
        return _tr._read_signatures.pop(_ntp(str(fp), _P(workspace)), None) is not None
    except Exception:
        return False
