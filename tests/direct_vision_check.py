# -*- coding: utf-8 -*-
"""Direct vision control, no HiveMind in the loop (2026-10-04, Sonnet #1+#4).

The chicken-and-egg fix: this script STARTS its own llama-server (model +
mmproj from the models dir) on a private port and kills it in `finally` -
nothing else has to be running, and HiveMind must be STOPPED so the two do
not fight over 8 GB of VRAM.

It answers exactly one question: can the planner model read the test code
from the image at all?
  - image probe reads the code   -> the pipeline loses it (plan text/window)
  - image probe cannot read it   -> model or glyph size is the problem
A no-image control (same question, temperature 0) must NOT contain the
code - a hit there would mean the "code" is hallucinatable, i.e. the test
image pattern is too guessable. That is why the code is RANDOM per run.

Wire parity with the UI: the image is 1280x720, so the browser's
_downscaleImage passes it through UNCHANGED (it only re-encodes above
1568px / gif+webp) - the base64 the script sends is byte-identical to
what the UI would send.

Usage:
  python tests/direct_vision_check.py [--install ...\\HiveMind\\live]
                                      [--port 8199] [--ctx 8192]
Exit 0 only when BOTH hold: image probe names the code, control does not.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_image import build_png  # noqa: E402

_ALPHABET = "0123456789ABCDEFXYZ"  # every char has a 5x7 glyph


def _ask(port: int, png: bytes | None, question: str, timeout: float = 300.0) -> str:
    content = []
    if png:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64,"
                                               + base64.b64encode(png).decode()}})
    content.append({"type": "text", "text": question})
    body = {"model": "vision-check",
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0, "max_tokens": 200, "stream": False}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    return (out.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _wait_health(port: int, deadline_s: float = 120.0) -> bool:
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "HiveMind", "live"))
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--code", default="", help="override; default = random per run")
    args = ap.parse_args()

    code = (args.code or "".join(secrets.choice(_ALPHABET) for _ in range(5))).upper()
    png = build_png(code)

    server = os.path.join(args.install, "llama")
    bins = sorted(glob.glob(os.path.join(server, "*", "llama-server.exe")))
    models_dir = None
    try:
        with open(os.path.join(args.install, "settings.json"), encoding="utf-8") as fh:
            models_dir = json.load(fh).get("models_dir")
    except (OSError, json.JSONDecodeError) as _se:
        print(f"[direct] could not read settings.json ({_se}) - pass --model/--mmproj manually.")
        return 2
    model = mmproj = None
    if models_dir:
        model = (glob.glob(os.path.join(models_dir, "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-*.gguf"))
                 or [None])[0]
        model = model or (glob.glob(os.path.join(models_dir, "*Gemma-4-E4B*.gguf")) or [None])[0]
        if model and "mmproj" not in os.path.basename(model):
            mmproj = (glob.glob(os.path.join(models_dir, "mmproj-Gemma-4-E4B-Uncensored*.gguf"))
                      or [None])[0]
    if not (bins and model):
        print(f"[direct] missing inputs: llama-server={bool(bins)} model={model!r} "
              f"(models_dir={models_dir!r})")
        return 2

    cmd = [bins[-1], "--model", model, "--port", str(args.port),
           "--ctx-size", str(args.ctx), "--n-gpu-layers", "99", "--parallel", "1",
           "--flash-attn", "on", "--threads", "8",
           "--device", "VULKAN0", "--no-webui"]
    if mmproj:
        cmd += ["--mmproj", mmproj]
    print(f"[direct] code={code!r} starting {os.path.basename(bins[-1])}")
    print(f"[direct]   model={os.path.basename(model)}")
    print(f"[direct]   mmproj={os.path.basename(mmproj) if mmproj else 'NONE - probe will FAIL'}")
    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    question = "Which exact code/letters are written in this image? Answer with the code only."
    try:
        if not _wait_health(args.port):
            print("[direct] FAIL: llama-server did not come up")
            return 2
        answer = _ask(args.port, png, question)
        control = _ask(args.port, None, question)
        print("\n[direct] IMAGE probe answer:\n  " + answer.strip()[:300])
        print("[direct] NO-IMAGE control answer:\n  " + control.strip()[:300])
        reads = code in answer.upper()
        hallucinated = code in control.upper()
        print("\n== VERDICT ==")
        print(f"  [{'PASS' if reads else 'FAIL'}] model reads the code from the image")
        print(f"  [{'PASS' if not hallucinated else 'FAIL'}] control (no image) does NOT name the code")
        print(f"  pipeline verdict: {'the PIPELINE loses the code (model can read)'
                                      if reads and not hallucinated else
                                      'the MODEL/GLYPHS are the problem' if not reads else 'test inconclusive'}")
        return 0 if (reads and not hallucinated) else 1
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        print(f"[direct] llama-server on port {args.port} stopped.")


if __name__ == "__main__":
    sys.exit(main())
