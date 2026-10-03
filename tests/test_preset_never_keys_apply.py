"""Preset APPLY must honor _PRESET_NEVER_KEYS, not only the snapshot.

UI-state keys (image_processing_mode, vision_agent_mode) lost their
backend readers (2026-10-03). Presets SAVED before that change still
carry these keys on disk; loading such a preset must not inject the
stale values back into the settings dict - the filter has to hold on
the apply path, not just when saving a snapshot.

Run: python tests/test_preset_never_keys_apply.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

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


def main():
    import routers.config as rc

    # Old-format preset: carries the retired UI-state keys alongside a
    # harmless real key so we can tell "apply ran" from "apply skipped".
    _FAKE_PRESETS = {
        "legacy-ui-state": {
            "image_processing_mode": "preprocess",
            "vision_agent_mode": "parallel",
            "git_commit_prefix": "legacy:",
        }
    }
    rc.load_presets = lambda: dict(_FAKE_PRESETS)

    _s = rc.settings
    _before_prefix = _s.get("git_commit_prefix")
    _had_img_mode = "image_processing_mode" in _s
    _had_va_mode = "vision_agent_mode" in _s
    _before_img = _s.get("image_processing_mode")
    _before_va = _s.get("vision_agent_mode")

    applied = asyncio_run(rc._apply_preset_internal("legacy-ui-state", persist=False))
    if not applied:
        fail("apply ran", "_apply_preset_internal returned False")
        return

    if _s.get("git_commit_prefix") == "legacy:":
        ok("regular preset keys still apply")
    else:
        fail("regular keys", f"git_commit_prefix={_s.get('git_commit_prefix')!r}")

    if "image_processing_mode" not in _s and (_before_img == _s.get("image_processing_mode")):
        ok("image_processing_mode from old preset is NOT applied")
    else:
        fail("image_processing_mode applied", repr(_s.get("image_processing_mode")))

    if "vision_agent_mode" not in _s and (_before_va == _s.get("vision_agent_mode")):
        ok("vision_agent_mode from old preset is NOT applied")
    else:
        fail("vision_agent_mode applied", repr(_s.get("vision_agent_mode")))

    # restore whatever the run had before
    _s["git_commit_prefix"] = _before_prefix
    if _had_img_mode:
        _s["image_processing_mode"] = _before_img
    if _had_va_mode:
        _s["vision_agent_mode"] = _before_va

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
