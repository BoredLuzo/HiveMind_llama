"""Shared source readers for static test suites.

SPLIT-AWARE (2026-11-05): DEFAULT_SETTINGS/DEFAULT_AGENT_CFG moved from
settings.py to settings_defaults.py. Static suites that grep the defaults
must see BOTH files — this helper concatenates them in a stable order so a
key finds its way no matter which file holds it.
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def settings_source() -> str:
    """settings.py + settings_defaults.py as one searchable text."""
    out = []
    for rel in ("settings.py", "settings_defaults.py"):
        f = _ROOT / rel
        if f.exists():
            out.append(f"# ── {rel} ──\n" + f.read_text(encoding="utf-8",
                                                 errors="replace"))
    return "\n".join(out)


def repo_src(rel: str) -> str:
    """Read one repo file relative to ROOT (errors='replace')."""
    return (_ROOT / rel).read_text(encoding="utf-8", errors="replace")
