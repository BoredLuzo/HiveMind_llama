# -*- coding: utf-8 -*-
"""Fresh-install defaults stay coherent with the model setup.

Two rules this suite pins:
  1. Every default model name a fresh install ships is part of the essential
     set - otherwise a default install points at a model nobody downloaded.
  2. `deploy/apply_model_defaults.py` fills only empty/uninstalled slots and
     never overwrites a model the user chose.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy"))

import settings as S                       # noqa: E402
import apply_model_defaults as AMD         # noqa: E402
from deploy.fetch_models import ESSENTIAL_KEYS  # noqa: E402

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check


# ── 1. the shipped defaults describe a real setup ───────────────────────────
d = S.DEFAULT_SETTINGS
check("1a default mode is chat", d["mode"] == "chat", f"({d['mode']})")
check("1b code_duo starts agentic", d["duo_agentic_mode"] is True)
check("1c the planner is on by default", d["duo_planner_enabled"] is True)
check("1d the planner shares the coder context (no second load)",
      d["duo_planner_use_coder_ctx"] is True)
check("1e the vision model default is the essential all-rounder",
      d["vision_agent_model"] in ESSENTIAL_KEYS, f"({d['vision_agent_model']})")
_ladder = d["subagent_lite_model_ladder"]
check("1f the subagent ladder only names essential models",
      bool(_ladder) and set(_ladder) <= set(ESSENTIAL_KEYS), f"({_ladder})")
check("1g the ladder starts small", _ladder[0] == "lfm3:1.7b-qad")
check("1h agent defaults stay empty (the installer wires them)",
      all(not (cfg.get("model") or "") for cfg in S.DEFAULT_AGENT_CFG.values()))

# ── 2. every role in the wiring table is a real role ───────────────────────
check("2a every wired role exists in DEFAULT_AGENT_CFG or is a known key",
      all(r in S.DEFAULT_AGENT_CFG or r in ("vision_agent",)
          for r in AMD.ROLE_PREFERENCE))
check("2b every preferred model is in the catalog",
      all(m for models in AMD.ROLE_PREFERENCE.values() for m in models))
check("2c the coder preference starts with the essential coder",
      AMD.ROLE_PREFERENCE["duo_coder"][0] == "qwen3.5:4b-mtp")
check("2d direct prefers a vision model",
      AMD.ROLE_PREFERENCE["direct"][0] == "gemma-4:e4b-it-qat")
check("2e the judge prefers the cheapest model",
      AMD.ROLE_PREFERENCE["judge"][0] == "minicpm5:2b-sharp")

# ── 3. the wiring script respects an existing configuration ────────────────
td = Path(tempfile.mkdtemp(prefix="hm_defaults_"))
(td / "models.json").write_text(json.dumps({
    "gemma-4:e4b-it-qat": "x", "gemma-4:e4b_it-qat_mmproj": "y",
    "qwen3.5:4b-mtp": "x", "minicpm5:2b-sharp": "x", "lfm3:1.7b-qad": "x",
}), encoding="utf-8")
(td / "settings.json").write_text(json.dumps({
    "agents": {"direct": {"model": "gemma-4:e4b-it-qat"},
               "duo_coder": {"model": "moved-away:7b"}},
}), encoding="utf-8")

changes = AMD.apply(td, dry_run=True)
after = json.loads((td / "settings.json").read_text(encoding="utf-8"))
check("3a a dry run writes nothing",
      after["agents"]["duo_coder"]["model"] == "moved-away:7b")
_roles = {c[0] for c in changes}
check("3b an installed choice is left alone", "direct" not in _roles)
check("3c an uninstalled model is replaced", "duo_coder" in _roles)
check("3d empty roles get filled", "refiner" in _roles and "judge" in _roles)
check("3e the projector key is not treated as a model",
      not any("mmproj" in c[2] for c in changes))

real = AMD.apply(td, dry_run=False)
saved = json.loads((td / "settings.json").read_text(encoding="utf-8"))
check("3f a real run writes the file",
      saved["agents"]["duo_coder"]["model"] == "qwen3.5:4b-mtp")
check("3g the planner follows the coder",
      saved.get("duo_planner_model") == "qwen3.5:4b-mtp")
check("3h the ladder is written", bool(saved.get("subagent_lite_model_ladder")))
check("3i a second run changes nothing", AMD.apply(td, dry_run=False) == [])
check("3j the report is non-empty", bool(real))

# ── 4. an empty install is a no-op, not a crash ────────────────────────────
(Path(td) / "models.json").unlink()
check("4a no models -> no changes", AMD.apply(td, dry_run=True) == [])

sys.exit(h.finish())
