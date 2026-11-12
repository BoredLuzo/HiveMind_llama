# -*- coding: utf-8 -*-
"""Patch-native editing (D3, 2026-11-11): parse + apply unified diffs.

Pure stdlib, no engine imports — the `apply_patch` tool handler
(tools/handlers/patch_apply.py) drives I/O around these functions and the
behavior tests drive the functions directly.

Design goal: CONSERVATIVE. A patch is applied only when every hunk matches
exactly (line-granular, line numbers may drift, but the context block itself
must be unique in the target file). Anything ambiguous or malformed is
refused with a named error — never guessed around.

Ordering guarantee (D3): file sections are applied in the order they appear
in the patch text; within a file, hunks are applied in header order and each
match must sit at or after the previous hunk's replacement (no overlaps).
The handler turns this into an all-or-nothing write with per-file rollback.

Error codes (PatchError.code):
    PATCH_MALFORMED          structural problem (no file sections, truncated
                             hunk, missing +++ line, junk outside hunks)
    PATCH_PATH_INVALID       empty/absolute/UNC/NUL path, ".." component
    PATCH_UNVERIFIABLE       hunk with zero context lines (cannot verify
                             where it belongs)
    PATCH_CONTEXT_NOT_FOUND  expected old lines match nowhere
    PATCH_CONTEXT_NOT_UNIQUE expected old lines match more than once
    PATCH_HUNK_ORDER         hunks overlap or the unique match sits before
                             an earlier hunk's replacement
    PATCH_EMPTY              the patch text carries no hunks at all
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "PatchError", "FilePatch", "Hunk", "DiffLine",
    "parse_unified_diff", "apply_hunks_to_text", "sanitize_patch_path",
    "strip_prefix",
]


class PatchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class DiffLine:
    tag: str   # " " context, "-" old-only, "+" new-only, "\\" comment marker
    text: str  # content WITHOUT the tag char and WITHOUT the trailing newline


@dataclass
class Hunk:
    old_start: int          # 1-based
    old_count: int
    new_start: int          # 1-based
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)
    new_ends_without_newline: bool = False  # "\ No newline at end of file" on the new side

    def old_lines(self) -> list[str]:
        return [l.text for l in self.lines if l.tag in (" ", "-")]

    def new_lines(self) -> list[str]:
        return [l.text for l in self.lines if l.tag in (" ", "+")]

    def added(self) -> int:
        return sum(1 for l in self.lines if l.tag == "+")

    def removed(self) -> int:
        return sum(1 for l in self.lines if l.tag == "-")

    def has_context(self) -> bool:
        return any(l.tag == " " for l in self.lines)


@dataclass
class FilePatch:
    old_path: str | None    # sanitized workspace-relative, None = new file
    new_path: str | None    # None = file deletion
    hunks: list[Hunk]

    @property
    def target(self) -> str:
        """The workspace-relative path this section operates on."""
        return self.new_path or self.old_path or ""

    @property
    def is_new_file(self) -> bool:
        return self.old_path is None and self.new_path is not None

    @property
    def is_delete(self) -> bool:
        return self.new_path is None and self.old_path is not None

    def added(self) -> int:
        return sum(h.added() for h in self.hunks)

    def removed(self) -> int:
        return sum(h.removed() for h in self.hunks)


# ── path handling ────────────────────────────────────────────────────────

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# git header noise between file sections (never carries hunk payload)
_HEADER_NOISE_PREFIXES = (
    "diff ", "index ", "old mode ", "new mode ", "new file mode ",
    "deleted file mode ", "similarity index ", "dissimilarity index ",
    "rename from ", "rename to ", "copy from ", "copy to ",
    "Binary files ", "GIT binary patch",
)


def strip_prefix(raw: str) -> str:
    """Strip one conventional a/ or b/ prefix from a ---/+++ path."""
    p = raw.strip()
    if p.startswith(("a/", "b/")) and len(p) > 2:
        return p[2:]
    return p


def _unquote(raw: str) -> str:
    p = raw.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1]
        p = p.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
        p = p.replace("\\t", "\t").replace("\\n", "\n")
    return p


def sanitize_patch_path(raw: str | None) -> str | None:
    """Normalize a ---/+++ path; None for /dev/null; raises PATCH_PATH_INVALID.

    Rejects absolute paths, drive letters, UNC, ".." components and NUL —
    the path must name a file DIRECTLY under the workspace."""
    if raw is None:
        return None
    p = _unquote(raw)
    if p in ("/dev/null", "NUL", "/NUL"):
        return None
    p = strip_prefix(p).replace("\\", "/")
    if not p:
        raise PatchError("PATCH_PATH_INVALID", "empty file path in patch header")
    if "\x00" in p:
        raise PatchError("PATCH_PATH_INVALID", "NUL byte in file path")
    if p.startswith("/") or p.startswith("//"):
        raise PatchError(
            "PATCH_PATH_INVALID", f"absolute path not allowed: {raw!r}")
    if _DRIVE_RE.match(p):
        raise PatchError(
            "PATCH_PATH_INVALID", f"drive-letter path not allowed: {raw!r}")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if not parts:
        raise PatchError(
            "PATCH_PATH_INVALID", f"path reduces to nothing: {raw!r}")
    if any(seg == ".." for seg in parts):
        raise PatchError(
            "PATCH_PATH_INVALID",
            f"path escape rejected ('..' component): {raw!r}")
    if any(seg.lower() in ("con", "prn", "aux", "nul") for seg in parts[:1]):
        # Windows reserved device names as the first component
        raise PatchError(
            "PATCH_PATH_INVALID", f"reserved device name: {raw!r}")
    return "/".join(parts)


# ── parsing ─────────────────────────────────────────────────────────────

def _parse_hunks(lines: list[str], i: int) -> tuple[list[Hunk], int]:
    """Read hunks starting at lines[i] (the first @@ header) until counts
    are satisfied and the next structural line appears. Count-driven, so a
    '--- ' line that is really a deletion of '-- ' inside a hunk body is
    never mistaken for the next file section."""
    hunks: list[Hunk] = []
    n = len(lines)
    while i < n and (m := _HUNK_HEADER_RE.match(lines[i])):
        h = Hunk(
            old_start=int(m.group(1)),
            old_count=int(m.group(2) if m.group(2) is not None else 1),
            new_start=int(m.group(3)),
            new_count=int(m.group(4) if m.group(4) is not None else 1),
        )
        i += 1
        need_old = h.old_count
        need_new = h.new_count
        while (need_old > 0 or need_new > 0) and i < n:
            raw = lines[i]
            if raw.startswith("\\"):
                if raw.rstrip().endswith("end of file"):
                    h.new_ends_without_newline = True
                i += 1
                continue
            if not raw:
                # empty body line = empty context line (common with -U0
                # emitters and hand-written patches)
                tag, text = " ", ""
            else:
                tag, text = raw[0], raw[1:]
            if tag == " ":
                if need_old <= 0 or need_new <= 0:
                    raise PatchError(
                        "PATCH_MALFORMED",
                        f"hunk header counts exceeded at line {i + 1} "
                        f"(extra context line)")
                need_old -= 1
                need_new -= 1
            elif tag == "-":
                if need_old <= 0:
                    raise PatchError(
                        "PATCH_MALFORMED",
                        f"hunk header counts exceeded at line {i + 1} "
                        f"(extra '-' line)")
                need_old -= 1
            elif tag == "+":
                if need_new <= 0:
                    raise PatchError(
                        "PATCH_MALFORMED",
                        f"hunk header counts exceeded at line {i + 1} "
                        f"(extra '+' line)")
                need_new -= 1
            else:
                raise PatchError(
                    "PATCH_MALFORMED",
                    f"line {i + 1} does not start with ' ', '+' or '-' inside "
                    f"a hunk: {raw[:60]!r} (truncated patch?)")
            h.lines.append(DiffLine(tag=tag, text=text))
            i += 1
        if need_old > 0 or need_new > 0:
            raise PatchError(
                "PATCH_MALFORMED",
                f"truncated hunk at line {i}: header promised "
                f"{h.old_count} old / {h.new_count} new lines, patch ended early")
        if h.old_count == 0 and h.new_count == 0:
            raise PatchError(
                "PATCH_MALFORMED", f"hunk at line {i} carries no lines")
        # NOTE: a hunk with zero context lines is NOT rejected here — a
        # whole-file deletion is exactly that. The unique-match requirement
        # in _find_unique still anchors it; only old_count == 0 hunks (no
        # old lines at all) are unverifiable and are refused at apply time.
        hunks.append(h)
    return hunks, i


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Parse a unified diff into per-file sections. Raises PatchError."""
    if not text or not text.strip():
        raise PatchError("PATCH_EMPTY", "patch is empty")
    lines = text.splitlines()
    patches: list[FilePatch] = []
    i = 0
    n = len(lines)
    saw_hunk = False
    while i < n:
        line = lines[i]
        if line.startswith("--- "):
            if i + 1 >= n or not lines[i + 1].startswith("+++ "):
                raise PatchError(
                    "PATCH_MALFORMED",
                    f"'--- ' header at line {i + 1} has no '+++ ' partner line")
            try:
                old_path = sanitize_patch_path(line[4:])
                new_path = sanitize_patch_path(lines[i + 1][4:])
            except PatchError:
                raise
            if old_path is None and new_path is None:
                raise PatchError(
                    "PATCH_MALFORMED",
                    "file section has /dev/null on BOTH sides")
            i += 2
            hunks, i = _parse_hunks(lines, i)
            if not hunks:
                raise PatchError(
                    "PATCH_MALFORMED",
                    f"file section for '{new_path or old_path}' has no @@ hunks")
            saw_hunk = True
            patches.append(FilePatch(old_path=old_path, new_path=new_path,
                                     hunks=hunks))
            continue
        if line.startswith(_HEADER_NOISE_PREFIXES):
            i += 1
            continue
        if line.startswith("@@"):
            raise PatchError(
                "PATCH_MALFORMED",
                f"'@@' hunk at line {i + 1} appears before any '--- /+++' "
                "file header")
        if not line.strip():
            i += 1
            continue
        raise PatchError(
            "PATCH_MALFORMED",
            f"line {i + 1} is not part of a unified diff: {line[:60]!r}")
    if not patches or not saw_hunk:
        raise PatchError(
            "PATCH_EMPTY", "no file sections with hunks found in patch")
    return patches


# ── application (pure: text in, text out) ──────────────────────────────

def apply_hunks_to_text(original_lf: str, hunks: list[Hunk], *,
                        is_new_file: bool = False,
                        is_delete: bool = False) -> str:
    """Apply one file's hunks to the LF-normalized original.

    Returns the new LF text ('' for a deletion). Raises PatchError with
    PATCH_CONTEXT_NOT_FOUND / PATCH_CONTEXT_NOT_UNIQUE / PATCH_HUNK_ORDER /
    PATCH_MALFORMED / PATCH_UNVERIFIABLE."""
    if is_new_file:
        for h in hunks:
            bad = [l for l in h.lines if l.tag in (" ", "-")]
            if bad:
                raise PatchError(
                    "PATCH_MALFORMED",
                    "new-file hunk carries context/deletion lines (the file "
                    "does not exist yet)")
        out = []
        for h in hunks:
            out.extend(h.new_lines())
        joined = "\n".join(out)
        if any(h.new_ends_without_newline for h in hunks):
            return joined
        return joined + "\n" if joined else ""

    src = original_lf.split("\n")
    trailing_nl = False
    if src and src[-1] == "":
        src.pop()
        trailing_nl = True
    out = list(src)
    cursor = 0  # exclusive end (line index) of the previous replacement
    for h in hunks:
        expected = h.old_lines()
        replacement = h.new_lines()
        if not expected:
            # insertion-only hunk (old_count == 0, no context lines): the
            # header line number alone cannot be verified against the file,
            # so a conservative engine refuses it (a drifted line number
            # would insert at the wrong place silently).
            raise PatchError(
                "PATCH_UNVERIFIABLE",
                "insertion hunk carries no context lines — include the "
                "surrounding lines so the insertion point is verifiable")
        pos = _find_unique(out, expected, h.old_start - 1)
        if pos < cursor:
            raise PatchError(
                "PATCH_HUNK_ORDER",
                "hunks overlap or a later hunk matched before an earlier "
                "hunk's replacement — re-generate the patch with hunks in "
                "file order")
        out[pos:pos + len(expected)] = replacement
        cursor = pos + len(replacement)
    result = "\n".join(out)
    if is_delete:
        if result.strip():
            raise PatchError(
                "PATCH_MALFORMED",
                "delete patch leaves content behind (only '-' and context "
                "lines are allowed)")
        return ""
    if trailing_nl and not any(h.new_ends_without_newline for h in hunks):
        result += "\n"
    return result


def _find_unique(lines: list[str], expected: list[str],
                 hint: int) -> int:
    """Find the unique occurrence of `expected` in `lines`.

    Line numbers may drift (patch produced against a slightly different
    revision), so the whole file is a candidate — but the block must match
    EXACTLY exactly once. The hint position is checked first (cheap common
    case), then the full scan collects every match. Raises
    PATCH_CONTEXT_NOT_FOUND / PATCH_CONTEXT_NOT_UNIQUE."""
    blen = len(expected)
    limit = len(lines) - blen
    if limit < 0:
        raise PatchError(
            "PATCH_CONTEXT_NOT_FOUND",
            "file is shorter than the patch context — read_file the current "
            "state and regenerate the patch")
    hint = min(max(hint, 0), limit)
    if lines[hint:hint + blen] == expected:
        # hinted hit: verify uniqueness with the full scan anyway (a block
        # that appears twice must be refused even when one hit is at hint)
        pass
    matches = [p for p in range(limit + 1)
               if lines[p:p + blen] == expected]
    if len(matches) > 1:
        raise PatchError(
            "PATCH_CONTEXT_NOT_UNIQUE",
            f"patch context matches {len(matches)} places — add more "
            "surrounding lines to the hunk")
    if not matches:
        raise PatchError(
            "PATCH_CONTEXT_NOT_FOUND",
            "patch context not found in the file (exact match required) — "
            "read_file the current state and regenerate the patch")
    return matches[0]
