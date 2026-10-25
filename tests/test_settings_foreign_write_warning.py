# -*- coding: utf-8 -*-
"""FORENSIC CASE 2026-10-24/25: silent full-dict saves made the revert
unprovable. save_settings() persists the whole in-memory dict; when another
writer (hand edit, second install) changed settings.json behind the engine's
back, the save reverted those edits without a trace — the live log had
nothing to correlate (the llama_mlock=true half of the case could only be
reconstructed, not proven, for exactly this reason).

Fix: save_settings keeps a (mtime_ns, size) watermark of the last read/write
by this process and logs a WARNING when the on-disk file changed in between.
Detection only — the save always proceeds (endpoints rely on it). Behavioral
test on a tmp file per house rules.
"""
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings as _settings  # noqa: E402

passed = failed = 0


def ok(name, extra=""):
    global passed
    passed += 1
    print(f"  PASS  {name}  {extra}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


_ORIG_FILE = _settings.SETTINGS_FILE
_ORIG_CACHE_KEY = _settings._load_cache_key
_ORIG_CACHE_DATA = _settings._load_cache_data
_ORIG_WATERMARK = _settings._last_sync_stat


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _reset_cache():
    _settings._load_cache_key = None
    _settings._load_cache_data = None
    _settings._last_sync_stat = None


def main() -> int:
    _tmp = Path(tempfile.mkdtemp(prefix="hivemind_test_foreignwrite_"))
    cap = _Capture()
    _lg = logging.getLogger("hivemind.settings")
    _lg.addHandler(cap)
    try:
        _test = _tmp / "settings.json"
        _test.write_text(json.dumps({
            "llama_mlock": False,
            "duo_test_feedback_chunk": True,
            "_marker_len_a": "aaaaaaaaaaaaaaaaaaaa",
        }), encoding="utf-8")
        _settings.SETTINGS_FILE = _test
        _reset_cache()

        print("\n=== save_settings foreign-write warning (forensic 2026-10-24/25) ===\n")

        # align the watermark the way every engine boot does
        _settings.load_settings()
        if _settings._last_sync_stat is not None:
            ok("A1: load_settings records the sync watermark")
        else:
            fail("A1: watermark not recorded on load")

        # a writer outside this process changes the file (the hand edit)
        _test.write_text(json.dumps({
            "llama_mlock": False,
            "duo_test_feedback_chunk": True,
            "_marker_len_b": "b",
        }), encoding="utf-8")

        cap.records.clear()
        _memory = {"llama_mlock": False, "duo_test_feedback_chunk": True}
        _settings.save_settings(_memory)
        warned = any(r.levelno == logging.WARNING and "foreign write" in r.getMessage()
                     for r in cap.records)
        if warned:
            ok("A2: foreign write before save -> WARNING logged")
        else:
            fail("A2: no warning on foreign write", [r.getMessage() for r in cap.records])
        on_disk = json.loads(_test.read_text(encoding="utf-8"))
        if on_disk.get("duo_test_feedback_chunk") is True:
            ok("A3: save still proceeds (detection only, no behavior change)")
        else:
            fail("A3: save was altered by the guard", str(on_disk))

        # second save with no external writer: must NOT warn again
        cap.records.clear()
        _settings.save_settings({"llama_mlock": False})
        if not any(r.levelno == logging.WARNING for r in cap.records):
            ok("B1: watermark updated after own save -> no false warning")
        else:
            fail("B1: false warning on consecutive save",
                 [r.getMessage() for r in cap.records])

        # first-ever save in a fresh process (no watermark yet): no false warning
        _reset_cache()
        cap.records.clear()
        _settings.save_settings({"llama_mlock": False})
        if not any(r.levelno == logging.WARNING for r in cap.records):
            ok("C1: no warning when the process never synced before")
        else:
            fail("C1: false warning without a prior sync",
                 [r.getMessage() for r in cap.records])

        # a reload re-aligns the watermark (boot after hand edit must not warn)
        cap.records.clear()
        _settings.load_settings()
        _settings.save_settings({"llama_mlock": False})
        if not any(r.levelno == logging.WARNING for r in cap.records):
            ok("D1: load after foreign edit re-aligns the watermark")
        else:
            fail("D1: warning after re-aligning load",
                 [r.getMessage() for r in cap.records])
    finally:
        _settings.SETTINGS_FILE = _ORIG_FILE
        _settings._load_cache_key = _ORIG_CACHE_KEY
        _settings._load_cache_data = _ORIG_CACHE_DATA
        _settings._last_sync_stat = _ORIG_WATERMARK
        _lg.removeHandler(cap)
        shutil.rmtree(str(_tmp), ignore_errors=True)

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
