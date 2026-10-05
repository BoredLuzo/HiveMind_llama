# -*- coding: utf-8 -*-
"""Token-Schaetzung fuer Context-Guards (chars/CHARS_PER_TOKEN, Default 3.0).

2026-09-05 Kalibrierung 3.5 -> 3.0: Live-Messung (est vs. reale prompt_tokens)
ergab ~2.7-2.9 chars/tok; 3.0 haelt die Anzeige nahe am realen Wert und laesst
den UI/Guard-Schaetzer nicht mehr um ~20% unterzaehlen.
"""

CHARS_PER_TOKEN = 3.0  # zentrale Konstante (chars pro Token)

# Flat per-image charge (2026-10-03): image content parts counted as 0
# tokens, so every vision request undercounted the context and a small ctx
# budget could overflow into truncated or rejected prompts server-side.
# Real projector cost varies by model (llava-style ~576, dynamic-patch VL
# up to ~1300 at 1568px); 1024 sits deliberately on the high side - an
# overestimate makes the guards shrink context early, which is cheap,
# while an underestimate breaks runs.
IMAGE_TOKENS_ESTIMATE = 1024

def window_by_budget(messages: list[dict], budget_tokens: int) -> list[dict]:
    """Newest-last window under a token budget (2026-10-03).

    Replaces fixed message-count caps: walk the list newest-first and keep
    messages while the accumulated estimate stays inside the budget. The
    NEWEST message always survives (a giant single turn still reaches the
    prompt, truncated by the caller's char caps), older turns are dropped
    whole - never mid-sentence."""
    if not messages:
        return []
    # HARD FILTER (2026-10-03): this window is for seeded chat text only.
    # A boundary landing inside an assistant(tool_calls)+tool pair would
    # emit an orphaned half that servers reject - drop anything that is
    # not a plain user/assistant turn, whatever the caller passes in.
    messages = [m for m in messages
                if m.get("role") in ("user", "assistant")
                and not m.get("tool_calls")]
    if not messages:
        return []
    kept: list = []
    total = 0
    for m in reversed(messages):
        est = estimate_ctx_tokens([m])
        if kept and total + est > budget_tokens:
            break
        kept.insert(0, m)
        total += est
    return kept


def estimate_ctx_tokens(messages: list[dict]) -> float:

    total = 0.0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
            _images = sum(
                1 for p in content
                if isinstance(p, dict) and str(p.get("type", "")) == "image_url"
            )
            total += _images * IMAGE_TOKENS_ESTIMATE
        else:
            text = str(content)
        total += len(text) / CHARS_PER_TOKEN
        for _tc in (m.get("tool_calls") or []):
            total += len(str(_tc.get("function", {}).get("arguments", ""))) / CHARS_PER_TOKEN
    return total
