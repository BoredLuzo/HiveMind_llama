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

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
