# -*- coding: utf-8 -*-
"""Negative test for the /props vision gate (2026-10-04, Sonnet #2).

Proves the ONE case the positive runs cannot: the image plan says raw,
the profile says vision, the mmproj file EXISTS - but the coder's server
RUNS without the projector (HIVEMIND_TEST_OMIT_MMPROJ=<substring>).
Expected: coder loads without --mmproj, /props vision:false, the
post-load gate downgrades with a visible warning and the round trace
shows images_in_request=0. The OLD file-existence precheck must stay
silent here (the file is present) - only the /props gate can catch it.

The script is try/finally end to end: it sets the env var, restarts the
install under test, runs the image smoke, and ALWAYS restores the env
and restarts clean. Run it with the install STOPPED.

Usage:
  python tests/smoke_vision_negative.py [--install C:\\path\\to\\live]
                                        [--code ZZ77] [--port 8001]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_image import build_png, post_stream  # noqa: E402


def _health(base: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except OSError:  # URLError/socket/timeouts - anything else must raise
        return False


def _start(install: str) -> None:
    subprocess.Popen(
        ["cmd.exe", "/c", "start_hivemind.bat"],
        cwd=install, creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def _stop(port: int) -> None:
    ps = ("$c = Get-NetTCPConnection -LocalPort %d -State Listen "
          "-ErrorAction SilentlyContinue | Select-Object -First 1 "
          "-ExpandProperty OwningProcess; if ($c) { Stop-Process -Id $c -Force }" % port)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "HiveMind", "live"))
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--code", default="ZZ77")
    ap.add_argument("--omit", default="9b-ud", help="model substring to strip the projector from")
    ap.add_argument("--timeout", type=float, default=1200)
    args = ap.parse_args()

    if _health(args.base):
        print("[neg] STOP HiveMind first - the env switch must be set before start.")
        return 2

    payload = {"q": "Reproduce the attached image as ONE file repro.html (plain divs + CSS). "
                    "State in plain text what the image shows before you start.",
               "images": [__import__("base64").b64encode(build_png(args.code)).decode()],
               "mode": "code_duo"}

    print(f"[neg] starting install WITHOUT projector for '{args.omit}' ...")
    _stop(int(args.base.rsplit(":", 1)[-1]))
    time.sleep(3)
    os.environ["HIVEMIND_TEST_OMIT_MMPROJ"] = args.omit
    try:
        _start(args.install)
        for _ in range(40):
            if _health(args.base):
                break
            time.sleep(3)
        else:
            print("[neg] FAIL: install did not come up")
            return 2

        events = post_stream(args.base, payload, args.timeout)
        statuses = [str(d.get("content", "")) for d in events if d.get("type") == "status"]
        done = next((d for d in reversed(events) if d.get("type") == "done"), None)
        warn = [s for s in statuses if "VISION-GATE" in s or "projector" in s.lower()]
        skipped = any("image skipped" in s or "falling back to the image description" in s
                      for s in statuses)

        print("\n== STATUSES ==")
        for s in statuses[:12]:
            print("  ", s)
        print("\n== GATE WARNINGS ==")
        for w in warn:
            print("  ", w)
        print("== DONE ==", done and done.get("stop_reason"))
        checks = {
            "run completed (text-only fallback)": bool(done) and done.get("stop_reason") == "completed",
            "gate warning emitted": bool(warn),
            "image downgraded/skipped for coder": skipped,
        }
        print("\n== VERDICT ==")
        for k, v in checks.items():
            print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        print("\n[neg] now verify in the install log:")
        print("   grep 'TOOL-LOOP\\|VISION-GATE\\|mmproj' logs/hivemind.log | tail -10")
        print("   expected: CMD without --mmproj, /props vision:false,")
        print("   [DUO][VISION-GATE] warning, TOOL-LOOP ... images_in_request=0")
        return 0 if all(checks.values()) else 1
    finally:
        os.environ.pop("HIVEMIND_TEST_OMIT_MMPROJ", None)
        print("\n[neg] restoring clean install (env cleared, restarting) ...")
        _stop(int(args.base.rsplit(":", 1)[-1]))
        time.sleep(3)
        _start(args.install)
        for _ in range(40):
            if _health(args.base):
                print("[neg] clean install back up.")
                break
            time.sleep(3)
        else:
            print("[neg] WARNING: clean install did not come back up - check manually.")


if __name__ == "__main__":
    sys.exit(main())
