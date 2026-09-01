# -*- coding: utf-8 -*-
"""Token-Schaetzung fuer Context-Guards (chars/_CHARS_PER_TOKEN, Default 3.5)."""

_CHARS_PER_TOKEN = 3.5  # empirical for 35B MoE GGUF; raise to 4.0 if compression too rare

def estimate_ctx_tokens(messages: list[dict]) -> int:


    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        else:
            text = str(content)
        total += len(text) / _CHARS_PER_TOKEN
        for _tc in (m.get("tool_calls") or []):
            total += len(str(_tc.get("function", {}).get("arguments", ""))) / _CHARS_PER_TOKEN
    return total
