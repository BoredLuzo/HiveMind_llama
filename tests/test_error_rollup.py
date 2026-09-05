# -*- coding: utf-8 -*-
"""Tests: deterministic error rollup (core/error_rollup.py).

Run: python tests/test_error_rollup.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.error_rollup import (
    ErrorItem,
    RollupState,
    parse_errors,
    render_rollup,
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


def tb(file_line: int, message: str) -> str:
    return (
        f'File "C:/src/app.py", line {file_line}, in compute\n'
        f"    return left + right\n"
        f"TypeError: {message}"
    )


# ── Identity: location is metadata, message is exact ─────────────────────────
items1 = parse_errors(tb(42, "cannot add int and str"))
items2 = parse_errors(tb(43, "cannot add int and str"))
check("I1: location shift (42->43) same signature",
      len(items1) == 1 and len(items2) == 1
      and items1[0].signature == items2[0].signature,
      f" sig={items1[0].signature if items1 else None} vs {items2[0].signature if items2 else None}")
check("I2: location fields separate",
      items1 and items1[0].line == 42 and items2 and items2[0].line == 43)

items3 = parse_errors(tb(43, "cannot add int and NoneType"))
check("I3: message change -> new signature",
      len(items3) == 1 and items1 and items3[0].signature != items1[0].signature)

# Rollup behaviour on both cases.
st = RollupState()
r1 = st.ingest(items1)
r2 = st.ingest(items2)   # same signature, line moved -> persistent, not new
check("I4: location shift -> persistent",
      r1["changed"] is False and r2["changed"] is True
      and r2["entries"][0]["status"] == "persistent", f" got={r2['entries'][0]['status'] if r2['entries'] else None}")
st2 = RollupState()
st2.ingest(items1)
r2b = st2.ingest(items3)  # message change -> new, not persistent
check("I5: message change -> new (not persistent)",
      r2b["entries"][0]["status"] == "new", f" got={r2b['entries'][0]['status']}")

# ── Run-state survives message-history compression ──────────────────────────
stc = RollupState()
sc1 = stc.ingest(parse_errors(tb(7, "boom")))
stc.on_message_history_compressed()   # simulated context compression (no-op)
sc2 = stc.ingest(parse_errors(tb(8, "boom")))
check("S1: first_seen unchanged after compression",
      sc2["entries"][0]["first_seen"] == sc1["entries"][0]["first_seen"] == 1)
check("S2: attempts continue (not reset to 1)",
      sc2["entries"][0]["attempts"] == 2)

# ── resolved register: FIXED then REOPENED ───────────────────────────────────
sr = RollupState()
sr.ingest(parse_errors(tb(1, "alpha")))          # round 1: new
sr.mark_all_fixed()                              # round 2: clean -> fixed
# unrelated failure in round 3
_other = parse_errors(tb(9, "unrelated"))
sr.ingest(_other)
# exact same signature returns in round 4
ro = sr.ingest(parse_errors(tb(2, "alpha")))
check("R1: reappeared signature -> REOPENED",
      ro["entries"][0]["status"] == "reopened",
      f" got={ro['entries'][0]['status'] if ro['entries'] else None}")
check("R2: REOPENED references the fixed round (2)",
      ro["entries"][0]["reopened_of"] == 2,
      f" got={ro['entries'][0].get('reopened_of')}")
check("R3: resolved register keeps history",
      sr.resolved.get(("TypeError", "alpha")) == 2)

# ── fixed detection: disappearing signature while others persist ────────────
sf = RollupState()
err_a = parse_errors(tb(1, "AAA") + "\n" + tb(2, "BBB"))
first = sf.ingest(err_a)
err_b = parse_errors(tb(1, "AAA") + "\n" + tb(3, "BBB"))
second = sf.ingest(err_b)
# BBB kept same body but line 2->3: persistent. AAA identical line 1: persistent.
check("F1: two signatures both persistent on repeat",
      {e["status"] for e in second["entries"]} == {"persistent"},
      f" got={[e['status'] for e in second['entries']]}")
# Now BBB disappears completely (only AAA present) -> BBB fixed this round.
err_c = parse_errors(tb(4, "AAA"))
third = sf.ingest(err_c)
check("F2: gone signature -> FIXED entry",
      len(third["fixed"]) == 1 and third["fixed"][0][1].message == "BBB",
      f" got={[i.message for _, i, _ in third['fixed']]}")
check("F3: resolved register contains BBB",
      ("TypeError", "BBB") in sf.resolved)
check("F4: fixed only reported once (not again in later round)",
      sf.ingest(err_c)["fixed"] == [])

# ── render output markers ────────────────────────────────────────────────────
rr = RollupState()
rr.ingest(parse_errors(tb(1, "gamma")))
res = rr.ingest(parse_errors(tb(2, "gamma")))
lines = render_rollup(res)
check("G1: PERSISTS marker rendered",
      any("[PERSISTS]" in l and "fix attempt #2" in l for l in lines),
      f" lines={lines}")
res_fix = rr.ingest(parse_errors(tb(9, "other")))
fixed_lines = render_rollup(res_fix)
check("G2: FIXED marker rendered once",
      sum(1 for l in fixed_lines if l.startswith("[FIXED] TypeError: gamma")) == 1,
      f" lines={fixed_lines}")

# ── parser coverage for common formats ───────────────────────────────────────
p_pytest = parse_errors("FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2\n")
check("P1: pytest FAILED line parsed",
      len(p_pytest) == 1 and p_pytest[0].error_type == "AssertionError"
      and "test_one" in (p_pytest[0].file or ""))
p_ts = parse_errors('src/a.ts(12,4): error TS2304: Cannot find name x\n')
check("P2: TS error parsed", len(p_ts) == 1 and p_ts[0].error_type == "TS2304"
      and p_ts[0].line == 12 and p_ts[0].col == 4)
p_gcc = parse_errors('src/a.c:12:4: error: use of undeclared identifier y\n')
check("P3: gcc error parsed", len(p_gcc) == 1 and p_gcc[0].file == "src/a.c"
      and p_gcc[0].line == 12 and p_gcc[0].col == 4)
check("P4: empty/unknown -> []", parse_errors("All tests passed.\n") == []
      and parse_errors("") == [])
check("P5: no double parse of python frame line",
      parse_errors(tb(1, "z"))[0].message == "z")

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
