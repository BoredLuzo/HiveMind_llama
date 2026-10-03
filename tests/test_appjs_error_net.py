"""Frontend error-net source guards + JS syntax check (2026-09-15).

Live complaint: "F12 errors are basically 0 catched" — the frontend had NO
window.onerror / unhandledrejection handlers at all, ~44 fully-silent catch
blocks, settings saves that failed invisibly, and an init() boot chain that
one failed fetch could abort silently.

Fixes (static/app.js, index.html): global error + unhandledrejection
listeners with a throttled dismissible toast (showErrorToast), per-step
boot wrapping, _postSettingsFF helper with visible failures + caller
reverts, SSE death chat-line no longer suppressed by _hadBody, cache-bust
of the script URL.

Run: python tests/test_appjs_error_net.py
Exit 0 = all pass, Exit 1 = failures.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP = ROOT / "static" / "app.js"
IDX = ROOT / "index.html"

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


def test_js_syntax():
    node = shutil.which("node")
    if not node:
        print("  SKIP  node not available — syntax check skipped")
        return
    r = subprocess.run([node, "--check", str(APP)], capture_output=True, text=True)
    if r.returncode == 0:
        ok("node --check static/app.js: Syntax ok")
    else:
        fail("js_syntax", r.stderr[:300])


def test_global_net_installed():
    src = APP.read_text(encoding="utf-8")
    checks = [
        "addEventListener('error'", "addEventListener('unhandledrejection'",
        "_showErrorToast", "GLOBAL ERROR NET",
    ]
    missing = [c for c in checks if c not in src]
    if not missing:
        ok("global error/unhandledrejection listeners + toast installed")
    else:
        fail("global_net", f"missing: {missing}")


def test_toast_throttle_and_benign_filter():
    src = APP.read_text(encoding="utf-8")
    if "8000" in src and "_suppressed" in src and "ResizeObserver loop" in src:
        ok("toast throttled (8s) + suppression counter + benign filter")
    else:
        fail("toast", "throttle/benign missing")


def test_boot_hardened():
    src = APP.read_text(encoding="utf-8")
    checks = ["_step('loadModels'", "_step('clear_session'", "_step('loadSettings'",
              "BOOT-HARDENING (2026-09-15)"]
    missing = [c for c in checks if c not in src]
    if not missing:
        ok("init(): every boot step wrapped individually")
    else:
        fail("boot", f"missing: {missing}")
    # the old bare unguarded fetch must be gone from init
    if "await fetch('/memory/clear_session', {method:'POST'});  // Seitenreload" in src:
        fail("boot_bare_fetch", "unguarded clear_session fetch still present")
    else:
        ok("old unguarded boot fetch removed")


def test_settings_saves_visible():
    src = APP.read_text(encoding="utf-8")
    n = src.count("_postSettingsFF(")
    # helper def + at least keepalive/exploration/soul/intent-enabled/intent-model
    if "function _postSettingsFF(" in src and n >= 6 and "Setting could NOT be saved" in src:
        ok(f"settings saves visible via _postSettingsFF ({n - 1} call sites)")
    else:
        fail("settings_vis", f"helper/call sites incomplete (n={n})")
    if "postSettings flush failed" in src and "could NOT be saved" in src:
        ok("_flushQueuedSettings error visible")
    else:
        fail("flush_vis", "flush error message missing")


def test_sse_death_always_visible():
    src = APP.read_text(encoding="utf-8")
    if "SSE-DEATH-VISIBILITY (2026-09-15)" in src and "if (!_isAbort) {" in src:
        ok("SSE error line always shown (no _hadBody suppression anymore)")
    else:
        fail("sse", "suppression not removed")


def test_cache_bust():
    html = IDX.read_text(encoding="utf-8")
    import re as _re
    if _re.search(r"app\.js\?v=\d{8}-\d+", html):
        ok("Cache-Bust: index.html references app.js with a ?v=YYYYMMDD-N buster")
    else:
        fail("cache_bust", "no ?v= cache-buster on the app.js script tag")


if __name__ == "__main__":
    test_js_syntax()
    test_global_net_installed()
    test_toast_throttle_and_benign_filter()
    test_boot_hardened()
    test_settings_saves_visible()
    test_sse_death_always_visible()
    test_cache_bust()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
