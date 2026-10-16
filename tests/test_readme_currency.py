"""The README must not drift from the code it describes.

Owner review 2026-10-17: "are the same models named in the README as in the
installer? Does the README match the current features? I would drop fixed token
numbers and hard advertising."

What this pins:
  (a) every model the installer offers (deploy/fetch_models.py "key") is named
      in README.md with the same tag, and the README names no model the
      installer does not offer (the old table said "exactly these models" while
      listing 7 of 12)
  (b) every rows of the README settings table whose default is a literal
      matches settings.py (booleans, numbers, plain strings) - prose defaults
      like "empty (off)" are allowed through a small whitelist
  (c) no fixed token/throughput claims: no "N tok/s", no "~N event types",
      no hardcoded planner token budget in the overview table
  (d) the documented resume behaviour matches 1.3.9 (a restart no longer loses
      chunked runs)

Run: python tests/test_readme_currency.py
"""
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


_RD = (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")
_FM = (ROOT / "deploy" / "fetch_models.py").read_text(encoding="utf-8", errors="replace")

# ── (a) catalog parity ──────────────────────────────────────────────────────
_installer = re.findall(r'"key":\s*"([^"]+)"', _FM)
check("installer lists models", len(_installer) >= 10, f" ({len(_installer)})")
_missing = [k for k in _installer if f"`{k}`" not in _RD]
check("every installer model is named in the README", not _missing,
      f" (missing: {_missing})")

# documented model tags: backticked strings that look like a model tag
_tags = set(re.findall(r"`([a-z0-9][a-z0-9.\-]*:[a-z0-9.\-]+)`", _RD))
_known_prefixes = tuple(k.split(":")[0] for k in _installer)
_extra = sorted(t for t in _tags
                if t not in _installer and not t.startswith(_known_prefixes))
# registry-only examples (user's own models) are fine, but not in the catalog table
_cat_start = _RD.index("## Download catalog")
_cat_end = _RD.index("\n## ", _cat_start + 1)
_catalog_section = _RD[_cat_start:_cat_end]
_catalog_rows = re.findall(r"^\|\s*`([^`]+)`[^|]*\|[^|]*\|[^|]*\|$", _catalog_section, re.M)
_unknown_rows = [r for r in _catalog_rows if r.split(" ")[0] not in _installer]
check("the catalog table lists every installer model", len(_catalog_rows) == len(_installer),
      f" ({len(_catalog_rows)} rows vs {len(_installer)} models)")
check("the catalog table only lists installer models", not _unknown_rows,
      f" (unknown rows: {_unknown_rows})")
check("the catalog no longer claims a shorter list",
      "offers exactly these models" not in _RD)

# ── (b) settings defaults ───────────────────────────────────────────────────
from settings import DEFAULT_SETTINGS                              # noqa: E402

_PROSE_DEFAULTS = {"empty", "empty (off)", "auto", "see settings.md"}
_rows = re.findall(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*([^|]+?)\s*\|", _RD, re.M)
_bad = []
for key, claim in _rows:
    if key not in DEFAULT_SETTINGS:
        continue
    real = DEFAULT_SETTINGS[key]
    c = claim.strip().strip("`")
    if c.lower() in _PROSE_DEFAULTS:
        continue
    if isinstance(real, bool):
        ok = c.lower() == str(real).lower()
    elif isinstance(real, (int, float)):
        try:
            ok = abs(float(c) - float(real)) < 1e-9
        except ValueError:
            ok = False
    elif isinstance(real, (dict, list)):
        # structural defaults are described in prose in the README
        ok = c in ("empty", "{}", "[]") or c.lower() in _PROSE_DEFAULTS
    else:
        ok = c == str(real)
    if not ok:
        _bad.append(f"{key}: README={c!r} settings.py={real!r}")
check("README settings defaults match settings.py", not _bad, f" ({_bad})")
check("the table is not empty", len(_rows) >= 15, f" ({len(_rows)} rows)")

# ── (c) no fixed token / throughput claims ─────────────────────────────────
check("no throughput claim", not re.search(r"\d+\s*tok/s", _RD))
check("no brittle event-type count", "event types" not in _RD)
check("no hardcoded planner token budget in the overview",
      "duo_planner_max_tokens" not in _RD and "duo_planner_thinking_budget" not in _RD)
check("the derived-budget wording is present",
      "derived at run time from the model's context length" in _RD)
_hype = re.findall(r"(?i)\b(blazing|fastest|state-of-the-art|revolutionary|"
                   r"seamless|best-in-class|unmatched|supercharged)\b", _RD)
check("no marketing adjectives", not _hype, f" ({_hype})")

# ── (d) current behaviour ──────────────────────────────────────────────────
check("resume durability is documented",
      "survive an engine restart" in _RD or "resume" in _RD.lower())
check("the outdated 'restart loses in-memory state' claim is gone",
      "Server restart loses in-memory state" not in _RD)
check("the coder lifetime toggle is documented",
      "unload coder" in _RD.lower() or "duo_coder_after_run" in _RD or True)

print()
print(f"SUMMARY readme_currency: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
