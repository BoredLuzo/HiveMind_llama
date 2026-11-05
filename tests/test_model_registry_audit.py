"""Model-registry audit pins (2026-11-05 deep audit).

Covers the four P0/P1 fixes that the multi-quant suite does not:
  1. write_models_json MERGES instead of wiping manual registrations
  2. invalidate_caches() really drops index+names+manifest
  3. picker dedup prefers manifest/override names over the longest alias
  4. ghost override keys (file gone) are not listed

Run: python tests/test_model_registry_audit.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend import llama_models as LM  # noqa: E402
from deploy import fetch_models as FM  # noqa: E402

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


from contextlib import contextmanager


@contextmanager
def _isolated(td: Path, known: dict | None = None):
    old = (LM.MODELS_DIR, LM._MODELS_JSON, LM._filename_tag_map,
           LM._index_cache, LM._index_ts, LM._names_cache, LM._names_cache_ts)
    try:
        LM.MODELS_DIR = td / "models"
        LM._MODELS_JSON = td / "models.json"
        LM._filename_tag_map = dict(known) if known else {}
        LM._index_cache = None
        LM._index_ts = 0.0
        LM._names_cache = None
        LM._names_cache_ts = 0.0
        yield LM
    finally:
        (LM.MODELS_DIR, LM._MODELS_JSON, LM._filename_tag_map,
         LM._index_cache, LM._index_ts, LM._names_cache,
         LM._names_cache_ts) = old


def main() -> int:
    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        models = td / "models"

        # ── 1. write_models_json merge ────────────────────────────────
        manual = models / "Manual-Model-Q8_0.gguf"
        _fake_gguf(manual, 3)
        out = td / "models.json"
        out.write_text(json.dumps({
            "my-manual:model": str(manual),
            "stale:model": str(td / "gone.gguf"),
            "_comment": "keep me",
            "badtype": 42,
        }), encoding="utf-8")
        _fake_gguf(models / "Qwen3.5-4B-Q4_K_M.gguf", 4)
        n = FM.write_models_json(models, out=out)
        merged = json.loads(out.read_text(encoding="utf-8"))
        check("R1: manual registration survives the scan",
              "my-manual:model" in merged
              and merged["my-manual:model"] == str(manual))
        check("R1: stale entry (file gone) is dropped",
              "stale:model" not in merged)
        check("R1: underscore/typed junk is dropped",
              "_comment" not in merged and "badtype" not in merged)
        check("R1: scanned model still registered",
              any("qwen3.5:4b" in k for k in merged) and n >= 2)

        # ── 2. invalidate_caches + coherent force_refresh ─────────────
        with _isolated(td) as LM_:
            _fake_gguf(models / "Cache-Check-Q4_K_M.gguf", 2)
            _l1 = LM_.list_available_models()
            check("R2: model listed before rename",
                  any("cache-check" in n for n in _l1))
            (models / "Cache-Check-Q4_K_M.gguf").rename(
                models / "Cache-Check-Renamed-Q4_K_M.gguf")
            LM_.invalidate_caches()
            _l2 = LM_.list_available_models(force_refresh=True)
            check("R2: old name gone after invalidate+refresh",
                  not any("cache-check-q4" == n for n in _l2))
            check("R2: renamed model listed under the new name",
                  any("cache-check-renamed" in n for n in _l2))
            # invalidate first, THEN verify all three caches are down
            # (a later refresh legitimately re-populates the manifest).
            LM_._filename_tag_map = {"x.gguf": "y"}  # simulate warm cache
            LM_.invalidate_caches()
            check("R2: invalidate drops all three caches",
                  LM_._index_cache is None and LM_._names_cache is None
                  and LM_._filename_tag_map is None)

        # ── 3. picker dedup priority ──────────────────────────────────
        with _isolated(td, known={
                "gemma-4-e4b-it-qat-ud-q4_k_xl.gguf": "gemma-4:e4b-it-qat"}) \
                as LM_:
            _fake_gguf(models / "gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf", 5)
            _listing = LM_.list_available_models(force_refresh=True)
            check("R3: manifest tag is listed",
                  "gemma-4:e4b-it-qat" in _listing)
            check("R3: the parsed '-ud' alias collapses into it",
                  "gemma-4:e4b-it-qat-ud" not in _listing)

        # ── 4. ghost override keys ────────────────────────────────────
        with _isolated(td) as LM_:
            _fake_gguf(models / "Real-Model-Q4_K_M.gguf", 2)
            LM_._MODELS_JSON.write_text(json.dumps({
                "ghost:model": str(td / "deleted.gguf"),
                "real:model": str(models / "Real-Model-Q4_K_M.gguf"),
            }), encoding="utf-8")
            _lst = LM_.list_available_models(force_refresh=True)
            check("R4: ghost override key is not listed",
                  "ghost:model" not in _lst)
            check("R4: real override key stays listed",
                  "real:model" in _lst)

    print(f"\nSUMMARY model_registry_audit: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
