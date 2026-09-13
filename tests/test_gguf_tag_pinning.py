"""GGUF tag pinning — filename parsing cannot always recover the canonical tag.

Live (2026-09-12): the 7 catalog models downloaded, but Qwen3.5-4B-Q4_K_M.gguf
registered as qwen3.5:4b — unsloth names their MTP quants WITHOUT "mtp" in the
filename, so the parser dropped the marker and the settings default
qwen3.5:4b-mtp found no GGUF. Fix layers, all tested here:
1. model_configs/gguf_filename_tags.json manifest (shipped + extended by
   fetch_models) — filename → canonical tag, applied BEFORE parsing in the
   backend index; parsed names stay as aliases.
2. fetch_models.write_models_json pins the spec tag into models.json
   (resolution priority 1).
3. mtp-* sidecar files (gemma-4 QAT drafters) are NOT standalone models —
   prefix-only rule (mid-name MTP like Hermes ...-MTP-APEX-Compact registers).

Run: python tests/test_gguf_tag_pinning.py
Exit 0 = all pass, Exit 1 = failures.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


ROOT = Path(__file__).parent.parent

# filename(lower) -> (primary tag or None=excluded, allowed aliases)
CATALOG = {
    "qwen3.5-4b-q4_k_m.gguf": ("qwen3.5:4b-mtp", {"qwen3.5:4b"}),
    "qwen3.5-2b-q4_k_m.gguf": ("qwen3.5:2b-mtp", {"qwen3.5:2b"}),
    "gemma-4-e4b-it-qat-ud-q4_k_xl.gguf": ("gemma-4:e4b-it-qat", {"gemma-4:e4b-it-qat-ud"}),
    "gemma-4-e2b-it-qat-ud-q4_k_xl.gguf": ("gemma-4:e2b-it-qat", {"gemma-4:e2b-it-qat-ud"}),
    "qwen3.6-35b-a3b-ud-q4_k_xl.gguf": ("qwen3.6:35b-a3b-ud", {"qwen3.6:35b"}),
    "hermes3.6-35b-a3b-uncensored-genesis-v13-mtp-apex-compact.gguf":
        ("hermes3.6:35b-a3b-uncensored-genesis-v13-mtp-apex-compact", set()),
    "lfm2.5-2.6b-q4_k_m.gguf": ("lfm2.5:2b-mtp-does-not-exist", None),  # replaced below
}
CATALOG["lfm2.5-2.6b-q4_k_m.gguf"] = ("lfm2.5:2.6b", set())
EXCLUDED = {"mtp-gemma-4-e4b-it.gguf", "mmproj-bf16.gguf"}


def _build_index_in(tmp: Path):
    import importlib
    os.environ["HIVEMIND_MODELS_DIR"] = str(tmp)
    import backend.llama_models as lm
    importlib.reload(lm)
    return lm._build_index(), lm


def test_manifest_primary_tags():
    tmp = Path(tempfile.mkdtemp(prefix="hvm_pin_"))
    for fname in list(CATALOG) + list(EXCLUDED):
        (tmp / fname).write_bytes(b"x" * 64)
    idx, _lm = _build_index_in(tmp)
    bad = []
    for fname, (want, aliases) in CATALOG.items():
        got = [k for k, v in idx.items() if v.name.lower() == fname]
        if want not in got:
            bad.append(f"{fname}: want primary '{want}', got {got}")
            continue
        for a in aliases:
            if a not in idx:
                bad.append(f"{fname}: alias '{a}' missing")
    if not bad:
        ok("Manifest-Tags sind primär, geparste Namen bleiben Aliasse (7/7)")
    else:
        fail("manifest_primary", "; ".join(bad))


def test_drafter_and_mmproj_excluded():
    tmp = Path(tempfile.mkdtemp(prefix="hvm_pin_x_"))
    for fname in list(CATALOG) + list(EXCLUDED):
        (tmp / fname).write_bytes(b"x" * 64)
    idx, _lm = _build_index_in(tmp)
    hits = [k for k, v in idx.items() if v.name.lower() in EXCLUDED]
    if not hits:
        ok("mtp- Drafter und mmproj sind NICHT als Modelle registriert")
    else:
        fail("excluded", f"registriert: {hits}")


def test_hermes_midname_mtp_registers():
    tmp = Path(tempfile.mkdtemp(prefix="hvm_pin_h_"))
    fname = "Hermes3.6-35B-A3B-Uncensored-Genesis-V13-MTP-APEX-Compact.gguf"
    (tmp / fname).write_bytes(b"x" * 64)
    idx, _lm = _build_index_in(tmp)
    if "hermes3.6:35b-a3b-uncensored-genesis-v13-mtp-apex-compact" in idx:
        ok("MTP mitten im Namen (Hermes) bleibt registriert (Prefix-Regel verletzt das nicht)")
    else:
        fail("hermes", f"index: {sorted(idx)}")


def test_manifest_file_covers_catalog():
    man = json.loads((ROOT / "model_configs" / "gguf_filename_tags.json").read_text(encoding="utf-8"))
    required = [
        "qwen3.5-4b-q4_k_m.gguf",
        "qwen3.5-2b-q4_k_m.gguf",
        "gemma-4-e4b-it-qat-ud-q4_k_xl.gguf",
        "gemma-4-e2b-it-qat-ud-q4_k_xl.gguf",
        "qwen3.6-35b-a3b-ud-q4_k_xl.gguf",
        "hermes3.6-35b-a3b-uncensored-genesis-v13-mtp-apex-compact.gguf",
        "lfm2.5-2.6b-q4_k_m.gguf",
    ]
    missing = [k for k in required if k not in man]
    if not missing:
        ok("Shipped manifest deckt alle 7 Katalog-Dateien ab")
    else:
        fail("manifest_file", f"fehlt: {missing}")


def test_settings_defaults_resolve():
    """The settings-default tags must resolve to the catalog files via the
    manifest (the actual live failure)."""
    sys.path.insert(0, str(ROOT))
    from settings import DEFAULT_SETTINGS
    tmp = Path(tempfile.mkdtemp(prefix="hvm_pin_res_"))
    for fname in CATALOG:
        (tmp / fname).write_bytes(b"x" * 64)
    idx, lm = _build_index_in(tmp)
    lm_tag = DEFAULT_SETTINGS["agents"]["duo_coder"]["model"]
    if lm_tag in idx:
        ok(f"Settings-Default '{lm_tag}' resolved über den Index (Live-Bug behoben)")
    else:
        fail("settings_default", f"'{lm_tag}' nicht im Index: {sorted(idx)}")


def test_write_models_json_pins():
    sys.path.insert(0, str(ROOT / "deploy"))
    import deploy.fetch_models as fm
    tmp = Path(tempfile.mkdtemp(prefix="hvm_pin_wmj_"))
    for fname in ("Qwen3.5-4B-Q4_K_M.gguf", "gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf",
                  "mtp-gemma-4-E4B-it.gguf"):
        (tmp / fname).write_bytes(b"x" * 64)
    fm.write_models_json(tmp)
    m = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    bad = []
    if not m.get("qwen3.5:4b-mtp", "").endswith("Qwen3.5-4B-Q4_K_M.gguf"):
        bad.append("qwen3.5:4b-mtp pin fehlt")
    if not m.get("gemma-4:e4b-it-qat", "").endswith("Q4_K_XL.gguf"):
        bad.append("gemma-4:e4b-it-qat pin fehlt")
    if any("mtp-gemma" in k for k in m):
        bad.append("drafter als Modell eingetragen")
    if not bad:
        ok("write_models_json: Spec-Tags gepinnt, Drafter ausgeschlossen")
    else:
        fail("wmj_pins", "; ".join(bad))


if __name__ == "__main__":
    test_manifest_primary_tags()
    test_drafter_and_mmproj_excluded()
    test_hermes_midname_mtp_registers()
    test_manifest_file_covers_catalog()
    test_settings_defaults_resolve()
    test_write_models_json_pins()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
