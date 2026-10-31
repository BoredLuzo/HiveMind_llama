# -*- coding: utf-8 -*-
"""[MEM] commit-charge snapshots (utils/mem_watch.py).

Run: python tests/test_mem_watch.py
"""
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from utils import mem_watch as MW  # noqa: E402

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


_mem_records: list[str] = []


class _Grab(logging.Handler):
    def emit(self, record):
        _mem_records.append(record.getMessage())


grab = _Grab()
logging.getLogger("hivemind.mem").addHandler(grab)
logging.getLogger("hivemind.mem").setLevel(logging.INFO)

sys_c = MW.system_commit_gb()
check("S1 system commit measured", sys_c is not None and sys_c[1] > 1,
      f"({sys_c})")
check("S2 used <= limit", sys_c is None or sys_c[0] <= sys_c[1])

own = MW.process_commit_mb(os.getpid())
check("S3 own process measured", own is not None and own[0] > 0, f"({own})")
check("S4 working set <= commit", own is None or own[1] <= own[0] * 10,
      f"({own})")
check("S5 a bogus pid answers None",
      MW.process_commit_mb(4_000_000) is None)
check("S6 pid 0 answers None", MW.process_commit_mb(0) is None)

parts = MW.snapshot_parts("selftest")
check("S7 the snapshot carries the label and commit",
      parts is not None and parts.startswith("selftest ")
      and "commit=" in parts and "engine=" in parts, f"({parts})")

MW.log_snapshot("hook-test")
check("L1 log_snapshot emits one [MEM] line",
      len(_mem_records) == 1 and _mem_records[0].startswith("[MEM] hook-test"),
      f"({_mem_records})")
check("L2 the line lists the engine process",
      "engine=" in _mem_records[0])

# disabled setting -> silence
from core import state as _st  # noqa: E402
_st.settings["mem_watch_enabled"] = False
_mem_records.clear()
MW.log_snapshot("should-not-log")
check("L3 mem_watch_enabled=false silences the snapshot",
      _mem_records == [])
_st.settings["mem_watch_enabled"] = True
MW.log_snapshot("re-enabled")
check("L4 re-enabling restores the line",
      any(r.startswith("[MEM] re-enabled") for r in _mem_records))

# malformed settings value -> defaults to enabled
_st.settings["mem_watch_enabled"] = "garbage-not-false"
_mem_records.clear()
MW.log_snapshot("string-form")
check("L5 a non-false string keeps the snapshot on",
      any("string-form" in r for r in _mem_records))
_st.settings.pop("mem_watch_enabled", None)

# the hooks are wired: grep the three call sites
load_src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
evict_src = (ROOT / "backend" / "manager_evict.py").read_text(encoding="utf-8")
srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
check("H1 load hook after slot-ready",
      'log_snapshot as _mem_snap' in load_src and '_mem_snap(f"load:{model}")' in load_src)
check("H2 evict hook after the kill",
      '_mem_snap(_mem_label)' in evict_src and 'evict:{model}' in evict_src)
check("H3 run-end hook after the RUN-TRACE line",
      '_mem_snap("run_end")' in srv_src)
check("H4 startup baseline hook",
      '_mem_snap("startup")' in srv_src)

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
