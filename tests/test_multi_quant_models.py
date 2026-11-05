"""Multi-quant model registration: same model, several quantizations.

Live case (2026-11-05): a user registered Qwen3.6-35B Q4_K_M, then added an
IQ1_M of the same model. Both parse to the SAME canonical names, and the old
index rule kept the SMALLER file — the registered Q4_K_M silently vanished
from the picker. Also: a llama.cpp slot id ('llamacpp:<sha>') must never be
persisted into models.json.

Run: python tests/test_multi_quant_models.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend import llama_models as LM  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _fake_gguf(path: Path, mb: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * (mb * 1024 * 1024))
    return path


def _reindex(td: Path, overrides: dict | None = None,
             known: dict | None = None):
    LM._MODELS_JSON = td / "models.json"
    if overrides is not None:
        LM._MODELS_JSON.write_text(json.dumps(overrides), encoding="utf-8")
    else:
        LM._MODELS_JSON = td / "models_missing.json"
    old_models_dir, old_map, old_cache = (
        LM.MODELS_DIR, LM._filename_tag_map, LM._index_cache)
    old_names_cache = getattr(LM, "_names_cache", None)
    try:
        LM.MODELS_DIR = td / "models"
        LM._filename_tag_map = dict(known) if known else {}
        LM._index_cache = None
        LM._index_ts = 0.0
        LM._names_cache = None
        LM._names_cache_ts = 0.0
        index = LM._build_index()
        listing = LM.list_available_models(force_refresh=True)
        return index, listing
    finally:
        LM.MODELS_DIR = old_models_dir
        LM._filename_tag_map = old_map
        LM._index_cache = old_cache
        LM._index_ts = 0.0
        LM._names_cache = old_names_cache
        LM._names_cache_ts = 0.0


def main() -> int:
    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        models = td / "models"
        # Two quants of the same model, the BIGGER one registered via
        # models.json (the setup_models flow).
        _fake_gguf(models / "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf", 21)
        _fake_gguf(models / "Qwen3.6-35B-A3B-UD-IQ1_M.gguf", 9)
        index, listing = _reindex(
            td, overrides={"qwen3.6:35b-a3b-ud":
                           str(models / "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf")})
        check("registered quant keeps the canonical name",
              index.get("qwen3.6:35b-a3b-ud", Path()).name
              == "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf")
        check("other quant stays reachable as quant alias",
              index.get("qwen3.6:35b-a3b-ud-iq1_m", Path()).name
              == "Qwen3.6-35B-A3B-UD-IQ1_M.gguf")
        check("both quants are listed",
              "qwen3.6:35b-a3b-ud" in listing
              and "qwen3.6:35b-a3b-ud-iq1_m" in listing)

        # No override: the LARGER file wins (higher quant ≈ better), the
        # smaller one gets the alias — never the old smaller-wins swap.
        index2, _ = _reindex(td)
        check("without override the larger quant wins",
              index2.get("qwen3.6:35b-a3b-ud", Path()).name
              == "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf")
        check("smaller quant keeps an alias",
              index2.get("qwen3.6:35b-a3b-ud-iq1_m", Path()).name
              == "Qwen3.6-35B-A3B-UD-IQ1_M.gguf")

        # Known-filename manifest claim beats size.
        _fake_gguf(models / "Qwen3.5-4B-Q4_K_S.gguf", 2)
        _fake_gguf(models / "Qwen3.5-4B-Q4_K_M.gguf", 3)
        index3, listing3 = _reindex(td, known={
            "qwen3.5-4b-q4_k_m.gguf": "qwen3.5:4b-mtp"})
        check("known-tag file wins its canonical name",
              index3.get("qwen3.5:4b-mtp", Path()).name
              == "Qwen3.5-4B-Q4_K_M.gguf")
        check("second quant lists under an explicit quant name",
              "qwen3.5:4b-mtp" in listing3
              and "qwen3.5:4b-q4_k_s" in listing3)

        # Same name, same size: deterministic (first file), no crash.
        _fake_gguf(models / "Qwen3.5-9B-Q4_K_M.gguf", 5)
        _fake_gguf(models / "alt" / "Qwen3.5-9B-Q4_K_M.gguf", 5)
        index4, _ = _reindex(td, known={
            "qwen3.5-4b-q4_k_m.gguf": "qwen3.5:4b-mtp",
            "qwen3.5-9b-q4_k_m.gguf": "qwen3.5:9b-mtp"})
        check("equal sizes resolve without crash",
              index4.get("qwen3.5:9b-mtp") is not None)

        # save_model_mapping refuses llama.cpp slot ids.
        LM._MODELS_JSON = td / "guard.json"
        LM._MODELS_JSON.write_text("{}", encoding="utf-8")
        LM.save_model_mapping("llamacpp:e508d318d8df", str(models / "x.gguf"))
        saved = json.loads(LM._MODELS_JSON.read_text(encoding="utf-8"))
        check("slot id never lands in models.json",
              not any(k.startswith("llamacpp:") for k in saved))

    print(f"\nSUMMARY multi_quant_models: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
