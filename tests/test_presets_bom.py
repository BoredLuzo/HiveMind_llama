#!/usr/bin/env python3
"""load_presets() tolerates a UTF-8 BOM in presets.json.

Live finding (2026-09-08): a presets.json written by an external Windows
editor carried a UTF-8 BOM; the strict utf-8 json.loads threw,
load_presets() silently returned {} and every preset load 404'd (the UI
swallowed the error). utf-8-sig reads both variants.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settings

_ok = True


def _check(name, cond):
    global _ok
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    _ok = _ok and cond


def main() -> int:
    data = {
        "p1": {"duo_chunking": True, "duo_planner_model": "m1"},
        "p2": {"duo_chunking": False},
    }
    orig = settings.PRESETS_FILE
    try:
        settings.PRESETS_FILE = orig.with_name(".presets_bom_test.json")

        settings.PRESETS_FILE.write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
        loaded = settings.load_presets()
        _check("bom payload loads", loaded.get("p1", {}).get("duo_planner_model") == "m1"
               and loaded.get("p2", {}).get("duo_chunking") is False)

        settings.PRESETS_FILE.write_text(json.dumps(data), encoding="utf-8")
        loaded = settings.load_presets()
        _check("plain utf-8 loads", "p1" in loaded and "p2" in loaded)

        settings.PRESETS_FILE.unlink()
        loaded = settings.load_presets()
        _check("missing file -> defaults", isinstance(loaded, dict))

        settings.PRESETS_FILE.write_text("{not json", encoding="utf-8")
        loaded = settings.load_presets()
        _check("broken json -> defaults", isinstance(loaded, dict))
    finally:
        if settings.PRESETS_FILE.exists():
            settings.PRESETS_FILE.unlink()
        settings.PRESETS_FILE = orig

    print("PASS" if _ok else "FAIL")
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
