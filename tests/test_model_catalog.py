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
check("catalog is non-empty condensed performance set", len(specs) >= 10, f"({len(specs)} entries)")

# ── Shipped profiles preserved for unlisted local models ────────────────
prof_spark_p = ROOT / "model_configs" / "models" / "spark-x2.6_4b.json"
check("spark profile stays shipped for existing GGUFs", prof_spark_p.exists())
if prof_spark_p.exists():
    import json
    p = json.loads(prof_spark_p.read_text(encoding="utf-8"))
    check("spark profile targets the tag", p.get("model") == "spark-x2.6:4b", f"({p.get('model')})")
    check("spark profile enables jinja", p.get("jinja") is True)
    check("spark profile uses the model card sampling",
          (p.get("sampling") or {}).get("thinking", {}).get("temperature") in (None, 1.0),
          f"({(p.get('sampling') or {}).get('thinking')})")

# ── Nemotron-3.5-Bolt E4B + DFlash Q8_0 (in condensed catalog) ──────────
nem = specs.get("nvidia-nemotron-3.5-bolt:e4b")
check("nvidia-nemotron-3.5-bolt:e4b is in the condensed catalog", nem is not None)
if nem:
    check("nemotron repo pinned to unsloth/NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF",
          nem["repo"] == "unsloth/NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF", f"({nem['repo']})")
    check("nemotron regex matches UD-Q4_K_XL",
          any(re.match(p, "NVIDIA-Nemotron-3.5-Bolt-E4B-UD-Q4_K_XL.gguf") for p in nem["file_regex"]))
    side_nem = (nem.get("sidecar") or [])
    check("nemotron ships DFlash Q8_0 drafter sidecar",
          len(side_nem) == 1 and side_nem[0]["path"] == "dflash-NVIDIA-Nemotron-3.5-Bolt-E4B-Q8_0.gguf",
          f"({side_nem})")

# ── Redundant bloated entries condensed out of catalog ──────────────────
for dropped in (
    "gemma-4:e2b-it-qat",
    "gemma-4:e4b-uncensored-hauhaucs-aggressive",
    "spark-x2.6:4b",
    "lfm2.5:24b-a2b",
    "groot:19b-a3b-261023",
    "qwen3.5:2b-mtp",
    "qwen3.5:0.8b-ud",
):
    check(f"condensed catalog omits redundant {dropped}", dropped not in specs)

# ── Drafter werden nie als Modell registriert ───────────────────────────
from backend.llama_models import is_non_model_gguf as _is_non_model  # noqa: E402
check("drafter names are excluded from models.json",
      _is_non_model("dflash-NVIDIA-Nemotron-3.5-Bolt-E4B-Q8_0.gguf")
      and _is_non_model("mtp-gemma-4-E4B-it.gguf")
      and _is_non_model("MiniCPM5-2.6B-DSpark.gguf"))

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
    "hermes3.6_35b-a3b-uncensored-genesis-mtp-apex-compact.json")
if _HERMES.exists():
    import json as _json
    _hd = _json.loads(_HERMES.read_text(encoding="utf-8"))
    check("hermes coder keeps mtp on (measured +55% decode)", _hd.get("mtp") is True,
          f"(mtp={_hd.get('mtp')})")
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

# ── Hermes & redundant sharp-q4/non-MTP Qwen3.6 removed from catalog ──────
check("hermes3.6:35b-a3b-uncensored-genesis-apex-mini removed from installer catalog",
      "hermes3.6:35b-a3b-uncensored-genesis-apex-mini" not in specs)
check("minicpm5:2b-sharp-q4 removed from installer catalog",
      "minicpm5:2b-sharp-q4" not in specs)
check("qwen3.6:35b-a3b-ud consolidated to qwen3.6:35b-a3b-mtp in installer catalog",
      "qwen3.6:35b-a3b-ud" not in specs and "qwen3.6:35b-a3b-mtp" in specs)

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
        check("twil profile uses 8 GB-safe 16384 ctx, jinja, flash_attn on, q4_0 KV & reasoning on",
              tp.get("num_ctx") == 16384
              and tp.get("jinja") is True
              and tp.get("reasoning") == "on"
              and tp.get("flash_attn") is True
              and tp.get("kv_cache_type") == "q4_0"
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
        check("air profile sets 131072 ctx, jinja, reasoning on, MoE=26, q4_0 KV",
              ap.get("num_ctx") == 131072
              and ap.get("jinja") is True
              and ap.get("reasoning") == "on"
              and ap.get("moe") is True
              and int(ap.get("moe_cpu_experts") or 0) == 26
              and ap.get("kv_cache_type") == "q4_0")
        asmp = (ap.get("sampling") or {}).get("thinking") or {}
        check("air profile sampling matches card (temp=1.0, top_p=0.95, top_k=64)",
              asmp.get("temperature") == 1.0
              and asmp.get("top_p") == 0.95
              and asmp.get("top_k") == 64,
              f"({asmp})")
    from backend.llama_config import is_moe_model, moe_kv_cache_types
    from backend.llama_models import _parse_gguf_filename
    from backend.llama_vram_table import moe_cfg_for
    check("air:26b-a4b is recognized as MoE with 26 calibrated experts & q4_0 KV",
          is_moe_model("air:26b-a4b")
          and moe_kv_cache_types("air:26b-a4b") == "q4_0"
          and (moe_cfg_for("air:26b-a4b") or {}).get("calibrated_n_cpu_moe") == 26)
    check("_parse_gguf_filename maps TwIL & Air GGUFs cleanly",
          "twil:8b" in _parse_gguf_filename("TwIL-8B-Q4_K_M-balanced.gguf")
          and _parse_gguf_filename("Air-26B-A4B.gguf")[0] == "air:26b-a4b")

# ── Qwen3.6 35B-A3B MTP + sharp Jinja template ────────────────────────────
q36_mtp = specs.get("qwen3.6:35b-a3b-mtp")
check("qwen3.6:35b-a3b-mtp is in the catalog", q36_mtp is not None)
if q36_mtp:
    check("qwen3.6:35b-a3b-mtp repo pinned to unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
          q36_mtp["repo"] == "unsloth/Qwen3.6-35B-A3B-MTP-GGUF", f"({q36_mtp['repo']})")
    check("qwen3.6:35b-a3b-mtp sets unique file_dest & mmproj_dest",
          q36_mtp.get("file_dest") == "Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf"
          and q36_mtp.get("mmproj_dest") == "mmproj-qwen3.6-35b-a3b-mtp-f16.gguf",
          f"({q36_mtp.get('file_dest')}, {q36_mtp.get('mmproj_dest')})")

    import json as _json_q36
    import tempfile as _tempfile_q36

    for _pname in (
        "qwen3.6_35b-a3b-ud.json",
        "qwen3.6_35b-a3b-mtp.json",
    ):
        _pp = ROOT / "model_configs" / "models" / _pname
        check(f"{_pname} exists and pins chat_template_sharp_v22.5.0.jinja",
              _pp.exists() and _json_q36.loads(_pp.read_text(encoding="utf-8")).get("chat_template")
              == "chat_template_sharp_v22.5.0.jinja")

    _ud_prof = _json_q36.loads((ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-ud.json").read_text(encoding="utf-8"))
    _mtp_prof = _json_q36.loads((ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-mtp.json").read_text(encoding="utf-8"))
    check("qwen3.6_35b-a3b-mtp.json enables mtp + 40 CPU experts + 2560 cache (0.95 share) + ubatch 1024",
          _mtp_prof.get("mtp") is True
          and int(_mtp_prof.get("moe_cpu_experts") or 0) == 40
          and int(_mtp_prof.get("moe_cache_mib") or 0) == 2560
          and float(_mtp_prof.get("moe_cache_cpu_share") or 0.0) == 0.95
          and int(_mtp_prof.get("ubatch") or 0) == 1024)
    check("qwen3.6_35b-a3b-ud.json keeps mtp disabled",
          _ud_prof.get("mtp") is False)

    with _tempfile_q36.TemporaryDirectory() as _tmp_q36:
        _mdir = Path(_tmp_q36)
        _f_mtp = _mdir / "Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf"
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
        check("Qwen3.6 Unsloth MTP registers cleanly",
              Path(_mj.get("qwen3.6:35b-a3b-mtp", "")).name == _f_mtp.name,
              f"({_mj})")

# ── Inkling-2-Mini GSQ-RCO-IQ3_XS (optional Tier-4 Looped MoE + Vision) ────
ink = specs.get("inkling-2-mini")
check("inkling-2-mini is in the catalog as optional upgrade",
      ink is not None and "inkling-2-mini" not in fm.ESSENTIAL_KEYS)
if ink:
    check("inkling-2-mini repo pinned to abenzerps/Inkling-2-Mini-GSQ-RCO-GGUF",
          ink["repo"] == "abenzerps/Inkling-2-Mini-GSQ-RCO-GGUF", f"({ink['repo']})")
    check("inkling-2-mini file_regex matches GSQ-RCO-IQ3_XS and rejects other variants",
          any(re.match(p, "Inkling-2-Mini-GSQ-RCO-IQ3_XS.gguf") for p in ink["file_regex"])
          and not any(re.match(p, "Inkling-2-Mini-GSQ-RCO-IQ4_XS.gguf") for p in ink["file_regex"])
          and not any(re.match(p, "Inkling-2-Mini-GSQ-RCO-IQ2_XS.gguf") for p in ink["file_regex"])
          and not any(re.match(p, "dspark-Inkling-2-Mini-Q8_0.gguf") for p in ink["file_regex"]))
    check("inkling-2-mini mmproj matches BF16/F16 with unique dest and ships no DSpark sidecar yet",
          any(re.match(p, "mmproj-Inkling-2-Mini-BF16.gguf") for p in ink["mmproj_regex"])
          and any(re.match(p, "mmproj-F16.gguf") for p in ink["mmproj_regex"])
          and ink.get("mmproj_dest") == "mmproj-Inkling-2-Mini-F16.gguf"
          and not ink.get("sidecar"))

    import json as _json_ink
    import tempfile as _tempfile_ink
    _ink_prof_p = ROOT / "model_configs" / "models" / "inkling-2-mini.json"
    check("inkling-2-mini profile exists with looped-MoE & projector settings",
          _ink_prof_p.exists())
    if _ink_prof_p.exists():
        _ip = _json_ink.loads(_ink_prof_p.read_text(encoding="utf-8"))
        check("inkling-2-mini profile sets ncmoe=15, ubatch=512, load_mode=none, q4_0 KV & mmproj",
              _ip.get("model") == "inkling-2-mini"
              and int(_ip.get("moe_cpu_experts") or 0) == 15
              and int(_ip.get("ubatch") or 0) == 512
              and _ip.get("load_mode") == "none"
              and _ip.get("kv_cache_type") == "q4_0"
              and _ip.get("mmproj_filename") == "mmproj-Inkling-2-Mini-F16.gguf")

    with _tempfile_ink.TemporaryDirectory() as _tmp_ink:
        _mdir_i = Path(_tmp_ink)
        _f_ink = _mdir_i / "Inkling-2-Mini-GSQ-RCO-IQ3_XS.gguf"
        _f_mm = _mdir_i / "mmproj-Inkling-2-Mini-F16.gguf"
        _f_ink.write_bytes(b"inkling")
        _f_mm.write_bytes(b"mmproj")
        (_mdir_i / "model_configs").mkdir(exist_ok=True)
        _out_i = _mdir_i / "models.json"
        _orig_fm_root_i = fm.ROOT
        fm.ROOT = _mdir_i
        try:
            fm.write_models_json(_mdir_i, out=_out_i)
        finally:
            fm.ROOT = _orig_fm_root_i
        _mj_i = _json_ink.loads(_out_i.read_text(encoding="utf-8"))
        check("write_models_json registers inkling-2-mini + inkling-2-mini_mmproj",
              Path(_mj_i.get("inkling-2-mini", "")).name == _f_ink.name
              and Path(_mj_i.get("inkling-2-mini_mmproj", "")).name == _f_mm.name,
              f"({_mj_i})")

        import backend.llama_models as _lm_i
        _f_ink.write_bytes(b"\x00" * (1024 * 1024 + 4096))
        _orig_lm_dirs = (_lm_i.MODELS_DIR, _lm_i._MODELS_JSON, _lm_i._index_cache,
                         _lm_i._index_ts, _lm_i._index_stamp, _lm_i._names_cache,
                         _lm_i._names_cache_ts, _lm_i._filename_tag_map)
        _lm_i.MODELS_DIR = _mdir_i
        _lm_i._MODELS_JSON = _out_i
        _lm_i._index_cache = None
        _lm_i._names_cache = None
        _lm_i._filename_tag_map = None
        try:
            _res_canon = _lm_i.resolve_model_path("inkling-2-mini")
            _res_alias = _lm_i.resolve_model_path("inkling-2-mini-gsq-rco")
            _avail = _lm_i.list_available_models(force_refresh=True)
            check("llama_models resolves both inkling-2-mini and inkling-2-mini-gsq-rco without duplicates",
                  _res_canon == _f_ink and _res_alias == _f_ink
                  and "inkling-2-mini" in _avail and "inkling-2-mini-gsq-rco" not in _avail,
                  f"(canon={_res_canon}, alias={_res_alias}, avail={_avail})")
        finally:
            (_lm_i.MODELS_DIR, _lm_i._MODELS_JSON, _lm_i._index_cache,
             _lm_i._index_ts, _lm_i._index_stamp, _lm_i._names_cache,
             _lm_i._names_cache_ts, _lm_i._filename_tag_map) = _orig_lm_dirs

    with _tempfile_ink.TemporaryDirectory() as _tmp_ink_ud:
        _mdir_u = Path(_tmp_ink_ud)
        _f_ud = _mdir_u / "Inkling-2-Mini-UD-IQ3_XXS.gguf"
        _f_ud.write_bytes(b"\x00" * (1024 * 1024 + 4096))
        _orig_lm_dirs_u = (_lm_i.MODELS_DIR, _lm_i._MODELS_JSON, _lm_i._index_cache,
                           _lm_i._index_ts, _lm_i._index_stamp, _lm_i._names_cache,
                           _lm_i._names_cache_ts, _lm_i._filename_tag_map)
        _lm_i.MODELS_DIR = _mdir_u
        _lm_i._MODELS_JSON = _mdir_u / "missing_models.json"
        _lm_i._index_cache = None
        _lm_i._names_cache = None
        _lm_i._filename_tag_map = None
        try:
            _res_ud = _lm_i.resolve_model_path("inkling-2-mini")
            check("llama_models still maps legacy UD-IQ3_XXS filename to inkling-2-mini",
                  _res_ud == _f_ud, repr(_res_ud))
        finally:
            (_lm_i.MODELS_DIR, _lm_i._MODELS_JSON, _lm_i._index_cache,
             _lm_i._index_ts, _lm_i._index_stamp, _lm_i._names_cache,
             _lm_i._names_cache_ts, _lm_i._filename_tag_map) = _orig_lm_dirs_u

sys.exit(h.finish("model_catalog"))
