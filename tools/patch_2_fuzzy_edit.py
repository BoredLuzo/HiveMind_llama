


from __future__ import annotations

from collections import Counter
import difflib
import time


def _split_lines_keepends(s: str) -> list[str]:
    """Split on LF only while preserving line endings so indices match _norm_lines."""
    text = str(s or "")
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
        return [p + "\n" for p in parts]
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def _split_raw_lines(s: str) -> list[str]:
    """Split raw block lines matching _norm_lines count."""
    n = str(s or "").replace("\r\n", "\n")
    if n.endswith("\n"):
        n = n[:-1]
    return n.split("\n") if n else []


def _norm_lines(s: str) -> list[str]:
    """Normalize line endings + trailing whitespace while preserving blank lines."""
    return [l.rstrip() for l in _split_raw_lines(s)]


def _sliding_line_overlaps(c_lines: list[str], old_lines: list[str]):
    """Yield ``(start, quick_ratio)`` in O(1) per window slide via multiset overlap.

    Because every candidate slice ``c_lines[start:start + len(old_lines)]`` has
    the exact same length ``o_len`` as ``old_lines``, ``overlap / o_len`` equals
    ``difflib.SequenceMatcher(None, old_lines, slice).quick_ratio()`` without
    allocating a SequenceMatcher per position.
    """
    o_len = len(old_lines)
    limit = len(c_lines) - o_len
    if limit < 0 or o_len <= 0:
        return
    old_counts = Counter(old_lines)
    win_counts: dict[str, int] = {}
    overlap = 0
    for elem in c_lines[:o_len]:
        prev = win_counts.get(elem, 0)
        if prev < old_counts.get(elem, 0):
            overlap += 1
        win_counts[elem] = prev + 1

    for start in range(limit + 1):
        yield start, overlap / o_len
        if start < limit:
            out_elem = c_lines[start]
            in_elem = c_lines[start + o_len]
            if out_elem != in_elem:
                prev_out = win_counts[out_elem]
                if prev_out <= old_counts.get(out_elem, 0):
                    overlap -= 1
                if prev_out <= 1:
                    win_counts.pop(out_elem, None)
                else:
                    win_counts[out_elem] = prev_out - 1
                prev_in = win_counts.get(in_elem, 0)
                if prev_in < old_counts.get(in_elem, 0):
                    overlap += 1
                win_counts[in_elem] = prev_in + 1


def best_match_location(
    content: str,
    old_str: str,
    *,
    max_seconds: float = 2.0,
) -> tuple[int, float] | None:
    """Approximate location of the closest old_str window in content.

    Audit phase 5 (2026-10-14): the edit_file failure anchor used to attach a
    fixed 60-line file head (up to 4000 chars per FAILED attempt); with this
    the anchor shows +/-5 REAL lines around where old_text almost matched.
    Deliberately NOT the fuzzy_replace scorer — lower gates, threshold-free:
    it must return a location even when no window is good enough to edit.
    Returns (0-based line index, best ratio) or None when nothing scores.
    """
    if not old_str or not content:
        return None
    old_lines = _norm_lines(old_str)
    o_norm = "\n".join(old_lines)
    c_lines = _norm_lines(content)
    o_len = len(old_lines)
    c_len = len(c_lines)
    if o_len == 0 or o_len > c_len:
        return None
    first_search = old_lines[0].strip()
    large_block = o_len >= 20
    best_start = -1
    best_ratio = 0.0
    deadline = time.monotonic() + max_seconds
    if large_block:
        for start, qr in _sliding_line_overlaps(c_lines, old_lines):
            if (start & 255) == 0 and (max_seconds <= 0.0 or time.monotonic() > deadline):
                break
            # AUTOJUNK-OFF (2026-10-15): autojunk purges every element that
            # occurs in >1% of b once len(b) >= 200.
            if qr < 0.5 or qr <= best_ratio:
                continue
            ratio = difflib.SequenceMatcher(
                None, old_lines, c_lines[start:start + o_len], autojunk=False
            ).ratio()
            if ratio > best_ratio:
                best_start = start
                best_ratio = ratio
    else:
        for start in range(max(0, c_len - o_len + 1)):
            if (start & 255) == 0 and (max_seconds <= 0.0 or time.monotonic() > deadline):
                break
            if first_search and c_lines[start].strip() != first_search:
                continue
            sm = difflib.SequenceMatcher(
                None, o_norm, "\n".join(c_lines[start:start + o_len]), autojunk=False
            )
            if sm.quick_ratio() <= best_ratio:
                continue
            ratio = sm.ratio()
            if ratio > best_ratio:
                best_start = start
                best_ratio = ratio
    return (best_start, best_ratio) if best_start >= 0 else None


def fuzzy_replace(
    content: str,
    old_str: str,
    new_str: str,
    *,
    max_seconds: float = 2.0,
) -> str | None:

    if not old_str or not content:
        return None

    old_lines = _norm_lines(old_str)
    c_lines = _norm_lines(content)
    o_norm = "\n".join(old_lines)
    o_len = len(old_lines)
    c_len = len(c_lines)

    if o_len == 0 or o_len > c_len:
        return None

    first_search = old_lines[0].strip() if old_lines else ""

    best_start = -1
    best_ratio = 0.0
    candidates: list[tuple[int, float]] = []

    # LARGE-BLOCK MODE (2026-09-16): for big SEARCH blocks the first-line
    # anchor + char-level ratio are too brittle — one drifted line inside the
    # block (or a changed first line) killed the match entirely. For blocks
    # >= 20 lines: slide a line-window over the file, gate with quick_ratio on
    # LINE lists, accept at line-ratio >= 0.85 — drift-tolerant for single
    # changed lines (197/200 = 0.985 passes), but a window matching a wrong
    # region at <= 15% similarity never does. The line-granular splice below
    # replaces exactly the o_len raw lines, so a match can never eat
    # neighboring lines. Small blocks keep the strict first-line + char-ratio
    # 0.85 path.
    large_block = o_len >= 20
    deadline = time.monotonic() + max_seconds

    if large_block:
        for start, qr in _sliding_line_overlaps(c_lines, old_lines):
            if (start & 255) == 0 and (max_seconds <= 0.0 or time.monotonic() > deadline):
                return None  # fail safe: never apply partial scan without ambiguity check
            if qr < 0.7:
                continue
            ratio = difflib.SequenceMatcher(
                None, old_lines, c_lines[start:start + o_len], autojunk=False
            ).ratio()
            if ratio >= 0.80:
                candidates.append((start, ratio))
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = start
    else:
        for start in range(max(0, c_len - o_len + 1)):
            if (start & 255) == 0 and (max_seconds <= 0.0 or time.monotonic() > deadline):
                return None
            if first_search and c_lines[start].strip() != first_search:
                continue
            cand = "\n".join(c_lines[start:start + o_len])
            sm = difflib.SequenceMatcher(None, o_norm, cand, autojunk=False)
            if sm.quick_ratio() < 0.80:
                continue
            ratio = sm.ratio()
            if ratio >= 0.80:
                candidates.append((start, ratio))
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = start

    # AMBIGUITY (2026-09-17, margin rule + overlap scoping):
    # Check every OTHER candidate regardless of scan order (before OR after
    # best_start). Within the winning window's own overlap (< o_len lines away),
    # an exact tie (cand_ratio == best_ratio, e.g. 20 lines inside 21 identical
    # lines) is genuinely ambiguous; a strictly lower overlapping score is only
    # an artifact of shifting out of a unique exact (1.0) or >=20-line match,
    # while small drifted blocks still treat tight overlaps as ambiguous.
    if best_start < 0 or best_ratio < 0.85:
        return None
    for cand_start, cand_ratio in candidates:
        if cand_start == best_start:
            continue
        _overlaps = abs(cand_start - best_start) < o_len
        if _overlaps and cand_ratio < best_ratio and (large_block or best_ratio >= 1.0):
            continue
        if cand_ratio >= best_ratio - 0.05:
            return None

    # LINE-GRANULAR SPLICE (2026-09-16): cut exactly the o_len raw lines via
    # keepends — the old char-offset math left CRLF/terminator residue behind
    # the replacement (blank-line artifacts).
    keepends = _split_lines_keepends(content)
    if best_start >= len(keepends):
        return None
    _eol = "\r\n" if "\r\n" in "".join(keepends[best_start:best_start + o_len]) else "\n"
    if new_str == "":
        new_body = ""
    else:
        _rep_str = new_str
        actual_raw = [ln.rstrip("\r\n") for ln in keepends[best_start:best_start + o_len]]
        old_raw = _split_raw_lines(old_str)
        if any(a != o and a.lstrip() == o.lstrip() for a, o in zip(actual_raw, old_raw)):
            from tools.patch_diff import reindent_matched_block
            new_raw = _split_raw_lines(new_str)
            _reindented = reindent_matched_block(actual_raw, old_raw, new_raw)
            if _reindented is not None:
                _rep_str = _eol.join(_reindented) + _eol
        _rep_norm = _rep_str.replace("\r\n", "\n")
        if _eol == "\r\n":
            _rep_norm = _rep_norm.replace("\n", "\r\n")
        new_body = _rep_norm if _rep_norm.endswith("\n") else _rep_norm + _eol
    result = "".join(keepends[:best_start]) + new_body + "".join(keepends[best_start + o_len:])
    return result if result != content else None
