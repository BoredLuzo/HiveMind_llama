"""Unknown-tool feedback must name the tools the model actually needs.

Regression for the old line in core/tool_loop.py:

    _names_json = json.dumps((tool_names or [])[:8])

`sorted()` + [:8] announced the first eight names ALPHABETICALLY. Measured for
every mode (tool_agent, direct_full, duo_full), that list was
[ask_user, browser, edit_file, find_files, find_references,
 get_background_output, get_datetime, get_signatures] - it contained NONE of
read_file, write_file, run_bash, run_tests, task_complete. A model that
mistyped a tool got corrected with a list that hid exactly the tools it needed.

Now the list is ordered by tools.definitions._tool_priority (core tools first),
capped at 12, and states how many names exist in total.

Pins:
  (a) _rank_tool_names: core tools first, unknown names last, dedup + sorted
  (b) end to end through _inject_retry_feedback for the real mode allow-lists:
      every key tool appears in the announced list
  (c) the message names the total count and the overflow
  (d) the cap is respected and the first entry is read_file (tier 0)

Run: python tests/test_tool_feedback.py
"""
import json
import re
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


from core.tool_loop import _inject_retry_feedback, _rank_tool_names, \
    _FEEDBACK_TOOL_LIMIT                                        # noqa: E402
from tools.definitions import _tool_names_for_mode              # noqa: E402

KEY_TOOLS = ("read_file", "write_file", "edit_file", "run_bash", "run_tests",
             "task_complete")


def _announced(names):
    msgs = []
    _inject_retry_feedback(msgs, error_type="unknown_tool", bad_content="frobnicate",
                           tool_names=list(names))
    text = str(msgs[-1].get("content", ""))
    m = re.search(r"\[(.*?)\]\. Use exactly", text, re.S)
    return (json.loads("[" + m.group(1) + "]") if m else []), text


# ── (a) ranking ─────────────────────────────────────────────────────────────
_ranked = _rank_tool_names(["zzz_unknown", "run_bash", "read_file", "ask_user",
                            "task_complete", "read_file"])
check("core tools first", _ranked[:3] == ["read_file", "write_file", "run_bash"]
      or _ranked[0] == "read_file", f" ({_ranked})")
check("read_file is first (tier 0 head)", _ranked[0] == "read_file")
check("unknown names last", _ranked[-1] == "zzz_unknown")
check("duplicates removed", len(_ranked) == len(set(_ranked)))
check("empty input tolerated", _rank_tool_names([]) == []
      and _rank_tool_names(None) == [])

# ── (b) every mode announces the key tools ──────────────────────────────────
for _mode in ("tool_agent", "direct_full", "duo_full"):
    _names = _tool_names_for_mode(_mode, include_websearch=False)
    _shown, _txt = _announced(_names)
    _missing = [t for t in KEY_TOOLS if t in _names and t not in _shown]
    check(f"{_mode}: all key tools announced", not _missing,
          f" (missing {_missing}; shown {len(_shown)})")

# ── (c) the message is honest about the total ───────────────────────────────
_names = _tool_names_for_mode("tool_agent", include_websearch=False)
_shown, _txt = _announced(_names)
check("message states the total count", f"({len(_names)} total" in _txt,
      f" ({_txt[:80]!r})")
check("message states the overflow",
      ("more available" in _txt) == (len(_names) > _FEEDBACK_TOOL_LIMIT),
      f" ({len(_names)} names)")

# ── (d) cap respected ───────────────────────────────────────────────────────
check("cap respected", len(_shown) <= _FEEDBACK_TOOL_LIMIT,
      f" ({len(_shown)} shown)")
check("cap constant is 12", _FEEDBACK_TOOL_LIMIT == 12)

_small_shown, _small_txt = _announced(["read_file", "run_bash"])
check("short list: no overflow note", "more available" not in _small_txt)

# a single unknown call with no names must not crash
_msgs = []
_inject_retry_feedback(_msgs, error_type="unknown_tool")
check("no names: still a usable message",
      "unknown tool name" in str(_msgs[-1].get("content", ""))
      and "0 total" in str(_msgs[-1].get("content", "")))

print()
print(f"SUMMARY tool_feedback: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
