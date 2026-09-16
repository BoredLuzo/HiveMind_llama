


from __future__ import annotations


import difflib


def _norm(s: str) -> str:
    """Normalisiere Whitespace fuer Fuzzy-Vergleich."""
    return "\n".join(l.rstrip() for l in s.splitlines())


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
    best_count = 0

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
            best_ratio = ratio
            best_start = start
            best_count = 1
        elif ratio == best_ratio:
            best_count += 1

    if best_ratio < 0.85:
        return None
    if not large_block and best_count > 1:
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
