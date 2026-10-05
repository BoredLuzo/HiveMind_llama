# -*- coding: utf-8 -*-
"""Vision smoke test over the live API (2026-10-04, Sonnet gate round).

Builds a PNG with verifiable ground truth (colored regions + an ASCII code
rendered from a 5x7 bitmap font - no Pillow needed), fires ONE code_duo run
against a running HiveMind (default http://localhost:8001) and prints the
evidence Sonnet's gate asks for:

  - planner/coder CMD --mmproj lines and /props modalities (from the logs)
  - images_in_request traces (planner POST + coder rounds + [LLM-CALL])
  - the planner's image description vs. ground truth
  - the coder's ROUND-1 answer BEFORE the first tool_call (the vision
    proof: tools could read the saved file or inherit the plan text - the
    pre-tool answer cannot)

Usage:
  python tests/smoke_image.py [--base http://localhost:8001] [--code XY42]
                              [--timeout 1200]

Exit code 0 when the run completed and the round-1 answer names the code.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import urllib.request
import zlib

# ── ground truth the grader checks against ─────────────────────────────────
COLORS = {"red": (192, 57, 43), "blue": (36, 113, 163),
          "yellow": (241, 196, 15), "white": (255, 255, 255)}

_GLYPHS = {
    "0": ("0111", "1001", "1011", "1101", "1001", "0111"),
    "1": ("0010", "0110", "0010", "0010", "0010", "0111"),
    "2": ("0111", "1001", "0001", "0010", "0100", "1111"),
    "3": ("1111", "0001", "0111", "0001", "1001", "0111"),
    "4": ("0011", "0101", "1001", "1111", "0001", "0001"),
    "5": ("1111", "1000", "1110", "0001", "1001", "0111"),
    "6": ("0111", "1000", "1110", "1001", "1001", "0111"),
    "7": ("1111", "0001", "0010", "0100", "0100", "0100"),
    "8": ("0111", "1001", "0111", "1001", "1001", "0111"),
    "9": ("0111", "1001", "0111", "0001", "0001", "0111"),
    "A": ("0111", "1001", "1001", "1111", "1001", "1001"),
    "B": ("1110", "1001", "1110", "1001", "1001", "1110"),
    "C": ("0111", "1000", "1000", "1000", "1000", "0111"),
    "D": ("1110", "1001", "1001", "1001", "1001", "1110"),
    "E": ("1111", "1000", "1110", "1000", "1000", "1111"),
    "F": ("1111", "1000", "1110", "1000", "1000", "1000"),
    "T": ("1111", "0010", "0010", "0010", "0010", "0010"),
    "Q": ("0111", "1001", "1001", "1011", "1101", "0111"),
    "X": ("1001", "1001", "0110", "0110", "1001", "1001"),
    "Y": ("1001", "1001", "0111", "0010", "0100", "0100"),
    "Z": ("1111", "0001", "0010", "0100", "1000", "1111"),
    " ": ("0000", "0000", "0000", "0000", "0000", "0000"),
}


def build_png(code: str) -> bytes:
    """1280x720: red top half, blue bottom half, yellow diagonal stripe,
    white center square, and the code in black glyphs on a WHITE PLATE
    (2026-10-04, eyeball finding: the stripe used to cut through the
    glyphs and made characters ambiguous - the plate sits above stripe
    and regions, glyphs stay readable)."""
    W, H = 1280, 720
    scale = 10
    glyph_w = 4 * scale + scale
    code = " " + code + " "
    x0, y0 = 40, 40
    text_pixels = set()
    for gi, ch in enumerate(code):
        g = _GLYPHS.get(ch.upper(), _GLYPHS[" "])
        for gy, row in enumerate(g):
            for gx, bit in enumerate(row):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            text_pixels.add((x0 + gi * glyph_w + gx * scale + sx,
                                             y0 + gy * scale + sy))
    xs = [p[0] for p in text_pixels]
    ys = [p[1] for p in text_pixels]
    plate = set()
    if text_pixels:
        px0, px1 = min(xs) - 3 * scale, max(xs) + 3 * scale
        py0, py1 = min(ys) - 2 * scale, max(ys) + 2 * scale
        for py in range(py0, py1 + 1):
            for px in range(px0, px1 + 1):
                plate.add((px, py))
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            if (x, y) in text_pixels:
                c = (0, 0, 0)
            elif (x, y) in plate:
                c = COLORS["white"]
            elif abs(x - int(y * W / H)) < 35:
                c = COLORS["yellow"]
            elif (W // 2 - 100) <= x < (W // 2 + 100) and (H // 2 - 100) <= y < (H // 2 + 100):
                c = COLORS["white"]
            elif y < H // 2:
                c = COLORS["red"]
            else:
                c = COLORS["blue"]
            row += bytes(c)
        rows.append(bytes(row))
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def post_stream(base: str, payload: dict, timeout: float):
    # CURL BACKEND (2026-10-04): urllib died with ConnectionReset mid-SSE on
    # the first live run (the SERVER kept running - tab-kill tolerance - but
    # the evidence was lost). curl -N streams to a file and survives; events
    # are parsed from the file afterwards.
    import subprocess
    import tempfile
    import os
    proc = subprocess.run(
        ["curl", "-sN", "--max-time", str(int(timeout)), "-X", "POST",
         base.rstrip("/") + "/stream",
         "-H", "Content-Type: application/json",
         "--data", json.dumps(payload),
         "-o", tempfile.gettempdir() + os.sep + "_smoke_sse.txt",
         "-w", "%{http_code}"],
        capture_output=True, text=True, timeout=timeout + 30)
    events = []
    try:
        with open(tempfile.gettempdir() + os.sep + "_smoke_sse.txt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("data: "):
                    try:
                        events.append(json.loads(line[6:]))
                    except ValueError:
                        pass
    except OSError:
        pass
    if proc.returncode != 0:
        print(f"[smoke] WARNING: curl exit {proc.returncode} - stream ended early, "
              f"parsing what arrived ({len(events)} events)")
    return events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--code", default="",
                    help="default: RANDOM per run (2026-10-04, Sonnet #1: a fixed "
                         "pattern like the old XY42 default is hallucinatable)")
    ap.add_argument("--timeout", type=float, default=1200)
    ap.add_argument("--chat-id", default=None, help="continue this chat instead of a new one")
    ap.add_argument("--mode", default="code_duo", choices=("code_duo", "chat"),
                    help="code_duo = runs A; chat = run D, the DIRECT image path "
                         "(single model, no tools - the answer itself is the vision proof)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the PNG + payload, write _smoke_preview.png, print the plan - no POST")
    args = ap.parse_args()

    import secrets as _secrets
    _CODE_ALPHABET = "0123456789ABCDEFQTXYZ"
    _code = args.code or "".join(_secrets.choice(_CODE_ALPHABET) for _ in range(5)).upper()
    args.code = _code

    # GLYPH VALIDATION (2026-10-04, eyeball dry-run finding): unknown chars
    # used to render as BLANK SPACES - "--code T7Q2" showed "7 2" and the
    # run then failed for a reason that had nothing to do with vision.
    _unknown = sorted(set(args.code.upper()) - set(_GLYPHS) - {" "})
    if _unknown:
        print(f"[smoke] ERROR: code contains glyphs without a bitmap: {_unknown} "
              f"(allowed: {''.join(sorted(k for k in _GLYPHS if k != ' '))})")
        return 2

    png = build_png(args.code)
    if args.mode == "chat":
        q = ("Which exact code/letters are written in the attached image? "
             "Answer with the code only.")
    else:
        q = ("The attached image has a code written on it. TRANSCRIBE that code "
             "character by character in plain text FIRST, before you use any tool. "
             "Then write ONE file repro.html that reproduces the image "
             "(colored divs + CSS) and put the code in the <title>.")
    payload = {"q": q, "images": [base64.b64encode(png).decode()], "mode": args.mode}
    if args.chat_id:
        payload["chat_id"] = args.chat_id

    import tempfile
    preview = os.path.join(tempfile.gettempdir(), "_smoke_preview.png")
    with open(preview, "wb") as fh:
        fh.write(png)
    if args.dry_run:
        print(f"[smoke] DRY RUN - nothing sent.")
        print(f"[smoke] PNG preview ({len(png)}B): {preview}  <- EYEBALL: code readable?")
        print(f"[smoke] payload: mode={payload['mode']} q={len(q)} chars, "
              f"1 image, code={args.code!r}")
        return 0

    print(f"[smoke] code={args.code!r} image={len(png)}B -> {args.base}/stream")
    events = post_stream(args.base, payload, args.timeout)

    statuses, planner_txt, round1_tokens, done = [], [], [], None
    saw_tool = False
    chat_id = None
    for d in events:
        t = d.get("type")
        if t == "run_id" and not chat_id:
            chat_id = d.get("run_id")
        elif t == "status":
            statuses.append(str(d.get("content", "")))
        elif t in ("token", "duo_coder_token") and not saw_tool:
            round1_tokens.append(str(d.get("content", "")))
        elif t == "tool_call":
            saw_tool = True
        elif t == "planner_result":
            planner_txt = [str(d.get("thinking", "")), str(d.get("plan", ""))
                           + json.dumps(d.get("chunks") or [])]
        elif t == "done":
            done = d
    round1 = "".join(round1_tokens)
    everything = " ".join(planner_txt + [round1]) + " " + " ".join(statuses)

    # IMAGE-PLAN ABORT (2026-10-04, Sonnet #5): vision_model.json enabled on
    # the install silently reroutes the run to the PREPROCESS path (text
    # description) - a PASS would then prove nothing about raw/projector.
    # Code_duo: abort unless the plan says raw/raw. Chat (run D): abort on
    # any preprocess indicator - only the raw direct path counts.
    if args.mode == "code_duo":
        _plan_line = next((s for s in statuses if "duo plan" in s), "")
        print(f"[smoke] image plan: {_plan_line or '(no plan line - abort)'}")
        if "planner: raw, coder: raw" not in _plan_line:
            print("[smoke] ABORT: image plan is NOT raw/raw - this run would not test "
                  "the raw/projector path. Check duo_image_mode + role checkboxes.")
            return 3
    else:
        _prepro = [s for s in statuses if "[Vision" in s or "describes the image" in s]
        if _prepro:
            print("[smoke] ABORT: direct run went the PREPROCESS path "
                  f"({_prepro[0][:80]}) - a PASS would say nothing about raw.")
            return 3

    print("\n== STATUSES ==")
    for s in statuses[:14]:
        print("  ", s)
    print(f"\n== ROUND-1 CODER ANSWER (before first tool_call, {len(round1)} chars) ==")
    print(round1[:600])
    print("\n== DONE ==", done and {k: done.get(k) for k in ("stop_reason", "elapsed")})
    print(f"== chat/run id: {chat_id} ==")

    checks = {
        "run completed": bool(done) and done.get("stop_reason") == "completed",
        "plan says raw/raw": any("planner: raw, coder: raw" in s for s in statuses),
        "round-1 names the code": args.code.upper() in round1.upper(),
        "code anywhere (model+tools)": args.code.upper() in everything.upper(),
    }
    if args.mode == "chat":
        # RUN D (2026-10-04, Sonnet #5): the direct image path has NO tools
        # and NO planner - the answer naming the code IS the vision proof.
        checks = {
            "run completed": bool(done) and done.get("stop_reason") == "completed",
            "answer names the code (no tools to inherit from)":
                args.code.upper() in round1.upper(),
        }
        print("\n== RUN-D MODE: chat (direct path, single model, no tools) ==")
    print("\n== VERDICT ==")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
