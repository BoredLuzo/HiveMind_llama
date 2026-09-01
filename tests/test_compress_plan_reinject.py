"""Behavioral Test: PLAN-REINJECT-FIX (2026-08-31) - Plan ueberlebt Compression.

Verifiziert:
  A) compression._compress_tool_context haengt den Plan-Anker an die RESULTIERENDE
     Summary-Message an (nicht nur in den Compression-Prompt) - auch im
     LLM-Fallback-Fall (Exception -> Fallback-Summary).
  B) duo_runner._strip_stale_ctx_notices entfernt veraltete CTX-Warn-Messages
     ([RUNTIME NOTICE]/[CTX CRITICAL]/[CTX: ~) nach einer Compression, haelt aber
     Tool-/Coder-Nachrichten.
  C) Plan-Pin-Bau (duo_runner-Logik inline): done/current/pending Checkliste mit
     korrekten Markern fuer den aktuellen Subtask.

Run: python tests/test_compress_plan_reinject.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from context.compression import (  # noqa: E402
    _compress_tool_context,
    _validate_compression_summary,
)
from core.duo_runner import (  # noqa: E402
    _build_plan_anchor_text,
    _strip_stale_ctx_notices,
)

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


class _MockResp:
    def __init__(self, content):
        self._c = content

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


class _MockClient:
    def __init__(self, summary):
        self.summary = summary
        self.captured = None

    async def post(self, url, json=None, timeout=None):
        self.captured = json
        return _MockResp(self.summary)


class _FailClient:
    async def post(self, url, json=None, timeout=None):
        raise TimeoutError("ReadTimeout")


def _msgs():
    out = [{"role": "system", "content": "sys"}]
    for i in range(4):
        out.append({"role": "assistant", "content": f"edited file {i}"})
        out.append({"role": "tool", "content": f"tool result {i}"})
    return out


_ANCHOR = "Implement add in a.py, Implement subtract in b.py, Wire router"


def test_reinject_in_summary():
    client = _MockClient("summary with add and subtract and router done")
    _ = asyncio.run(_compress_tool_context(
        messages=_msgs(),
        model="test-model", port=1, client=client,
        system_prompt="sys", original_task="Task X",
        written_files=["a.py"], done_tasks=["Implement add"],
        keep_recent_msgs=0, plan_state="Plan: step 2/3",
        plan_anchor_text=_ANCHOR,
    ))
    result = asyncio.run(_compress_tool_context(
        messages=_msgs(),
        model="test-model", port=1, client=_MockClient("summary add subtract router done"),
        system_prompt="sys", original_task="Task X",
        written_files=["a.py"], done_tasks=["Implement add"],
        keep_recent_msgs=0, plan_state="Plan: step 2/3",
        plan_anchor_text=_ANCHOR,
    ))
    _compressed, _, _ = result
    _summary_content = _compressed[1]["content"] if len(_compressed) >= 2 else ""
    if "[PLAN - must continue" in _summary_content and _ANCHOR in _summary_content:
        ok("A1: Plan-Anker in der RESULTIERENDEN Summary-Message enthalten")
    else:
        fail("A1: anchor missing in summary message", _summary_content[-200:])
    if "| PLAN:" in _summary_content:
        fail("A1b: anchor injected twice/as prompt remnant")
    else:
        ok("A1b: anchor only as a clear plan block (not a prompt duplicate)")


def test_reinject_in_fallback():
    # LLM fallback: _FailClient throws -> fallback summary. The plan anchor MUST
    # still be present in the summary message (live-finding fix).
    result = asyncio.run(_compress_tool_context(
        messages=_msgs(),
        model="test-model", port=1, client=_FailClient(),
        system_prompt="sys", original_task="Task X",
        written_files=["a.py"], done_tasks=["Implement add"],
        keep_recent_msgs=0, plan_state="Plan: step 2/3",
        plan_anchor_text=_ANCHOR,
    ))
    _compressed, _, _ = result
    _summary_content = _compressed[1]["content"] if len(_compressed) >= 2 else ""
    if _ANCHOR in _summary_content:
        ok("A2: plan anchor survives the LLM fallback (fallback summary)")
    else:
        fail("A2: anchor lost in the fallback", _summary_content[-200:])


def test_validation_accepts_reinjected():
    # validation: a summary that (partially) contains the anchor is accepted.
    summary = ("State Reconstruction a.py: add and subtract implemented, router still "
               "wired, plan add subtract router continues in full detail")
    if _validate_compression_summary(summary, ["a.py"], [], plan_anchor=_ANCHOR):
        ok("A3: reinject summary passes the plan-anchor validation")
    else:
        fail("A3: reinject summary fails validation")


def test_strip_stale_ctx_notices():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[RUNTIME NOTICE] [CTX CRITICAL: ~85% full] Stop reading new files."},
        {"role": "user", "content": "[RUNTIME NOTICE] [CTX: ~60% full] Avoid large reads."},
        {"role": "user", "content": "[CTX: ~49k tokens] something"},
        {"role": "user", "content": "[GOAL - this is your fixed objective]"},
        {"role": "tool", "content": "[RUNTIME NOTICE] fake tool content stays"},
        {"role": "assistant", "content": "good"},
    ]
    out = _strip_stale_ctx_notices(msgs)
    _count = sum(1 for m in out if m.get("role") == "user")
    if _count == 1:
        ok("B1: exactly 1 user message remains (only GOAL pin)")
    else:
        fail("B1: expected 1 user message, got", str(_count))
    if any("CTX CRITICAL" in m.get("content", "") for m in out):
        fail("B2: CTX CRITICAL message not removed")
    else:
        ok("B2: [CTX CRITICAL] removed")
    if any("CTX: ~" in m.get("content", "") for m in out):
        fail("B3: [CTX: ~] message not removed")
    else:
        ok("B3: [CTX: ~] message removed")
    _kept_tool = any(m.get("role") == "tool" for m in out)
    if _kept_tool:
        ok("B4: tool message with RUNTIME NOTICE text stays")
    else:
        fail("B4: tool message dropped")
    if _strip_stale_ctx_notices([]) == []:
        ok("B5: empty list -> empty list")
    else:
        fail("B5: empty list")


def test_strip_stale_no_side_effects():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "normal user question"},
        {"role": "assistant", "content": "normal reply"},
    ]
    out = _strip_stale_ctx_notices(msgs)
    if len(out) == len(msgs):
        ok("B6: no CTX markers -> no change")
    else:
        fail("B6: changed without markers", str(len(out)))


def test_plan_pin_build_logic():
    # replicates the plan-pin building from duo_runner (done/current/pending).
    subtasks = ["Add a", "Add b", "Wire router"]
    di = 1  # 0-based -> current subtask 2
    lines = []
    for _pi, _pt in enumerate(subtasks):
        if _pi < di:
            lines.append(f"  {_pi+1}. \u2713 {str(_pt)[:120]}")
        elif _pi == di:
            lines.append(f"  {_pi+1}. \u2192 {str(_pt)[:120]}  \u25c0 YOU ARE HERE")
        else:
            lines.append(f"  {_pi+1}. \u25cb {str(_pt)[:120]}")
    pin = f"[PLAN-PIN - current subtask {di+1}/{len(subtasks)}]\n" + "\n".join(lines)
    if "\u2713 Add a" in pin and "\u2192 Add b" in pin and "\u25cb Wire router" in pin:
        ok("C1: plan pin contains the done/current/pending markers correctly")
    else:
        fail("C1: pin markers wrong", pin)
    if "current subtask 2/3" in pin:
        ok("C2: pin header names the current subtask")
    else:
        fail("C2: pin header wrong", pin)


def test_source_structure():
    src_c = Path(__file__).parent.parent / "context" / "compression.py"
    text = src_c.read_text(encoding="utf-8")
    if "[PLAN - must continue" in text and "plan_anchor_text" in text:
        ok("D1: compression.py contains the plan-reinject block")
    else:
        fail("D1: plan-reinject block missing in compression.py")
    src_d = Path(__file__).parent.parent / "core" / "duo_runner.py"
    text_d = src_d.read_text(encoding="utf-8")
    if "_strip_stale_ctx_notices(_dtool_msgs)" in text_d:
        ok("D2: duo_runner calls _strip_stale_ctx_notices after compression")
    else:
        fail("D2: _strip_stale_ctx_notices not wired")
    if "_call_sigs.clear()" in text_d:
        ok("D3: call_sigs cleared after compression")
    else:
        fail("D3: call_sigs reset missing")
    if "[PLAN-PIN - current subtask" in text_d:
        ok("D4: plan pin appended after compression")
    else:
        fail("D4: plan pin missing")
    # FIX (2026-08-31): JS was moved out of index.html into static/app.js.
    # checklist functions are now checked in static/app.js.
    _root = Path(__file__).parent.parent
    _candidates = [_root / "index.html", _root / "static" / "app.js"]
    text_i = "\n".join(p.read_text(encoding="utf-8") for p in _candidates if p.exists())
    if "_renderPlanChecklist" in text_i and "_updatePlanChecklist" in text_i:
        ok("D5: index.html/app.js has the checklist functions")
    else:
        fail("D5: checklist functions missing in index.html/app.js")


def main():
    print("\n=== Compress-Plan-Reinject (2026-08-31) ===\n")
    print("-- A: plan reinject in the summary message --")
    test_reinject_in_summary()
    print()
    test_reinject_in_fallback()
    print()
    test_validation_accepts_reinjected()
    print("\n-- B: Stale-CTX-notice cleanup --")
    test_strip_stale_ctx_notices()
    print()
    test_strip_stale_no_side_effects()
    print("\n-- C: Plan-pin building --")
    test_plan_pin_build_logic()
    print("\n-- D: Source structure --")
    test_source_structure()
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
