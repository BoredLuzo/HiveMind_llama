"""Tool handlers: file read/write/edit tools (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.patterns import _RE_SEARCH_REPLACE_BLOCK, _RE_SEARCH_REPLACE_BLOCK_LENIENT
from utils.file import fuzzy_resolve_path as _fuzzy_resolve_path, _inline_resolve_path, _inline_check_workspace
from tools.errors import tool_error_response as _tool_error_response
import asyncio
from tools.workspace import get_transaction
import json
import os
import re
import sys
import tempfile
import time

from . import _shared

_JSON_EDIT_KEY_NEW = ("new_str", "new_string", "replace")

_JSON_EDIT_KEY_OLD = ("old_str", "old_string", "search")

from .linting import _auto_lint_result
from .exec_tools import _stage_split

# ── Server-side AUTO-SPLIT remainder store (2026-09-03) ───────────────────────
# When a single write_file / edit_file / write_file_append call exceeds the
# per-call char limit, the leading chunk is written immediately and the
# REMAINDER is cached here. The model only sends a tiny continuation marker
# (content="<AUTO_SPLIT_CONTINUE>") and the rest is appended server-side - no
# regeneration / reproduction of the big content, no 7-minute total loss.
AUTO_SPLIT_CONTINUE_MARKER = "<AUTO_SPLIT_CONTINUE>"
_PENDING_TTL_S = 300.0          # 5 minutes
_PENDING_MAX_ENTRIES = 8
_PENDING_MAX_CHARS = 250_000
_pending_splits: dict[str, dict] = {}


def _split_key(path) -> str:
    """Kanonischer Key fuer Pending-Entries (Pfad-Varianten vereinheitlichen)."""
    return os.path.normcase(os.path.normpath(str(path)))


def _looks_like_split_marker(content) -> bool:
    """Erkennt den AUTO-SPLIT-Continue-Marker robust.

    Modelle kopieren die Anweisung inkl. Anfuehrungszeichen in den Content
    (content = "\\"<AUTO_SPLIT_CONTINUE>\\""), was einen strikten Vergleich
    scheitern liess und den Marker LITERAL in die Datei schrieb. Akzeptiert:
    - exakt der nackte Marker (nach strip)
    - mit umschliessenden ' / " / ` Quotes
    - kurze Antworten (<=64 Zeichen), die den Marker enthalten
    """
    c = str(content or "").strip()
    if not c:
        return False
    c = c.strip('"\'`')
    if not c:
        return False
    if c == AUTO_SPLIT_CONTINUE_MARKER:
        return True
    if len(c) <= 64 and AUTO_SPLIT_CONTINUE_MARKER in c:
        return True
    return False


def _heal_trailing_split_marker(text: str, content: str) -> str:
    """Entfernt am Datei-Ende literal geschriebene Marker-Zeilen.

    War der Marker vor dem Fix einmal als normaler Append gelandet, steht er
    wörtlich (ggf. mit Quotes) am File-Ende. Vor dem Drain wird er entfernt,
    damit der Rest sauber an den echten Teil1 anschliesst.
    """
    if content and text.endswith(content):
        return text[:-len(content)]
    for _q in ('"', "'", '`'):
        _v = _q + AUTO_SPLIT_CONTINUE_MARKER + _q
        if text.endswith(_v):
            return text[:-len(_v)]
    if text.endswith(AUTO_SPLIT_CONTINUE_MARKER):
        return text[:-len(AUTO_SPLIT_CONTINUE_MARKER)]
    return text


def _prune_pending_splits() -> None:
    _now = time.time()
    for _k in [k for k, _v in _pending_splits.items()
               if (_now - float(_v.get("ts", 0.0))) > _PENDING_TTL_S]:
        _pending_splits.pop(_k, None)


def _store_pending_remainder(path: str, content: str, written_chars: int) -> None:
    _prune_pending_splits()
    while len(_pending_splits) >= _PENDING_MAX_ENTRIES:
        _oldest = min(_pending_splits, key=lambda k: float(_pending_splits[k].get("ts", 0.0)))
        _pending_splits.pop(_oldest, None)
    _remainder = str(content)[written_chars:]
    _truncated = False
    if len(_remainder) > _PENDING_MAX_CHARS:
        _remainder = _remainder[:_PENDING_MAX_CHARS]
        _truncated = True
    # DEFERRED import: tools.runner imports this package top-level
    from tools.runner import _current_run_id as _cv
    _owner_run = _cv.get(None)
    _pending_splits[_split_key(path)] = {
        "content": _remainder,
        "ts": time.time(),
        "total": len(content),
        "truncated": _truncated,
        "run_id": _owner_run,
    }


def _auto_split_instr(path, written_chars: int, total_chars: int) -> str:
    _remaining = max(0, total_chars - written_chars)
    return (
        f"[AUTO-SPLIT] '{path}' got the FIRST {written_chars} of {total_chars} chars. "
        f"REMAINING {_remaining} chars are stored server-side.\n"
        f"Call NOW to finish it automatically:\n"
        f"  write_file_append(path, content={AUTO_SPLIT_CONTINUE_MARKER})\n"
        f"content MUST be the bare token {AUTO_SPLIT_CONTINUE_MARKER} - WITHOUT any "
        f"quotes. Never resend the content and never wrap the token in quotes."
    )


def _atomic_write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
            _f.write(text)
        os.replace(_tmp_path, str(p))
    except Exception:
        try:
            os.unlink(_tmp_path)
        except Exception:
            pass
        raise


def _read_binary_error(p) -> str:
    """READ-FIX helper: konsistente Binary-Fehlerantwort (sys.platform-aware)."""
    _inspect_hint = (
        "Use run_bash with 'Get-Content -Encoding Byte {p} | Select-Object -First 64' "
        "to inspect binary content."
        if sys.platform == "win32"
        else "Use run_bash with 'file {p}' to identify the file type, "
             "or 'xxd {p} | head' to inspect binary content."
    )
    return _tool_error_response(
        "BINARY_FILE",
        f"'{p}' appears to be a binary file (image, compiled binary, archive, etc.) "
        "and cannot be read as text. "
        + _inspect_hint,
        tool="read_file")


def _read_head_bytes(p: Path, n: int = 1024) -> bytes:
    """READ-FIX helper: liest nur den Dateianfang (kein Voll-Read)."""
    try:
        with open(p, "rb") as f:
            return f.read(n)
    except Exception:
        return b""


def _looks_binary_bytes(b: bytes) -> bool:
    """READ-FIX helper: Binary-Sniff auf Bytes (NUL + Steuerzeichen)."""
    if not b:
        return False
    _binary = sum(1 for c in b if c == 0 or (c < 32 and c not in (10, 13, 9)))
    return (_binary / len(b)) > 0.10


def _fast_newline_count(p: Path, cap: int = 401) -> tuple:
    """READ-FIX helper: gestreamter Newline-Count.

    Bricht bei cap ab -> liefert (cap, True). Liefert (count, False) sonst.
    Verhindert, dass eine grosse Datei nur zum Zaehlen komplett als String+Liste
    in den Speicher geladen wird.
    """
    _count = 0
    try:
        with open(p, "rb") as f:
            while True:
                _chunk = f.read(1 << 16)
                if not _chunk:
                    break
                _count += _chunk.count(b"\n")
                if _count >= cap:
                    return cap, True
    except Exception:
        return 0, False
    return _count, False


async def _inline_tool_read_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "read_file"):
        return err
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    _full = start_line is None and end_line is None
    try:
        # READ-EARLY-ABORT (2026-09-04): bei Voll-Reads (kein Range) wird eine
        # grosse Datei NICHT mehr komplett eingelesen, nur um sie dann als "too
        # large" abzulehnen. Staette dessen: billiger Binary-Sniff am Anfang +
        # gestreamter Newline-Count (bricht bei Zeile 401 ab).
        if _full:
            _head = await asyncio.to_thread(_read_head_bytes, p, 1024)
            if _looks_binary_bytes(_head):
                return _read_binary_error(p)
            _nl, _capped = await asyncio.to_thread(_fast_newline_count, p, 401)
            if _nl > 400:
                _sig_fn = _shared.get_signatures_report if callable(_shared.get_signatures_report) else None
                _sig = await asyncio.to_thread(_sig_fn, p, 200) if _sig_fn else (
                    "(signature extraction unavailable) — use read_file with line_range=[1,50] to inspect the file header first."
                )
                _disp = f"{_nl}+" if _capped else str(_nl)
                return _tool_error_response(
                    "FILE_TOO_LARGE_NEED_RANGE",
                    f"File '{p}' is too large ({_disp} lines) to read fully into limited context. "
                    "Please use 'start_line' and 'end_line' inside read_file to read specific sections.\n\n"
                    f"File Outline / Signatures:\n{_sig}",
                    tool="read_file" )

        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)

        # Binary file detection (Range-Reads; Voll-Reads wurden oben schon per
        # Head-Sniff abgefangen). Check first 512 chars for NUL bytes and
        # control characters (excluding \n \r \t).
        _sample = content[:512]
        if _sample:
            _binary_chars = sum(
                1 for c in _sample
                if ord(c) == 0 or (ord(c) < 32 and c not in "\n\r\t")
            )
            if len(_sample) > 0 and (_binary_chars / len(_sample)) > 0.10:
                return _read_binary_error(p)

        # Hinweis: Voll-Reads mit >400 Zeilen werden oben frueh abgebrochen
        # (READ-EARLY-ABORT) - hier ist len(lines) <= 400 garantiert.

        # Hallucination guard: validate start_line/end_line against actual file length
        _actual_lines = len(lines)
        if start_line is not None and int(start_line) > _actual_lines:
            return _tool_error_response(
                "HALLUCINATION_GUARD",
                f"start_line={start_line} exceeds file length ({_actual_lines} lines). "
                f"Call read_file('{args.get('path')}') WITHOUT start_line/end_line to read the full file.",
                tool="read_file")
        if end_line is not None and int(end_line) > _actual_lines * 2:
            end_line = _actual_lines

        if start_line is not None or end_line is not None:
            s = max(0, int(start_line) - 1) if start_line else 0
            e = int(end_line) if end_line else len(lines)
            content_chunk = "".join(lines[s:e])
            _chunk = content_chunk[:32000]
            if len(content_chunk) > 32000:
                _chunk += f"\n[TRUNCATED: {len(content_chunk)-32000} more chars omitted — use smaller line range]"
            return f"[{p} lines {s+1}-{min(e, len(lines))} / {len(lines)}]\n" + _chunk
        
        _MAX_READ_LINES = 200
        if len(lines) > _MAX_READ_LINES:
            _chunk = "".join(lines[:_MAX_READ_LINES])
            _chunk += f"\n[FILE TRUNCATED — {len(lines)} lines total. Use search_code for specific sections.]"
            return f"[{p} total lines: {len(lines)}]\n" + _chunk
        _chunk = content[:32000]
        if len(content) > 32000:
            _chunk += f"\n[TRUNCATED: {len(content)-32000} more chars omitted — use start_line/end_line to read sections]"
        return f"[{p} total lines: {len(lines)}]\n" + _chunk
    except Exception as e:
        return _tool_error_response(
            "FILE_READ_FAILED",
            f"Failed to read file '{p}': {e} — try list_dir(parent_directory) to check if the file exists, or find_files(pattern) to locate it.",
            tool="read_file" )


async def _inline_tool_write_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Backward compat alias — delegates to unified edit_file handler."""
    return await _inline_tool_edit_file(
        {"path": args.get("path", ""), "edits": args.get("content", ""),
         "__model__": args.get("__model__", ""),
         "__allow_overwrite__": args.get("allow_overwrite", False),
         "_tool_name": "write_file"},
        workspace, workspace_lock)


async def _inline_tool_write_file_append(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    target = workspace / args.get("path", "")
    if not target.exists():
        return _tool_error_response(
            "FILE_NOT_FOUND",
            "write_file_append requires an EXISTING file — create it with write_file first.",
            tool="write_file_append")
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "write_file_append"):
        return err
    content = str(args.get("content", ""))
    get_transaction().capture_before(p)

    # ── AUTO-SPLIT continuation marker ──
    # The remainder of an oversized write_file/edit_file call is stored
    # server-side (see AUTO_SPLIT_CONTINUE_MARKER). This marker appends it in
    # one go without the model re-sending the big content.
    # FIX (2026-09-04): Modelle kopieren die Quotes in den Content
    # (content="<AUTO_SPLIT_CONTINUE>") -> strikter Vergleich schlug fehl und
    # der Marker landete LITERAL in der Datei. Detektion ist jetzt robust
    # (Quotes/Backticks/kurze Zusatztexte); ein Pending wird beim naechsten
    # Append gedrained, nie literal geschrieben.
    _split_key_ = _split_key(p)
    _looks_marker = _looks_like_split_marker(content)
    _split_discard_note = ""
    _prune_pending_splits()
    _pending_now = _pending_splits.get(_split_key_)
    from tools.runner import _current_run_id as _current_run_id_cv

    if _looks_marker or _pending_now is not None:
        if _looks_marker:
            _entry = _pending_splits.pop(_split_key_, None)
            if not _entry or not str(_entry.get("content", "")):
                return _tool_error_response(
                    "AUTO_SPLIT_NO_PENDING",
                    f"[AUTO-SPLIT] No pending remainder found for '{p}'. "
                    "read_file the end of the file to see what is already written, then "
                    "finish it with normal chunks (write_file_append) or edit_file "
                    "SEARCH/REPLACE blocks - do NOT send a full rewrite of the whole file.",
                    tool="write_file_append")
            _rest = str(_entry["content"])
            _rest_truncated = bool(_entry.get("truncated"))
            try:
                def _drain() -> int:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _cur = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        _cur = ""
                    # Heilung: ggf. wörtlich geschriebene Marker-Zeile(n) vom
                    # File-Ende entfernen, damit der Rest sauber anschliesst.
                    _healed = _heal_trailing_split_marker(_cur, content)
                    if _healed != _cur:
                        with open(p, "w", encoding="utf-8", newline="") as f:
                            f.write(_healed)
                    with open(p, "a", encoding="utf-8", newline="") as f:
                        f.write(_rest)
                    return p.stat().st_size
                _total = await asyncio.to_thread(_drain)
            except Exception as e:
                return _tool_error_response(
                    "WRITE_FILE_APPEND_FAILED",
                    f"write_file_append (AUTO-SPLIT drain) failed for '{p}': {e}",
                    tool="write_file_append")
            _lines = _rest.count("\n") + 1
            if _rest_truncated:
                return _tool_error_response(
                    "AUTO_SPLIT_REMAINDER_CAPPED",
                    f"[AUTO-SPLIT] '{p}' was completed only PARTIALLY: the stored "
                    f"remainder exceeded the {_PENDING_MAX_CHARS} char server cache and "
                    "was cut. The file is now TRUNCATED at that point. Do NOT send "
                    "<AUTO_SPLIT_CONTINUE> again (the cache is gone). Read the end of "
                    "the file and reconstruct the missing tail (edit_file / "
                    "write_file_append).",
                    tool="write_file_append")
            return (f"[AUTO-SPLIT-DONE] '{p}' completed: +{_lines} lines, "
                    f"{_total} bytes total (full content written).")
        # Pending existiert, aber der Content ist KEIN Marker: das Modell ist
        # weitergezogen / resendet selbst - alter Remainder ist veraltet.
        # EMPTY-CONTENT-GUARD (2026-09-13): empty content must NOT pop the
        # pending remainder (old order dropped it first, errored after —
        # file stayed truncated with the remainder unrecoverable).
        # PENDING-GUARD (2026-09-15): a self-made continuation chunk used to
        # SILENTLY DESTROY the pending remainder — part1 stayed truncated and
        # only the model's small chunk landed ("append added just one line").
        # The remainder is the authoritative tail of the original content; the
        # model's chunk would duplicate/misalign with it. Reject instead.
        # CROSS-RUN-STALE (2026-09-15): a remainder owned by a DIFFERENT (dead)
        # run must not block this run's legitimate appends — drop it silently.
        if (_pending_now is not None
                and _pending_now.get("run_id")
                and _pending_now.get("run_id") != _current_run_id_cv.get(None)):
            import logging as _lg_fa
            _lg_fa.getLogger("hivemind.tools").debug(
                "[AUTO-SPLIT] stale pending from run %s dropped (current run %s)",
                _pending_now.get("run_id"), _current_run_id_cv.get(None))
            _pending_splits.pop(_split_key_, None)
            _pending_now = None

        if content and _pending_now is not None:
            # ABANDON-VERB (2026-09-15): the model can explicitly drop the
            # stored remainder when it has genuinely moved on.
            _cand_abandon = str(content).strip().strip("\"'`")
            if _cand_abandon == "<AUTO_SPLIT_ABANDON>":
                _pending_splits.pop(_split_key_, None)
                return (f"[AUTO-SPLIT-ABANDONED] '{p}': stored remainder dropped. "
                        "The file currently ends with part1 of the old (truncated) "
                        "write. read_file the end of it before appending, so you "
                        "know exactly where it stands.")
            _pend_chars = len(str(_pending_now.get("content", "")))
            # INSIST-ESCALATION (2026-09-16): a substantial non-marker chunk on
            # the 2nd consecutive attempt means the model will not use the
            # marker — rejecting forever just wedges the run (live spark:
            # append chunks of 6-9k chars rejected in a loop). On the 2nd
            # attempt: discard the remainder and let the chunk append.
            if len(str(content)) >= 500:
                _pending_now["refusals"] = int(_pending_now.get("refusals", 0)) + 1
                if _pending_now["refusals"] >= 2:
                    _pending_splits.pop(_split_key_, None)
                    _pending_now = None
                    _split_discard_note = (" [stored remainder discarded — read_file "
                                           "the file end to verify completeness]")
            if content and _pending_now is not None:
                return _tool_error_response(
                    "AUTO_SPLIT_PENDING",
                    f"[AUTO-SPLIT] '{p}' still has a {_pend_chars}-char remainder stored "
                    "server-side. Do NOT write your own continuation — it would duplicate "
                    "or misalign with the stored tail. Two options:\n"
                    f"1. Finish the intended content: write_file_append(path, content={AUTO_SPLIT_CONTINUE_MARKER})"
                    " (bare marker token, no quotes).\n"
                    "2. Abandon the stored remainder (you rewrote your plan): "
                    "write_file_append(path, content=<AUTO_SPLIT_ABANDON>) — then read_file "
                    "the end of the file and append fresh content.",
                    tool="write_file_append")
        if content:
            _pending_splits.pop(_split_key_, None)
        else:
            return _tool_error_response(
                "EDIT_FILE_EMPTY_EDITS",
                "write_file_append requires non-empty content. If you meant to "
                "finish an auto-split rewrite, send the bare marker "
                + AUTO_SPLIT_CONTINUE_MARKER + " as content.",
                tool="write_file_append")

    # Normal append: the content already arrived in full, so append it in one
    # go (no artificial chunking - splitting would only force the model to
    # regenerate the same content again).
    # NEWLINE-BOUNDARY (2026-09-15): if the file does not end with a newline
    # and the chunk does not start with one, the first appended line GLUED
    # onto the last existing line (looked like "append added only one line").
    try:
        _boundary = {"note": ""}
        def _append_and_size() -> int:
            p.parent.mkdir(parents=True, exist_ok=True)
            _head = p.read_text(encoding="utf-8", errors="replace", newline="") if p.stat().st_size else ""
            _body = content
            if _head and not _head.endswith("\n") and not _body.startswith("\n"):
                _body = "\n" + _body
                _boundary["note"] = " (newline boundary inserted)"
            with open(p, "a", encoding="utf-8", newline="") as f:
                f.write(_body)
            return p.stat().st_size

        total = await asyncio.to_thread(_append_and_size)
        _appended = ("\n" + content) if _boundary["note"] else content
        lines = _appended.count("\n")
        _lint = await _auto_lint_result(p, workspace)
        return f"[Appended: {p} (+{lines} lines, total {total} bytes)]{_boundary['note']}{_split_discard_note}{_lint}"
    except Exception as e:
        return _tool_error_response(
            "WRITE_FILE_APPEND_FAILED",
            f"write_file_append failed for '{p}': {e}",
            tool="write_file_append" )


async def _inline_tool_patch_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "patch_file"):
        return err
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")
    get_transaction().capture_before(p)
    if not old_str:
        return _tool_error_response(
            "PATCH_FILE_EMPTY_OLD_STR",
            "patch_file requires non-empty old_str.",
            tool="patch_file" )
    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")  # CRLF-PRESERVE (2026-09-13)
        _has_crlf = "\r\n" in content
        _cnorm = content.replace("\r\n", "\n")
        _onorm = old_str.replace("\r\n", "\n")
        _nnorm = new_str.replace("\r\n", "\n")
        count = _cnorm.count(_onorm)

        _fuzzy_applied = False
        if count == 0:
            def _strip_lines(s: str) -> str:
                return "\n".join(l.rstrip() for l in s.splitlines())

            _cnorm_stripped = _strip_lines(_cnorm)
            _onorm_stripped = _strip_lines(_onorm)
            _fuzzy_count = _cnorm_stripped.count(_onorm_stripped)

            if _fuzzy_count == 1:
                _first_line_stripped = _onorm.splitlines()[0].strip() if _onorm.strip() else ""
                _fuzzy_start = -1
                _old_line_count = _onorm.count("\n") + 1
                _clines = _cnorm.splitlines()
                for _li, _cl in enumerate(_clines):
                    if _cl.strip() == _first_line_stripped:
                        _old_lines_s = [l.strip() for l in _onorm.splitlines()]
                        _file_slice = [_clines[_li + i].strip() if _li + i < len(_clines) else "" for i in range(_old_line_count)]
                        if _old_lines_s == _file_slice:
                            _fuzzy_start = _li
                            break
                if _fuzzy_start >= 0:
                    _pre = "\n".join(_clines[:_fuzzy_start])
                    _post = "\n".join(_clines[_fuzzy_start + _old_line_count:])
                    _patched = (_pre + "\n" if _pre else "") + _nnorm + ("\n" + _post if _post else "")
                    new_content = _patched.replace("\n", "\r\n") if _has_crlf else _patched

                    def _write_fuzzy_patch() -> None:
                        _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                        try:
                            with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                                _f.write(new_content)
                            os.replace(_tmp_path, str(p))
                        except Exception:
                            try:
                                os.unlink(_tmp_path)
                            except Exception:
                                pass
                            raise

                    await asyncio.to_thread(_write_fuzzy_patch)
                    added = new_str.count("\n") + (1 if new_str else 0)
                    removed = old_str.count("\n") + (1 if old_str else 0)
                    _fuzzy_applied = True
                    _lint = await _auto_lint_result(p, workspace)
                    _snippet = _old_str_snippet(old_str)
                    return f"[patch_file: {p} patched (+{added}/-{removed} lines, fuzzy-whitespace match)]{_snippet}{_lint}"
            elif _fuzzy_count > 1:
                return _tool_error_response(
                    "PATCH_FILE_NON_UNIQUE_MATCH",
                    (
                        f"patch_file old_str not unique after stripped matching ({_fuzzy_count} matches). "
                        "Add more context to old_str."
                    ),
                    tool="patch_file" )

        if count == 0 and not _fuzzy_applied:
            try:
                from tools.patch_2_fuzzy_edit import fuzzy_replace as _p2_fzr
                _p2_result = _p2_fzr(_cnorm, _onorm, _nnorm)
            except Exception:
                _p2_result = None
            if _p2_result is not None:
                _nc_p2 = _p2_result.replace("\n", "\r\n") if _has_crlf else _p2_result
                def _write_p2_patch() -> None:
                    _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                    try:
                        with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                            _f.write(_nc_p2)
                        os.replace(_tmp_path, str(p))
                    except Exception:
                        try:
                            os.unlink(_tmp_path)
                        except Exception:
                            pass
                        raise
                await asyncio.to_thread(_write_p2_patch)
                _lint = await _auto_lint_result(p, workspace)
                _snippet = _old_str_snippet(old_str)
                return f"[patch_file: {p} patched (fuzzy-jaccard match)]{_snippet}{_lint}"
            _hint = old_str.splitlines()[0][:80] if old_str.strip() else "(empty)"
            return _tool_error_response(
                "PATCH_FILE_OLD_STR_NOT_FOUND",
                f"patch_file old_str not found in '{p}'.",
                tool="patch_file" ,
                details={"hint": _hint})
        if count > 1:
            return _tool_error_response(
                "PATCH_FILE_NON_UNIQUE_MATCH",
                f"patch_file old_str appears {count} times; it must be unique.",
                tool="patch_file" )

        _patched = _cnorm.replace(_onorm, _nnorm, 1)
        new_content = _patched.replace("\n", "\r\n") if _has_crlf else _patched

        def _write_patch_result() -> None:
            _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            try:
                with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                    _f.write(new_content)
                os.replace(_tmp_path, str(p))
            except Exception:
                try:
                    os.unlink(_tmp_path)
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_write_patch_result)
        added = new_str.count("\n") + (1 if new_str else 0)
        removed = old_str.count("\n") + (1 if old_str else 0)
        _lint = await _auto_lint_result(p, workspace)
        _snippet = _old_str_snippet(old_str)
        return f"[patch_file: {p} patched (+{added}/-{removed} lines)]{_snippet}{_lint}"
    except Exception as e:
        return _tool_error_response(
            "PATCH_FILE_FAILED",
            f"patch_file failed for '{p}': {e}",
            tool="patch_file" )


async def _inline_tool_write_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Create or overwrite a file with the COMPLETE content.

    The primary write tool (2026-09-17 consolidation): the result on disk is
    exactly what the model wrote — no matching, no drift. Oversized content is
    auto-split (part 1 written, remainder stored server-side, finished via a
    write_file_append marker call).
    """
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "write_file"):
        return err
    content = str(args.get("content", ""))
    # BLOCK-FORMAT SNIFF (2026-09-17): SEARCH/REPLACE markers belong to
    # edit_file. A truncated/malformed marker call used to reach this handler
    # as "full content" and destroy the file (live: 1259-line collapse).
    if "<<<<<<<" in content or "<parameter=" in content:
        return _tool_error_response(
            "WRITE_FILE_BLOCK_FORMAT",
            "content contains SEARCH/REPLACE or foreign call markers — that format "
            "is gone. write_file expects the COMPLETE plain file content. To change "
            "parts of an existing file use edit_file (old_text/new_text).",
            tool="write_file")
    if not content.strip():
        return _tool_error_response(
            "EDIT_FILE_EMPTY_EDITS",
            "write_file requires non-empty content.",
            tool="write_file")

    existed = p.exists()
    get_transaction().capture_before(p)
    _had_crlf = False
    _old_n = 0
    if existed:
        try:
            _old_raw = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")
            _had_crlf = "\r\n" in _old_raw
            _old_n = len([l for l in _old_raw.splitlines() if l.strip()])
        except Exception:
            _old_raw, _had_crlf, _old_n = "", False, 0
    else:
        _old_raw = ""

    if content.replace("\r\n", "\n").strip() == _old_raw.replace("\r\n", "\n").strip():
        return _tool_error_response(
            "WRITE_FILE_NOOP",
            f"no change — '{p}' already contains exactly this content.",
            tool="write_file")
    if existed and _old_n >= 30:
        _new_n = len([l for l in content.replace("\r\n", "\n").splitlines() if l.strip()])
        if _new_n <= 3 and not args.get("confirm_shrink"):
            return _tool_error_response(
                "WRITE_FILE_SUSPICIOUS_SHRINK",
                f"This overwrite would shrink '{p}' from {_old_n} to {_new_n} content "
                "lines — almost always a truncated or mistaken write. If you REALLY "
                "want a tiny file: read_file it first, then resend with "
                "\"confirm_shrink\": true.",
                tool="write_file")

    # ── Model-dependent char limit + AUTO-SPLIT ──
    try:
        from utils.tool import resolve_write_char_limits as _resolve_limits
        from tools.runner import _get_write_budget as _get_wb
        _wb = _get_wb() or (None, None)
        _wf_limit, _ = _resolve_limits(args.get("__model__", ""), *_wb)
    except Exception:
        _wf_limit = 5000

    if len(content) > _wf_limit:
        part1, rest_at = _stage_split(content, _wf_limit)
        await asyncio.to_thread(_atomic_write_text, p, part1)
        _store_pending_remainder(str(p), content, rest_at)
        return _auto_split_instr(p, len(part1), len(content))

    if _had_crlf:
        content = content.replace("\n", "\r\n")

    def _write_now() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                _f.write(content)
            os.replace(_tmp_path, str(p))
        except Exception:
            try: os.unlink(_tmp_path)
            except Exception: pass
            raise

    await asyncio.to_thread(_write_now)
    _lint = await _auto_lint_result(p, workspace)
    _lines = content.count("\n") + 1
    if existed:
        return f"[write_file: rewrote '{p}' (+{_lines}/-{_old_n or '?'} lines)]{_lint}"
    return f"[write_file: created '{p}' (+{_lines} lines)]{_lint}"


async def _inline_tool_edit_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Exact-match edit: replace ONE unique occurrence of old_text.

    THE edit tool (2026-09-17 consolidation): no SEARCH/REPLACE markers, no
    fuzzy rate-guessing. old_text must be copied verbatim from the last
    read_file and must appear exactly once — anything else is a clear,
    deterministic error that tells the model how to fix it.
    """
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "edit_file"):
        return err
    if not p.exists():
        return _tool_error_response(
            "EDIT_FILE_INVALID_PATH",
            f"'{args.get('path')}' does not exist. read_file it first (to create a "
            "new file use write_file).",
            tool="edit_file")
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    # FOREIGN/TRUNCATED-FORMAT SNIFF (2026-09-17): a cut-off call in a foreign
    # marker style (live: <parameter=SEARCH> with no REPLACE — 1259 lines lost)
    # used to fall through to the full-rewrite path and destroy the file.
    for _frag in ("<<<<<<<", "=======", ">>>>>>>", "<parameter="):
        if _frag in old_text or _frag in new_text:
            return _tool_error_response(
                "EDIT_FILE_MALFORMED_BLOCK",
                "old_text/new_text contains block/parameter marker fragments — that "
                "format is gone. old_text = the EXACT text to replace (copied "
                "verbatim from read_file, unique in the file); new_text = the "
                "replacement.",
                tool="edit_file")
    get_transaction().capture_before(p)
    if not old_text.strip():
        return _tool_error_response(
            "EDIT_FILE_EMPTY_OLD_TEXT",
            "edit_file requires non-empty old_text (the exact text to replace). "
            "read_file the file first, then copy the passage verbatim.",
            tool="edit_file")
    if not new_text.strip():
        return _tool_error_response(
            "EDIT_FILE_EMPTY_NEW_TEXT",
            "new_text is empty — replace the passage with the corrected version "
            "instead, or use run_bash if you truly need an empty result.",
            tool="edit_file")

    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")
    except Exception as e:
        return _tool_error_response(
            "EDIT_FILE_READ_FAILED",
            f"Failed to read '{p}': {e}",
            tool="edit_file")
    _has_crlf = "\r\n" in content
    working = content.replace("\r\n", "\n")
    old_n = old_text.replace("\r\n", "\n")
    new_n = new_text.replace("\r\n", "\n")

    count = working.count(old_n)
    _first_hint = old_n.splitlines()[0][:80] if old_n.strip() else "(empty)"
    if count == 0:
        # Fuzzy fallback: ONE best window, line-level ratio >= 0.85, spliced at
        # line granularity (replaces exactly the matched raw lines).
        try:
            from tools.patch_2_fuzzy_edit import fuzzy_replace as _p2_fzr
            _fzr = _p2_fzr(working, old_n, new_n)
        except ImportError:
            _fzr = None
        if _fzr is not None:
            working = _fzr
            final = working.replace("\n", "\r\n") if _has_crlf else working

            def _write_fuzzy() -> None:
                _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                try:
                    with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                        _f.write(final)
                    os.replace(_tmp_path, str(p))
                except Exception:
                    try: os.unlink(_tmp_path)
                    except Exception: pass
                    raise
            await asyncio.to_thread(_write_fuzzy)
            _lint = await _auto_lint_result(p, workspace)
            return f"[edit_file: '{p}' edited via fuzzy match]{_lint}"
        return _tool_error_response(
            "EDIT_FILE_OLD_TEXT_NOT_FOUND",
            f"old_text not found in '{p}' (exact or fuzzy).\n"
            f"  Looking for: {_first_hint!r}\n"
            "  read_file the file and COPY the passage verbatim — check indentation "
            "and whitespace; add surrounding lines to make it unique.",
            tool="edit_file")
    if count > 1:
        return _tool_error_response(
            "EDIT_FILE_NOT_UNIQUE",
            f"old_text appears {count} times in '{p}' — it must be unique. Add "
            "surrounding lines from read_file to make it unique.",
            tool="edit_file")

    working = working.replace(old_n, new_n, 1)
    if working.strip() == content.replace("\r\n", "\n").strip():
        return _tool_error_response(
            "EDIT_FILE_NOOP",
            f"no change — '{p}' already contains exactly this content.",
            tool="edit_file")
    _orig_n = len([l for l in working.splitlines() if l.strip()])
    _new_n = len([l for l in new_n.splitlines() if l.strip()])
    if (not args.get("confirm_shrink") and _orig_n >= 30 and _new_n <= 3):
        return _tool_error_response(
            "EDIT_FILE_SUSPICIOUS_SHRINK",
            f"This replacement would shrink '{p}' from {_orig_n} to {_new_n} content "
            "lines — almost always a malformed edit, not a real refactor. If you "
            "REALLY want that: read_file first, then resend with "
            "\"confirm_shrink\": true.",
            tool="edit_file")

    final = working.replace("\n", "\r\n") if _has_crlf else working

    def _write_exact() -> None:
        _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                _f.write(final)
            os.replace(_tmp_path, str(p))
        except Exception:
            try: os.unlink(_tmp_path)
            except Exception: pass
            raise

    await asyncio.to_thread(_write_exact)
    _old_lines_n = content.count("\n") + 1
    _new_lines_n = final.count("\n") + 1
    _delta = _new_lines_n - _old_lines_n
    _lint = await _auto_lint_result(p, workspace)
    result = f"[edit_file: '{p}' replaced ({_delta:+d} lines)]"
    if _delta == 0:
        # 0-LINES-CLARITY: same line count, different content — report the
        # char delta so the UI does not misleadingly show "0 lines".
        _chard = len(new_n) - len(old_n)
        result += f", {_chard:+d} chars"
    return result + _lint


async def _inline_tool_replace_lines(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Removed from the coder toolset (2026-09-17 consolidation) — edit_file
    covers exact-replace (old_text/new_text). Kept dispatchable for old
    recorded sessions."""
    return _tool_error_response(
        "TOOL_REMOVED",
        "replace_lines was consolidated into edit_file: use edit_file with "
        "old_text/new_text (exact, copied verbatim from read_file).",
        tool="replace_lines")

async def _inline_tool_replace_lines(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "replace_lines"):
        return err
    start_line = int(args.get("start_line", 0))
    end_line = int(args.get("end_line", 0))
    replacement = args.get("replacement", "")
    get_transaction().capture_before(p)
    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")  # CRLF-PRESERVE (2026-09-13)
        _has_crlf = "\r\n" in content
        working_lines = content.replace("\r\n", "\n").splitlines()
        if not working_lines and content.strip():
            working_lines = [content]

        if start_line < 1 or start_line > len(working_lines) + 1:
            return _tool_error_response(
                "REPLACE_LINES_INVALID_START",
                f"start_line {start_line} out of bounds (1-{len(working_lines)}).",
                tool="replace_lines" )
        if end_line < start_line or end_line > len(working_lines) + 1:
            return _tool_error_response(
                "REPLACE_LINES_INVALID_END",
                f"end_line {end_line} out of bounds ({start_line}-{len(working_lines)}).",
                tool="replace_lines" )

        prefix = working_lines[:start_line - 1]
        suffix = working_lines[end_line:]
        repl_lines = replacement.replace("\r\n", "\n").splitlines() if replacement else []
        new_lines = prefix + repl_lines + suffix

        _ends_with_nl = content.endswith("\n") or content.endswith("\r\n")
        new_content = "\n".join(new_lines) + ("\n" if _ends_with_nl else "")
        if _has_crlf:
            new_content = new_content.replace("\n", "\r\n")

        def _write_replace_lines() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            try:
                with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                    _f.write(new_content)
                os.replace(_tmp_path, str(p))
            except Exception:
                try:
                    os.unlink(_tmp_path)
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_write_replace_lines)

        _lint = await _auto_lint_result(p, workspace)
        added = len(repl_lines)
        removed = end_line - start_line + 1
        return f"[replace_lines: {p} updated lines {start_line}-{end_line} (+{added}/-{removed})]{_lint}"

    except Exception as e:
        return _tool_error_response(
            "REPLACE_LINES_FAILED",
            f"replace_lines failed for '{p}': {e}",
            tool="replace_lines" )


async def _inline_tool_undo_last(args: dict, workspace: Path, workspace_lock: str | None) -> str:


    from tools.workspace import get_transaction

    _path_arg = str(args.get("path", "") or "").strip()

    if _shared._GIT_TOOLS_AVAILABLE:
        try:
            from hive_functions.git_tools import (
                exec_git_undo_full as _g_undo_full,
                exec_git_undo_file as _g_undo_file,
                get_last_checkpoint as _g_last_cp,
            )
            _cp = await _g_last_cp(str(workspace))
            if _cp:
                if _path_arg:
                    _rp = _inline_resolve_path(workspace, _path_arg)
                    if err := _inline_check_workspace(_rp, workspace_lock, "undo_last"):
                        return err
                    _g_out = await _g_undo_file(str(workspace), str(_rp))
                    if _g_out.startswith("❌"):
                        return _tool_error_response(
                            "UNDO_LAST_FAILED", _g_out, tool="undo_last")
                    if _g_out:
                        _lint = await _auto_lint_result(_rp, workspace)
                        return f"[undo_last: {_g_out}]{_lint}"
                else:
                    _g_out = await _g_undo_full(str(workspace))
                    if _g_out.startswith("❌"):
                        return _tool_error_response(
                            "UNDO_LAST_FAILED", _g_out, tool="undo_last")
                    return f"[undo_last: {_g_out}]"
        except Exception as _g_err:
            pass

    _tx = get_transaction()
    if not _tx.has_changes:
        return _tool_error_response(
            "UNDO_LAST_NO_CHANGES",
            "No file changes in the current round to undo.",
            tool="undo_last" )
    if _path_arg:
        _rp = _inline_resolve_path(workspace, _path_arg)
        if err := _inline_check_workspace(_rp, workspace_lock, "undo_last"):
            return err
        _ok, _info = _tx.undo_path(_rp)
        if _ok:
            _lint = await _auto_lint_result(_rp, workspace)
            return f"[undo_last: restored '{_info}']{_lint}"
        return _tool_error_response(
            "UNDO_LAST_PATH_NOT_FOUND",
            _info,
            tool="undo_last" )
    _restored = _tx.rollback()
    if _restored:
        return f"[undo_last: restored {len(_restored)} file(s): {', '.join(_restored[:10])}]"
    return _tool_error_response(
        "UNDO_LAST_NO_CHANGES",
        "No file changes in the current round to undo.",
        tool="undo_last" )


def _old_str_snippet(old_str: str) -> str:
    """Extract first 5 lines of old_str for 'changed near' context snippet."""
    if not old_str or not old_str.strip():
        return ""
    _lines = old_str.replace("\r\n", "\n").splitlines()
    _show = _lines[:5]
    _snippet = "\n".join(_l[:80] for _l in _show)
    return f"\n[changed near:\n{_snippet}]"


def _first_dict_value(e: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in e and e[k]:
            return str(e[k])
    return ""


def _looks_like_json_edit_args(edits_str: str) -> bool:


    s = edits_str.strip()
    if not s or s[0] not in "[{":
        return False
    return any(k in s for k in ('"old_string"', '"old_str"', '"new_string"', '"new_str"', '"search"', '"replace"'))


def _try_convert_json_edits(edits_str: str) -> str:

    try:
        parsed = json.loads(edits_str)
        if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict):
            _keys = parsed[0].keys()
            if any(k in _keys for k in _JSON_EDIT_KEY_OLD):
                blocks = []
                for e in parsed:
                    old = _first_dict_value(e, _JSON_EDIT_KEY_OLD)
                    new = _first_dict_value(e, _JSON_EDIT_KEY_NEW)
                    blocks.append(f"<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE")
                return "\n\n".join(blocks)
    except Exception:
        pass
    return edits_str
