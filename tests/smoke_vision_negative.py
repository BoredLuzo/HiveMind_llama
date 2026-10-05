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
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_image import build_png, post_stream  # noqa: E402

# VISUAL-CLAIM PATTERNS (2026-10-04, Sonnet #2): patterns that ASSERT the
# model sees the image. A bare "in the image" is deliberately NOT here -
# it also matches negations like "there is nothing in the image" and the
# echoed question. Pinned by tests/test_smoke_harness.py.
_VISUAL_CLAIM_RE = re.compile(
    r"\bi (?:can|could) see\b"
    r"|\bi see (?:a|an|the|it)\b"
    r"|\bas shown\b"
    r"|\bthe image shows\b"
    r"|\blooking at the (?:image|picture|photo)\b"
    r"|\bthe attached (?:image|picture)\b", re.I)


def find_visual_claims(text: str) -> list:
    """Substring-safe visual-claim hits in a model answer (pure)."""
    return sorted({m.group(0).lower() for m in _VISUAL_CLAIM_RE.finditer(text or "")})


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
    ap.add_argument("--dry-run", action="store_true",
                    help="validate install paths, write the PNG preview, print the plan - no restart")
    args = ap.parse_args()

    if args.dry_run:
        _log = os.path.join(args.install, "logs", "hivemind.log")
        print("[neg] DRY RUN - nothing restarted.")
        print(f"[neg] install exists      : {os.path.isdir(args.install)}")
        print(f"[neg] start_hivemind.bat  : {os.path.isfile(os.path.join(args.install, 'start_hivemind.bat'))}")
        print(f"[neg] log file exists     : {os.path.isfile(_log)}")
        print(f"[neg] env to set          : HIVEMIND_TEST_OMIT_MMPROJ={args.omit!r} (scoped: planner keeps its projector)")
        print("[neg] sequence            : stop -> env set -> start -> image run -> env cleared -> start -> /health flag check")
        print("[neg] graded after the run: [DUO][VISION-GATE] log line (post-load gate - the")
        print("                              attach check runs before the coder load in lazy mode and")
        print("                              only sees vision_active=None), [TEST] CMD line, coder")
        print("                              round-1 text without a visual claim")
        return 0

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
        round1 = []
        saw_tool = False
        for d in events:
            if d.get("type") in ("token", "duo_coder_token") and not saw_tool:
                round1.append(str(d.get("content", "")))
            elif d.get("type") == "tool_call":
                saw_tool = True
        round1_text = "".join(round1)
        done = next((d for d in reversed(events) if d.get("type") == "done"), None)
        skipped = any("image skipped" in s or "falling back to the image description" in s
                      for s in statuses)

        # WHICH GATE FIRED (2026-10-04, Sonnet #3): in lazy mode the
        # attach-time check runs BEFORE the coder load and only sees
        # vision_active=None (trusts the plan). Only the POST-LOAD gate can
        # know vision_active:false - its log line is the actual evidence.
        _log = os.path.join(args.install, "logs", "hivemind.log")
        gate_lines, test_lines, toolloop_lines, retract_lines = [], [], [], []
        try:
            with open(_log, encoding="utf-8", errors="replace") as fh:
                for ln in fh.readlines()[-4000:]:
                    if "[DUO][VISION-GATE]" in ln:
                        gate_lines.append(ln.strip()[:160])
                        if "note retracted" in ln:
                            retract_lines.append(ln.strip()[:160])
                    elif "[TEST] HIVEMIND_TEST_OMIT_MMPROJ" in ln:
                        test_lines.append(ln.strip()[:160])
                    elif "[TOOL-LOOP]" in ln and "images_in_request=" in ln \
                            and args.omit.lower() in ln.lower():
                        toolloop_lines.append(ln.strip()[:160])
        except OSError as _le:
            print(f"[neg] WARNING: cannot read install log: {_le}")

        # /props AT THE CODER PORT (2026-10-04, Sonnet #3): the server must
        # really run without modalities.vision while the plan said raw -
        # found by scanning the fixed llama port range for the coder model.
        coder_props = None
        for _p in (8101, 8102, 8103, 8104):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{_p}/props", timeout=3) as r:
                    _pp = json.loads(r.read().decode("utf-8", "replace"))
                if args.omit.lower() in str(_pp.get("model_path", "")).lower():
                    coder_props = (_p, (_pp.get("modalities") or {}).get("vision"))
                    break
            except (OSError, ValueError):
                continue
        # VISUAL-CLAIM CHECK (2026-10-04, Sonnet #3): without image and
        # without a description the coder must not CLAIM to see anything.
        # (The code itself MAY legitimately appear via the planner text -
        # the planner keeps its projector by design - so it is reported,
        # not failed.)
        claims = find_visual_claims(round1_text)
        code_in_round1 = args.code.upper() in round1_text.upper()

        print("\n== STATUSES ==")
        for s in statuses[:12]:
            print("  ", s)
        print("\n== ROUND-1 CODER TEXT (before first tool_call) ==")
        print("  " + (round1_text.strip()[:400] or "(none before tools)"))
        print("\n== GATE LOG EVIDENCE ==")
        for t in test_lines[:2]:
            print("  ", t)
        for g in gate_lines[:4]:
            print("  ", g)
        for rl in retract_lines[:2]:
            print("  ", rl)
        print("== TOOL-LOOP ROUNDS (outgoing payload counts) ==")
        for tl in toolloop_lines[:8]:
            print("  ", tl)
        print("== CODER /props ==")
        print("  ", coder_props)
        print("== DONE ==", done and done.get("stop_reason"))
        _toolloop_all_zero = bool(toolloop_lines) and all(
            "images_in_request=0" in tl for tl in toolloop_lines)
        checks = {
            "run completed (text-only fallback)": bool(done) and done.get("stop_reason") == "completed",
            "post-load gate fired ([DUO][VISION-GATE] in log)": bool(gate_lines),
            "[VISION] note retracted (log line, was present=True)": bool(retract_lines),
            "[TEST] omit line in log (server ran without projector)": bool(test_lines),
            "TOOL-LOOP rounds all images_in_request=0": _toolloop_all_zero,
            "coder /props vision is False during run": coder_props is not None
                                                          and coder_props[1] is False,
            "image downgraded/skipped for coder": skipped,
            "round-1 makes NO visual claim": not claims,
        }
        print("\n== VERDICT ==")
        for k, v in checks.items():
            print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        print(f"  [INFO] code in round-1 text: {code_in_round1} "
              f"(plan-inheritance is legitimate - the planner keeps its projector)")
        return 0 if all(checks.values()) else 1
    finally:
        os.environ.pop("HIVEMIND_TEST_OMIT_MMPROJ", None)
        print("\n[neg] restoring clean install (env cleared, restarting) ...")
        _stop(int(args.base.rsplit(":", 1)[-1]))
        time.sleep(3)
        _start(args.install)
        _clean_ok = False
        for _ in range(40):
            if _health(args.base):
                _clean_ok = True
                break
            time.sleep(3)
        # NO returns here (finally) - failures are reported loudly instead,
        # so they cannot silently override the test's own exit code.
        if not _clean_ok:
            print("[neg] FAIL-CLEANUP: clean install did not come back up - check manually!")
        else:
            # POST-CLEANUP PROOF (2026-10-04, Sonnet #2): the restarted
            # process must carry NO test env - /health exposes it. A failed
            # cleanup would otherwise leave HiveMind silently running
            # without projectors.
            try:
                with urllib.request.urlopen(args.base.rstrip("/") + "/health", timeout=5) as r:
                    _h = json.loads(r.read().decode())
                _flag = _h.get("test_omit_mmproj")
                print(f"[neg] /health test_omit_mmproj after cleanup: {_flag!r}")
                if _flag:
                    print("[neg] FAIL-CLEANUP: test env still active in the restarted install!")
                else:
                    print("[neg] PASS: clean install runs without the test switch.")
            except (OSError, ValueError) as _he:
                print(f"[neg] WARN: could not read /health for the flag check: {_he}")
            print("[neg] next real load must show --mmproj again "
                  "(grep 'CMD:' logs/llama_server_*.log | tail).")


if __name__ == "__main__":
    sys.exit(main())
