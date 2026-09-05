# -*- coding: utf-8 -*-
"""Token-Schaetzung fuer Context-Guards (chars/CHARS_PER_TOKEN, Default 3.0).

2026-09-05 Kalibrierung 3.5 -> 3.0: Live-Messung (est vs. reale prompt_tokens)
ergab ~2.7-2.9 chars/tok; 3.0 haelt die Anzeige nahe am realen Wert und laesst
den UI/Guard-Schaetzer nicht mehr um ~20% unterzaehlen.
"""

CHARS_PER_TOKEN = 3.0  # zentrale Konstante (chars pro Token)

def estimate_ctx_tokens(messages: list[dict]) -> float:

    total = 0.0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        else:
            text = str(content)
        total += len(text) / CHARS_PER_TOKEN
        for _tc in (m.get("tool_calls") or []):
            total += len(str(_tc.get("function", {}).get("arguments", ""))) / CHARS_PER_TOKEN
    return total
