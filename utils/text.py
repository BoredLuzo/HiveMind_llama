# -*- coding: utf-8 -*-
"""Text-Utilities (aus server.py extrahiert)."""
import re


def is_truncated(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if len(t) < 6:
        return True
    last_third = t[-(max(30, len(t) // 3)):]
    has_sentence_end = bool(re.search(r'[.!?\n»…]', last_third))
    last_char = t[-1]
    mid_word = last_char.isalpha() or last_char in '-'
    return mid_word and not has_sentence_end


def build_fix_insight_sentence(failed_cmd: str, failed_excerpt: str, changed_files: list[str]) -> str:
    """Generates one compact learned insight after a fail->fix->pass run_bash cycle."""
    _cmd = (failed_cmd or "run_bash command").strip()[:120]
    _files = ", ".join(changed_files[:3]) or "unknown files"
    _excerpt = (failed_excerpt or "").strip()[:200]
    return (
        f"Fix applied after '{_cmd}' failed ({_excerpt}). "
        f"Changed: {_files}."
    )
