# -*- coding: utf-8 -*-
"""VISION-TRUTH gate (2026-10-04, Sonnet review).

effective_image_plan() must downgrade 'raw' entries when the LOADED server
really runs without a projector (slot.vision_active is False from
/props.modalities.vision), fall back to the description when one exists,
and trust the plan when /props was unreachable (None). count_image_parts()
backs the images_in_request=N trace line. All pure - no server needed.
"""
import copy
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.duo_helpers import (
    count_image_parts,
    effective_image_plan,
    CODER_VISION_NOTE,
)


def _plan(planner="raw", coder="raw"):
    return {"planner": planner, "coder": coder, "warnings": [], "mode": "direct"}


# ── matrix: slot truth × plan ──────────────────────────────────────────────

def test_slots_see_images_plan_untouched():
    plan, warns = effective_image_plan(_plan(), planner_slot_vision=True,
                                       coder_slot_vision=True)
    assert plan["planner"] == "raw" and plan["coder"] == "raw"
    assert warns == []


def test_props_unreachable_trusts_plan():
    plan, warns = effective_image_plan(_plan(), planner_slot_vision=None,
                                       coder_slot_vision=None)
    assert plan["planner"] == "raw" and plan["coder"] == "raw"
    assert warns == []


def test_planner_vision_false_falls_back_to_description():
    plan, warns = effective_image_plan(_plan(), planner_slot_vision=False,
                                       has_description=True)
    assert plan["planner"] == "description"
    assert plan["coder"] == "raw"  # other role untouched
    assert len(warns) == 1 and "planner" in warns[0]


def test_planner_vision_false_no_description_goes_none():
    plan, warns = effective_image_plan(_plan(), planner_slot_vision=False,
                                       has_description=False)
    assert plan["planner"] == "none"
    assert len(warns) == 1 and "skipped" in warns[0]


def test_coder_vision_false_falls_back_to_description():
    plan, warns = effective_image_plan(_plan(), coder_slot_vision=False,
                                       has_description=True)
    assert plan["coder"] == "description"
    assert plan["planner"] == "raw"
    assert len(warns) == 1 and "coder" in warns[0]


def test_coder_vision_false_no_description_goes_none():
    plan, warns = effective_image_plan(_plan(), coder_slot_vision=False,
                                       has_description=False)
    assert plan["coder"] == "none"
    assert len(warns) == 1


def test_both_roles_false_two_warnings():
    plan, warns = effective_image_plan(_plan(), planner_slot_vision=False,
                                       coder_slot_vision=False,
                                       has_description=True)
    assert plan["planner"] == "description" and plan["coder"] == "description"
    assert len(warns) == 2


def test_description_plan_never_downgraded():
    plan, warns = effective_image_plan(_plan("description", "description"),
                                       planner_slot_vision=False,
                                       coder_slot_vision=False,
                                       has_description=True)
    assert plan["planner"] == "description" and plan["coder"] == "description"
    assert warns == []


def test_none_plan_untouched():
    plan, warns = effective_image_plan(_plan("none", "none"),
                                       planner_slot_vision=False,
                                       coder_slot_vision=False)
    assert plan["planner"] == "none" and plan["coder"] == "none"
    assert warns == []


def test_pure_does_not_mutate_input():
    src = _plan()
    snapshot = copy.deepcopy(src)
    effective_image_plan(src, planner_slot_vision=False, coder_slot_vision=False,
                         has_description=True)
    assert src == snapshot


# ── count_image_parts (images_in_request trace) ────────────────────────────

def test_count_zero_for_plain_strings_and_empty():
    assert count_image_parts([]) == 0
    assert count_image_parts(None) == 0
    assert count_image_parts([{"role": "user", "content": "hi"}]) == 0


def test_counts_image_url_parts_only():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,y"}},
        {"type": "text", "text": "fix this"},
    ]}]
    assert count_image_parts(msgs) == 2


def test_counts_across_roles():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:x"}},
            {"type": "text", "text": "q"},
        ]},
        {"role": "assistant", "content": [
            {"type": "image_url", "image_url": {"url": "data:y"}},
        ]},
    ]
    assert count_image_parts(msgs) == 2


# ── note retraction constant ───────────────────────────────────────────────

def test_vision_note_retracts_cleanly():
    sys_prompt = "base prompt" + CODER_VISION_NOTE
    assert "[VISION]" in sys_prompt
    assert sys_prompt.replace(CODER_VISION_NOTE, "") == "base prompt"
    # idempotent: stripping twice changes nothing
    once = sys_prompt.replace(CODER_VISION_NOTE, "")
    assert once.replace(CODER_VISION_NOTE, "") == once


# ── slot lifecycle: vision_active truth flag ───────────────────────────────

def test_slot_vision_active_defaults_none_and_resets_on_kill():
    from backend.llama_slots import ModelSlot
    s = ModelSlot(0)
    assert s.vision_active is None  # unknown until /props was fetched
    s.vision_active = True
    s.kill()  # process is None - safe, resets state
    assert s.vision_active is None


def test_manager_get_slot_by_port():
    from backend.llama_server_manager import manager
    s = manager._slots[1]
    class _Live:
        def poll(self):
            return None
    s.process = _Live()  # simulate a running server on that slot
    assert manager.get_slot_by_port(s.port) is s
    # a dead process without orphan port must NOT be returned
    s.process = None
    s._orphan_port = None
    assert manager.get_slot_by_port(s.port) is None
