"""Harness quality counters: measurement only, never a cap.

Owner decision 2026-10-17 (after the tool-list review): NO
model-dependent cap, no "tier_cap" profile. The tier drop stays purely
window-based (schema tokens against the configured context) - that is the only
hard limit. But the events a model trips over should be a NUMBER, not a feeling:

  unknown_tool  - called a name that is not in the tool list
  invalid_args  - a tool result came back INVALID_ARGUMENT
  repeat_call   - the same call as the previous round (stuck hook)

core/harness_stats.py keeps bounded per-model counters (MAX_MODELS 32, three
examples per kind). Nothing branches on them; the first occurrence per
(model, kind) is logged at INFO, the rest at DEBUG, and summary(model) gives one
line for run ends.

Wired sites:
  core/tool_loop.py     _inject_retry_feedback(...) -> unknown_tool/parse_error/
                        invalid_args, with the loop's model (cfg.model)
  core/tool_executor.py INVALID_ARGUMENT results -> invalid_args (duo coder)
  core/tool_executor.py stuck hook -> repeat_call

Run: python tests/test_harness_stats.py
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


from core import harness_stats as HS                              # noqa: E402

# ── behaviour ───────────────────────────────────────────────────────────────
HS.reset()
check("starts empty", HS.stats() == {})
HS.note("m1", "unknown_tool", "write_files")
HS.note("m1", "unknown_tool", "read_filex")
HS.note("m1", "invalid_args", "edit_file")
HS.note("m2", "repeat_call", "run_bash")
_s = HS.stats()
check("counts per model and kind", _s["m1"] == {"unknown_tool": 2, "invalid_args": 1}
      and _s["m2"] == {"repeat_call": 1}, f" ({_s})")
check("zero kinds are not reported", "repeat_call" not in _s["m1"])
check("details keep the first examples",
      HS.details()["m1"]["unknown_tool"] == ["write_files", "read_filex"])
check("summary names the model and the numbers",
      "model=m1" in HS.summary("m1") and "unknown_tool=2" in HS.summary("m1"))
check("summary shows an example", "write_files" in HS.summary("m1"))
check("clean model says so", "clean" in HS.summary("never-seen"))
check("all-model summary covers both", "m1" in HS.summary() and "m2" in HS.summary())
check("unknown kinds are ignored (no crash, no row)",
      (HS.note("m3", "nonsense"), "m3" not in HS.stats())[1])
check("empty model name gets a stable key",
      (HS.note(None, "repeat_call"), "(unset)" in HS.stats())[1])
check("note never raises on junk",
      (HS.note({"x": 1}, "repeat_call"), True)[1])
HS.reset()
check("reset clears everything", HS.stats() == {} and HS.details() == {})

# bounded memory
HS.reset()
for i in range(HS.MAX_MODELS + 10):
    HS.note(f"model-{i}", "unknown_tool")
check("model count stays bounded", len(HS.stats()) <= HS.MAX_MODELS,
      f" ({len(HS.stats())})")
for i in range(20):
    HS.note("chatty", "repeat_call", f"call-{i}")
check("details stay bounded",
      len(HS.details()["chatty"]["repeat_call"]) <= HS.MAX_DETAILS)
HS.reset()

# ── wiring ──────────────────────────────────────────────────────────────────
_src_loop = (ROOT / "core" / "tool_loop.py").read_text(encoding="utf-8", errors="replace")
_src_exec = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8", errors="replace")
check("feedback path counts (unknown_tool/parse_error/invalid_args)",
      "core.harness_stats import note as _hs_note" in _src_loop
      and '"unknown_tool" if error_type == "unknown_tool"' in _src_loop)
check("the loop passes its model into the counter",
      _src_loop.count('model=getattr(self.cfg, "model", "")') >= 3)
check("duo executor counts INVALID_ARGUMENT",
      'tool_error_has_code as _hs_has' in _src_exec
      and '"invalid_args", detail=str(_dname)' in _src_exec)
check("duo executor counts repeated calls on the stuck hook",
      '"repeat_call", detail=str(_dname)' in _src_exec)
check("counters never gate behaviour (no branch on stats)",
      "if HS.stats()" not in _src_exec and "harness_stats.stats()" not in _src_exec
      and "harness_stats.stats()" not in _src_loop)

print()
print(f"SUMMARY harness_stats: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
