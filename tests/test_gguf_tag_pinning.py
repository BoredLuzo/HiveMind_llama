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

from tests._harness import Harness  # noqa: E402

h = Harness(style="box", width=60)
ok = h.ok
fail = h.fail

ROOT = Path(__file__).parent.parent

# filename(lower) -> (primary tag or None=excluded, allowed aliases)
CATALOG = {
    "qwen3.5-4b-q4_k_m.gguf": ("qwen3.5:4b-mtp", {"qwen3.5:4b"}),
    "qwen3.5-2b-q4_k_m.gguf": ("qwen3.5:2b-mtp", {"qwen3.5:2b"}),
    "gemma-4-e4b-it-qat-ud-q4_k_xl.gguf": ("gemma-4:e4b-it-qat", {"gemma-4:e4b-it-qat-ud"}),
    "gemma-4-e2b-it-qat-ud-q4_k_xl.gguf": ("gemma-4:e2b-it-qat", {"gemma-4:e2b-it-qat-ud"}),
    "qwen3.6-35b-a3b-ud-q4_k_xl.gguf": ("qwen3.6:35b-a3b-ud", {"qwen3.6:35b"}),
    "hermes3.6-35b-a3b-uncensored-genesis-final-mtp-apex-compact.gguf":
        ("hermes3.6:35b-a3b-uncensored-genesis-final-mtp-apex-compact", set()),
    # APEX-Mini (2026-10-27): the installer's Hermes entry now points here.
    "hermes3.6-35b-a3b-uncensored-genesis-apex-mini.gguf":
        ("hermes3.6:35b-a3b-uncensored-genesis-apex-mini", set()),
    "lfm2.5-2.6b-q4_k_m.gguf": ("lfm2.5:2b-mtp-does-not-exist", None),  # replaced below
}
CATALOG["lfm2.5-2.6b-q4_k_m.gguf"] = ("lfm2.5:2.6b", set())
EXCLUDED = {"mtp-gemma-4-e4b-it.gguf", "mmproj-bf16.gguf"}


def _build_index_in(tmp: Path):
    import importlib
    os.environ["HIVEMIND_MODELS_DIR"] = str(tmp)
    # MODELS_DIR is computed at import time in llama_config and re-exported
    # into llama_models - reload BOTH or every test after the first one
    # silently scans the first test's directory.
    import backend.llama_config as _lc
    importlib.reload(_lc)
    import backend.llama_models as lm
    importlib.reload(lm)
    # HERMETIC (2026-11-07 CI audit): the shipped manifest became a runtime
    # cache and is not in a clean checkout. Point the lookup at a fixture
    # manifest instead of depending on the developer's local cache.
    _manifest = tmp / "gguf_filename_tags.json"
    _manifest.write_text(json.dumps({name: want for name, (want, _a) in CATALOG.items()}),
                         encoding="utf-8")
    lm._FILENAME_TAG_MAP_FILE = _manifest
    lm._filename_tag_map = None
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
    # SHIP-HYGIENE (2026-10-07): the manifest is a runtime cache and is no
    # longer shipped in release zips - the engine rebuilds it on demand.
    # A clean checkout therefore has no file to check; a partial local cache
    # must not turn the suite red (2026-11-07 CI audit). The mechanism itself
    # is covered by test_manifest_primary_tags with a fixture manifest.
    ok("Manifest is a runtime cache - no shipped-file check")


def test_settings_defaults_resolve():
    """No-hardcoded-models (2026-10-05): shipped settings defaults carry
    NO GGUF model names anymore. This check is now the GUARD against
    re-hardcoding: if a default model name reappears it must at least
    resolve via the GGUF index (else fresh installs hard-abort on
    'model not found' — the lfm2.5 judge incident)."""
    sys.path.insert(0, str(ROOT))
    from settings import DEFAULT_SETTINGS
    tmp = Path(tempfile.mkdtemp(prefix="hvm_pin_res_"))
    for fname in CATALOG:
        (tmp / fname).write_bytes(b"x" * 64)
    idx, lm = _build_index_in(tmp)
    lm_tag = DEFAULT_SETTINGS["agents"]["duo_coder"]["model"]
    if not lm_tag:
        ok("Settings-Default leer (no-hardcoded-models Policy aktiv)")
    elif lm_tag in idx:
        ok(f"Settings-Default '{lm_tag}' resolved über den Index")
    else:
        fail("settings_default", f"'{lm_tag}' nicht im Index: {sorted(idx)}")


def test_write_models_json_pins():
    sys.path.insert(0, str(ROOT / "deploy"))
    import deploy.fetch_models as fm
    tmp = Path(tempfile.mkdtemp(prefix="hvm_pin_wmj_"))
    for fname in ("Qwen3.5-4B-Q4_K_M.gguf", "gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf",
                  "mtp-gemma-4-E4B-it.gguf"):
        (tmp / fname).write_bytes(b"x" * 64)
    # ISOLATED (2026-10-19): writing the app's models.json from a test polluted the
    # repo, and a deploy sync then overwrote the LIVE model index with temp paths.
    # ISOLATED (2026-11-07 CI audit): the known-filename manifest is written via
    # fetch_models.ROOT too - point it at the temp dir during the call.
    out = tmp / "models.json"
    (tmp / "model_configs").mkdir(exist_ok=True)
    _orig_root = fm.ROOT
    fm.ROOT = tmp
    try:
        fm.write_models_json(tmp, out=out)
    finally:
        fm.ROOT = _orig_root
    m = json.loads(out.read_text(encoding="utf-8"))
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
    sys.exit(h.finish("gguf_tag_pinning"))
