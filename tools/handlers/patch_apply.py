# -*- coding: utf-8 -*-
"""`apply_patch` tool handler (D3 patch-native editing, 2026-11-11).

Applies a unified diff to ONE OR MORE workspace files:

- safe path checks: every path is sanitized (tools/patch_diff) and resolved
  strictly INSIDE the workspace (symlink escapes rejected);
- validation BEFORE any byte is written: parse errors, path escapes, missing
  targets, context mismatches and per-file patching all happen in memory —
  a rejected patch never touches disk;
- deterministic ordering: file sections are applied in patch order; file
  DELETIONS run last so no earlier section ever sees a half-refactored tree;
- per-file atomicity: every file is written via the shared atomic
  write (temp file + os.replace) — a crash leaves either the old or the new
  content, never a half-written file;
- rollback on failure: if a write fails mid-sequence, files already written
  are restored from their captured originals (creations are unlinked,
  deletions recreated) and the failure names exactly what was rolled back.

The existing edit_file APIs (old_text/new_text and edits=[...]) are
unchanged; apply_patch is the patch-native ADDITION for multi-file edits.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from tools.errors import tool_error_response as _tool_error_response
from tools.patch_2_fuzzy_edit import best_match_location
from tools.patch_diff import (
    PatchError,
    apply_hunks_to_text,
    parse_unified_diff,
)
from tools.workspace import get_transaction

from .file_ops import _atomic_write, _recheck_workspace_before_replace

# Lint at most this many files per patch call (keep the result bounded).
_MAX_LINT_FILES = 4
# Auto-test at most this many paths per patch call (G#1 same-turn verify).
_MAX_AUTO_TEST_PATHS = 6

_ARG_FRAGMENTS = ("<parameter=", "<<<<<<<", ">>>>>>>",
                  "[applied diff:", "[applied write:", "[applied edit:",
                  "content not in context anymore")

_PLAN_NOTE = (
    "\nOrdering guarantee: file sections are applied in patch order, "
    "deletions last, each file atomically; a failure rolls back the files "
    "already written.")


def _err(code: str, message: str) -> str:
    return _tool_error_response(code, message, tool="apply_patch")


def _build_patch_mismatch_anchor(rel: str, current_lf: str, hunks: list) -> str:
    """Return a 15-20 line current-file snippet near the first failing hunk."""
    cur_lines = current_lf.splitlines()
    if not cur_lines:
        return ""
    center = 1
    ctx_len = 1
    for h in hunks or ():
        old_lines = h.old_lines() if hasattr(h, "old_lines") else []
        if old_lines:
            expected_txt = "\n".join(old_lines)
            ctx_len = len(old_lines)
            try:
                loc = best_match_location(current_lf, expected_txt)
            except (ValueError, TypeError, AttributeError):
                loc = None
            if loc and loc[0] is not None and int(loc[0]) >= 0:
                center = int(loc[0]) + 1
            else:
                center = max(1, int(getattr(h, "old_start", 1) or 1))
            break
    lo = max(0, center - 1 - 9)
    hi = min(len(cur_lines), center - 1 + max(ctx_len, 1) + 9)
    snippet = "\n".join(cur_lines[lo:hi])
    if len(snippet) > 1400:
        snippet = snippet[:1400] + "\n... [truncated]"
    return (
        f"\n\n[CURRENT CONTENT of '{Path(rel).name}' lines {lo + 1}-{hi} — "
        f"build your next patch or edit_file from EXACTLY this:]\n{snippet}"
    )


async def _inline_tool_apply_patch(args: dict, workspace: Path,
                                   workspace_lock: str | None) -> str:
    text = str(args.get("patch") or args.get("diff") or "")
    if not text.strip():
        return _err(
            "APPLY_PATCH_EMPTY",
            "apply_patch requires 'patch': the unified diff text (--- / +++ "
            "/ @@ sections). For single-passage edits use edit_file.")
    for frag in _ARG_FRAGMENTS:
        if frag in text:
            if "[applied " in frag or "context anymore" in frag:
                # STUB-ECHO: the model echoed one of OUR history compaction
                # stubs back as patch content — never write that to disk.
                return _err(
                    "APPLY_PATCH_MALFORMED",
                    "patch contains an internal compaction stub marker "
                    f"({frag!r}), not real file content. read_file the path "
                    "and build the patch from the CURRENT content. NOTHING "
                    "was written.")
            return _err(
                "APPLY_PATCH_MALFORMED",
                f"patch contains {frag!r} fragments from a truncated tool "
                "call — nothing was written. Resend the complete unified "
                "diff.")
    try:
        patches = parse_unified_diff(text)
    except PatchError as e:
        if e.code == "PATCH_PATH_INVALID":
            return _err(
                "APPLY_PATCH_PATH_INVALID",
                f"patch path rejected: {e.message} NOTHING was written.")
        return _err(
            "APPLY_PATCH_MALFORMED",
            f"patch rejected ({e.code}): {e.message} NOTHING was written.")

    # ── Phase A: validate + compute every file's new content in memory ──
    # A failure anywhere in this loop aborts the whole call with no write.
    plan_by_target: dict[Path, dict] = {}
    plan_order: list[Path] = []
    for fp in patches:
        rel = fp.target
        try:
            target = _safe_workspace_path(workspace, rel)
        except PatchError as e:
            return _err(
                "APPLY_PATCH_PATH_INVALID",
                f"patch path rejected: {e.message} NOTHING was written.")
        # Security: re-check symlink/junction containment BEFORE touching disk.
        _chk_pre = _recheck_workspace_before_replace(
            target, workspace_lock, "apply_patch")
        if _chk_pre:
            return _chk_pre
        prev = plan_by_target.get(target)
        if prev is not None:
            exists = not prev["is_delete"]
            disk_original = prev["original"]
            disk_existed_before = prev["existed_before"]
            current_content = "" if prev["is_delete"] else prev["final"]
        else:
            exists = target.exists()
            disk_existed_before = exists
            if exists:
                try:
                    disk_original = await asyncio.to_thread(
                        target.read_text, encoding="utf-8", errors="replace",
                        newline="")
                except OSError as e:
                    return _err(
                        "APPLY_PATCH_READ_FAILED",
                        f"failed to read '{rel}': {e} NOTHING was written.")
            else:
                disk_original = ""
            current_content = disk_original
        if fp.is_new_file and exists:
            return _err(
                "APPLY_PATCH_TARGET_CONFLICT",
                f"patch creates '{rel}' but it already exists — use edit_file "
                "for existing files. NOTHING was written.")
        if not fp.is_new_file and not exists:
            return _err(
                "APPLY_PATCH_NO_TARGET",
                f"patch targets '{rel}' which does not exist (new files need "
                "'--- /dev/null' + '+++ b/<path>'). NOTHING was written.")
        has_crlf = "\r\n" in current_content
        try:
            new_lf = apply_hunks_to_text(
                current_content.replace("\r\n", "\n"), fp.hunks,
                is_new_file=fp.is_new_file, is_delete=fp.is_delete)
        except PatchError as e:
            _anchor = (
                _build_patch_mismatch_anchor(
                    rel, current_content.replace("\r\n", "\n"), fp.hunks
                )
                if e.code == "PATCH_CONTEXT_NOT_FOUND"
                else ""
            )
            return _err(
                "APPLY_PATCH_FAILED",
                f"patch for '{rel}' not applicable ({e.code}): {e.message} "
                f"NOTHING was written.{_anchor}")
        final = new_lf.replace("\n", "\r\n") if has_crlf else new_lf
        if prev is None:
            plan_order.append(target)
            plan_by_target[target] = {
                "rel": rel, "path": target,
                "existed_before": disk_existed_before,
                "original": disk_original, "final": final,
                "is_new": fp.is_new_file or not disk_existed_before,
                "is_delete": fp.is_delete,
                "added": fp.added(), "removed": fp.removed(),
            }
        else:
            prev["final"] = final
            prev["is_delete"] = fp.is_delete
            prev["is_new"] = not disk_existed_before
            prev["added"] += fp.added()
            prev["removed"] += fp.removed()
    planned = [plan_by_target[t] for t in plan_order]

    # DETERMINISTIC ORDER: patch order for writes, deletions last.
    writes = [p for p in planned if not p["is_delete"]]
    deletes = [p for p in planned if p["is_delete"] and p["existed_before"]]
    ordered = writes + deletes
    for p in ordered:
        get_transaction().capture_before(p["path"])

    # ── Phase B: write sequentially; roll back on the first failure ──
    done: list[dict] = []
    for p in ordered:
        chk = _recheck_workspace_before_replace(
            p["path"], workspace_lock, "apply_patch")
        if chk:
            _rb = await _rollback(workspace, done)
            return chk + (_rb_note(_rb) or "\n[rollback: nothing had been written yet]")
        try:
            if p["is_delete"]:
                await asyncio.to_thread(os.unlink, str(p["path"]))
            else:
                await asyncio.to_thread(_atomic_write, p["path"], p["final"])
        except OSError as e:
            _rb = await _rollback(workspace, done, also_failed=p)
            code = "APPLY_PATCH_WRITE_FAILED"
            return _err(
                code,
                f"writing '{p['rel']}' failed: {e}. {_rb['message']}")
        done.append(p)

    # ── Post: written-set registration, lint, auto-test (G#1), summary ──
    _register_written([p for p in writes], workspace)
    _lint = ""
    try:
        from .linting import _auto_lint_result
        for p in writes[:_MAX_LINT_FILES]:
            _lint += await _auto_lint_result(p["path"], workspace)
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        pass  # _auto_lint_result is internally guarded; belt-and-braces only
    changed = [p for p in writes]  # new + edited (deletions are not test targets)
    try:
        from tools.auto_test import auto_test_note
        from tools.runner import _run_settings_snapshot
        note = await auto_test_note(
            [str(p["path"]) for p in changed[:_MAX_AUTO_TEST_PATHS]],
            workspace, workspace_lock,
            settings=_run_settings_snapshot.get(None))
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        note = ""  # auto_test_note never raises by design; belt-and-braces

    parts = []
    for p in planned:
        if p["is_delete"]:
            parts.append(f"  - {p['rel']} deleted (-{p['removed']} lines)")
        elif p["is_new"]:
            parts.append(f"  - {p['rel']} created (+{p['added']} lines)")
        else:
            parts.append(
                f"  - {p['rel']} (+{p['added']}/-{p['removed']} lines)")
    _note_sep = ("\n" + note) if (note and not note.startswith("\n")) else note
    return (f"[apply_patch: {len(planned)} file(s) patched "
            f"(+{sum(p['added'] for p in planned)}/"
            f"-{sum(p['removed'] for p in planned)} lines)]\n"
            + "\n".join(parts) + _PLAN_NOTE + _lint + _note_sep)


def _rb_note(rb: dict) -> str:
    return "\n" + rb["message"] if rb["message"] else ""


async def _rollback(workspace: Path, done: list[dict],
                    also_failed: dict | None = None) -> dict:
    """Best-effort restore of everything written before the failure.
    Never raises — the message reports exactly what happened on disk."""
    restored: list[str] = []
    failed_rb: list[str] = []
    for p in reversed(done):
        try:
            if p["is_new"] or not p.get("existed_before", True):
                if p["path"].exists():
                    await asyncio.to_thread(os.unlink, str(p["path"]))
            else:
                await asyncio.to_thread(_atomic_write, p["path"], p["original"])
            restored.append(p["rel"])
        except OSError:
            failed_rb.append(p["rel"])
    bits: list[str] = []
    if restored:
        bits.append("restored " + ", ".join(restored))
    if failed_rb:
        bits.append("COULD NOT restore " + ", ".join(failed_rb) + (
            " — inspect these files before continuing"))
    if also_failed and not bits:
        bits.append("nothing else had been written")
    msg = "[rollback: " + "; ".join(bits) + "]" if bits else ""
    return {"restored": restored, "failed": failed_rb, "message": msg}


def _safe_workspace_path(workspace: Path, rel: str) -> Path:
    """Resolve a sanitized patch path strictly inside the workspace."""
    from tools.patch_diff import PatchError as _PE
    target = workspace / Path(rel.replace("/", os.sep))
    try:
        ws_res = workspace.resolve()
        t_res = target.resolve()
        t_res.relative_to(ws_res)
    except (OSError, ValueError):
        raise _PE(
            "PATCH_PATH_INVALID",
            f"'{rel}' does not resolve inside the workspace (symlink or "
            "drive escape)")
    return target


def _register_written(writes: list[dict], workspace: Path) -> None:
    """Mirror patched files into the run's written-set (same canonical
    spelling the write tools use). Best-effort: never raises."""
    if not writes:
        return
    try:
        from tools.runner import (
            _files_written_in_run as _wset_cv,
            _files_written_by_run as _by_run,
            _current_run_id as _rid_cv,
            _normalize_tool_path as _ntp,
        )
        _wset = _wset_cv.get(None)
        if _wset is None:
            _wset = set()
            _wset_cv.set(_wset)
        rid = str(_rid_cv.get(None) or "")
        for p in writes:
            fp = p["path"]
            try:
                fp = fp.resolve()
            except OSError:
                pass
            key = ""
            try:
                from core.send_files import canonical_workspace_path as _canon
                key = _canon(str(fp), str(workspace))
            except (ImportError, OSError, RuntimeError, ValueError):
                key = ""
            if not key:
                key = _ntp(str(fp), workspace)
            if not key:
                continue
            _wset.add(key)
            if rid:
                _by_run.setdefault(rid, set()).add(key)
    except (ImportError, AttributeError, TypeError, ValueError):
        pass
