"""STRICT-VISION: the [VISION] note ("you can see the image") may only appear
when the coder slot is CONFIRMED to run with its projector.

Owner request 2026-10-17: "vision should only come when mmproj is also loaded".
Before this, the note was appended whenever the PLAN said the coder would get
the raw image - including a fresh load where the slot did not exist yet and
/props had never been read. Only a post-load retraction cleaned that up, and it
deliberately stayed silent when the vision state was unknown (None).

Rule now (core/duo_helpers.py vision_note_for_coder):
  coder_slot_vision is True  -> note
  coder_slot_vision is False -> no note (projector missing, existing behaviour)
  coder_slot_vision is None  -> no note (unproven: slot not loaded, /props
                                unreachable, no modalities field)
The post-load gate adds the note once /props confirms vision:true, so a normal
vision run still carries it.

Pins:
  (a) the pure helper for all three states and a non-raw plan
  (b) source guards: the pre-prompt build uses the helper; the post-load gate
      adds on True and removes on None
  (c) the old "trusting the plan" wording is gone

Run: python tests/test_vision_strict.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from core.duo_helpers import vision_note_for_coder, CODER_VISION_NOTE  # noqa: E402

RAW = {"planner": "raw", "coder": "raw", "warnings": [], "mode": "direct"}
DESC = {"planner": "raw", "coder": "description", "warnings": [], "mode": "direct"}
NONE_PLAN = {"planner": "none", "coder": "none", "warnings": [], "mode": "direct"}

# ── (a) the helper ──────────────────────────────────────────────────────────
check("raw + True -> note", vision_note_for_coder(RAW, True) == CODER_VISION_NOTE)
check("raw + False -> no note", vision_note_for_coder(RAW, False) == "")
check("raw + None -> no note (unproven)", vision_note_for_coder(RAW, None) == "")
check("description plan -> no note", vision_note_for_coder(DESC, True) == "")
check("none plan -> no note", vision_note_for_coder(NONE_PLAN, True) == "")
check("empty plan -> no note", vision_note_for_coder({}, True) == "")
check("None plan -> no note", vision_note_for_coder(None, True) == "")
check("truthy-but-not-True is not proof (1)",
      vision_note_for_coder(RAW, 1) == "")
check("truthy-but-not-True is not proof ('yes')",
      vision_note_for_coder(RAW, "yes") == "")

# ── (b) wiring ──────────────────────────────────────────────────────────────
_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("pre-prompt build asks the helper",
      "_vn_pre = vision_note_for_coder(_img_plan, _cv_pre)" in _src)
check("the raw branch no longer appends the note unconditionally",
      "elif _img_plan.get(\"coder\") == \"raw\":\n            # VISION NOTE" in _src
      and "_duo_coder_sys += CODER_VISION_NOTE\n        # SAVED-PATH" not in _src)
check("slot vision is read before the prompt is built",
      "_cv_pre = getattr(_cslot_pre, \"vision_active\", None)" in _src)
check("post-load ADDS the note when the projector is confirmed",
      "[VISION] note added" in _src and "_cv2 is True" in _src)
check("post-load removes the note when vision is unproven",
      "strict rule" in _src and "_cv2 is None" not in _src)
check("post-load keeps the False retraction",
      "[VISION] note retracted" in _src)

# ── (c) the coder gate no longer trusts an unknown state ────────────────────
_coder_gate = _src.split("VISION-TRUTH GATE post-load")[-1][:2600]
check("coder post-load gate has no 'trusting the plan' branch",
      "trusting the plan" not in _coder_gate,
      " (the planner gate keeps its log-only Unknown branch - it makes no "
      "claim to the model)")
check("planner gate is unchanged (Unknown still only logs)",
      "planner vision state UNKNOWN" in _src)

# the note constant itself is unchanged (the retraction depends on its text)
check("note text unchanged",
      vision_note_for_coder(RAW, True).startswith(CODER_VISION_NOTE)
      and "you can see it" in CODER_VISION_NOTE)

print()
print(f"SUMMARY vision_strict: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
