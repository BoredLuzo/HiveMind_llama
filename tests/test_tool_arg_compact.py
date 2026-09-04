# -*- coding: utf-8 -*-
"""Regression: CONTEXT-COMPACTION of executed write tool-call args.

Verifiziert (2026-09-04):
  - nur erfolgreiche, GROSSE (>= _WRITE_ARG_COMPACT_MIN) Write-Calls werden
    nach Erfolg auf einen kleinen Stub gekuerzt,
  - bei mehreren Writes in EINER Runde werden exakt die richtigen Eintraege
    per tool_call id gekuerzt (kein 'Danebengreifen'),
  - kleine Calls / falsche ids / falscher Index bleiben unangetastet,
  - der Stub behaelt path + Referenz (chars + sha-Prefix).

Run: python tests/test_tool_arg_compact.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tool_executor import (  # noqa: E402
    _compact_round_write_args as _compact,
    _WRITE_ARG_COMPACT_MIN,
)

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


def _big_args(path, n=None):
    n = n or (_WRITE_ARG_COMPACT_MIN + 5000)
    return '{"path":"%s","content":"%s"}' % (path, "x" * n)


def _assistant(tool_calls):
    return {"role": "assistant", "content": None, "tool_calls": tool_calls}


def _tc(cid, name, args):
    return {"id": cid, "function": {"name": name, "arguments": args}}


# ── Einzelner grosser Write -> Stub ──────────────────────────────────────────
msgs = [{"role": "user", "content": "t"}, _assistant([_tc("c1", "write_file", _big_args("a.py"))])]
n = _compact(msgs, 1, {"id": "c1"}, "write_file", _big_args("a.py"), {"path": "a.py"})
check("S1: grosser erfolgreicher Write wird gekuerzt", n == 1)
check("S2: Stub ist klein", len(msgs[1]["tool_calls"][0]["function"]["arguments"]) < 300)
check("S3: Stub enthaelt Pfad", 'a.py' in msgs[1]["tool_calls"][0]["function"]["arguments"])
check("S4: Stub enthaelt sha-Referenz", 'sha ' in msgs[1]["tool_calls"][0]["function"]["arguments"])

# ── Batch: 3 Writes in einer Runde, dazwischen andere Calls ─────────────────
_batch_msgs = [_assistant([
    _tc("w1", "write_file", _big_args("a.py")),
    _tc("r1", "search_code", '{"q":"foo"}'),
    _tc("w2", "write_file_append", _big_args("b.py")),
    _tc("p1", "patch_file", '{"path":"c.py","old":"x","new":"y"}'),
    _tc("w3", "edit_file", _big_args("d.py")),
])]
_batch = _batch_msgs[0]
r_w1 = _compact(_batch_msgs, 0, {"id": "w1"}, "write_file", _big_args("a.py"), {"path": "a.py"})
r_w2 = _compact(_batch_msgs, 0, {"id": "w2"}, "write_file_append", _big_args("b.py"), {"path": "b.py"})
r_w3 = _compact(_batch_msgs, 0, {"id": "w3"}, "edit_file", _big_args("d.py"), {"path": "d.py"})
check("B1: alle 3 grossen Writes gekuerzt", (r_w1 == r_w2 == r_w3 == 1))
_w1_len = len(_batch["tool_calls"][0]["function"]["arguments"])
_w2_len = len(_batch["tool_calls"][2]["function"]["arguments"])
_w3_len = len(_batch["tool_calls"][4]["function"]["arguments"])
check("B2: w1/w2/w3 sind Stubs", (_w1_len < 300 and _w2_len < 300 and _w3_len < 300))
check("B3: search_code-Args unangetastet",
      _batch["tool_calls"][1]["function"]["arguments"] == '{"q":"foo"}')
check("B4: kleiner patch_file-Args unangetastet",
      _batch["tool_calls"][3]["function"]["arguments"] == '{"path":"c.py","old":"x","new":"y"}')
check("B5: Stub zeigt auf richtige Datei",
      ('a.py' in _batch["tool_calls"][0]["function"]["arguments"])
      and ('b.py' in _batch["tool_calls"][2]["function"]["arguments"])
      and ('d.py' in _batch["tool_calls"][4]["function"]["arguments"]))

# ── No-op Faelle ─────────────────────────────────────────────────────────────
small = [{"role": "user", "content": "t"}, _assistant([_tc("c1", "write_file", '{"content":"small"}')])]
check("N1: kleiner Write -> no-op",
      _compact(small, 1, {"id": "c1"}, "write_file", '{"content":"small"}', {}) == 0)
check("N2: falsche id -> no-op",
      _compact(small, 1, {"id": "nope"}, "write_file", _big_args("x.py"), {}) == 0)
check("N3: falscher Index -> no-op",
      _compact(small, 99, {"id": "c1"}, "write_file", _big_args("x.py"), {}) == 0)
check("N4: kein assistant/keine tool_calls -> no-op",
      _compact([{"role": "user", "content": "t"}], 0, {"id": "x"}, "write_file", _big_args("x.py"), {}) == 0)

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
