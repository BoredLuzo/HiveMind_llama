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

from . import _shared

_JSON_EDIT_KEY_NEW = ("new_str", "new_string", "replace")

_JSON_EDIT_KEY_OLD = ("old_str", "old_string", "search")

from .linting import _auto_lint_result
from .exec_tools import _stage_split

async def _inline_tool_read_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "read_file"):
        return err
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)

        # Binary file detection: check first 512 chars for NUL bytes and
        # control characters (excluding \n \r \t). Non-ASCII (Umlaute, Emojis,
        _sample = content[:512]
        if _sample:
            _binary_chars = sum(
                1 for c in _sample
                if ord(c) == 0 or (ord(c) < 32 and c not in "\n\r\t")
            )
            if len(_sample) > 0 and (_binary_chars / len(_sample)) > 0.10:
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

        # Enforce line ranges for large files to prevent context bloat
        if start_line is None and end_line is None and len(lines) > 400:
            _sig_fn = _shared.get_signatures_report if callable(_shared.get_signatures_report) else None
            _sig = await asyncio.to_thread(_sig_fn, p, 200) if _sig_fn else (
                "(signature extraction unavailable) — use read_file with line_range=[1,50] to inspect the file header first."
            )
            return _tool_error_response(
                "FILE_TOO_LARGE_NEED_RANGE",
                f"File '{p}' is too large ({len(lines)} lines) to read fully into limited context. "
                "Please use 'start_line' and 'end_line' inside read_file to read specific sections.\n\n"
                f"File Outline / Signatures:\n{_sig}",
                tool="read_file" )

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
    content = args.get("content", "")
    get_transaction().capture_before(p)
    # ── Model-dependent chunk limit (same tier as write_file) ──
    # LIMITS-RAISE (2026-08-22): 8000→16000 (14b+), 5000→8000, 3500→5000, 2500→3500.
    # cut off at the output limit. Now: tier limit MINUS token-budget cap
    # (duo_runner passes budget + factor via _set_write_budget).
    try:
        from utils.tool import resolve_write_char_limits as _resolve_limits
        from tools.runner import _get_write_budget as _get_wb
        _wb = _get_wb() or (None, None)
        _, _append_limit = _resolve_limits(args.get("__model__", ""), *_wb)
    except Exception:
        _append_limit = 3500
    if len(content) > _append_limit:
        part1, rest_at = _stage_split(content, _append_limit)
        remaining = len(content) - rest_at
        _anchor = part1.rstrip()[-80:].replace("\n", "\\n")
        content = part1
    else:
        remaining = 0
        _anchor = ""
    try:
        def _append_and_size() -> int:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(content)
            return p.stat().st_size

        total = await asyncio.to_thread(_append_and_size)
        lines = content.count("\n") + 1
        base = f"[Appended: {p} (+{lines} lines, total {total} bytes)]"
        if remaining > 0:
            return (f"{base} [AUTO-SPLIT] REMAINING {remaining} chars NOT written yet. "
                    f"Call write_file_append again, resuming directly after anchor: ...{_anchor}")
        _lint = await _auto_lint_result(p, workspace)
        return f"{base}{_lint}"
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
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
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


async def _inline_tool_edit_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "edit_file"):
        return err
    # HEADER-FIX (2026-08-10): write_file delegiert hierher (_tool_name="write_file").
    _display_tool = str(args.get("_tool_name", "") or "edit_file")
    if not p.exists():
        _tool_name = args.get("_tool_name", "edit_file")
        if _tool_name == "edit_file":
            return _tool_error_response(
                "EDIT_FILE_INVALID_PATH",
                f"'{args.get('path')}' does not exist. Use write_file (NOT write_file_append) to create new files.",
                tool="edit_file")
    edits_raw = args.get("edits", "")
    edits_raw = re.sub(r"<think[^>]*>[\s\S]*?</think(?:ing)?>", "", edits_raw, flags=re.DOTALL).strip()
    get_transaction().capture_before(p)
    if not edits_raw:
        return _tool_error_response(
            "EDIT_FILE_EMPTY_EDITS",
            f"{_display_tool} requires non-empty content or SEARCH/REPLACE edits.",
            tool=_display_tool )

    # ── Sanity check: parent directory must exist ──
    if not p.parent.exists():
        return _tool_error_response(
            "EDIT_FILE_INVALID_PATH",
            f"Parent directory '{p.parent}' does not exist. Create it first (e.g. run_bash mkdir).",
            tool=_display_tool )

    # ── Model-dependent char limits (shared between write/edit paths) ──
    # LIMITS-RAISE (2026-08-22): 12000→20000 (14b+), 7000→10000, 5000→7000, 3500→5000 —
    # MINUS Budget-Deckel (via _set_write_budget, Kalibrierung [WRITE-CALIBRATION]).
    try:
        from utils.tool import resolve_write_char_limits as _resolve_limits
        from tools.runner import _get_write_budget as _get_wb
        _wb = _get_wb() or (None, None)
        _wf_limit, _ = _resolve_limits(args.get("__model__", ""), *_wb)
    except Exception:
        _wf_limit = 5000

    # ── NEW FILE: edits = full content ──
    if not p.exists():
        content = edits_raw
        if len(content) > _wf_limit:
            part1, rest_at = _stage_split(content, _wf_limit)
            remaining = len(content) - rest_at

            def _write_part() -> None:
                p.parent.mkdir(parents=True, exist_ok=True)
                _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                try:
                    with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                        _f.write(part1)
                    os.replace(_tmp_path, str(p))
                except Exception:
                    try: os.unlink(_tmp_path)
                    except Exception: pass
                    raise

            await asyncio.to_thread(_write_part)
            _anchor = part1.rstrip()[-80:].replace("\n", "\\n")
            return (
                f"[{_display_tool}: AUTO-SPLIT] created '{p}' with the FIRST {len(part1)} chars. "
                f"REMAINING {remaining} chars are NOT written yet.\n"
                f"Continue NOW: call write_file_append with the remaining content, "
                f"resuming directly after anchor: ...{_anchor}"
            )
        try:
            def _write_new() -> None:
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
            await asyncio.to_thread(_write_new)
            lines = content.count("\n") + 1
            _lint = await _auto_lint_result(p, workspace)
            return f"[{_display_tool}: created '{p}' (+{lines} lines)]{_lint}"
        except Exception as e:
            return _tool_error_response(
                "EDIT_FILE_FAILED",
                f"{_display_tool} failed for '{p}': {e}",
                tool=_display_tool )

    # ── EXISTING FILE ──
    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
        _has_crlf = "\r\n" in content
        working = content.replace("\r\n", "\n")

        # Auto-convert JSON old_str/new_str format to SEARCH/REPLACE blocks
        parsed = _try_convert_json_edits(edits_raw)
        parsed = parsed.replace("\r\n", "\n")
        blocks = _RE_SEARCH_REPLACE_BLOCK.findall(parsed)
        if not blocks:
            blocks = _RE_SEARCH_REPLACE_BLOCK_LENIENT.findall(parsed)

        # ── No SEARCH/REPLACE blocks → full rewrite (was write_file with allow_overwrite) ──
        if not blocks:
            if re.search(r'(?:<<<<<<<|>>>>>>>|^=======$)', parsed, re.MULTILINE):
                return _tool_error_response(
                    "EDIT_FILE_MALFORMED_BLOCK",
                    "Content has SEARCH/REPLACE markers but no valid blocks found. "
                    "Use exact format:\n<<<<<<< SEARCH\n<text>\n=======\n<replacement>\n>>>>>>> REPLACE",
                    tool=_display_tool,
                )
            # Voll-Rewrite legitimerweise).
            if _display_tool == "edit_file" and _looks_like_json_edit_args(edits_raw):
                return _tool_error_response(
                    "EDIT_FILE_MALFORMED_BLOCK",
                    "edits was sent as JSON (old_string/new_string or old_str/new_str) "
                    "and could not be converted. Use SEARCH/REPLACE blocks instead:\n"
                    "<<<<<<< SEARCH\n<exact existing code>\n=======\n<new code>\n>>>>>>> REPLACE\n"
                    "Copy the SEARCH text VERBATIM from read_file output.",
                    tool=_display_tool,
                )
            content_new = edits_raw
            if len(content_new) > _wf_limit:
                lines = content_new.count("\n") + 1
                _chunk = 60 if _wf_limit <= 3500 else 120
                return _tool_error_response(
                    "EDIT_FILE_CONTENT_TOO_LARGE",
                    f"Content too large for full rewrite ({len(content_new)} chars, {lines} lines). "
                    f"Max {_wf_limit} chars. Use SEARCH/REPLACE blocks for partial edits, or split with write_file_append.",
                    tool=_display_tool ,
                    details={"max_chars": _wf_limit, "lines": lines, "suggested_chunk_lines": _chunk})
            if content_new.replace("\r\n", "\n").strip() == str(content or "").replace("\r\n", "\n").strip():
                return _tool_error_response(
                    "EDIT_FILE_NOOP",
                    f"no change — '{p}' already contains exactly this content. "
                    f"Your new content is identical to the existing file content.",
                    tool=_display_tool,
                )
            def _write_rewrite() -> None:
                _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                try:
                    with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                        _f.write(content_new)
                    os.replace(_tmp_path, str(p))
                except Exception:
                    try: os.unlink(_tmp_path)
                    except Exception: pass
                    raise
            await asyncio.to_thread(_write_rewrite)
            old_lines = content.count("\n") + 1
            new_lines = content_new.count("\n") + 1
            _lint = await _auto_lint_result(p, workspace)
            return f"[{_display_tool}: rewrote '{p}' (+{new_lines}/-{old_lines} lines)]{_lint}"

        # ── SEARCH/REPLACE blocks found → apply patches ──
        old_lines = content.count("\n") + 1
        applied = 0
        errors = []
        for idx, (old, new) in enumerate(blocks, 1):
            old_n = old.replace("\r\n", "\n")
            new_n = new.replace("\r\n", "\n")
            count = working.count(old_n)
            if count == 0:
                try:
                    from tools.patch_2_fuzzy_edit import fuzzy_replace as _p2_fzr
                    _p2_result = _p2_fzr(working, old_n, new_n)
                except Exception:
                    _p2_result = None
                if _p2_result is not None:
                    working = _p2_result
                    applied += 1
                    continue
                hint = old_n.splitlines()[0][:80] if old_n.strip() else "(empty)"
                errors.append(
                    f"Block {idx}: SEARCH text not found (fuzzy-match also failed).\n"
                    f"  Looking for: {hint!r}\n"
                    "  Tip: copy text verbatim from read_file - check indentation and whitespace"
                )
            elif count > 1:
                errors.append(
                    f"Block {idx}: SEARCH text appears {count}x (must be unique).\n"
                    "  Tip: include more surrounding context lines to make it unique"
                )
            else:
                working = working.replace(old_n, new_n, 1)
                applied += 1

        if errors and applied == 0:
            return _tool_error_response(
                "EDIT_FILE_NO_BLOCKS_APPLIED",
                "No SEARCH/REPLACE blocks could be applied.",
                tool=_display_tool ,
                details={"errors": errors[:8]})

        final = working.replace("\n", "\r\n") if _has_crlf else working

        if final.replace("\r\n", "\n") == str(content or "").replace("\r\n", "\n"):
            return _tool_error_response(
                "EDIT_FILE_NOOP",
                f"no change — '{p}' is already up to date. The SEARCH/REPLACE blocks "
                f"produced no new content (SEARCH and REPLACE were identical). "
                f"Provide the actually NEW code in the REPLACE block.",
                tool=_display_tool,
            )

        def _write_edit_result() -> None:
            _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            try:
                with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                    _f.write(final)
                os.replace(_tmp_path, str(p))
            except Exception:
                try: os.unlink(_tmp_path)
                except Exception: pass
                raise

        await asyncio.to_thread(_write_edit_result)

        new_lines = final.count("\n") + 1
        delta = new_lines - old_lines
        result = f"[{_display_tool}: '{p}' - {applied}/{len(blocks)} blocks applied ({delta:+d} lines)]"
        if errors:
            result += "\nWarnings:\n" + "\n".join(errors)
        if blocks and applied > 0:
            _first_old = blocks[0][0].replace("\r\n", "\n")
            _snippet = _old_str_snippet(_first_old)
            if _snippet:
                result += _snippet
        _lint = await _auto_lint_result(p, workspace)
        return result + _lint
    except Exception as e:
        return _tool_error_response(
            "EDIT_FILE_FAILED",
            f"{_display_tool} failed for '{p}': {e}",
            tool=_display_tool )


async def _inline_tool_replace_lines(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "replace_lines"):
        return err
    start_line = int(args.get("start_line", 0))
    end_line = int(args.get("end_line", 0))
    replacement = args.get("replacement", "")
    get_transaction().capture_before(p)
    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
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
