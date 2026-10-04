# -*- coding: utf-8 -*-
"""Direct vision control, no HiveMind in the loop (2026-10-04, Sonnet #1).

Cheap disambiguation for "the coder wrote a generic title instead of the
code": POST the SAME test image straight to a llama-server
/v1/chat/completions (planner model, port 8101 by default) and ask only
"which code is in the image?".

  - model reads the code  -> the pipeline loses it (planner text / window)
  - model cannot read it  -> model or glyph size is the problem

Usage (needs ONLY that one llama-server running, HiveMind itself not
required):
  python tests/direct_vision_check.py [--port 8101] [--code XY42]
The image is generated locally with the same 5x7 glyph renderer as
smoke_image.py, so the ground truth is exact.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from smoke_image import build_png  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8101)
    ap.add_argument("--code", default="XY42")
    args = ap.parse_args()

    png = build_png(args.code)
    body = {
        "model": "vision-check",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                {"type": "text",
                 "text": "Which exact code/letters are written in this image? "
                         "Answer with the code only."},
            ],
        }],
        "temperature": 0.0,
        "max_tokens": 200,
        "stream": False,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    print(f"[direct] code={args.code!r} image={len(png)}B -> 127.0.0.1:{args.port}")
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    text = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print("[direct] model answer:\n" + text.strip()[:400])
    hit = args.code.upper() in text.upper()
    print(f"\n[direct] verdict: {'PASS - model reads the code' if hit else 'FAIL - model does NOT read the code (glyph size / model)'}")
    return 0 if hit else 1


if __name__ == "__main__":
    sys.exit(main())
