#!/usr/bin/env python
"""Warm latency of the read-aloud chunks (what the browser waits for).

Run against the running engine: python tools/bench_tts_latency.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8001"
TEXTS = {
    "first chunk (60 chars)": "The planner split the work into three parts, then",
    "one sentence (150)": ("The planner split the work into three parts, then it read "
                           "the failing test and patched the runner."),
    "old whole answer (320)": ("A longer answer with several sentences that the UI "
                               "used to synthesise in one request. " * 5)[:320],
}


def once(text: str) -> tuple[float, float]:
    t0 = time.time()
    req = urllib.request.Request(
        f"{BASE}/tts", data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        dur = float(resp.headers.get("X-HiveMind-Duration") or 0)
        resp.read()
    return time.time() - t0, dur


def main() -> int:
    print("cold (first call starts the sidecar):")
    dt, dur = once(TEXTS["first chunk (60 chars)"])
    print(f"  {dt:5.1f}s for {dur:.1f}s of audio")
    print("warm:")
    for label, text in TEXTS.items():
        dt, dur = once(text)
        print(f"  {label:24} {len(text):3} chars -> {dur:5.1f}s audio in {dt:5.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
