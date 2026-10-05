# -*- coding: utf-8 -*-
"""Direct vision control, no HiveMind in the loop (2026-10-04, Sonnet #1+#4).

Starts its OWN llama-server on a private port with the SAME model, the SAME
projector resolution and the SAME flags as the real planner CMD line, asks
the test code question, and kills the process tree in `finally`.

  - image probe reads the code   -> the pipeline loses it (plan text/window)
  - image probe cannot read it   -> model or glyph size is the problem
  - no-image control names it    -> the pattern is hallucinatable (bad test)

Model/mmproj come from HiveMind's own resolvers (resolve_model_path +
resolve_mmproj_strict) run against the INSTALL - never a directory glob,
so the wrong-projector bug class cannot sneak in here.

Wire parity with the UI: the image is 1280x720, so the browser's
_downscaleImage passes it through UNCHANGED (it only re-encodes above
1568px / gif+webp) - the base64 the script sends is byte-identical to
what the UI would send.

Dry run (no GPU): --dry-run resolves everything, writes the PNG next to
this script as _direct_vision_preview.png and prints the exact CMD.
EYEBALL that PNG before the real run - a human must be able to read it.

HiveMind itself must be STOPPED for the real run (8 GB VRAM).

Usage:
  python tests/direct_vision_check.py [--install ...\\HiveMind\\live]
                                      [--dry-run] [--port 8199]
Exit 0 only when: image probe names the code, control does not.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_image import build_png  # noqa: E402

_ALPHABET = "0123456789ABCDEFXYZ"  # every char has a 5x7 glyph
# FLAGS PARITY (2026-10-04, Sonnet): copied from the real planner CMD line
# in logs/llama_server_8101.log (vision behaviour depends on ctx/batch).
_BASE_FLAGS = ["--n-gpu-layers", "99", "--parallel", "1", "--flash-attn", "on",
               "--batch-size", "1024", "--ubatch-size", "256",
               "--threads", "16", "--threads-batch", "8",
               "--split-mode", "none", "--cache-reuse", "512",
               "--device", "VULKAN0", "--load-mode", "mmap+mlock",
               "--cache-type-k", "q4_0", "--cache-type-v", "q4_0", "--jinja"]
PLANNER_MODEL = "gemma-4:e4b-uncensored-hauhaucs-aggressive"


def _import_resolvers(install: str):
    """Import HiveMind's own model resolvers against the INSTALL context."""
    sys.path.insert(0, install)
    os.chdir(install)
    from backend.llama_models import resolve_model_path as _rmp
    from backend.llama_manager_utils import resolve_mmproj_strict as _rms
    return _rmp, _rms


def _ask(port: int, png: bytes | None, question: str, timeout: float = 600.0):
    content = []
    if png:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64,"
                                               + base64.b64encode(png).decode()}})
    content.append({"type": "text", "text": question})
    body = {"model": "vision-check",
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0, "max_tokens": 1024, "stream": False}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    choice = (out.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    return {
        "content": str(msg.get("content") or ""),
        "reasoning": str(msg.get("reasoning_content") or ""),
        "finish_reason": choice.get("finish_reason"),
    }


def _wait_health(port: int, deadline_s: float = 180.0) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:  # not up yet - backoff and retry
            time.sleep(1.0)
        time.sleep(1.0)
    return False


def _port_free(port: int) -> bool:
    # TCP-connect check, NOT HTTP (2026-10-04, harness test finding): a
    # listening-but-deaf socket answers at kernel level without HTTP -
    # urlopen timed out and misreported the port as free.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _stop_tree(proc: subprocess.Popen) -> None:
    """Kill the llama-server AND its children (2026-10-04, Sonnet #2): an
    orphaned child keeps holding the VRAM. taskkill /T walks the tree;
    pinned by tests/test_smoke_harness.py against a dummy process tree."""
    if proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "HiveMind", "live"))
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--code", default="", help="override; default = random per run")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve everything, write the PNG, print the CMD - no GPU")
    args = ap.parse_args()

    from smoke_image import _GLYPHS
    _unknown = sorted(set(args.code.upper()) - set(_GLYPHS) - {" "}) if args.code else []
    if _unknown:
        print(f"[direct] ERROR: --code has glyphs without a bitmap: {_unknown} "
              f"(allowed: {''.join(sorted(k for k in _GLYPHS if k != ' '))})")
        return 2
    code = (args.code or "".join(secrets.choice(_ALPHABET) for _ in range(5))).upper()
    png = build_png(code)
    import tempfile
    preview = os.path.join(tempfile.gettempdir(), "_direct_vision_preview.png")
    with open(preview, "wb") as fh:
        fh.write(png)

    try:
        _rmp, _rms = _import_resolvers(args.install)
        gguf = _rmp(PLANNER_MODEL)
        mmproj = _rms(PLANNER_MODEL)
    except (ImportError, OSError, AttributeError) as _re:
        print(f"[direct] resolver failed: {type(_re).__name__}: {_re}")
        return 2
    server_bins = sorted(glob.glob(
        os.path.join(args.install, "llama", "*", "llama-server.exe")))
    if not (server_bins and gguf):
        print(f"[direct] missing inputs: llama-server={bool(server_bins)} "
              f"model={gguf!r}")
        return 2

    cmd = [server_bins[-1], "--model", str(gguf), "--port", str(args.port),
           "--ctx-size", str(args.ctx)] + _BASE_FLAGS
    if mmproj:
        cmd += ["--mmproj", str(mmproj)]
    print(f"[direct] code={code!r} (random per run)")
    print(f"[direct] PNG preview written: {preview}  <- EYEBALL IT: is the code readable?")
    print(f"[direct] model : {gguf}")
    print(f"[direct] mmproj: {mmproj}  (resolve_mmproj_strict - HiveMind's own resolver)")
    print(f"[direct] CMD   : {' '.join(cmd)}")
    if args.dry_run:
        print("[direct] dry run - nothing started, no GPU touched.")
        return 0

    if not _port_free(args.port):
        print(f"[direct] ABORT: port {args.port} is already answering - free it first.")
        return 2

    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    question = "Which exact code/letters are written in this image? Answer with the code only."
    try:
        if not _wait_health(args.port):
            print("[direct] FAIL: llama-server did not come up (check its console window)")
            return 2
        answer = _ask(args.port, png, question)
        control = _ask(args.port, None, question)
        combined_img = (answer["content"] + " " + answer["reasoning"]).upper()
        combined_ctl = (control["content"] + " " + control["reasoning"]).upper()
        print("\n[direct] IMAGE probe:")
        print("  finish_reason:", answer["finish_reason"])
        print("  content  :", answer["content"].strip()[:300] or "(empty)")
        if answer["reasoning"]:
            print("  reasoning:", answer["reasoning"].strip()[:200])
        print("[direct] NO-IMAGE control:")
        print("  finish_reason:", control["finish_reason"])
        print("  content  :", control["content"].strip()[:300] or "(empty)")

        if not combined_img.strip() and answer["finish_reason"] == "length":
            print("\n[direct] INCONCLUSIVE: model spent the whole budget thinking "
                  "(finish_reason=length, empty content) - raise max_tokens and retry.")
            return 2
        reads = code in combined_img
        hallucinated = code in combined_ctl
        print("\n== VERDICT ==")
        print(f"  [{'PASS' if reads else 'FAIL'}] model reads the code from the image")
        print(f"  [{'PASS' if not hallucinated else 'FAIL'}] control (no image) does NOT name the code")
        if reads and not hallucinated:
            print("  pipeline verdict: the PIPELINE loses the code (model can read)")
        elif not reads:
            print("  pipeline verdict: the MODEL/GLYPHS are the problem")
        return 0 if (reads and not hallucinated) else 1
    finally:
        _stop_tree(proc)
        print(f"[direct] llama-server on port {args.port} stopped (tree).")


if __name__ == "__main__":
    sys.exit(main())
