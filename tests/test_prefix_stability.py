"""PREFIX-STABILITY (2026-10-14, audit phase 3): mid-run mutations of the
sent message prefix kill llama.cpp's KV prefix cache — the reactive-think
activation used to push "<|think_on|>" into EVERY system message and a live
run measured [CACHE-MISS] prompt=40302 cached=0 (full re-prefill on 8 GB
Vulkan). After the fix the activation is append-only: system messages stay
byte-identical, thinking is re-enabled payload-side (chat_template_kwargs
keys off DuoRoundState.think_runtime in duo_runner).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.tool_exec_helpers import _maybe_activate_reactive_think
from core.agentic_duo_state import DuoRoundState

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


class _Hooks:
    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


print("=== prefix stability (reactive-think must be append-only) ===")

_sys = {"role": "system", "content": "You are a coder. " * 50}
_msgs = [_sys, {"role": "user", "content": "do the thing"}]
_rs = DuoRoundState(think_runtime=False)
_rs.tool_fail_streak = 0
_rs.reactive_think_activated = False
_hooks = _Hooks()

_sys_before = dict(_sys)

_run(_maybe_activate_reactive_think(
    "run_bash", "[run_bash error: exit code 1]", _rs, _msgs, _hooks,
    "on_fail", exec_has_thinking=True))

check("system message byte-identical after activation",
      _msgs[0] == _sys_before)
check("no think_on token anywhere in the prefix",
      all("<|think_on|>" not in str(m.get("content", "")) for m in _msgs))
check("activation appended exactly one user note",
      len(_msgs) == 3 and _msgs[-1].get("role") == "user"
      and "THINKING ON" in str(_msgs[-1].get("content", "")))
check("round_state.think_runtime flipped (payload-side re-enable)",
      _rs.think_runtime is True)
check("status event emitted", any(e.get("type") == "status" for e in _hooks.events))

# Second failure must NOT append a second note (activation is one-shot).
_run(_maybe_activate_reactive_think(
    "run_bash", "[run_bash error: exit code 1 again]", _rs, _msgs, _hooks,
    "on_fail", exec_has_thinking=True))
check("activation is one-shot per run", len(_msgs) == 3)

# Success resets the streak, no activation.
_rs2 = DuoRoundState(think_runtime=False)
_rs2.tool_fail_streak = 0
_rs2.reactive_think_activated = False
_m2 = [_sys, {"role": "user", "content": "ok?"}]
_run(_maybe_activate_reactive_think(
    "run_bash", "all good, exit 0", _rs2, _m2, _Hooks(),
    "on_fail", exec_has_thinking=True))
check("success neither activates nor appends",
      len(_m2) == 2 and _rs2.think_runtime is False)

print("=== prefix stability: no-think + vision-retract must be append-only ===")

_src_loop = (Path(__file__).resolve().parent.parent / "core" / "agentic_tool_loop.py").read_text(
    encoding="utf-8")
check("no in-place no-think system rewrite remains",
      "dtool_msgs[:] = _inject_no_think_directive" not in _src_loop)
check("no in-place vision-retract system rewrite remains",
      "_sys0[\"content\"] = _sys0[\"content\"].replace" not in _src_loop)

from core.agentic_tool_loop import _retract_vision_note_if_lost, _CODER_VISION_NOTE
_msgs2 = [{"role": "system", "content": "note " + _CODER_VISION_NOTE},
          {"role": "user", "content": "q"}]
_rs = DuoRoundState.think_runtime  # touch import chain
_before = dict(_msgs2[0])
_retract_vision_note_if_lost(_msgs2)  # plain coroutine-less helper
check("vision retract leaves system byte-identical", _msgs2[0] == _before)
check("vision retract appends correction note",
      len(_msgs2) == 3 and "NO LONGER" in str(_msgs2[-1].get("content", "")))

# ONCE PER RUN (2026-10-17): the helper runs on EVERY round while the image
# stays out of the window. The old version appended the note again each time
# (measured: 5 rounds -> 5 notes, 995 chars). It must append at most once.
_n_after_first = len(_msgs2)
for _ in range(6):
    _retract_vision_note_if_lost(_msgs2)
check("vision retract is idempotent across rounds (no repeated append)",
      len(_msgs2) == _n_after_first,
      f" ({_n_after_first} -> {len(_msgs2)} messages)")
_notes = [m for m in _msgs2
          if m.get("role") == "user" and "NO LONGER" in str(m.get("content", ""))]
check("exactly one vision-retract note in the list", len(_notes) == 1,
      f" ({len(_notes)} found)")

# a transcript that already carries the note from an earlier round must not
# get a second one (e.g. after a reload of the persisted message list)
_from_history = [{"role": "system", "content": "sys " + _CODER_VISION_NOTE},
                 {"role": "user", "content": "q"},
                 {"role": "user",
                  "content": __import__("core.agentic_tool_loop",
                                        fromlist=["x"])._CODER_VISION_REPLACEMENT.strip()}]
_retract_vision_note_if_lost(_from_history)
check("vision retract sees a note already in history", len(_from_history) == 3,
      f" ({len(_from_history)} messages)")

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
