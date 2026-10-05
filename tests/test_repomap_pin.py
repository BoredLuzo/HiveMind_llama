# -*- coding: utf-8 -*-
"""Tests: static repo-map pinning (core/repomap_pin.py).

Run: python tests/test_repomap_pin.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.repomap_pin import pin_static_map

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


MAP = (
    "## Static Repo-Map\n"
    "- src/a.py (funcs: run, main)\n"
    "- src/b.py (imports: a)\n"
)
MAP_V2 = (
    "## Static Repo-Map\n"
    "- src/a.py (funcs: run, main)\n"
    "- src/b.py (imports: a)\n"
    "- src/c.py (funcs: fresh, NEW file added mid-run)\n"
)
SYSTEM = "You are the coder.\n"
BIG_USER = (
    "TASK: fix the bug\n\n"
    + MAP
    + "\n## Codebase Architecture Map\n[contracts ...]\n\nRules: edit files.\n"
)
MAP_ONLY_USER = MAP


# ── pin moves the map out of a big user message into system ─────────────────
msgs = [
    {"role": "system", "content": SYSTEM},
    {"role": "user", "content": BIG_USER},
    {"role": "user", "content": "more"},
]
moved = pin_static_map(msgs)
check("P1: move happened", moved is True)
sys_content = msgs[0]["content"]
check("P2: system contains map", "## Static Repo-Map" in sys_content and "src/a.py" in sys_content)
check("P3: pinned marker present", "[REPO-MAP" in sys_content)
check("P4: system keeps original instructions", sys_content.startswith(SYSTEM.strip()))
user_contents = [m.get("content", "") for m in msgs if m.get("role") == "user"]
check("P5: map removed from user content",
      not any("## Static Repo-Map" in c for c in user_contents))
check("P6: volatile part of big user message preserved",
      any("TASK: fix the bug" in c for c in user_contents))
check("P7: untouched user message preserved", any(c == "more" for c in user_contents))

# ── idempotent: second call is a no-op ───────────────────────────────────────
before = [dict(m, content=m["content"]) for m in msgs]
moved2 = pin_static_map(msgs)
check("P8: already pinned -> no-op", moved2 is False)
check("P9: no content change on second call",
      [m["content"] for m in msgs] == [b["content"] for b in before])

# ── a user message that contained ONLY the map gets dropped ─────────────────
msgs_only = [
    {"role": "system", "content": SYSTEM},
    {"role": "user", "content": MAP_ONLY_USER},
]
pin_static_map(msgs_only)
roles = [m.get("role") for m in msgs_only]
check("P10: map-only user message dropped",
      roles == ["system"], f" roles={roles}")
check("P11: system got the map", "## Static Repo-Map" in msgs_only[0]["content"])

# ── no map anywhere -> unchanged, returns False ─────────────────────────────
plain = [
    {"role": "system", "content": SYSTEM},
    {"role": "user", "content": "TASK: no map here"},
]
orig = [dict(m) for m in plain]
check("P12: no map -> False", pin_static_map(plain) is False)
check("P13: no map -> unchanged", plain == orig)

# ── system not at index 0 -> no-op ───────────────────────────────────────────
odd = [{"role": "user", "content": BIG_USER}]
check("P14: no system at 0 -> no-op", pin_static_map(odd) is False)

# ── pass stability: identical map across two fresh builds -> byte-identical ──
_pass1 = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": BIG_USER}]
pin_static_map(_pass1)
s1 = _pass1[0]["content"]
_pass2 = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": BIG_USER}]
pin_static_map(_pass2)
s2 = _pass2[0]["content"]
check("T1: fresh pass with identical map -> byte-identical system",
      s1 == s2, f" len1={len(s1)} len2={len(s2)}")

# ── fresh-copy safety net: system pinned, UPDATED map arrives in a user msg ──
_msgs_v2 = [
    {"role": "system", "content": s1},                       # already pinned (map v1)
    {"role": "user", "content": "TASK: keep going\n\n" + MAP_V2},  # updated map copy
]
moved_v2 = pin_static_map(_msgs_v2)
check("T2: pinned system untouched when updated map copy arrives",
      moved_v2 is False and _msgs_v2[0]["content"] == s1)
check("T3: updated map copy stays in user history (append-only)",
      MAP_V2 in str(_msgs_v2[1].get("content", "")))

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
