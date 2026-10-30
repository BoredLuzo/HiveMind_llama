# -*- coding: utf-8 -*-
"""Command-line builder integrity (audit 2026-10-30).

A duplicated flag silently wins by position in llama.cpp - the 30.10. MTP
A/B ran with BOTH `--spec-type draft-mtp` (base) and `--spec-type none`
(appended), so the variant label could not be trusted. This test guards:
  1. the loader CMD contains no duplicated flags,
  2. the bench sweep builds exactly one --spec-type per variant,
  3. the generic helper catches duplicates when they exist.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def duplicate_flags(argv):
    """Flags (tokens starting with '-') that appear more than once."""
    seen, dups = set(), []
    for tok in argv:
        if not isinstance(tok, str) or not tok.startswith("-"):
            continue
        if re.match(r"^-\d", tok):   # negative numbers are values, not flags
            continue
        if tok in seen and tok not in dups:
            dups.append(tok)
        seen.add(tok)
    return dups


# 1. loader CMD literal
src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
start = src.index('cmd += [')
block = src[start:start + 2000]
flags = re.findall(r'"(--[a-z0-9-]+)"', block)
dups = [f for f in set(flags) if flags.count(f) > 1]
check("loader CMD: no duplicated flag in the literal", not dups, f"({dups})")
check("loader CMD carries --ubatch-size once",
      flags.count("--ubatch-size") == 1 or "--ubatch-size" not in flags,
      f"(count {flags.count('--ubatch-size')})")

# 2. the sweep: base + variant, one --spec-type per variant
sweep = Path(r"C:/Users/NtheP/Desktop/HiveMind_dev/bench_sonnet_sweep.py")
if sweep.exists():
    sw = sweep.read_text(encoding="utf-8")
    base_block = sw[sw.index("BASE_SERVER_CMD = ["):sw.index("]", sw.index("BASE_SERVER_CMD = ["))]
    check("sweep base: no --spec-type (variants own it)",
          "--spec-type" not in base_block)
    variants = re.findall(r'\("(mtp_[a-z0-9_]+)", \[([^\]]*)\]\)', sw)
    check("sweep has 3 MTP variants", len(variants) == 3, f"({len(variants)})")
    for name, body in variants:
        n = body.count('"--spec-type"')
        check(f"sweep variant {name}: exactly one --spec-type", n == 1, f"({n})")
else:
    print("  SKIP sweep check (script outside the repo)")

# 3. helper sanity
check("helper finds a duplicate",
      duplicate_flags(["--a", "1", "--b", "2", "--a", "3"]) == ["--a"])
check("helper accepts unique flags",
      duplicate_flags(["--a", "1", "--b", "2"]) == [])
check("helper ignores values that start with -",
      duplicate_flags(["--a", "-1", "--b", "-1"]) == [])

print(f"\ncmd_flags: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
