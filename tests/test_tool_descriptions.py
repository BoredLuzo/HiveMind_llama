"""Tool descriptions must not be self-referential, stale or point at tools the
model cannot call.

Owner question 2026-10-17: "the tool descriptions aren't paradoxical? Not too
long and too complicated?"

Findings and fixes:
  1. read_file carried an internal note ("Run-specific budget numbers are
     inserted when the run resolves its budgets.") that reached the model on
     every path that does not apply budget texts - critic_verify, openai_agent,
     the helper agents. Internal plumbing does not belong in a prompt.
  2. run_bash routed to git_status / install_package / find_references /
     start_background unconditionally. With the git gate (git_tools_enabled)
     and the schema fit (tier-2 tools are dropped when the window is small)
     some of those are simply not in the list - the sentence now says so.
  3. subagent_lite / fix_agent shipped the placeholder schema text; they now
     apply the same budget texts the handlers enforce.

Also pinned: no description is absurdly long (the longest single description is
run_bash with ~730 chars, every tool has a <=80 char minimal level).

Run: python tests/test_tool_descriptions.py
"""
import json
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


import tools.definitions as D                                   # noqa: E402
import core.budgets as B                                        # noqa: E402

# ── 1. no internal plumbing in model-visible text ───────────────────────────
_raw = json.dumps(D._INLINE_CODING_TOOLS)
check("no 'budget numbers are inserted' plumbing note in the raw schema",
      "are inserted when the run resolves" not in _raw)
B.set_active(B.resolve(16384, 3.92, 2000, {}))
_fitted = D._get_inline_tools(include_websearch=False, mode="duo_full",
                              budget_chars=B.schema_budget_for(16384, "m"))
_fitted_txt = json.dumps(_fitted)
check("fitted schema has no plumbing note either",
      "are inserted when the run resolves" not in _fitted_txt)
check("fitted read_file states the real cap",
      str(B.active().read_char_cap) in _fitted_txt,
      f" (cap {B.active().read_char_cap})")

# ── 2. tool pointers are honest ─────────────────────────────────────────────
def _run_bash_desc(tools):
    for t in tools:
        if t["function"]["name"] == "run_bash":
            return t["function"]["description"]
    return ""


_desc_full = _run_bash_desc(D._get_inline_tools(include_websearch=False,
                                                mode="duo_full"))
check("run_bash marks its tool pointers as conditional",
      "only if it is in your" in _desc_full)
check("run_bash still routes the main cases",
      all(x in _desc_full for x in ("search_code", "find_files", "git_status",
                                    "install_package")))

# a small window really does drop pointed-at tools -> the wording is warranted
_small = D._get_inline_tools(include_websearch=False, mode="duo_full",
                             budget_chars=B.schema_budget_for(8192, "m"))
_small_names = {t["function"]["name"] for t in _small}
check("8k really drops some pointed-at tools",
      not {"git_status", "install_package"} <= _small_names,
      f" (dropped: {sorted({'git_status','install_package','run_python'} - _small_names)})")

# ── 3. helper schemas carry the real caps ───────────────────────────────────
from core.subagent_lite import _tool_schemas as _sub              # noqa: E402
from core.fix_agent import _tool_schemas as _fix                 # noqa: E402

_sub_txt = json.dumps(_sub())
_fix_txt = json.dumps(_fix())
check("subagent_lite schema has no plumbing note",
      "are inserted when the run resolves" not in _sub_txt)
check("fix_agent schema has no plumbing note",
      "are inserted when the run resolves" not in _fix_txt)
check("subagent_lite schema states the real read cap",
      str(B.active().read_char_cap) in _sub_txt)

# ── 4. length discipline ────────────────────────────────────────────────────
_desc_lens = [(len(t["function"].get("description", "")), t["function"]["name"])
              for t in D._INLINE_CODING_TOOLS]
_worst = max(_desc_lens)
check("longest single description stays under 800 chars", _worst[0] < 800,
      f" ({_worst[1]}={_worst[0]})")
_min_lens = [len(D._desc_at_level(t["function"]["description"], 0))
             for t in D._INLINE_CODING_TOOLS]
# 120 chars: the longest minimal level is run_tests (105, it lists the detected
# frameworks). Anything above that would mean the minimal level is not minimal.
check("minimal level stays short for every tool",
      max(_min_lens) <= 120,
      f" (max {max(_min_lens)}: {max(((len(D._desc_at_level(t['function']['description'], 0)), t['function']['name']) for t in D._INLINE_CODING_TOOLS))[1]})")
check("minimal level is a real sentence, not a stub",
      min(_min_lens) >= 20, f" (min {min(_min_lens)})")

# ── 5. no contradictory instructions between the two "must read" rules ──────
_rf = next(t["function"]["description"] for t in D._INLINE_CODING_TOOLS
           if t["function"]["name"] == "read_file")
_wf = next(t["function"]["description"] for t in D._INLINE_CODING_TOOLS
           if t["function"]["name"] == "write_file")
check("read_file states the read-before-edit rule",
      "ALWAYS call read_file before edit_file" in _rf)
# the old wording ("Create a NEW file ... Existing files: use edit_file instead")
# contradicted the handler, which overwrites with a shrink guard. The contract is
# now: create OR overwrite completely, prefer edit_file for a small change.
check("write_file explains create-or-overwrite",
      "overwrite it completely" in _wf and "prefer edit_file" in _wf.lower())
check("write_file documents the shrink escape hatch",
      "confirm_shrink" in _wf)
check("write_file explains the append fallback for truncated writes",
      "write_file_append" in _wf)

B.set_active(None)
print()
print(f"SUMMARY tool_descriptions: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
