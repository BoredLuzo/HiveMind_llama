# -*- coding: utf-8 -*-
"""Cache-freundliche Kontext-Guard-Entscheidungen (pure, testbar).

Design (2026-09-04):
- llama.cpp prefix cache braucht einen byte-/token-identischen Prompt-Verlauf.
- Die Entscheidung "wann komprimieren vs. wann (nur als Notfall) in-place
  evicten" wird hier als pure Funktion gekapselt, damit sie ohne den
  duo_runner-Loop unit-getestet werden kann.
- Schwelle (auto, wenn kein UI-Override):
    guard_tokens > P1 (Cache-Floor: auto_floor * ctx)   ODER
    ctx - guard_tokens < Reserve  (Overflow-Safety fuer kleine Ctx / grosse
    num_predict)
  Effektivschwelle = min(P1, ctx - reserve) -> komprimiert wird beim
  frueheren der beiden Ausloeser. Bei grossen Kontexten dominiert P1.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("hivemind.ctx_guard")

# ── Defaults (werden pro Aufruf ueber die Settings ueberschrieben) ──────────
DEFAULT_COMPRESS_FLOOR = 0.72          # P1: komprimieren ab X% Kontext
DEFAULT_EMERGENCY_PCT = 0.90           # Notfall-Eviction erst >90%
DEFAULT_OVERFLOW_RESERVE = 1024        # extra Reserve neben num_predict (P2)
DEFAULT_MIN_FREE_TOKENS = 0            # 0 = auto (siehe resolve_compress_threshold)
DEFAULT_PARTIAL_POST_FRACTION = 0.60   # partial-Ziel: Kontext <=60% nach Kompression
DEFAULT_PARTIAL_MIN_TAIL_MSGS = 8      # Mindest-Raw-Tail (Nachrichten) bei partial


@dataclass
class ContextDecision:
    """Entscheidung der Guard-Logik fuer die naechste Tool-Round."""
    action: str = "none"               # none | compress | emergency_evict
    reason: str = ""
    compress_mode: str = "full"        # full | partial (nur bei action=compress)
    cut_index: int = -1                # fuer partial; -1 = vom Aufrufer berechnen
    can_compress: bool = True
    threshold: int = 0
    extras: dict = field(default_factory=dict)


def _to_num(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_compress_threshold(
    *,
    ctx_tokens: int,
    num_predict: int = 0,
    ui_threshold: int = 0,
    auto_floor: float = DEFAULT_COMPRESS_FLOOR,
    min_free_tokens: int = DEFAULT_MIN_FREE_TOKENS,
    overflow_reserve: int = DEFAULT_OVERFLOW_RESERVE,
) -> int:
    """Effektive Kompressionsschwelle (guard_tokens ab der komprimiert wird).

    - ui_threshold > 0  -> exakter UI-Override (absolute Tokens).
    - sonst auto: min(P1 = auto_floor*ctx, P2 = ctx - reserve), wobei
      reserve = max(num_predict + overflow_reserve, min_free_tokens).
    """
    ctx_tokens = int(ctx_tokens or 0)
    if ctx_tokens <= 0:
        return 0
    if ui_threshold is not None and int(ui_threshold) > 0:
        return int(ui_threshold)

    floor = _to_num(auto_floor, DEFAULT_COMPRESS_FLOOR)
    floor = max(0.10, min(0.95, floor))
    p1 = int(ctx_tokens * floor)

    num_predict = max(0, int(num_predict or 0))
    reserve = max(
        num_predict + int(overflow_reserve or 0),
        int(min_free_tokens or 0),
    )
    p2 = max(0, ctx_tokens - reserve)

    threshold = min(p1, p2) if p2 > 0 else p1
    return max(1, threshold)


def decide_context_action(
    *,
    guard_tokens: int,
    ctx_tokens: int,
    threshold: int = 0,
    can_compress: bool = True,
    force_compress: bool = False,
    swa_warn: bool = False,
    emergency_pct: float = DEFAULT_EMERGENCY_PCT,
) -> ContextDecision:
    """Entscheide zwischen `compress`, `emergency_evict` und `none`.

    Regeln:
    - force_compress / swa_warn / guard > threshold / guard > 90%  -> compress,
      sofern Kompression verfuegbar (can_compress).
    - Ist Kompression nicht mehr verfuegbar (Cap erreicht) und guard > 90%,
      bleibt nur noch emergency_evict (In-place, akzeptierter Cache-Bust).
    - Unterhalb dessen: nichts tun (append-only, Cache bleibt intakt).
    """
    ctx_tokens = int(ctx_tokens or 0)
    guard = int(guard_tokens or 0)

    dec = ContextDecision(can_compress=bool(can_compress))
    if ctx_tokens <= 0 or guard <= 0:
        dec.action = "none"
        dec.reason = "no-context"
        return dec

    if threshold is None or int(threshold) <= 0:
        threshold = resolve_compress_threshold(ctx_tokens=ctx_tokens)
    dec.threshold = int(threshold)

    emergency_tokens = int(ctx_tokens * _to_num(emergency_pct, DEFAULT_EMERGENCY_PCT))

    over_threshold = guard > dec.threshold
    over_emergency = guard > emergency_tokens

    if can_compress and (force_compress or swa_warn or over_threshold or over_emergency):
        reason = (
            "force" if force_compress else
            ("swa" if swa_warn else
             ("emergency-90" if over_emergency else "threshold"))
        )
        dec.action = "compress"
        dec.reason = reason
        dec.extras = {"guard_tokens": guard, "emergency_tokens": emergency_tokens}
        return dec

    if not can_compress and over_emergency:
        dec.action = "emergency_evict"
        dec.reason = "no-compress-cap-reached"
        dec.extras = {"guard_tokens": guard, "emergency_tokens": emergency_tokens}
        return dec

    dec.action = "none"
    dec.reason = "append-only"
    return dec


def should_use_partial(
    *,
    partial_enabled: bool,
    messages: list,
    min_tail_msgs: int = DEFAULT_PARTIAL_MIN_TAIL_MSGS,
) -> bool:
    """Partial-Kompression nur, wenn genug "alte" History vor dem Tail liegt.

    Der Rebuild ersetzt [.. cut ..] durch eine Summary und haelt den Tail
    ab cut byte-identisch am Ende (-> llama.cpp --cache-reuse KV-Shift kann
    den Suffix retten). Braucht mindestens ein paar Nachrichten vor dem Tail.
    """
    if not partial_enabled:
        return False
    msgs = messages or []
    if len(msgs) < int(min_tail_msgs) + 2:
        return False
    return True


def _msg_chars(msg) -> int:
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, list):
        c = " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in c
        )
    return len(str(c or ""))


def plan_partial_cut_index(
    messages: list,
    *,
    ctx_tokens: int,
    guard_tokens: int,
    target_post_fraction: float = DEFAULT_PARTIAL_POST_FRACTION,
    min_tail_msgs: int = DEFAULT_PARTIAL_MIN_TAIL_MSGS,
    estimate_fn: Optional[Callable[[list], int]] = None,
) -> int:
    """Cut-Index fuer partial-Kompression.

    Akkumuliert ab vorne geschaetzte Tokens bis der verbleibende Tail
    (ab cut) <= target_post_fraction * ctx waere. Liefert -1, wenn kein
    sinnvoller Cut existiert (Aufrufer faellt dann auf full zurueck).
    """
    msgs = messages or []
    n = len(msgs)
    min_tail = max(2, int(min_tail_msgs or 0))
    if n < min_tail + 2:
        return -1

    target = int(ctx_tokens * _to_num(target_post_fraction, DEFAULT_PARTIAL_POST_FRACTION))
    target = max(1, min(target, int(guard_tokens or 0)))

    # Gesamtlaenge ueber Char-Heuristik (3.5 Zeichen/Tok), konsistent mit
    # utils.token.estimate_ctx_tokens, wenn kein estimate_fn uebergeben wird.
    total = int(guard_tokens or 0)
    if total <= 0:
        total = sum(_msg_chars(m) for m in msgs) // 3

    if total <= target:
        return -1

    to_remove = total - target
    acc = 0
    for i, m in enumerate(msgs):
        acc += _msg_chars(m) // 3
        # Nachricht i wird Teil des zu komprimierenden Alt-Teils. cut zeigt auf
        # den ersten Message-Index, der roh erhalten bleibt.
        cut = i + 1
        if n - cut < min_tail:
            break
        if acc >= to_remove:
            return max(1, cut)
    # Nicht genug entfernbar, ohne den Mindest-Tail zu verletzen.
    return -1
