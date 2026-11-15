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
from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


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
    if prof.exists():
        import json
        check("lfm 24b profile exists", True)
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
    else:
        # Live-installation profile, deliberately untracked: a clean checkout
        # cannot carry it (2026-11-07 CI audit).
        print("  SKIP lfm 24b profile (live-installation only, not tracked)")

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

# ── LFM3 1.7B Uncensored AD (2026-11-13) ────────────────────────────────
# Must live ONLY in fetch_models SPECS (and docs/guide.md catalog table) so
# no extra profile file ships in model_configs/models/ by default; defaults
# are written into model_configs/models/ only when the user downloads it.
_lfm_u = specs.get("lfm3:1.7b-uncensored-ad")
check("lfm3:1.7b-uncensored-ad is in the catalog", _lfm_u is not None)
if _lfm_u:
    check("lfm3 uncensored repo pinned to AtomicChat",
          _lfm_u["repo"] == "AtomicChat/LFM3-1.7B-Uncensored-GGUF", f"({_lfm_u['repo']})")
    check("lfm3 uncensored regex matches Q4_K_M",
          any(re.match(p, "LFM3-1.7B-Uncensored-AD-Q4_K_M.gguf") for p in _lfm_u["file_regex"]))
    check("lfm3 uncensored regex matches Q6_K fallback",
          any(re.match(p, "LFM3-1.7B-Uncensored-AD-Q6_K.gguf") for p in _lfm_u["file_regex"]))
    check("lfm3 uncensored regex rejects unrelated quant",
          not any(re.match(p, "LFM3-1.7B-Uncensored-AD-Q8_0.gguf") for p in _lfm_u["file_regex"]))
    check("lfm3 uncensored tag matches key",
          _lfm_u.get("tag") == "lfm3:1.7b-uncensored-ad")
    check("lfm3 uncensored stays optional (not in ESSENTIAL_KEYS)",
          "lfm3:1.7b-uncensored-ad" not in fm.ESSENTIAL_KEYS)
    _shipped_u = ROOT / "model_configs" / "models" / "lfm3_1.7b-uncensored-ad.json"
    check("lfm3 uncensored profile file does NOT ship in repo",
          not _shipped_u.exists(), f"({_shipped_u})")

    import json as _json
    import tempfile as _tempfile
    import model_configs.models_registry as _mr

    _orig_models_dir = _mr._MODELS_DIR
    try:
        with _tempfile.TemporaryDirectory() as _tmp:
            _mr._MODELS_DIR = Path(_tmp)
            _mr.refresh()
            _seeded = fm._seed_spec_default_profile(_lfm_u)
            check("on-download seeding writes lfm3_1.7b-uncensored-ad.json",
                  _seeded is not None and _seeded.name == "lfm3_1.7b-uncensored-ad.json")
            if _seeded and _seeded.exists():
                _data = _json.loads(_seeded.read_text(encoding="utf-8"))
                check("seeded profile targets lfm3:1.7b-uncensored-ad",
                      _data.get("model") == "lfm3:1.7b-uncensored-ad")
                check("seeded profile carries expected num_ctx & jinja/reasoning defaults",
                      _data.get("num_ctx") == 32768
                      and _data.get("jinja") is True
                      and _data.get("reasoning") == "on"
                      and (_data.get("sampling") or {}).get("thinking", {}).get("temperature") == 0.7)
                _data["num_ctx"] = 65536
                _mr.save_profile("lfm3:1.7b-uncensored-ad", _data)
                _second = fm._seed_spec_default_profile(_lfm_u)
                check("re-seeding never overwrites an existing user profile",
                      _second is None and _mr.get_profile("lfm3:1.7b-uncensored-ad").get("num_ctx") == 65536)
    finally:
        _mr._MODELS_DIR = _orig_models_dir
        _mr.refresh()

# ── Hermes removed from installer catalog (existing profile preserved) ───
check("hermes3.6:35b-a3b-uncensored-genesis-apex-mini removed from installer catalog",
      "hermes3.6:35b-a3b-uncensored-genesis-apex-mini" not in specs)
check("existing hermes apex-mini profile preserved on disk",
      (ROOT / "model_configs" / "models" / "hermes3.6_35b-a3b-uncensored-genesis-apex-mini.json").exists())

# ── TwIL-8B (webAI-Official, Granite 4.2 8.8B dense reasoning) ───────────
twil = specs.get("twil:8b")
check("twil:8b is in the catalog", twil is not None)
if twil:
    check("twil repo pinned to webAI-Official/TwIL-8B",
          twil["repo"] == "webAI-Official/TwIL-8B", f"({twil['repo']})")
    check("twil regex matches balanced default GGUF",
          any(re.match(p, "TwIL-8B-Q4_K_M-balanced.gguf") for p in twil["file_regex"]))
    check("twil regex rejects verbose variant",
          not any(re.match(p, "TwIL-8B-Q4_K_M-verbose.gguf") for p in twil["file_regex"]))
    check("twil tag is twil:8b", twil.get("tag") == "twil:8b")
    twil_prof_p = ROOT / "model_configs" / "models" / "twil_8b.json"
    check("twil profile exists", twil_prof_p.exists())
    if twil_prof_p.exists():
        import json as _json_tw
        tp = _json_tw.loads(twil_prof_p.read_text(encoding="utf-8"))
        check("twil profile targets twil:8b", tp.get("model") == "twil:8b")
        check("twil profile uses recommended 40960 ctx & jinja & reasoning on",
              tp.get("num_ctx") == 40960
              and tp.get("jinja") is True
              and tp.get("reasoning") == "on"
              and tp.get("moe") is False
              and int(tp.get("moe_cpu_experts") or 0) == 0)
        ts = (tp.get("sampling") or {}).get("thinking") or {}
        check("twil profile sampling matches card (temp=1.0, top_p=0.95)",
              ts.get("temperature") == 1.0 and ts.get("top_p") == 0.95, f"({ts})")

# ── Air-26B-A4B (webAI-Official, Gemma 4 26B-A4B MoE reasoning) ──────────
air = specs.get("air:26b-a4b")
check("air:26b-a4b is in the catalog", air is not None)
if air:
    check("air repo pinned to webAI-Official/Air-26B-A4B-gguf",
          air["repo"] == "webAI-Official/Air-26B-A4B-gguf", f"({air['repo']})")
    check("air regex matches Air-26B-A4B.gguf",
          any(re.match(p, "Air-26B-A4B.gguf") for p in air["file_regex"]))
    check("air regex rejects projector file",
          not any(re.match(p, "mmproj-Air-26B-A4B-f16.gguf") for p in air["file_regex"]))
    check("air tag is air:26b-a4b", air.get("tag") == "air:26b-a4b")
    air_prof_p = ROOT / "model_configs" / "models" / "air_26b-a4b.json"
    check("air profile exists", air_prof_p.exists())
    if air_prof_p.exists():
        import json as _json_air
        ap = _json_air.loads(air_prof_p.read_text(encoding="utf-8"))
        check("air profile targets air:26b-a4b", ap.get("model") == "air:26b-a4b")
        check("air profile sets 131072 ctx, jinja, reasoning on, MoE=30, q8_0 KV",
              ap.get("num_ctx") == 131072
              and ap.get("jinja") is True
              and ap.get("reasoning") == "on"
              and ap.get("moe") is True
              and int(ap.get("moe_cpu_experts") or 0) == 30
              and ap.get("kv_cache_type") == "q8_0")
        asmp = (ap.get("sampling") or {}).get("thinking") or {}
        check("air profile sampling matches card (temp=1.0, top_p=0.95, top_k=64)",
              asmp.get("temperature") == 1.0
              and asmp.get("top_p") == 0.95
              and asmp.get("top_k") == 64,
              f"({asmp})")
    from backend.llama_config import is_moe_model, moe_kv_cache_types
    from backend.llama_models import _parse_gguf_filename
    from backend.llama_vram_table import moe_cfg_for
    check("air:26b-a4b is recognized as MoE with 30 calibrated experts & q8_0 KV",
          is_moe_model("air:26b-a4b")
          and moe_kv_cache_types("air:26b-a4b") == "q8_0"
          and (moe_cfg_for("air:26b-a4b") or {}).get("calibrated_n_cpu_moe") == 30)
    check("_parse_gguf_filename maps TwIL & Air GGUFs cleanly",
          "twil:8b" in _parse_gguf_filename("TwIL-8B-Q4_K_M-balanced.gguf")
          and _parse_gguf_filename("Air-26B-A4B.gguf")[0] == "air:26b-a4b")

# ── Qwen3.6 35B-A3B Unsloth non-MTP + MTP + sharp Jinja template ─────────
q36_ud = specs.get("qwen3.6:35b-a3b-ud")
q36_mtp = specs.get("qwen3.6:35b-a3b-mtp")
check("qwen3.6:35b-a3b-ud and qwen3.6:35b-a3b-mtp are both in the catalog",
      q36_ud is not None and q36_mtp is not None)
if q36_ud and q36_mtp:
    check("qwen3.6:35b-a3b-ud repo pinned to unsloth/Qwen3.6-35B-A3B-GGUF",
          q36_ud["repo"] == "unsloth/Qwen3.6-35B-A3B-GGUF", f"({q36_ud['repo']})")
    check("qwen3.6:35b-a3b-mtp repo pinned to unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
          q36_mtp["repo"] == "unsloth/Qwen3.6-35B-A3B-MTP-GGUF", f"({q36_mtp['repo']})")
    check("qwen3.6:35b-a3b-mtp sets unique file_dest & mmproj_dest",
          q36_mtp.get("file_dest") == "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"
          and q36_mtp.get("mmproj_dest") == "mmproj-qwen3.6-35b-a3b-mtp-f16.gguf",
          f"({q36_mtp.get('file_dest')}, {q36_mtp.get('mmproj_dest')})")

    import json as _json_q36
    import tempfile as _tempfile_q36

    for _pname in (
        "qwen3.6_35b-a3b-ud.json",
        "qwen3.6_35b-a3b-mtp.json",
        "qwen3.6_35b-a3b-uncensored-genesis-final-apex-compact.json",
    ):
        _pp = ROOT / "model_configs" / "models" / _pname
        check(f"{_pname} exists and pins chat_template_sharp_v22.5.0.jinja",
              _pp.exists() and _json_q36.loads(_pp.read_text(encoding="utf-8")).get("chat_template")
              == "chat_template_sharp_v22.5.0.jinja")

    _ud_prof = _json_q36.loads((ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-ud.json").read_text(encoding="utf-8"))
    _mtp_prof = _json_q36.loads((ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-mtp.json").read_text(encoding="utf-8"))
    check("qwen3.6_35b-a3b-mtp.json enables mtp + 35 CPU experts + ubatch 512",
          _mtp_prof.get("mtp") is True
          and int(_mtp_prof.get("moe_cpu_experts") or 0) == 35
          and int(_mtp_prof.get("ubatch") or 0) == 512)
    check("qwen3.6_35b-a3b-ud.json keeps mtp disabled",
          _ud_prof.get("mtp") is False)

    with _tempfile_q36.TemporaryDirectory() as _tmp_q36:
        _mdir = Path(_tmp_q36)
        _f_ud = _mdir / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
        _f_mtp = _mdir / "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"
        _f_ud.write_bytes(b"ud")
        _f_mtp.write_bytes(b"mtp")
        (_mdir / "model_configs").mkdir(exist_ok=True)
        _out = _mdir / "models.json"
        _orig_fm_root = fm.ROOT
        fm.ROOT = _mdir
        try:
            fm.write_models_json(_mdir, out=_out)
        finally:
            fm.ROOT = _orig_fm_root
        _mj = _json_q36.loads(_out.read_text(encoding="utf-8"))
        check("both Qwen3.6 Unsloth non-MTP and MTP register cleanly without collision",
              Path(_mj.get("qwen3.6:35b-a3b-ud", "")).name == _f_ud.name
              and Path(_mj.get("qwen3.6:35b-a3b-mtp", "")).name == _f_mtp.name
              and _mj.get("qwen3.6:35b-a3b-ud") != _mj.get("qwen3.6:35b-a3b-mtp"),
              f"({_mj})")

sys.exit(h.finish("model_catalog"))
