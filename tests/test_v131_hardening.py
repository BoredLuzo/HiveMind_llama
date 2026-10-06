"""1.3.1 hardening pins (deep-audit round, 2026-10-06).

Pins for the 34-finding hardening batch. Each pin targets a concrete
verified finding; see the audit report for the full evidence trail.

Run: python tests/test_v131_hardening.py
Exit 0 = all pass, Exit 1 = failures.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


ROOT = Path(__file__).parent.parent


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_h1_agent_sse_newlines():
    src = _src("server.py")
    seg = src.split("async def _agent_stream")[1].split("return StreamingResponse")[0]
    bs2n = chr(92) + chr(92) + "n"          # literal double-backslash-n text
    if bs2n not in seg and 'data: [DONE]' in seg:
        ok("H1: agent SSE frames carry real newline escapes (no literal text)")
    else:
        fail("h1", "literal backslash-n still in the agent stream")


def test_h2_upgrade_locked():
    src = _src("backend/manager_load.py")
    seg = src.split("async def upgrade_port_to_vision")[1].split("async def _start_process")[0]
    if "async with self._lock" in seg and 'getattr(_slot, "_loading", False)' in seg \
            and "except Exception:\n            await _kill_slot_async(_slot)\n            raise" in seg:
        ok("H2/H4: vision upgrade locked, loading-guarded, zombie-kill on failure")
    else:
        fail("h2", "lock/guard wiring incomplete")


def test_h3_ready_event_gate_gone():
    src = _src("backend/manager_load.py")
    if "_need_start and not slot._ready_event.is_set()" not in src:
        ok("H3: fresh-claim start no longer gated on a possibly-stale event")
    else:
        fail("h3", "stale-event gate still present")


def test_h5_browser_containment():
    src = _src("tools/browser.py")
    if "_inline_check_workspace" in src:
        ok("H5: browser screenshot runs the workspace containment check")
    else:
        fail("h5", "containment check missing")


def test_h6_preview_keys():
    src = _src("tools/runner.py")
    if '("cmd", "command", "code", "packages", "package", "url", "path")' in src:
        ok("H6: approval preview reads cmd/packages (full command on the card)")
    else:
        fail("h6", "preview keys not extended")


def test_steer_queue_swept():
    src = _src("infra/run_control.py")
    hits = src.count("_steer_queue.pop(rid, None)")
    if hits >= 1:
        ok(f"steer queue: stale sweeper pops leaked queues ({hits} site)")
    else:
        fail("steer_sweep", "sweeper not wired")


def test_read_guard_post_dispatch():
    src = _src("tools/runner.py")
    ok_read = "H-audit: record ONLY successful reads" in src
    no_name_err = "_last_result_str" not in src
    if ok_read and no_name_err:
        ok("read guard: recorded post-dispatch, success-only (no NameError)")
    else:
        fail("read_guard", f"ok_read={ok_read} name_error={not no_name_err}")


def test_drive_root_refused():
    sys.path.insert(0, str(ROOT))
    from utils.workspace_resolve import extract_task_path
    if extract_task_path("mach was in C:\\. und weiter") in ("", None):
        ok("task-path: drive root (C:\\.) refused as workspace")
    else:
        fail("drive_root", "drive root accepted")


def test_mask_never_persisted():
    src = _src("routers/config.py")
    if 'data.get("git_token") in ("****",)' in src:
        ok("git_token: mask sentinel stripped before settings.update")
    else:
        fail("mask", "mask guard missing")


def test_models_cache_inplace():
    src = _src("routers/models.py")
    if "S_models_cache[:] = models" in src:
        ok("models cache: in-place mutation (from-import consumers stay live)")
    else:
        fail("cache", "rebind still present")


def test_version_131():
    src = _src("server.py")
    if 'HIVEMIND_VERSION = "1.3.1"' in src:
        ok("version bumped to 1.3.1")
    else:
        fail("version", "still 1.3.0")


if __name__ == "__main__":
    test_h1_agent_sse_newlines()
    test_h2_upgrade_locked()
    test_h3_ready_event_gate_gone()
    test_h5_browser_containment()
    test_h6_preview_keys()
    test_steer_queue_swept()
    test_read_guard_post_dispatch()
    test_drive_root_refused()
    test_mask_never_persisted()
    test_models_cache_inplace()
    test_version_131()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
