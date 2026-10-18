"""Prove the preset merge: a partial preset must not shrink a budget."""
import asyncio
import copy
import sys

sys.path.insert(0, r"C:\Users\NtheP\Desktop\HiveMind\repo_gateway")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import routers.config as rc  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# a preset saved BEFORE the budget was raised / before a role existed
_OLD_PRESET = {
    "old-partial": {
        "agents": {"direct": {"model": "qwen3.5:4b-mtp"}},   # no max_tokens
        "duo_agentic_mode": False,
    }
}
rc.load_presets = lambda: copy.deepcopy(_OLD_PRESET)

s = rc.settings
s["agents"] = {
    "direct": {"model": "gemma-4:e4b-uncensored-hauhaucs-aggressive",
               "temperature": 0.6, "max_tokens": 4096, "thinking": True,
               "thinking_budget": 8000},
    "duo_coder": {"model": "qwen3.6:35b-a3b-ud", "max_tokens": 12000},
}

ok = asyncio.run(rc._apply_preset_internal("old-partial", persist=False))
check("preset applied", bool(ok))
_d = s["agents"].get("direct", {})
check("model from the preset wins", _d.get("model") == "qwen3.5:4b-mtp", _d.get("model"))
check("budget is NOT reset to a default", _d.get("max_tokens") == 4096,
      f"max_tokens={_d.get('max_tokens')}")
check("thinking settings survive", _d.get("thinking") is True
      and _d.get("thinking_budget") == 8000)
check("roles the preset never knew survive",
      s["agents"].get("duo_coder", {}).get("max_tokens") == 12000)

print()
print(f"SUMMARY preset_merge: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
