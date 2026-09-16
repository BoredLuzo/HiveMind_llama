


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
    # LINE lists, accept at line-ratio >= 0.7. Small blocks keep the strict
    # first-line + char-ratio 0.85 path.
    large_block = o_len >= 20

    for start in range(max(0, c_len - o_len + 1)):
        if not large_block and first_search and c_lines[start].strip() != first_search:
            continue

        cand = "\n".join(c_lines[start:start + o_len])
        if large_block:
            sm = difflib.SequenceMatcher(None, old_lines, c_lines[start:start + o_len])
            if sm.quick_ratio() < 0.6:
                continue
            ratio = sm.ratio()
            _threshold = 0.7
        else:
            ratio = difflib.SequenceMatcher(None, o_norm, cand).ratio()
            _threshold = 0.85

        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start
            best_count = 1
        elif ratio == best_ratio and ratio >= _threshold:
            best_count += 1

    if large_block:
        # repetitive code ties windows at near-equal line ratios; the strict
        # ambiguity counter would kill valid matches. Best window wins.
        if best_ratio < 0.7:
            return None
    elif best_ratio < 0.85 or best_count > 1:
        return None

    content_lines_keepends = content.splitlines(True)
    char_offset = sum(len(l) for l in content_lines_keepends[:best_start])
    orig_match = "\n".join(content.splitlines()[best_start:best_start + o_len])
    result = content[:char_offset] + new_str + content[char_offset + len(orig_match):]
    return result if result != content else None
