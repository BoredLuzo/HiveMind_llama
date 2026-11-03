# -*- coding: utf-8 -*-
"""--fit on/off toggle: layers/ctx sized to VRAM when enabled."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settings import DEFAULT_SETTINGS          # noqa: E402

ML = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
CFG = (ROOT / "backend" / "llama_config.py").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 fit is ON by default (toggle it off for a custom MoE split)",
      DEFAULT_SETTINGS.get("llama_fit") is True)
check("2 the loader passes --fit", '"--fit", "on"' in ML)
check("3 the flag is capability-checked",
      '_help_supports("--fit")' in ML)
check("4 fit omits --n-gpu-layers",
      "if _gpu_layers is not None and not _fit_supported:" in ML)
check("5 the target is passed when set", '"--fit-target"' in ML)
check("6 runtime globals exist", "LLAMA_FIT" in CFG)
check("7 the model tab has the toggle", 'id="llama-fit-toggle"' in HTML
      and 'id="llama-fit-target"' in HTML)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
