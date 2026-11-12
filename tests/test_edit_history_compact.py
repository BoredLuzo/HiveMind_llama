# -*- coding: utf-8 -*-
"""Stale edit-payload compaction behavior tests (G#3 lever, 2026-11-11).

_compact_stale_edit_payloads() compacts OLD write/edit tool-call payloads in
the conversation history to `[applied diff: path (+X/-Y lines)]` stubs once
enough stale mass has accumulated. This suite pins:

  - the exact stub formats (edit_file, patch_file, write_file, apply_patch)
  - the safe trigger: below the mass threshold nothing happens
  - the keep window: the most recent assistant rounds are never touched
  - small payloads stay; stubbed payloads are skipped (idempotent)
  - the per-pass call cap
  - the stub-echo guard extension (echoing the marker is blocked)

Run: python tests/test_edit_history_compact.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.tool_executor import (          # noqa: E402
    _compact_stale_edit_payloads as _compact,
    _edit_stub_text as _stub_text,
    _APPLIED_DIFF_MARK,
    _STALE_COMPACT_MIN_CHARS,
    _STALE_COMPACT_MAX_CALLS,
)

from tests._harness import Harness        # noqa: E402

h = Harness(pad=True)
check = h.check


def _pad(n: int) -> str:
    return "x" * n


def _edit_args(path, old, new):
    return json.dumps({"path": path, "old_text": old, "new_text": new},
                      ensure_ascii=False)


def _assistant(tool_calls):
    return {"role": "assistant", "content": None, "tool_calls": tool_calls}


def _tc(cid, name, args_str):
    return {"id": cid, "function": {"name": name, "arguments": args_str}}


def _args_len(tc) -> int:
    raw = tc["function"]["arguments"]
    return len(raw) if isinstance(raw, str) else len(json.dumps(raw))


# ── stub format unit checks ────────────────────────────────────────────────
check("edit_file stub format (+added/-removed lines)",
      _stub_text("edit_file", {"path": "app.py",
                               "old_text": "a\nb",
                               "new_text": "a\nb\nc"})
      == "[applied diff: app.py (+3/-2 lines)]",
      f" ({_stub_text('edit_file', {'path': 'app.py', 'old_text': 'a\\nb', 'new_text': 'a\\nb\\nc'})})")
check("edit_file batch stub sums all entries",
      _stub_text("edit_file", {
          "path": "app.py",
          "edits": [{"old_text": "a\nb", "new_text": "z"},
                    {"old_text": "q", "new_text": "q\nr"}]})
      == "[applied diff: app.py (+3/-3 lines)]")
check("patch_file stub uses old_str/new_str",
      _stub_text("patch_file", {"path": "p.py", "old_str": "one\ntwo",
                                "new_str": "one\ntwo\nthree"})
      == "[applied diff: p.py (+3/-2 lines)]")
check("write_file stub reports only the added side",
      _stub_text("write_file", {"path": "n.py", "content": "l1\nl2\nl3"})
      == "[applied write: n.py (+3 lines)]")
check("apply_patch stub counts hunk +/- lines",
      _stub_text("apply_patch", {"patch":
          "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n-a\n+b\n c\n"})
      == "[applied diff: 1 file section(s) (+1/-1 lines)]")
check("unrecognized args shape -> None (left untouched)",
      _stub_text("edit_file", {"path": "x.py", "bogus": 1}) is None)


# ── trigger: below the mass threshold nothing happens ─────────────────────
big = _pad(_STALE_COMPACT_MIN_CHARS + 50)  # above min, far below trigger
msgs = [
    {"role": "user", "content": "task"},
    _assistant([_tc("c1", "edit_file", _edit_args("a.py", big, "NEW"))]),
    {"role": "tool", "content": "[edit_file: ok]", "tool_call_id": "c1"},
    _assistant([_tc("c2", "edit_file", _edit_args("b.py", big, "NEW"))]),
    {"role": "tool", "content": "[edit_file: ok]", "tool_call_id": "c2"},
    _assistant([_tc("c3", "edit_file", _edit_args("c.py", big, "NEW"))]),
    {"role": "tool", "content": "[edit_file: ok]", "tool_call_id": "c3"},
    _assistant([_tc("c4", "edit_file", _edit_args("d.py", big, "NEW"))]),
    {"role": "tool", "content": "[edit_file: ok]", "tool_call_id": "c4"},
]
saved = _compact(msgs, 7)  # current assistant = index 7
check("below the trigger mass nothing is compacted", saved == 0)
check("payloads are untouched below the threshold",
      _args_len(msgs[1]["tool_calls"][0]) > _STALE_COMPACT_MIN_CHARS)
check("keep window: the two most recent assistant rounds survive",
      _args_len(msgs[3]["tool_calls"][0]) > _STALE_COMPACT_MIN_CHARS
      and _args_len(msgs[5]["tool_calls"][0]) > _STALE_COMPACT_MIN_CHARS)


# ── trigger fires: stubs applied with the exact format ────────────────────
def _big_edit_args(path):
    return _edit_args(path, _pad(9000), "NEW")


def _history(n_edits: int):
    msgs = [{"role": "user", "content": "task"}]
    for i in range(n_edits):
        msgs.append(_assistant([_tc(f"c{i}", "edit_file",
                                    _big_edit_args(f"f{i}.py"))]))
        msgs.append({"role": "tool", "content": "[edit_file: ok]",
                     "tool_call_id": f"c{i}"})
    cur = len(msgs)
    msgs.append(_assistant([_tc(f"c{n_edits}", "edit_file",
                                _edit_args("live.py", "sm", "BIG"))]))
    return msgs, cur


msgs, cur = _history(4)  # 4 stale x ~9000 old chars = 36000 >= trigger
saved = _compact(msgs, cur)
check("trigger reached: stale payloads compacted", saved > 0)
first_stub = msgs[1]["tool_calls"][0]["function"]["arguments"]
check("stub is valid JSON with path + content keys",
      json.loads(first_stub) == {"path": "f0.py",
                                 "content": "[applied diff: f0.py (+1/-1 lines)]"},
      f" ({first_stub[:120]})")
check("stub carries the applied-diff marker", _APPLIED_DIFF_MARK in first_stub)
check("keep window: current round + previous assistant untouched",
      _args_len(msgs[cur]["tool_calls"][0]) < 200  # current round compacts separately
      and _args_len(msgs[cur - 2]["tool_calls"][0]) > _STALE_COMPACT_MIN_CHARS)
check("old tool RESULT messages untouched",
      msgs[2]["content"] == "[edit_file: ok]")

# idempotent: second pass finds nothing (stubs carry the marker)
saved2 = _compact(msgs, cur)
check("idempotent: second pass saves nothing", saved2 == 0)

# ── keep window respected with many rounds ────────────────────────────────
msgs, cur = _history(6)
saved = _compact(msgs, cur)
# assistants at indices 1,3,5,7,9,11; keep window protects 9,11 (the two
# most recent before `cur`); compacted: 1,3,5,7 -> 4 calls
check("exactly the rounds below the keep window were compacted",
      saved > 0
      and _args_len(msgs[1]["tool_calls"][0]) < 200
      and _args_len(msgs[7]["tool_calls"][0]) < 200
      and _args_len(msgs[9]["tool_calls"][0]) > _STALE_COMPACT_MIN_CHARS
      and _args_len(msgs[11]["tool_calls"][0]) > _STALE_COMPACT_MIN_CHARS,
      f" (saved={saved})")

# ── small payloads never compacted even when the trigger is reachable ─────
small = _edit_args("s.py", "old line", "new line")
msgs = [{"role": "user", "content": "t"},
        _assistant([_tc("a", "edit_file", small)]),
        {"role": "tool", "content": "ok", "tool_call_id": "a"},
        _assistant([_tc("b", "edit_file", small)]),
        {"role": "tool", "content": "ok", "tool_call_id": "b"},
        _assistant([_tc("c", "edit_file", small)]),
        {"role": "tool", "content": "ok", "tool_call_id": "c"},
        _assistant([_tc("d", "edit_file", small)]),
        {"role": "tool", "content": "ok", "tool_call_id": "d"},
        _assistant([_tc("e", "edit_file", small)]),
        {"role": "tool", "content": "ok", "tool_call_id": "e"}]
check("small payloads stay below the per-call minimum",
      _compact(msgs, 9) == 0)

# ── per-pass cap ──────────────────────────────────────────────────────────
msgs, cur = _history(_STALE_COMPACT_MAX_CALLS + 4)
saved = _compact(msgs, cur)
_stubbed = sum(
    1 for m in msgs if isinstance(m, dict)
    for tc in (m.get("tool_calls") or [])
    if isinstance(tc.get("function", {}).get("arguments"), str)
    and _APPLIED_DIFF_MARK in tc["function"]["arguments"])
check("per-pass call cap respected",
      saved > 0 and _stubbed == _STALE_COMPACT_MAX_CALLS,
      f" (saved={saved}, stubbed={_stubbed}, cap={_STALE_COMPACT_MAX_CALLS})")

# ── write_file / apply_patch payload stubs end-to-end ─────────────────────
wf_args = json.dumps({"path": "big.py", "content": _pad(8000)},
                     ensure_ascii=False)
ap_args = json.dumps({"patch": ("--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n"
                                "-o\n+n\n c\n") + _pad(8000) + "\n"},
                     ensure_ascii=False)
msgs = [
    {"role": "user", "content": "t"},
    _assistant([_tc("w1", "write_file", wf_args)]),
    {"role": "tool", "content": "ok", "tool_call_id": "w1"},
    _assistant([_tc("p1", "apply_patch", ap_args)]),
    {"role": "tool", "content": "ok", "tool_call_id": "p1"},
    _assistant([_tc("w2", "write_file", json.dumps(
        {"path": "x.py", "content": _pad(8000)}, ensure_ascii=False))]),
    {"role": "tool", "content": "ok", "tool_call_id": "w2"},
    _assistant([_tc("w3", "write_file", json.dumps(
        {"path": "y.py", "content": _pad(8000)}, ensure_ascii=False))]),
    {"role": "tool", "content": "ok", "tool_call_id": "w3"},
]
saved = _compact(msgs, 8)
check("write_file payload compacted to applied-write stub",
      json.loads(msgs[1]["tool_calls"][0]["function"]["arguments"])
      == {"path": "big.py", "content": "[applied write: big.py (+1 lines)]"},
      f" ({msgs[1]['tool_calls'][0]['function']['arguments'][:120]})")
check("apply_patch payload compacted to applied-diff stub",
      "applied diff:" in msgs[3]["tool_calls"][0]["function"]["arguments"]
      and '"patch"' in msgs[3]["tool_calls"][0]["function"]["arguments"])
check("mixed trigger fired across write + patch payloads", saved > 0)

# ── stub-echo guard: echoing the marker is blocked ────────────────────────
import asyncio  # noqa: E402
import tools.handlers.patch_apply as _pa  # noqa: E402
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "a.py").write_text("z = 1\n", encoding="utf-8")
    echo_patch = ("--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n"
                  "-z = 1\n+[applied diff: a.py (+1/-1 lines)]\n")
    out = asyncio.run(_pa._inline_tool_apply_patch({"patch": echo_patch},
                                                   ws, str(ws)))
    check("echoing the compaction stub into a patch is rejected",
          "APPLY_PATCH_MALFORMED" in out, f" ({out.strip()[:100]})")

# NOTE on the echo guard for edit_file: the guard lives in
# execute_tool_round (STUB-ECHO) and cannot be driven without a full round
# fixture; its condition is pinned by core/test_duo_split-style source
# checks below.
src = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8",
                                                     errors="replace")
check("edit/write stub-echo guard also checks the applied-diff marker",
      "_APPLIED_DIFF_MARK in _raw_s.lower()" in src)
check("compaction trigger wired after a successful write/edit",
      "_compact_stale_edit_payloads(" in src)

sys.exit(h.finish("edit_history_compact"))
