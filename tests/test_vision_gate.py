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


# ── TEST SWITCH: HIVEMIND_TEST_OMIT_MMPROJ does nothing by default ─────────

def test_omit_mmproj_default_is_inert(monkeypatch):
    from backend.manager_load import _omit_mmproj_for
    monkeypatch.delenv("HIVEMIND_TEST_OMIT_MMPROJ", raising=False)
    assert _omit_mmproj_for("qwen3.5:9b-ud", vision=True) is False
    assert _omit_mmproj_for("gemma-4:e4b", vision=True) is False


def test_omit_mmproj_substring_and_star(monkeypatch):
    from backend.manager_load import _omit_mmproj_for
    monkeypatch.setenv("HIVEMIND_TEST_OMIT_MMPROJ", "9b-ud")
    assert _omit_mmproj_for("qwen3.5:9b-ud", vision=True) is True
    assert _omit_mmproj_for("gemma-4:e4b", vision=True) is False
    monkeypatch.setenv("HIVEMIND_TEST_OMIT_MMPROJ", "*")
    assert _omit_mmproj_for("gemma-4:e4b", vision=True) is True


def test_omit_mmproj_needs_vision_request(monkeypatch):
    # a plain text load (vision=False) never needs a projector - the switch
    # must not touch it either
    from backend.manager_load import _omit_mmproj_for
    monkeypatch.setenv("HIVEMIND_TEST_OMIT_MMPROJ", "*")
    assert _omit_mmproj_for("qwen3.5:9b-ud", vision=False) is False


def test_coder_reload_keeps_same_vision_arg():
    # ORIGINAL BUG (2026-10-04): the in-loop tool reload lost --mmproj while
    # the fresh load had it. Cheap tripwire: every ensure_loaded call that
    # passes a vision flag must derive it from the image plan (planner or
    # coder role) - never a literal or an unrelated expression.
    import io
    import re
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "duo_runner.py")
    lines = io.open(path, encoding="utf-8").read().splitlines()
    _allowed = re.compile(r'^\(?_img_plan\.get\("(planner|coder)"\) == "raw"\)?$')
    _checked = 0
    for i, line in enumerate(lines):
        if "ensure_loaded(" not in line:
            continue
        window = lines[i:i + 10]
        for wline in window:
            if "vision=" not in wline:
                continue
            expr = wline.split("vision=", 1)[1].strip().rstrip(",").strip()
            expr = expr.rstrip(")").strip().lstrip("(").strip()
            _checked += 1
            assert expr in ('_img_plan.get("planner") == "raw"',
                            '_img_plan.get("coder") == "raw"'), \
                f"ensure_loaded near line {i+1}: vision arg not from the image plan: {expr!r}"
    assert _checked >= 3, f"expected at least the 3 known vision loads, found {_checked}"


def test_direct_load_path_has_truth_gate():
    # RUN D / SONNET #5: the direct mode is the path fresh installs use
    # first. chat_stream must (a) request the projector from the wire shape
    # and (b) check the /props truth and strip images when it says false.
    import io
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "backend", "llama_client.py")
    src = io.open(path, encoding="utf-8").read()
    assert "_vision = self._has_images(messages)" in src \
        and "ensure_loaded(model, num_ctx=ctx, vision=_vision)" in src, \
        "chat_stream must request the projector from the message shape"
    assert "vision_active" in src and "images dropped" in src, \
        "chat_stream must check /props truth and strip images visibly in the log"


# ── direct-mode drop decision (Sonnet round 7 #2): the warning may fire
#    ONLY when the slot belongs to the requested model AND /props really
#    said false. All other states trust the plan. ─────────────────────────

def test_direct_drop_no_slot_trusts_plan():
    from core.duo_helpers import direct_vision_should_drop
    # the client loads AFTER this check - an absent slot must not drop
    assert direct_vision_should_drop(None, "", "minicpm5:2b") is False


def test_direct_drop_other_model_never_drops():
    from core.duo_helpers import direct_vision_should_drop
    # a pinned planner (no projector) on another slot must not cause a
    # false drop on a healthy run
    assert direct_vision_should_drop(False, "gemma-4:e4b-uncensored-hauhaucs-aggressive",
                                     "minicpm5:2b") is False


def test_direct_drop_matching_slot_true_keeps_images():
    from core.duo_helpers import direct_vision_should_drop
    assert direct_vision_should_drop(True, "minicpm5:2b", "minicpm5:2b") is False


def test_direct_drop_matching_slot_false_drops():
    from core.duo_helpers import direct_vision_should_drop
    assert direct_vision_should_drop(False, "minicpm5:2b", "minicpm5:2b") is True


def test_direct_drop_unknown_props_trusts_plan():
    from core.duo_helpers import direct_vision_should_drop
    assert direct_vision_should_drop(None, "minicpm5:2b", "minicpm5:2b") is False


# ── RUN-D FIX regression (2026-10-04): the ToolLoop builds its own POST
#    payload - it MUST convert the raw "images" key to image_url parts, or
#    llama-server silently ignores the image ("please attach the image"
#    although it was in the payload). Source-level pin + unit pin. ─────────

def test_toolloop_payload_converts_images():
    import io
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "tool_loop.py")
    src = io.open(path, encoding="utf-8").read()
    assert "_msgs_to_parts(_tool_messages)" in src, \
        "ToolLoop payload must run messages through _convert_messages"


def test_convert_messages_turns_images_key_into_parts():
    from backend.llama_client import _convert_messages
    msgs = [{"role": "user", "content": "[USER]\nhi",
             "images": ["QUJD", "data:image/png;base64,WFla"]}]
    out = _convert_messages(msgs)
    c = out[0]["content"]
    assert isinstance(c, list) and len(c) == 3
    assert c[0]["type"] == "image_url" and "QUJD" in c[0]["image_url"]["url"]
    assert c[1]["type"] == "image_url" and "WFla" in c[1]["image_url"]["url"]
    assert c[2] == {"type": "text", "text": "[USER]\nhi"}
    assert "images" not in out[0]  # raw key gone - llama-server cannot read it
    # no images -> untouched passthrough
    assert _convert_messages([{"role": "user", "content": "x"}]) == [{"role": "user", "content": "x"}]


# ── NOTE-RETRACT ON LOSS (round 9 #4, blocker): when the outgoing window
#    no longer carries image parts, the [VISION] note must leave the
#    system prompt. Exercised through the REAL detection code path. ───────

def test_vision_note_retracted_when_outgoing_count_zero():
    from core.duo_helpers import CODER_VISION_NOTE
    from core import agentic_tool_loop as atl
    msgs = [
        {"role": "system", "content": "base prompt" + CODER_VISION_NOTE},
        {"role": "user", "content": "plan text"},
    ]
    assert atl._count_image_parts(msgs) == 0
    # the replacement must BOTH stop the false claim AND tell the model
    # the image is gone (round 10 #3: remove-only left it improvising)
    msgs = atl._retract_vision_note_if_lost(msgs)
    assert CODER_VISION_NOTE not in msgs[0]["content"]
    assert "NO LONGER in your context" in msgs[0]["content"]
    assert "do NOT invent" in msgs[0]["content"]
    assert "plan text" == msgs[1]["content"]  # untouched


def test_vision_note_kept_while_parts_flow():
    from core.duo_helpers import CODER_VISION_NOTE
    from core import agentic_tool_loop as atl
    msgs = [
        {"role": "system", "content": "base" + CODER_VISION_NOTE},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}},
            {"type": "text", "text": "q"},
        ]},
    ]
    assert atl._count_image_parts(msgs) == 1
    out = atl._retract_vision_note_if_lost(msgs)
    assert CODER_VISION_NOTE in out[0]["content"]


def test_retract_helper_tolerates_non_string_system():
    from core import agentic_tool_loop as atl
    msgs = [{"role": "system", "content": [{"type": "text", "text": "parts"}]}]
    assert atl._retract_vision_note_if_lost(msgs) == msgs


# ── ToolLoop payload BEHAVIOR test: a fake llama-server records the body -
#    the request the loop actually sends must carry image_url parts and no
#    raw "images" key (Sonnet round 9 #5). ────────────────────────────────

def test_toolloop_fake_server_sees_parts(tmp_path=None):
    import asyncio
    import http.server
    import json as _json
    import threading

    captured = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            captured["body"] = _json.loads(body)
            resp = (b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
                    b'data: [DONE]\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import httpx as _httpx
        from core.tool_loop import ToolLoop, ToolLoopConfig
        loop = ToolLoop(
            config=ToolLoopConfig(stream=True, max_rounds=1, model="fake:1b",
                                  tools=[], tool_mode="", require_tool_call=False),
            http_client=_httpx.AsyncClient(), port=port, workspace="",
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[USER]\nfix", "images": ["QUJD"]},
        ]
        async def _drive():
            async for _ev in loop.run(msgs):
                pass
        asyncio.run(_drive())
        sent = captured.get("body", {}).get("messages", [])
        user = next(m for m in sent if m.get("role") == "user")
        assert isinstance(user["content"], list) and user["content"][0]["type"] == "image_url", \
            f"fake server saw non-part content: {type(user['content'])}"
        assert "images" not in user, "raw images key must not reach the wire"
    finally:
        srv.shutdown()


# ── the graded-down plan must never carry the [VISION] note
#    (Sonnet round 7 #3): tests the REAL prompt-build branch, not the
#    retraction string-replace. ────────────────────────────────────────────

class _PromptCtx:
    settings = {}
    websearch_available = False
    active_preset = None
    use_learned = False

    class duo_config:
        test_feedback_chunk = False
        test_feedback_final = False
        until_finished = False

    def get_effective_prompt_with_override(self, *_a, **_k):
        return ""


def test_downgraded_plan_builds_coder_prompt_without_vision_note():
    from core.duo_helpers import _build_duo_coder_sys, effective_image_plan, CODER_VISION_NOTE
    plan = {"planner": "raw", "coder": "raw", "warnings": [], "mode": "direct"}
    # the post-load gate says the coder slot really runs without a projector:
    plan, _warns = effective_image_plan(plan, coder_slot_vision=False,
                                        has_description=True)
    assert plan["coder"] == "description"
    # prompt build EXACTLY like the runner: note only when the plan says raw
    base = _build_duo_coder_sys(_PromptCtx(), has_plan=True, has_subtasks=False,
                                has_explore_ctx=False)
    sys_prompt = base
    if plan.get("planner") == "raw" and plan.get("coder") != "raw":
        sys_prompt += "[IMAGE NOTE]"  # planner-saw-it branch
    elif plan.get("coder") == "raw":
        sys_prompt += CODER_VISION_NOTE
    assert CODER_VISION_NOTE not in sys_prompt
    assert "[VISION]" not in sys_prompt

    # control: the SAME builder WITH a raw plan DOES carry the note
    plan_raw = {"planner": "raw", "coder": "raw", "warnings": [], "mode": "direct"}
    sys_raw = base
    if plan_raw.get("planner") == "raw" and plan_raw.get("coder") != "raw":
        sys_raw += "[IMAGE NOTE]"
    elif plan_raw.get("coder") == "raw":
        sys_raw += CODER_VISION_NOTE
    assert CODER_VISION_NOTE in sys_raw
