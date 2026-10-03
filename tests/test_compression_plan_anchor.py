"""Behavioral test: PLAN-ANCHOR-FIX (2.2) - plan survival guarantee in compression.

Verifies:
  A) Prompt building (real _compress_tool_context with a mock client):
     - chunking case: compression prompt contains the subtask comma list
     - non-chunking case: contains the full step descriptions from the plan
     - max_tokens raised to 800
  B) Validation (real _validate_compression_summary):
     - accepts a summary that contains the plan anchor (at least partially)
     - rejects a summary without the plan anchor
     - loose 25% threshold: partial adoption suffices (not 50% like partitions)
     - partition rule unchanged (regression)
  C) Structure: the plan anchor is SEPARATE from the 12-entry partition list

Run: python tests/test_compression_plan_anchor.py
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


def _msgs():
    out = [{"role": "system", "content": "sys"}]
    for i in range(4):
        out.append({"role": "assistant", "content": f"edited file {i}"})
        out.append({"role": "tool", "content": f"tool result {i}"})
    return out


def _run_compress(plan_anchor_text, summary_text):
    client = _MockClient(summary_text)
    result = asyncio.run(_compress_tool_context(
        messages=_msgs(),
        model="test-model",
        port=1,
        client=client,
        system_prompt="sys",
        original_task="Task X",
        written_files=["a.py", "b.py"],
        done_tasks=["a.py"],
        keep_recent_msgs=0,
        plan_state="Plan: step 1/3 - first",
        plan_anchor_text=plan_anchor_text,
    ))
    return client, result


def test_prompt_chunking_case():
    client, _ = _run_compress(
        "Implement add in a.py, Implement subtract in b.py",
        "Summary content here.",
    )
    prompt = client.captured["messages"][0]["content"]
    if "| PLAN: Implement add in a.py, Implement subtract in b.py |" in prompt:
        ok("A1: chunking case - subtask comma list in the compression prompt")
    else:
        fail("A1: comma list missing in the prompt", prompt[-200:])
    if "NON-COMPRESSIBLE PLAN ANCHOR" in prompt:
        ok("A2: dedicated PLAN-ANCHOR section present")
    else:
        fail("A2: PLAN-ANCHOR section missing")
    if client.captured.get("max_tokens") == 800:
        ok("A3: max_tokens raised to 800")
    else:
        fail("A3: max_tokens", client.captured.get("max_tokens"))


def test_prompt_non_chunking_case():
    anchor = "step 1: Implement add -> a.py; step 2: Implement subtract -> b.py"
    client, _ = _run_compress(anchor, "Summary content here.")
    prompt = client.captured["messages"][0]["content"]
    if "step 1: Implement add -> a.py; step 2: Implement subtract -> b.py" in prompt:
        ok("A4: non-chunking case - full step descriptions in the prompt")
    else:
        fail("A4: step list missing in the prompt", prompt[-200:])


def test_prompt_empty_anchor():
    client, _ = _run_compress("", "Summary content here.")
    prompt = client.captured["messages"][0]["content"]
    if "NON-COMPRESSIBLE PLAN ANCHOR" not in prompt:
        ok("A5: no anchor -> no PLAN-ANCHOR section")
    else:
        fail("A5: empty anchor still creates a section")


_CHUNK_ANCHOR = "Implement add in a.py, Implement subtract in b.py"
_FULL_ANCHOR = "step 1: Implement add -> a.py; step 2: Implement subtract -> b.py"


def test_validate_chunking_case():
    _long_ok = ("summary: implement add and subtract done, with the remaining "
                "plan details and context preserved across the compression step")
    _long_no = ("summary: nothing plan related here, only tool results and "
                "generic progress notes across the whole compression output")
    if _validate_compression_summary(_long_ok, [], [], plan_anchor=_CHUNK_ANCHOR):
        ok("B1: chunking - summary with (partially) contained comma list accepted")
    else:
        fail("B1: partial adoption rejected")
    if not _validate_compression_summary(_long_no, [], [], plan_anchor=_CHUNK_ANCHOR):
        ok("B2: chunking - summary without plan anchor rejected")
    else:
        fail("B2: summary without anchor accepted")


def test_validate_non_chunking_case():
    _long_ok = ("summary: step 1 implement add, then step 2 continues with the "
                "remaining plan steps and their target files in full detail")
    _long_no = ("summary: everything finished successfully, no further steps "
                "required and all tasks were completed in this session")
    if _validate_compression_summary(_long_ok, [], [],
                                     plan_anchor=_FULL_ANCHOR):
        ok("B3: non-chunking - partial adoption (2 of 3 words) accepted (25% threshold)")
    else:
        fail("B3: 25% threshold too strict")
    if not _validate_compression_summary(_long_no, [], [], plan_anchor=_FULL_ANCHOR):
        ok("B4: non-chunking - summary without plan steps rejected")
    else:
        fail("B4: accepted without steps")


def test_validate_empty_anchor_and_partition_regression():
    _long = ("summary without plan words but long enough content to pass the "
             "minimum length check of the compression validation function")
    if _validate_compression_summary(_long, [], []):
        ok("B5: empty plan_anchor -> rule skipped (no effect)")
    else:
        fail("B5: empty anchor blocks")
    # partition rule unchanged: 1 of 4 labels < 50% -> False
    summary = "summary with one partition: p1 and other content here"
    if not _validate_compression_summary(
            summary, [], [], known_partitions=["p1", "p2", "p3", "p4"]):
        ok("B6: partition rule unchanged (1/4 < 50% -> rejected)")
    else:
        fail("B6: partition rule changed")


def test_real_duo_runner_anchor_path():
    """Point 3: REAL duo_runner anchor-building path - real PlanTracker/PlanStep
    objects go through the non-chunking branch (subtasks empty, tracker present)."""
    from core.duo_runner import _build_plan_anchor_text
    from core.plan_tracker import PlanTracker

    tracker = PlanTracker([
        {"step": 1, "file": "a.py", "action": "Implement add"},
        {"step": 2, "file": "b.py", "action": "Implement subtract"},
    ])
    anchor = _build_plan_anchor_text([], tracker)
    if all(s in anchor for s in ("Implement add", "a.py", "Implement subtract", "b.py")):
        ok("D1: non-chunking - real PlanTracker -> anchor with intent+path content")
    else:
        fail("D1: anchor empty/generic", repr(anchor))
    if "step 1:" in anchor and "step 2:" in anchor:
        ok("D2: both step ids present")
    else:
        fail("D2: step ids missing", repr(anchor))
    anchor_c = _build_plan_anchor_text(["Implement add", "Implement subtract"], None)
    if anchor_c == "Implement add, Implement subtract":
        ok("D3: chunking branch -> subtask comma list")
    else:
        fail("D3: chunking list wrong", repr(anchor_c))
    if _build_plan_anchor_text([], None) == "":
        ok("D4: no PlanTracker -> empty anchor (no crash)")
    else:
        fail("D4: None case wrong")
    if _build_plan_anchor_text([], PlanTracker()) == "":
        ok("D5: PlanTracker without steps -> empty anchor")
    else:
        fail("D5: empty tracker wrong")


def test_source_structure():
    src_c = Path(__file__).parent.parent / "context" / "compression.py"
    text = src_c.read_text(encoding="utf-8")
    if "NON-COMPRESSIBLE PLAN ANCHOR" in text:
        ok("C1: PLAN-ANCHOR section present in the compression prompt")
    else:
        fail("C1: PLAN-ANCHOR missing")
    if "for _p in list(dict.fromkeys(_partitions))[:12]:" in text:
        ok("C2: 12-entry partition loop unchanged (untouched)")
    else:
        fail("C2: partition loop changed")
    seg_plan = text[text.find("NON-COMPRESSIBLE PLAN ANCHOR") - 200:]
    if "for _p in list(dict.fromkeys(_partitions))" not in seg_plan:
        ok("C3: plan-anchor block is NOT inside the partition loop")
    else:
        fail("C3: plan anchor mixed into the partition loop")
    if re.search(r"plan_anchor_text: str = \"\"", text):
        ok("C4: new plan_anchor_text parameter in _compress_tool_context")
    else:
        fail("C4: parameter missing")
    if "* 0.25" in text and "plan_anchor" in text:
        ok("C5: dedicated 25% threshold for the plan anchor in validation")
    else:
        fail("C5: 25% threshold missing")
    src_d = Path(__file__).parent.parent / "core" / "duo_runner.py"
    text_d = src_d.read_text(encoding="utf-8")
    if "plan_anchor_text=_plan_anchor_text" in text_d and "plan_anchor=_plan_anchor_text" in text_d:
        ok("C6: duo_runner passes the anchor to compression AND validation")
    else:
        fail("C6: duo_runner wiring missing")
    if "def _build_plan_anchor_text(subtasks: list, plan_tracker) -> str:" in text_d:
        ok("C7: anchor building as an importable pure module function in duo_runner")
    else:
        fail("C7: module function missing")
    if "_plan_anchor_text = _build_plan_anchor_text(_subtasks, _plan_tracker)" in text_d:
        ok("C8: compression call site uses the module function (no inline duplicate)")
    else:
        fail("C8: inline duplicate still present")


def main():
    print("\n=== Plan-Anchor (2.2 Compression) ===\n")
    print("-- A: Prompt-Bau (echte Funktion, Mock-Client) --")
    test_prompt_chunking_case()
    print()
    test_prompt_non_chunking_case()
    print()
    test_prompt_empty_anchor()
    print("\n-- B: Validierung --")
    test_validate_chunking_case()
    print()
    test_validate_non_chunking_case()
    print()
    test_validate_empty_anchor_and_partition_regression()
    print("\n-- C: Quelltext-Struktur --")
    test_source_structure()
    print("\n-- D: Real duo_runner anchor-building path --")
    test_real_duo_runner_anchor_path()
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
