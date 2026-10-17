"""Two fixes for the ThinkPad report (2026-10-17).

Owner: "on the ThinkPad the model forgets the workspace and cannot get a
write_file done, 1500 s for a write_file with a 2B model".

1. WS-FALLBACK-WARN (`core/chat_run.py`): with no workspace configured the
   resolver falls back to the process CWD (= the engine folder) and the run then
   persisted THAT as "last used" - one accidental run pinned the install
   directory as the workspace, so later runs (and chats) kept their files next to
   the app and the model "forgot" the real project. A fallback run now logs a
   warning, tells the user in the UI, is never written to last-used, and cannot
   auto-commit.

2. CPU-CONTEXT-GUARD (`core/duo_helpers.cpu_prefill_warning` + call in
   `core/duo_runner.py`): without GPU offload n_ctx is a *time* setting. A
   CPU-only install with a large ctx (the owner's settings carry 70656) pays the
   whole prompt on the CPU - minutes per round, which looks exactly like a hang.
   The run now says so once, with the number, and points at 8k-16k.

Run: python tests/test_workspace_and_cpu_guards.py
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


from core.duo_helpers import cpu_prefill_warning, CPU_CTX_WARN_TOKENS  # noqa: E402

# ── 2. CPU/ctx warning helper ───────────────────────────────────────────────
check("threshold is a sane laptop range", 16384 <= CPU_CTX_WARN_TOKENS <= 65536,
      f" ({CPU_CTX_WARN_TOKENS})")
check("no warning on a GPU backend",
      cpu_prefill_warning(70656, "vulkan") == ""
      and cpu_prefill_warning(70656, "cuda") == "")
check("no warning for a small CPU ctx",
      cpu_prefill_warning(8192, "cpu") == ""
      and cpu_prefill_warning(CPU_CTX_WARN_TOKENS, "cpu") == "")
_w = cpu_prefill_warning(70656, "cpu")
check("warns on a large CPU ctx", bool(_w))
check("the warning carries the real number", "70656" in _w)
check("the warning explains the consequence",
      "first prompt" in _w and "re-prefill" in _w)
check("the warning names the practical range", "8k-16k" in _w)
check("backend match is case/space tolerant",
      bool(cpu_prefill_warning(70656, " CPU ")))
check("junk input is safe",
      cpu_prefill_warning(None, "cpu") == "" and cpu_prefill_warning("x", "cpu") == "")

_src_duo = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("the duo run calls the guard",
      "cpu_prefill_warning as _cpu_warn" in _src_duo
      and "_pw = _cpu_warn(_exec_ctx, _gpu_backend)" in _src_duo)
check("the guard reports to the user, not only the log",
      '"[PERF-WARN] %s"' in _src_duo and 'yield await ctx.emit({"type": "status", "content": "⚠ " + _pw})' in _src_duo)
check("the guard never breaks the run",
      "except (ImportError, AttributeError, TypeError, ValueError):" in _src_duo)

# ── 1. workspace fallback ───────────────────────────────────────────────────
_src_cr = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8", errors="replace")
check("cwd fallback is detected", 'if _ws_src == "cwd_fallback":' in _src_cr)
check("the fallback is logged loudly",
      "no workspace configured - using the engine directory" in _src_cr)
check("the fallback is NOT persisted as last-used",
      "this fallback is NOT saved as last-used" in _src_cr
      and _src_cr.index('if _ws_src == "cwd_fallback":') < _src_cr.index("_save_last_ws(_ws_str)"))
check("the fallback cannot auto-commit", "_ws_commit_allowed = False" in _src_cr)
check("real workspaces are still persisted",
      _src_cr.count("_save_last_ws(_ws_str)") == 1)

# resolver contract: the fallback is the LAST resort and returns the flag
from utils.workspace_resolve import resolve_workspace                   # noqa: E402
_ws, _src = resolve_workspace({}, None, "")
check("empty settings resolve to the cwd fallback",
      _src == "cwd_fallback", f" (source={_src})")
print()
print(f"SUMMARY workspace_and_cpu_guards: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
