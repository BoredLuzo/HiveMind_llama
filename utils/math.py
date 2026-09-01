# -*- coding: utf-8 -*-
"""Mathematische Hilfsfunktionen (aus server.py extrahiert)."""


def percentile_float(values: list[float], q: float) -> float:
    """Simple percentile helper without external dependencies."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 0.0
    vals.sort()
    if len(vals) == 1:
        return vals[0]
    qn = max(0.0, min(1.0, float(q)))
    idx = int(round((len(vals) - 1) * qn))
    return vals[idx]
