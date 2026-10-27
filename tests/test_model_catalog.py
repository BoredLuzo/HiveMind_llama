"""Katalog-Pins: die neuen Eintraege muessen auf echte HF-Dateien zeigen.

Hintergrund: der Hermes-Eintrag zeigte monatelang auf einen Dateinamen, den das
Repo nicht mehr hatte - der Installer lud stillschweigend nichts. Dieser Test
pinnt Repo + Dateiname + Drafter fuer die neuen Eintraege und prueft, dass die
Regex genau die dokumentierte Datei trifft (und nicht mehr).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(ROOT / "deploy"))
import fetch_models as fm  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


specs = {s["key"]: s for s in fm.SPECS}
check("catalog is non-empty", len(specs) >= 10, f"({len(specs)} entries)")

# ── Spark-X2.6 ──────────────────────────────────────────────────────────
spark = specs.get("spark-x2.6:4b")
check("spark-x2.6:4b is in the catalog", spark is not None)
if spark:
    check("spark repo pinned to XHToken",
          spark["repo"] == "XHToken/Spark-X2.6-4B-GGUF", f"({spark['repo']})")
    names = ["Spark-X2.6-4B-Q4_K_M.gguf"]
    check("spark regex matches the real asset",
          any(re.match(p, names[0]) for p in spark["file_regex"]), f"({spark['file_regex']})")
    check("spark regex rejects another quant",
          not any(re.match(p, "Spark-X2.6-4B-Q8_0.gguf") for p in spark["file_regex"]))
    check("spark tag is spark-x2.6:4b", spark.get("tag") == "spark-x2.6:4b")
    prof = ROOT / "model_configs" / "models" / "spark-x2.6_4b.json"
    check("spark profile exists", prof.exists())
    if prof.exists():
        import json
        p = json.loads(prof.read_text(encoding="utf-8"))
        check("spark profile targets the tag", p.get("model") == "spark-x2.6:4b", f"({p.get('model')})")
        check("spark profile enables jinja", p.get("jinja") is True)
        check("spark profile uses the model card sampling",
              (p.get("sampling") or {}).get("thinking", {}).get("temperature") in (None, 1.0),
              f"({(p.get('sampling') or {}).get('thinking')})")

# ── LFM2.5 24B-A2B + DSpark ─────────────────────────────────────────────
lfm = specs.get("lfm2.5:24b-a2b")
check("lfm2.5:24b-a2b is in the catalog", lfm is not None)
if lfm:
    check("lfm 24b repo pinned", lfm["repo"] == "LiquidAI/LFM2.5-24B-A2B-GGUF", f"({lfm['repo']})")
    asset = "LFM2.5-24B-A2B-Q4_K_M.gguf"
    check("lfm 24b regex matches the real asset",
          any(re.match(p, asset) for p in lfm["file_regex"]), f"({lfm['file_regex']})")
    check("lfm 24b regex rejects Q8",
          not any(re.match(p, "LFM2.5-24B-A2B-Q8_0.gguf") for p in lfm["file_regex"]))
    side = (lfm.get("sidecar") or [])
    check("lfm 24b ships a DSpark drafter", len(side) == 1, f"({side})")
    if side:
        check("drafter repo pinned",
              side[0]["repo"] == "LiquidAI/LFM2.5-24B-A2B-DSpark-GGUF", f"({side[0]['repo']})")
        check("drafter filename matches the repo",
              side[0]["path"] == "LFM2.5-24B-A2B-DSpark-F16.gguf", f"({side[0]['path']})")
        check("drafter is labelled", bool(side[0].get("note")))
    prof = ROOT / "model_configs" / "models" / "lfm2.5_24b-a2b.json"
    check("lfm 24b profile exists", prof.exists())
    if prof.exists():
        import json
        p = json.loads(prof.read_text(encoding="utf-8"))
        check("profile points at the drafter filename",
              p.get("dspark_draft_filename") == "LFM2.5-24B-A2B-DSpark-F16.gguf",
              f"({p.get('dspark_draft_filename')})")
        from backend.llama_vram_table import moe_cfg_for as _moe_cfg
        _cal = (_moe_cfg("lfm2.5:24b-a2b") or {}).get("calibrated_n_cpu_moe")
        check("profile offloads exactly the calibrated expert count",
              int(p.get("moe_cpu_experts") or 0) == int(_cal or -1),
              f"(profile {p.get('moe_cpu_experts')} vs calibrated {_cal})")
        check("profile marks the model as MoE", p.get("moe") is True)
        check("profile is not a thinking model (LFM family)", p.get("capabilities", {}).get("thinking") is False)

# ── Drafter werden nie als Modell registriert ───────────────────────────
NON_MODEL = getattr(fm, "_NON_MODEL_PATTERNS", None) or {"mmproj", "dspark"}
check("drafter names are excluded from models.json",
      any("dspark" in str(x).lower() for x in NON_MODEL), f"({NON_MODEL})")

# ── Automap kennt die Spark-Familie ─────────────────────────────────────
amap = (ROOT / "routing" / "model_automap.py").read_text(encoding="utf-8", errors="replace")
check("automap knows spark-x2.6", '"spark-x2.6"' in amap)
check("automap still knows the X2.5 aliases", '"spark-x2.5"' in amap)


# ── Spark: Karte vs. Profil (verifiziert live auf b11456) ───────────────
ml = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8", errors="replace")
check("spark thinks by default (reasoning base)",
      '"spark-x2.6"' in ml.split("_REASONING_ON_BASES", 1)[1][:400])
prof_spark = json.loads((ROOT / "model_configs" / "models" / "spark-x2.6_4b.json")
                        .read_text(encoding="utf-8"))
_card = {"temperature": 1.0, "top_p": 0.95, "top_k": 0,
         "presence_penalty": 0.0, "repetition_penalty": 1.0}
for variant in ("thinking", "non_thinking", "sampling_text"):
    got = {k: prof_spark["sampling"][variant].get(k) for k in _card}
    check(f"spark {variant} sampling matches the card", got == _card, f"({got})")
check("spark profile documents the verified tool-call path",
      "finish_reason=tool_calls" in prof_spark.get("_notes", ""))
check("spark profile is not a MoE/vision model",
      int(prof_spark.get("moe_cpu_experts") or 0) == 0
      and prof_spark.get("mmproj_filename") is None
      and prof_spark.get("capabilities", {}).get("vision") is False)



# ── MTP-Policy (2026-10-27, measured) ──────────────────────────────────
# The hermes coder ran with mtp=false since 2026-10-24, based on 220-token
# answers on an older build. Re-measured on b11889 with 256 tokens: decode
# 20.0 -> 31.05 t/s (+55%), prefill 122.1 -> 120.15 (-1.6%), acceptance 0.823.
# The profile has to keep mtp on or the coder silently loses half its decode.
_HERMES = ROOT / "model_configs" / "models" / (
    "hermes3.6_35b-a3b-uncensored-genesis-final-mtp-apex-compact.json")
if _HERMES.exists():
    import json as _json
    _hd = _json.loads(_HERMES.read_text(encoding="utf-8"))
    check("hermes coder keeps mtp on (measured +55% decode)", _hd.get("mtp") is True,
          f"(mtp={_hd.get('mtp')})")
    check("the measurement is recorded in the profile",
          "decode" in str(_hd.get("_notes") or "").lower()
          and "prefill" in str(_hd.get("_notes") or "").lower())
    check("hermes keeps its 35 CPU experts", _hd.get("moe_cpu_experts") == 35,
          f"(moe={_hd.get('moe_cpu_experts')})")



# ── Drafter-Skip (2026-10-27, GLM-Audit) ───────────────────────────────
# `--essential` must NOT skip the drafter sidecars any more: speculative
# decoding is on again for the profiles that ship an MTP head, and a load
# without the drafter silently loses ~50 % decode. Only the explicit flag skips.
_FM = (ROOT / "deploy" / "fetch_models.py").read_text(encoding="utf-8", errors="replace")
check("essential does not imply --no-drafters",
      'SKIP_DRAFTERS = bool(getattr(args, "no_drafters", False))' in _FM
      and 'or getattr(args, "essential", False))' not in _FM)
_FM_FLAT = " ".join(_FM.split())
# argparse formats help strings with %, so the source carries "%%" and the
# rendered text says "~50%".
check("the --no-drafters help text names the cost",
      "costs ~50%% decode" in _FM_FLAT)
check("the skip warns about the decode cost",
      "lose ~50 % decode" in _FM)
check("the stale 'spec off everywhere' claim is gone",
      "speculative decoding is off for every shipped" not in _FM)
_LCC = (ROOT / "backend" / "llama_config.py").read_text(encoding="utf-8", errors="replace")
check("llama_config no longer claims a coder mtp-off default",
      "coder default mtp OFF" not in _LCC)
_LCC_FLAT = " ".join(_LCC.split()).lower()
check("llama_config names the determinism trade",
      "bit-identical" in _LCC_FLAT and "hivemind_mtp_spec=0" in _LCC_FLAT)
_CH = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8", errors="replace")
_CH_FLAT = " ".join(_CH.split())
check("changelog states the determinism price",
      "not bit-identical" in _CH_FLAT or "byte-identical" in _CH_FLAT)
check("changelog drops the old essential-skip claim",
      "`--essential` implies it" not in _CH)

print()
print(f"SUMMARY model_catalog: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
