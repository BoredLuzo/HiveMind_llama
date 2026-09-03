"""Behavioral Test: Planner-Model/-Context persist across settings reloads (2026-09-03).

Live-Befund: `_load_settings_from_disk()` hat duo_planner_model bei JEDEM
Reload hart auf None gesetzt (settings.py, "data[\"duo_planner_model\"] = None").
Dadurch ging ein per Preset/UI gewaehltes Planner-Modell nach jedem Neustart
verloren (Planner fiel still auf das Coder-Modell zurueck).

Run: python tests/test_planner_model_persist.py
Exit 0 = all pass, Exit 1 = failures.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import settings as _settings  # noqa: E402

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


_ORIG_FILE = _settings.SETTINGS_FILE
_ORIG_CACHE_KEY = _settings._load_cache_key
_ORIG_CACHE_DATA = _settings._load_cache_data


def _reset_cache():
    _settings._load_cache_key = None
    _settings._load_cache_data = None


def main():
    _tmp = Path(tempfile.mkdtemp(prefix="hivemind_test_settings_"))
    try:
        _test = _tmp / "settings.json"
        _test.write_text(json.dumps({
            "duo_planner_model": "hermes3.6:35b-a3b-uncensored-genesis-v12-mtp-apex-compact",
            "duo_planner_ctx_target": 20480,
            "duo_planner_use_coder_ctx": False,
            "active_preset": "1",
        }), encoding="utf-8")
        _settings.SETTINGS_FILE = _test
        _reset_cache()

        print("\n=== Planner model/context persistence (2026-09-03) ===\n")
        loaded = _settings._load_settings_from_disk()
        if loaded.get("duo_planner_model") == "hermes3.6:35b-a3b-uncensored-genesis-v12-mtp-apex-compact":
            ok("A1: duo_planner_model survives a reload (was forced None)")
        else:
            fail("A1: duo_planner_model lost", repr(loaded.get("duo_planner_model")))
        if loaded.get("duo_planner_ctx_target") == 20480:
            ok("A2: duo_planner_ctx_target survives a reload")
        else:
            fail("A2: duo_planner_ctx_target lost", repr(loaded.get("duo_planner_ctx_target")))
        if loaded.get("duo_planner_use_coder_ctx") is False:
            ok("A3: duo_planner_use_coder_ctx survives a reload")
        else:
            fail("A3: duo_planner_use_coder_ctx lost", repr(loaded.get("duo_planner_use_coder_ctx")))
        if loaded.get("active_preset") == "1":
            ok("A4: active_preset survives a reload (startup auto-load anchor)")
        else:
            fail("A4: active_preset lost", repr(loaded.get("active_preset")))
        if "mode" in loaded and loaded.get("mode") == _settings.DEFAULT_SETTINGS.get("mode"):
            ok("A5: defaults still merged for missing keys")
        else:
            fail("A5: default merge broken")

        # Round-trip via save_settings → reload must keep the planner model.
        _settings.save_settings(loaded)
        _reset_cache()
        loaded2 = _settings._load_settings_from_disk()
        if loaded2.get("duo_planner_model") == loaded.get("duo_planner_model"):
            ok("B1: planner model survives a save-load round-trip")
        else:
            fail("B1: round-trip lost planner model", repr(loaded2.get("duo_planner_model")))
    finally:
        _settings.SETTINGS_FILE = _ORIG_FILE
        _settings._load_cache_key = _ORIG_CACHE_KEY
        _settings._load_cache_data = _ORIG_CACHE_DATA
        shutil.rmtree(str(_tmp), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
