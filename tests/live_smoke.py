# -*- coding: utf-8 -*-
"""Live smoke: Direct answer + Duo tool-layer file write.

Prerequisites:
- the engine is running (`python run.py`, default http://127.0.0.1:8001)
- enough free VRAM for the chosen coder model
- `settings.json` may have `duo_tool_rounds=0`; this script pins 8 per request

Run:
    python tests/live_smoke.py
    python tests/live_smoke.py --skip-duo
    python tests/live_smoke.py --coder-model qwen3.5:4b-mtp --ctx 16384

Exit 0 = both passed, 1 = failed. No benchmark, no task corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stream(base: str, body: dict, timeout_s: int = 600) -> tuple[list[dict], str]:
    events: list[dict] = []
    tokens: list[str] = []
    with httpx.stream(
        "POST", f"{base}/stream", json=body,
        timeout=httpx.Timeout(connect=10, read=timeout_s, write=10, pool=5),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            events.append(ev)
            if ev.get("type") == "token":
                tokens.append(str(ev.get("content") or ""))
    return events, "".join(tokens)


def _done(events: list[dict]) -> dict:
    for ev in reversed(events):
        if ev.get("type") == "done":
            return ev
    return {}


def run_direct(base: str, model: str) -> tuple[bool, str]:
    body = {
        "q": "Reply with exactly: OK",
        "mode": "chat",
        "force_complexity": "simple",
        "direct_model": model,
        "max_tokens": 64,
    }
    t0 = time.time()
    events, text = _stream(base, body, timeout_s=300)
    done = _done(events)
    ok = done.get("stop_reason") == "completed" and "OK" in text.upper()
    return ok, (f"direct: stop={done.get('stop_reason')} "
                f"text={text[:80]!r} {time.time() - t0:.1f}s")


def run_duo(base: str, args) -> tuple[bool, str]:
    ws = tempfile.mkdtemp(prefix="hm_live_duo_")
    body = {
        "q": ("Create hello.py with a function add(a, b) that returns a+b. "
              "Use write_file (the file does not exist yet). Then verify with run_bash: "
              "python -c \"import hello; assert hello.add(2, 3) == 5\". "
              "Then call task_complete with a short status."),
        "mode": "code_duo",
        "force_complexity": "code_duo",
        "workspace": ws,
        "duo_pre_explore": False,
        "duo_planner_model": args.planner_model,
        "duo_coder_model": args.coder_model,
        "duo_critic_model": args.critic_model,
        "duo_planner": 0,
        "duo_chunking": 0,
        "duo_agentic_mode": 1,
        "duo_coder_tool_tier": "core",
        "duo_tool_rounds": 8,
        "duo_max_tool_rounds": 8,
        "duo_coder_ctx_normal": args.ctx,
        "duo_coder_ctx_agentic": args.ctx,
        # The per-round output budget comes from agents.duo_coder.max_tokens
        # (there is no request-level override). With 8k ctx and a 16k agent
        # budget every round trips the CTX-FULL guard; 16k is the loaded slot.
        "duo_test_feedback_chunk": 0,
        "duo_test_feedback_final": 0,
        "until_finished": 0,
    }
    t0 = time.time()
    events, _text = _stream(base, body, timeout_s=args.timeout)
    done = _done(events)
    files = os.listdir(ws)
    target = Path(ws) / "hello.py"
    content = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    ok = (done.get("stop_reason") == "completed" and target.exists()
          and "def add" in content and "return" in content)
    detail = (f"duo: stop={done.get('stop_reason')} files={files} "
              f"hello={content[:120]!r} {time.time() - t0:.1f}s")
    if not ok:
        reasons = [e.get("content") for e in events
                   if e.get("type") == "status" and isinstance(e.get("content"), str)
                   and any(k in e["content"] for k in ("⛔", "unreachable", "Pre-Explore"))]
        if reasons:
            detail += " | " + " || ".join(reasons[-3:])
    return ok, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--direct-model", default="lfm3:1.7b-qad")
    ap.add_argument("--coder-model", default="qwen3.5:4b-mtp")
    ap.add_argument("--planner-model", default="qwen3.5:2b")
    ap.add_argument("--critic-model", default="qwen3.5:2b")
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--skip-duo", action="store_true")
    args = ap.parse_args()

    try:
        health = httpx.get(f"{args.base}/health", timeout=5).json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"FAIL health: engine not reachable at {args.base}: {exc}")
        return 1
    if health.get("status") != "ok":
        print(f"FAIL health: {health}")
        return 1
    print(f"PASS health: {health.get('version')}")

    ok, detail = run_direct(args.base, args.direct_model)
    print(("PASS " if ok else "FAIL ") + detail)
    if not ok:
        return 1
    if args.skip_duo:
        print("SKIP duo (--skip-duo)")
        return 0

    ok, detail = run_duo(args.base, args)
    print(("PASS " if ok else "FAIL ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
