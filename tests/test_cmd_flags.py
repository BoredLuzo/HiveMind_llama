# -*- coding: utf-8 -*-
"""Command-line builder integrity (audit 2026-10-30).

A duplicated flag silently wins by position in llama.cpp - the 30.10. MTP
A/B ran with BOTH `--spec-type draft-mtp` (base) and `--spec-type none`
(appended), so the variant label could not be trusted. This test guards:
  1. the loader CMD contains no duplicated flags,
  2. the bench sweep builds exactly one --spec-type per variant,
  3. the generic helper catches duplicates when they exist.
"""
import os
import re
import sys
from pathlib import Path




ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="plain")

check = h.check


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


# 1. template resolution: BEHAVIOURAL (the earlier version grepped a source
# slice, which started matching the two mutually exclusive branches inside
# _append_chat_template and reported a duplicate that cannot happen).
src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
check("loader carries --ubatch-size exactly once as a literal",
      src.count('"--ubatch-size"') == 1,
      f"(count {src.count('"--ubatch-size"')})")
try:
    from backend.manager_load import _append_chat_template as _act
    c_reg = ["srv"]
    _act(c_reg, "hermes3.6", "chat_template22.5.jinja")
    check("registry template -> exactly ONE --chat-template-file",
          c_reg.count("--chat-template-file") == 1, f"({c_reg})")
    c_chain = ["srv"]
    _act(c_chain, "hermes3.6", "")
    check("no registry template -> exactly ONE from the fallback chain",
          c_chain.count("--chat-template-file") == 1, f"({c_chain})")
    c_other = ["srv"]
    _act(c_other, "groot", "")
    check("base outside the chain -> no template flag",
          "--chat-template-file" not in c_other, f"({c_other})")
except ImportError as e:
    check("_append_chat_template importable", False, f"({e})")

# 2. the sweep: base + variant, one --spec-type per variant
def _pick(env_name: str, *candidates):
    """First existing candidate (or the env override) - no user name needed."""
    _v = os.environ.get(env_name)
    if _v:
        return Path(_v)
    for _c in candidates:
        if _c.exists():
            return _c
    return candidates[0]


_ROOT = Path(__file__).resolve().parent.parent
_INSTALL = _pick("HIVEMIND_INSTALL_DIR", _ROOT.parent / "live", _ROOT / "live")
_MODELS = _pick("HIVEMIND_MODELS_DIR", _ROOT.parent.parent / "Models",
                _ROOT.parent / "Models")
_DEV = _pick("HIVEMIND_DEV_DIR", _ROOT.parent.parent / "HiveMind_dev",
             _ROOT.parent / "HiveMind_dev")

sweep = _DEV / "bench_sonnet_sweep.py"
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

sys.exit(h.finish("cmd_flags"))
