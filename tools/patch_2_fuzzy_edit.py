


from __future__ import annotations


import difflib


def _norm(s: str) -> str:
    """Normalisiere Whitespace fuer Fuzzy-Vergleich."""
    return "\n".join(l.rstrip() for l in s.splitlines())


def best_match_location(content: str, old_str: str) -> tuple[int, float] | None:
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
    o_norm = _norm(old_str)
    old_lines = o_norm.splitlines()
    c_lines = _norm(content).splitlines()
    o_len = len(old_lines)
    c_len = len(c_lines)
    if o_len == 0 or o_len > c_len:
        return None
    first_search = old_lines[0].strip()
    large_block = o_len >= 20
    best_start = -1
    best_ratio = 0.0
    for start in range(max(0, c_len - o_len + 1)):
        if not large_block and first_search and c_lines[start].strip() != first_search:
            continue
        if large_block:
            sm = difflib.SequenceMatcher(None, old_lines, c_lines[start:start + o_len])
            if sm.quick_ratio() < 0.5:
                continue
            ratio = sm.ratio()
        else:
            ratio = difflib.SequenceMatcher(
                None, o_norm, "\n".join(c_lines[start:start + o_len])).ratio()
        if ratio > best_ratio:
            best_start = start
            best_ratio = ratio
    return (best_start, best_ratio) if best_start >= 0 else None


def fuzzy_replace(content: str, old_str: str, new_str: str) -> str | None:


    if not old_str or not content:
        return None

    c_norm = _norm(content)
    o_norm = _norm(old_str)

    old_lines = o_norm.splitlines()
    c_lines = c_norm.splitlines()
    o_len = len(old_lines)
    c_len = len(c_lines)

    if o_len == 0 or o_len > c_len:
        return None

    first_search = old_lines[0].strip() if old_lines else ""

    best_start = -1
    best_ratio = 0.0
    good_windows = 0
    second_ratio = 0.0

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

    for start in range(max(0, c_len - o_len + 1)):
        if not large_block and first_search and c_lines[start].strip() != first_search:
            continue

        cand = "\n".join(c_lines[start:start + o_len])
        if large_block:
            sm = difflib.SequenceMatcher(None, old_lines, c_lines[start:start + o_len])
            if sm.quick_ratio() < 0.7:
                continue
            ratio = sm.ratio()
        else:
            ratio = difflib.SequenceMatcher(None, o_norm, cand).ratio()

        if ratio > best_ratio:
            second_ratio = best_ratio
            best_ratio = ratio
            best_start = start
            good_windows = 1
        elif ratio == best_ratio:
            good_windows += 1

    # AMBIGUITY (2026-09-17, margin rule): EVERY window above the threshold
    # counts — not just exact ties. AND a near-tie second window (within 0.05
    # of the best, e.g. 0.99 vs 0.985 in repetitive code) means the edit is
    # genuinely ambiguous: reject and let the model add surrounding context.
    if best_ratio < 0.85 or good_windows > 1 or second_ratio >= best_ratio - 0.05:
        return None

    # LINE-GRANULAR SPLICE (2026-09-16): cut exactly the o_len raw lines via
    # keepends — the old char-offset math left CRLF/terminator residue behind
    # the replacement (blank-line artifacts).
    keepends = content.splitlines(True)
    if best_start >= len(keepends):
        return None
    new_body = new_str if new_str.endswith("\n") else new_str + "\n"
    result = "".join(keepends[:best_start]) + new_body + "".join(keepends[best_start + o_len:])
    return result if result != content else None
