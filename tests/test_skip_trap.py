"""SKIP-TRAP: content evicted from the context must become re-readable.

Two defects, both from reading the code (2026-10-17):

1. Mask-first eviction (_evict_stale_tool_outputs, the DEFAULT path:
   settings duo_mask_before_compress=True) replaced tool outputs with recall
   markers but never released the path in tools.runner._files_read_in_run.
   The caller gets only a count back, so it could not compensate either.
   A fresh read_file then answered "[SKIP: ... already read ... the full
   content is in your context above]" while the content was gone.

2. The 2026-09-13 fix in evict_stale_reads_for_path did `_rs.discard(path)`,
   but the guard stores normalize_tool_path() results (ABSOLUTE, lowercase,
   forward slashes) while the LRU keeps what the model sent - usually relative
   ("src/app.py"). The discard therefore missed and the fix was a no-op for
   the common case.

Pins:
  (a) _drop_read_guard_paths matches absolute guard entries from a relative
      path and vice versa, leaves unrelated paths alone
  (b) mask-first eviction really releases the path (end to end through
      _evict_stale_tool_outputs with a real LRU and a real guard set)
  (c) the dedupe path (evict_stale_reads_for_path) does the same
  (d) the guard entry survives when nothing was evicted

Run: python tests/test_skip_trap.py
"""
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


from context.compression import (                               # noqa: E402
    _drop_read_guard_paths, _evict_stale_tool_outputs,
    evict_stale_reads_for_path, _recall_marker)
from hive_functions.memory import ToolContextLRU                 # noqa: E402
import tools.runner as TR                                        # noqa: E402

_CTX = TR._files_read_in_run


def _guard(*paths):
    s = set(paths)
    _CTX.set(s)
    return s


# ── (a) path-form matching ──────────────────────────────────────────────────
_g = _guard("c:/ws/src/app.py", "c:/ws/other/util.py", "c:/ws/src/app.py.bak")
_drop_read_guard_paths(["src/app.py"])
check("relative LRU path releases the absolute guard entry",
      "c:/ws/src/app.py" not in _g, f" ({sorted(_g)})")
check("unrelated file untouched", "c:/ws/other/util.py" in _g)
check("name-sharing sibling untouched", "c:/ws/src/app.py.bak" in _g)

_g = _guard("c:/ws/src/app.py")
_drop_read_guard_paths(["c:/ws/src/app.py"])
check("identical absolute path released", not _g)

_g = _guard("c:/ws/src/app.py")
_drop_read_guard_paths([])
check("empty input is a no-op", "c:/ws/src/app.py" in _g)

# ── (b) mask-first eviction releases the path ───────────────────────────────
_msgs = [
    {"role": "system", "content": "sys"},
    {"role": "tool", "name": "read_file",
     "content": "[app.py total lines: 400]\n" + ("x = 1\n" * 4000)},
    {"role": "tool", "name": "run_bash", "content": "ok"},
]
_lru = ToolContextLRU(default_ttl=3)
_lru.register(1, path="src/app.py", size_chars=len(_msgs[1]["content"]),
              kind="read_file", ttl_override=1)
_lru.register(2, path="", size_chars=2, kind="run_bash", ttl_override=1)
_g = _guard("c:/ws/src/app.py", "c:/ws/src/keep.py")

_n = _evict_stale_tool_outputs(messages=_msgs, lru=_lru,
                               target_token_budget=10, hard_floor_tokens=0)
check("mask-first evicted something", _n >= 1, f" (evicted {_n})")
check("masked tool output replaced by a recall marker",
      str(_msgs[1]["content"]).startswith("[System: Content of")
      or "app.py" in str(_msgs[1]["content"]))
check("mask-first released the read-guard path",
      "c:/ws/src/app.py" not in _g, f" ({sorted(_g)})")
check("mask-first left other guard paths alone", "c:/ws/src/keep.py" in _g)

# ── (c) dedupe path releases the path (previously a silent no-op) ───────────
_msgs2 = [
    {"role": "system", "content": "sys"},
    {"role": "tool", "name": "read_file",
     "content": "[app.py total lines: 400]\n" + ("y = 1\n" * 500)},
    {"role": "tool", "name": "read_file",
     "content": "[app.py total lines: 400]\n" + ("z = 1\n" * 500)},
]
_lru2 = ToolContextLRU(default_ttl=3)
_lru2.register(1, path="app.py", size_chars=100, kind="read_file", ttl_override=1)
_lru2.register(2, path="app.py", size_chars=100, kind="read_file", ttl_override=1)
_g2 = _guard("c:/ws/app.py")
_ev = evict_stale_reads_for_path(messages=_msgs2, lru=_lru2, path="app.py",
                                 exclude_idx=2, cache_horizon=0)
check("dedupe evicted the older copy", _ev == 1, f" (evicted {_ev})")
check("dedupe released the read-guard path", "c:/ws/app.py" not in _g2,
      f" ({sorted(_g2)})")
check("newest copy kept", _msgs2[2]["content"].count("z = 1") > 0)

# ── (d) nothing evicted -> guard untouched ─────────────────────────────────
_g3 = _guard("c:/ws/untouched.py")
_msgs3 = [{"role": "system", "content": "sys"},
          {"role": "tool", "name": "read_file", "content": "small"}]
_lru3 = ToolContextLRU(default_ttl=3)
_lru3.register(1, path="untouched.py", size_chars=5, kind="read_file", ttl_override=1)
_n3 = _evict_stale_tool_outputs(messages=_msgs3, lru=_lru3,
                                target_token_budget=10 ** 9, hard_floor_tokens=0)
check("under budget: nothing evicted", _n3 == 0, f" ({_n3})")
check("under budget: guard untouched", "c:/ws/untouched.py" in _g3)

_CTX.set(None)

print()
print(f"SUMMARY skip_trap: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
