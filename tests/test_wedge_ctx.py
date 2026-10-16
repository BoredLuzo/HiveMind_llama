"""Wedge-handoff fix agent: wired settings and a context that matches the coder.

Two defects from the audit (2026-10-17):

1. core/fix_agent.py:116 reads `duo_wedge_handoff_max_tokens`, but the key was
   in neither settings.py DEFAULT_SETTINGS nor docs/settings.md - the value was
   pinned to the literal 1200 and could not be configured (while every sibling
   handoff key is configurable).

2. duo_runner called run_fix_agent(...) without num_ctx, so the agent used its
   8192 default. With a coder running at another ctx, its ensure_loaded() saw a
   mismatch and killed + reloaded the warm slot (backend/manager_load.py:402) -
   the opposite of the "NEVER evicts" promise in the module docstring.

Pins:
  (a) the key exists in DEFAULT_SETTINGS with 1200 and is documented
  (b) fix_agent really reads that key (source guard, default kept)
  (c) duo_runner passes the coder ctx
  (d) the default value still applies when the key is absent

Run: python tests/test_wedge_ctx.py
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


from settings import DEFAULT_SETTINGS                          # noqa: E402

# ── (a) the key ─────────────────────────────────────────────────────────────
check("DEFAULT_SETTINGS carries duo_wedge_handoff_max_tokens",
      "duo_wedge_handoff_max_tokens" in DEFAULT_SETTINGS)
check("its default is 1200",
      DEFAULT_SETTINGS.get("duo_wedge_handoff_max_tokens") == 1200)
_doc = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8", errors="replace")
check("docs/settings.md documents it", "`duo_wedge_handoff_max_tokens`" in _doc)
for _sib in ("duo_wedge_handoff_enabled", "duo_wedge_handoff_max_rounds",
             "duo_wedge_handoff_timeout_s", "duo_wedge_handoff_max_per_run",
             "duo_wedge_handoff_streak_threshold", "duo_wedge_handoff_model"):
    check(f"sibling key still present ({_sib})", _sib in DEFAULT_SETTINGS)

# ── (b) fix_agent reads it ──────────────────────────────────────────────────
_fa = (ROOT / "core" / "fix_agent.py").read_text(encoding="utf-8", errors="replace")
check("fix_agent reads the key",
      '"max_tokens": int(_s("duo_wedge_handoff_max_tokens", 1200))' in _fa)
check("fix_agent still defaults to 1200 without the key",
      '_s("duo_wedge_handoff_max_tokens", 1200)' in _fa)

# ── (c) the caller passes the coder ctx ─────────────────────────────────────
_du = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("duo_runner passes num_ctx to the fix agent",
      "num_ctx=int(_dtool_ctx or 8192)" in _du)
check("the call still passes port/max_rounds/timeout_s",
      "port=_cached_coder_port" in _du
      and 'duo_wedge_handoff_max_rounds' in _du
      and 'duo_wedge_handoff_timeout_s' in _du)
check("run_fix_agent accepts num_ctx",
      "num_ctx: int = 8192" in _fa)
check("the docstring promise is now true (ctx comes from the caller)",
      "NEVER evict" in _fa)

# ── (d) settings fallback behaviour ─────────────────────────────────────────
from core.fix_agent import _s as _fa_s                          # noqa: E402

check("_s returns the default for a missing key",
      _fa_s("duo_wedge_handoff_max_tokens_does_not_exist", 777) == 777)
check("_s honours a configured value",
      _fa_s("duo_wedge_handoff_max_tokens", 1) == 1200,
      " (DEFAULT_SETTINGS wins over the fallback - expected)")

print()
print(f"SUMMARY wedge_ctx: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
