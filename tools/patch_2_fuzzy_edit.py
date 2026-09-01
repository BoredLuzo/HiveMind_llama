


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
    n_norm = _norm(new_str)

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

    for start in range(max(0, c_len - o_len + 1)):
        if first_search and c_lines[start].strip() != first_search:
            continue

        cand = "\n".join(c_lines[start:start + o_len])
        ratio = difflib.SequenceMatcher(None, o_norm, cand).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start
            best_count = 1
        elif ratio == best_ratio and ratio >= 0.85:
            best_count += 1

    if best_ratio < 0.85 or best_count > 1:
        return None

    content_lines_keepends = content.splitlines(True)
    char_offset = sum(len(l) for l in content_lines_keepends[:best_start])
    orig_match = "\n".join(content.splitlines()[best_start:best_start + o_len])
    result = content[:char_offset] + new_str + content[char_offset + len(orig_match):]
    return result if result != content else None
