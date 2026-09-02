# -*- coding: utf-8 -*-
"""Regression: Semantic-LRU A/B/C (stale-read invalidation, dedupe, pathless decay).

A) Stale read_file outputs are evicted immediately after a successful
   edit/write/patch of the same path (context/compression.evict_stale_reads_for_path).
B) A NEW full read_file of an already-read path dedupes older full copies
   (keeps the newest copy alive).
C) Path-less outputs (run_bash / run_python / web) age out via half-rate decay
   instead of staying at full TTL forever; the newest path-less output is kept
   fresh like a focus-refresh.

Run: python tests/test_ctx_lru_invalidation.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hive_functions.memory import ToolContextLRU, _recall_marker  # noqa: E402
from context.compression import evict_stale_reads_for_path  # noqa: E402
from core import tool_exec_helpers as H  # noqa: E402

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


# ── Fixtures ──────────────────────────────────────────────────────────────

def _msgs(paths, full=True):
    """Build a dtool_msgs list with one read_file tool result per path."""
    out = [{"role": "system", "content": "SYS"}]
    for i, p in enumerate(paths):
        out.append({"role": "assistant", "content": f"call {i}",
                    "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": "{}"}}]})
        header = f"[{p} total lines: 100]" if full else f"[{p} lines 1-50 / 100]"
        out.append({"role": "tool", "content": header + "\ncontent-" + p,
                    "tool_call_id": f"c{i}", "name": "read_file"})
    return out


def _register_reads(lru, msgs):
    for idx, m in enumerate(msgs):
        if m.get("role") == "tool" and m.get("name") == "read_file":
            path = m["content"].split(" ")[0][1:]  # strip leading '['
            lru.register(idx, path=path, kind="read_file")


# ── A: stale read invalidated after edit ──────────────────────────────────

msgs = _msgs(["a.py", "b.py"])
lru = ToolContextLRU(default_ttl=3)
_register_reads(lru, msgs)

ev = evict_stale_reads_for_path(messages=msgs, lru=lru, path="a.py")
check("A1: evicts stale read of edited path", ev == 1)
check("A2: content replaced by recall marker",
      msgs[2]["content"] == _recall_marker("a.py"), f" got={msgs[2]['content'][:40]!r}")
check("A3: sibling file read untouched",
      msgs[4]["content"].startswith("[b.py"), msgs[4]["content"][:40])
check("A4: lru entry marked evicted",
      all(e.get("evicted") for e in lru._entries if e.get("path") == "a.py"))

# no re-eviction on second pass (marker guard)
ev2 = evict_stale_reads_for_path(messages=msgs, lru=lru, path="a.py")
check("A5: idempotent - no double eviction", ev2 == 0)

# ── A: no-op when path absent from context ────────────────────────────────

msgs3 = _msgs(["x.py"])
lru3 = ToolContextLRU(default_ttl=3)
_register_reads(lru3, msgs3)
ev3 = evict_stale_reads_for_path(messages=msgs3, lru=lru3, path="nonexistent.py")
check("A6: unknown path -> 0 evictions", ev3 == 0)

# ── B: dedupe on re-read ──────────────────────────────────────────────────

msgs4 = _msgs(["a.py", "a.py"])
lru4 = ToolContextLRU(default_ttl=3)
_register_reads(lru4, msgs4)
# new read index is 4 (second tool result: idx 3 assistant, idx 4 tool)
ev4 = evict_stale_reads_for_path(messages=msgs4, lru=lru4, path="a.py", exclude_idx=4)
check("B1: older duplicate evicted", ev4 == 1)
check("B2: newest copy kept intact",
      msgs4[4]["content"].startswith("[a.py"), msgs4[4]["content"][:40])
check("B3: marker in older copy", msgs4[2]["content"] == _recall_marker("a.py"))

# ── C: pathless decay ─────────────────────────────────────────────────────

lru5 = ToolContextLRU(default_ttl=3)  # pathless_ttl = 6
lru5.register(0, path="src/z.py", kind="read_file")
lru5.register(1, path="", kind="run_bash")   # older pathless
lru5.register(2, path="", kind="run_bash")   # newest pathless
for _ in range(4):
    lru5.decay("src/z.py")
byidx = {e["idx"]: e for e in lru5.candidates()}
check("C1: read_file refreshes on focus", byidx[0]["ttl"] == 3, f" ttl={byidx[0]['ttl']}")
check("C2: newest pathless stays fresh", byidx[2]["ttl"] == 6, f" ttl={byidx[2]['ttl']}")
check("C3: older pathless aged", byidx[1]["ttl"] < 6, f" ttl={byidx[1]['ttl']}")

# over many turns the old pathless output eventually reaches evictable low ttl
lru6 = ToolContextLRU(default_ttl=3)
lru6.register(1, path="", kind="run_bash")
lru6.register(2, path="", kind="run_bash")   # newest
for _ in range(30):
    lru6.decay("other.py")
by6 = {e["idx"]: e for e in lru6.candidates()}
check("C4: very old pathless output evictable (ttl 0)",
      by6[1]["ttl"] == 0, f" ttl={by6[1]['ttl']}")
check("C5: newest pathless still fresh after long run",
      by6[2]["ttl"] == 6, f" ttl={by6[2]['ttl']}")

# ── Integration guard: _register_context_lru dedupe gating (full read only) ─

from context.compression import _weighted_ttl  # noqa: E402

_full_re = re.compile(r"\[[^\]]* total lines: \d+\]")
check("I1: full-read regex matches full read", _full_re.match("[a.py total lines: 100]") is not None)
check("I2: full-read regex rejects partial read", _full_re.match("[a.py lines 1-50 / 100]") is None)

# pathless TTL weighting on register (errors get 2x on top of pathless_ttl)
lru7 = ToolContextLRU(default_ttl=3)
lru7.register(0, path="", kind="run_bash",
              ttl_override=_weighted_ttl("normal output", 3))
lru7.register(1, path="", kind="run_bash",
              ttl_override=_weighted_ttl("FAILED: x", 3))
by7 = {e["idx"]: e["ttl"] for e in lru7.candidates()}
check("I3: pathless error output gets longer TTL", by7[1] > by7[0], f" {by7}")

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
