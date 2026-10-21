"""Post-Run-Hooks: warmes Modell zuerst, Judge zuletzt.

Anlass (live): nach jedem Run wurde `agents.judge.model` (LFM) geladen, dafuer
der warme Coder evictet. Der Test pinnt die Prioritaet und den Quelltext.
"""
from __future__ import annotations

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


from learning import insights as I  # noqa: E402

SRC = (ROOT / "learning" / "insights.py").read_text(encoding="utf-8", errors="replace")

# ── 1. Prioritaet: warmes Modell gewinnt ───────────────────────────────
_orig_loaded = I._loaded_model_name
_orig_reg = I._registry_get

I._loaded_model_name = lambda: "hermes3.6:35b-coder"
I._registry_get = lambda role: {"refiner": "qwen3.5:2b", "analyst": "qwen3.5:4b-mtp",
                                "judge": "lfm2.5:2.6b"}.get(role, "")
try:
    warm = I._post_run_worker_model()
finally:
    I._loaded_model_name = _orig_loaded
    I._registry_get = _orig_reg
check("a warm model wins over any role lookup", warm == "hermes3.6:35b-coder", f"({warm})")

# ── 2. Ohne warmes Modell: refiner -> analyst -> judge ─────────────────
def _pick(roles: dict) -> str:
    I._loaded_model_name = lambda: None
    I._registry_get = lambda role: roles.get(role, "")
    try:
        return I._post_run_worker_model()
    finally:
        I._loaded_model_name = _orig_loaded
        I._registry_get = _orig_reg


check("refiner is preferred", _pick({"refiner": "R", "analyst": "A", "judge": "J"}) == "R")
check("analyst beats the judge", _pick({"analyst": "A", "judge": "J"}) == "A")
check("judge is the last resort", _pick({"judge": "J"}) == "J")
check("no roles at all yields empty", _pick({}) == "")

# ── 3. Beide Hooks benutzen den Helfer, keiner greift mehr direkt zum Judge ─
check("extractor uses the helper", "_extractor_model = _post_run_worker_model()" in SRC)
check("distiller uses the helper", "_distiller_model = _post_run_worker_model()" in SRC)
check("no direct judge preference is left",
      '_registry_get("judge") or _registry_get' not in SRC)
check("helper documents the eviction reason", "still-warm coder got evicted" in SRC)
check("helper lists the role ladder in order",
      '("refiner", "analyst", "judge")' in SRC)

# ── 4. Der RAM-Guard bleibt erhalten ───────────────────────────────────
check("low-RAM guard still present", "def _low_ram_skip(" in SRC)
check("warm-model reuse is still documented for low RAM",
      "reusing loaded model" in SRC)

print()
print(f"SUMMARY post_run_model: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
