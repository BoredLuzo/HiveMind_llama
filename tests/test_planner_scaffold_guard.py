# -*- coding: utf-8 -*-
"""Planner darf kein ungefragtes Projektgeruest planen.

Live-Befund 2026-10-26: der Auftrag "erstelle zwei Dateien, greet.py und
main.py" wurde zu sieben Subtasks (Projekt-Intent, src/, requirements.txt,
.gitignore, README). Der Coder baute dann sieben Chunks lang ein Geruest.

Der Prompt sagt "NO generic boilerplate", ein 4B-Modell haelt sich nicht daran.
Deshalb filtert der Parser die Geruest-Schritte deterministisch, solange der
Auftrag sie nicht erwaehnt.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from hive_functions.planner import (  # noqa: E402
    drop_scaffold_steps, goal_allows_scaffold, parse_planner_output,
)

SCAFFOLD_PLAN = [
    "file: README.txt | touch: review project intent | decision: app type | risk: none",
    "file: [project-root] | touch: create src/ directory structure | decision: layout | risk: none",
    "file: requirements.txt | touch: list dependencies | decision: none | risk: none",
    "file: .gitignore | touch: exclude files | decision: none | risk: none",
    "file: greet.py | touch: add greet(name) | decision: return format | risk: none",
    "file: main.py | touch: print greet | decision: none | risk: none",
]
GOAL = "Create two short files: greet.py with greet(name) and main.py that prints it"

# ── 1. Der Filter selbst ───────────────────────────────────────────────
kept, dropped = drop_scaffold_steps(SCAFFOLD_PLAN, GOAL)
check("1a scaffold steps are dropped", len(dropped) == 4, f"(dropped={len(dropped)})")
check("1b the requested files survive", len(kept) == 2 and all(
    ("greet.py" in s or "main.py" in s) for s in kept), f"({kept})")
check("1c no drop when nothing looks like scaffolding",
      drop_scaffold_steps(["file: a.py | touch: x"], "create a.py")[1] == [])

# ── 2. Ein echter Projekt-Auftrag behaelt das Geruest ──────────────────
proj_goal = "Set up a new python project with a gitignore and requirements.txt"
kept2, dropped2 = drop_scaffold_steps(SCAFFOLD_PLAN, proj_goal)
check("2a a project request keeps the scaffolding", dropped2 == [],
      f"(dropped={len(dropped2)})")
check("2b goal_allows_scaffold detects it", goal_allows_scaffold(proj_goal) is True)

# ── 3. Ein Plan aus NUR Geruest bleibt nicht leer ──────────────────────
only_scaffold = SCAFFOLD_PLAN[:4]
raw_all = "\n".join(f"{i+1}. {s}" for i, s in enumerate(only_scaffold))
subs_all, _, q_all = parse_planner_output(raw_all, step_cap=20, goal=GOAL)
# The request names greet.py and main.py, so the plan is derived from THOSE
# instead of the scaffold (live finding 2026-10-26).
check("3a an all-scaffold plan is replaced by the requested files",
      len(subs_all) == 2 and all("greet.py" in s or "main.py" in s for s in subs_all),
      f"({subs_all})")
check("3b the replacement is reported", q_all.get("scaffold_replaced") == 4,
      f"({q_all.get('scaffold_replaced')})")

# ── 4. Verdrahtung im Parser ───────────────────────────────────────────
raw = "\n".join(f"{i+1}. {s}" for i, s in enumerate(SCAFFOLD_PLAN))
subs, mode, quality = parse_planner_output(raw, step_cap=20, goal=GOAL)
check("4a the parser applies the filter", len(subs) == 2, f"({len(subs)} subtasks)")
check("4b it reports how many were dropped", quality.get("scaffold_dropped") == 4,
      f"({quality.get('scaffold_dropped')})")
check("4c the parse mode is unchanged", mode == "numbered", f"({mode})")
subs2, _, q2 = parse_planner_output(raw, step_cap=20, goal=proj_goal)
check("4d a project goal keeps all steps", len(subs2) == 6 and not q2.get("scaffold_dropped"),
      f"({len(subs2)})")
subs3, _, _ = parse_planner_output(raw, step_cap=20)
check("4e without a goal the filter stays out of the way", len(subs3) == 6, f"({len(subs3)})")

# ── 5. Quelle: der Aufruf im Runner gibt das Ziel mit ──────────────────
SRC = (ROOT / "hive_functions" / "planner.py").read_text(encoding="utf-8", errors="replace")
check("5a the filter gets the user request, not the enriched task",
      "goal=_goal_for_guard" in SRC and "user_goal or task" in SRC)
check("5c the runner hands the raw request in",
      "user_goal=ctx.user_input" in (ROOT / "core" / "duo_runner.py").read_text(
          encoding="utf-8", errors="replace"))
check("5b the filter is wired after validate_chunk_plan",
      SRC.index("drop_scaffold_steps(subtasks, goal)") > SRC.index("validate_chunk_plan(subtasks"))


# ── 6. "Completed Tasks" sind keine offenen Schritte ───────────────────
COMPLETED_PLAN = (
    "## Plan: two files\n\n### Open Tasks\n"
    "1. file: greet.py | touch: greet(name) | decision: return | risk: none\n"
    "2. file: main.py | touch: print greet | decision: none | risk: none\n\n"
    "### Completed Tasks\n1. [x] greet.py created\n2. [x] main.py created\n"
)
subs_c, mode_c, _ = parse_planner_output(COMPLETED_PLAN, goal=GOAL)
check("6a completed tasks are not plan steps",
      len(subs_c) == 2 and all("[x]" not in s for s in subs_c), f"({subs_c})")
check("6b the open steps survive", any("greet.py" in s for s in subs_c) and
      any("main.py" in s for s in subs_c))
subs_ck, _, _ = parse_planner_output(
    "### Open Tasks\n1. [x] already done thing here\n2. build the real thing now\n",
    goal="build the real thing")
check("6c a [x] line inside Open Tasks is skipped too",
      len(subs_ck) == 1 and "[x]" not in subs_ck[0], f"({subs_ck})")

print()
print(f"SUMMARY planner_scaffold_guard: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
