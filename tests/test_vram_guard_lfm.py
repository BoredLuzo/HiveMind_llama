"""VRAM-Guard: LFM2.5 24B-A2B muss durch den Pre-Flight passen.

Der Guard rechnet mit `_MOE_TABLE` (gemessene Werte @c4096) + KV pro Token.
Wenn Profil und Kalibrierung auseinanderlaufen, plant er entweder zu knapp
(Load scheitert) oder zu grosszuegig (ctx wird unnoetig beschnitten).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.llama_vram_table import moe_cfg_for, vram_of_moe  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# 8 GB Karte, ~7.4 GB frei (gemessen auf der Zielmaschine)
FREE_GB = 7.4

# ── 1. Der Tag findet die gemessene Zeile (Prefix) ──────────────────────
cfg_tag = moe_cfg_for("lfm2.5:24b-a2b")
cfg_base = moe_cfg_for("lfm2.5:24b")
check("tag resolves to the measured row", cfg_tag is not None and cfg_tag is cfg_base,
      f"({cfg_tag})")
check("measured base footprint is 5.91 GB (@4096)",
      abs(float(cfg_tag["active_gpu_gb"]) - 5.91) < 0.01, f"({cfg_tag['active_gpu_gb']})")
check("KV cost per token is calibrated", int(cfg_tag["measured_bytes_per_token"]) == 20480,
      f"({cfg_tag['measured_bytes_per_token']})")

# ── 2. Profil und Kalibrierung sagen dasselbe ───────────────────────────
_prof_path = ROOT / "model_configs" / "models" / "lfm2.5_24b-a2b.json"
if _prof_path.exists():
    prof = json.loads(_prof_path.read_text(encoding="utf-8"))
    check("profile uses the calibrated expert count",
          int(prof.get("moe_cpu_experts") or 0) == int(cfg_tag["calibrated_n_cpu_moe"]),
          f"(profile {prof.get('moe_cpu_experts')} vs calibrated {cfg_tag['calibrated_n_cpu_moe']})")
    check("profile keeps the model in the MoE path", prof.get("moe") is True)
else:
    # Live-installation profile, deliberately untracked (2026-11-07 CI audit).
    print("  SKIP lfm 24b profile (live-installation only, not tracked)")

# ── 3. Projektion passt fuer jede sinnvolle ctx-Groesse ────────────────
for ctx in (4096, 16384, 32768):
    v = vram_of_moe("lfm2.5:24b-a2b", ctx)
    check(f"ctx {ctx}: fits with headroom", v < FREE_GB - 0.4,
          f"({v:.2f} GB, {FREE_GB - v:.2f} GB free)")

# Der Guard darf nicht versehentlich den Dense-Pfad nehmen (der rechnet mit
# der ganzen Dateigroesse und wuerde 13.5 GB verlangen).
dense_would_be = 13.5
check("MoE path is used, not the dense fallback",
      vram_of_moe("lfm2.5:24b-a2b", 4096) < dense_would_be / 2,
      f"({vram_of_moe('lfm2.5:24b-a2b', 4096):.2f} vs dense ~{dense_would_be} GB)")

# ── 4. Der Kalibrier-Guard nutzt denselben Lookup ──────────────────────
ml = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8", errors="replace")
ml += (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8", errors="replace")
guard = ml.split("[VRAM-CALIBRATION]", 1)[0][-900:]
check("calibration guard resolves via moe_cfg_for (prefix-aware)",
      "_moe_cfg_for(_strip_alias" in guard, f"({guard[-160:]!r})")
check("calibration guard no longer does an exact table lookup",
      "_MOE_TABLE.get(_cal_key)" not in ml)
check("guard still compares against the calibrated value",
      "calibrated_n_cpu_moe" in ml and "deviates from the calibrated value" in ml)

sys.exit(h.finish("vram_guard"))
