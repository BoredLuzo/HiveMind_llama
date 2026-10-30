# -*- coding: utf-8 -*-
"""Test: the TOOL-CALL-MISS warning in the coder tool loop (2026-10-30).

Live Groot run: 7/7 coder rounds emitted tool calls as plain text and nothing
parsed. Nine offline probes against the same slot (same tools, system prompt,
sampler, streaming and non-streaming) parsed fine, so the request state of the
failing round is the only remaining evidence. This suite pins that the miss
path logs exactly that state — once per round, at WARNING, without breaking
the round when formatting fails.

Source pin, not a loop simulation: the branch lives in the 5.5k-line
run_code_duo and constructing its locals would test the fixture, not the code.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "core" / "duo_runner.py"

# Fields the incident needs to be diagnosable from the log alone.
_REQUIRED = (
    "thinking=", "ctk=", "tools=%d", "schema_chars=", "hash=",
    "tool_choice=", "slot_ctx=", "req_ctx=", "prompt_tokens=", "cached=",
    "finish=", "completion=", "max_tokens=",
    "temp=", "presence=", "top_p=", "top_k=", "min_p=", "seed=",
    "content[0:500]=", "reasoning[0:500]=",
)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _miss_segment(src: str) -> str:
    """The text between `if not _dr_tcs:` and its `if _drop_notices:`."""
    _start = src.find("if not _dr_tcs:")
    if _start < 0:
        return ""
    _end = src.find("if _drop_notices:", _start)
    return src[_start:_end if _end > 0 else _start + 6000]


def test_tool_call_miss_log():
    src = _SRC.read_text(encoding="utf-8")
    seg = _miss_segment(src)

    check("1 miss branch located", bool(seg), f"({len(seg)} chars)")
    check("2 warning emitted in the no-tool-calls branch",
          "[TOOL-CALL-MISS]" in seg)
    check("3 emitted at WARNING (visible without debug logging)",
          "logger.warning(" in seg)
    check("4 only for real tool rounds", "if _is_tool_round:" in seg)
    _missing = [f for f in _REQUIRED if f not in seg]
    check("5 every incident field is logged", not _missing, f"missing={_missing}")
    check("6 content and reasoning are sliced to 500 chars",
          "_miss_content[:500]" in seg and "_miss_reason[:500]" in seg)
    check("7 tools are identified by a stable hash",
          "sha256" in seg and "sort_keys=True" in seg)
    check("8 formatting failure cannot break the round",
          "except (TypeError, ValueError," in seg
          and "AttributeError, KeyError) as _miss_err" in seg
          and 'logger.debug(\n                                        "[TOOL-CALL-MISS] log failed' in seg)


if __name__ == "__main__":
    _err = None
    try:
        test_tool_call_miss_log()
    except AssertionError as _ae:
        _err = _ae
    print()
    print(f"{'=' * 50}")
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print(f"{'=' * 50}")
    if _err is not None:
        print(f"  ASSERT: {_err}")
    sys.exit(0 if failed == 0 and _err is None else 1)
