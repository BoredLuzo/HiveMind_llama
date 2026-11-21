"""Regression tests for headless destructive_gate/ask_user fast-deny,
abort-aware wait_for_resume wake-up, and bench llama warmup.

Prevents the 2026-11-21 benchmark failure modes:
1. Destructive commands in headless/benchmark runs (e.g. find __pycache__ -exec rm -rf)
   must fail-closed immediately without pausing 600s and stalling the top run lock.
2. Interactive pauses in wait_for_resume (_destructive_gate / ask_user) must wake
   immediately on /abort?chat_id=... or /abort/{run_id} instead of waiting for timeout_s.
3. bench/runner.py pins headless=True in _hivemind_body and warms up llama-server
   with a 1-token ping before cell #1.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra import run_control as rc  # noqa: E402
from tools import runner as tr  # noqa: E402
from bench.runner import Config, Task, _hivemind_body, _warmup_llama  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        msg = f"{name}" + (f" — {detail}" if detail else "")
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


async def test_wait_for_resume_abort_wake() -> None:
    print("-- 1. run_control.wait_for_resume wakes immediately on abort --")
    chat_id = "deadbeef1234"
    run_id = "run-abort-wake-1"
    rc._register_abort(run_id)
    await rc.claim_chat_run(chat_id, run_id)
    await rc.initiate_pause(run_id, "Confirm?")

    async def _trigger_abort():
        await asyncio.sleep(0.05)
        await rc.abort_chat_run(chat_id)

    t0 = time.monotonic()
    abort_task = asyncio.create_task(_trigger_abort())
    ans = await rc.wait_for_resume(run_id, timeout_s=60)
    elapsed = time.monotonic() - t0
    await abort_task
    rc._unregister_abort(run_id)

    check("abort wakes wait_for_resume quickly (<1s vs 60s)", elapsed < 1.0, f"elapsed={elapsed:.3f}s")
    check("abort returns ABORTED sentinel", ans.startswith("[ask_user ABORTED"), f"ans={ans!r}")
    check("pause event cleaned up after abort", not rc.is_pause_waiting(run_id))

    # Pre-aborted run_id returns immediately without sleeping
    run_id2 = "run-abort-wake-2"
    ev2 = rc._register_abort(run_id2)
    ev2.set()
    await rc.initiate_pause(run_id2, "Confirm?")
    t0 = time.monotonic()
    ans2 = await rc.wait_for_resume(run_id2, timeout_s=60)
    elapsed2 = time.monotonic() - t0
    rc._unregister_abort(run_id2)
    check("pre-aborted wait_for_resume returns immediately", elapsed2 < 0.2 and ans2.startswith("[ask_user ABORTED"), f"elapsed={elapsed2:.3f}s ans={ans2!r}")

    # Normal interactive answer still works identically
    run_id3 = "run-normal-answer-3"
    rc._register_abort(run_id3)
    await rc.initiate_pause(run_id3, "Confirm?")

    async def _answer_soon():
        await asyncio.sleep(0.03)
        rc.set_user_answer(run_id3, "yes")

    t_ans = asyncio.create_task(_answer_soon())
    ans3 = await rc.wait_for_resume(run_id3, timeout_s=60)
    await t_ans
    rc._unregister_abort(run_id3)
    check("normal user answer still resolves cleanly", ans3 == "yes", f"ans={ans3!r}")


async def test_headless_destructive_gate_and_ask_user() -> None:
    print("-- 2. headless fast fail-closed in _destructive_gate and ask_user --")
    run_id = "run-headless-1"
    tok_rid = tr._current_run_id.set(run_id)
    tok_snap = tr._run_settings_snapshot.set({"headless": True})
    tok_to = tr._pause_timeout_s.set(600)

    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("hivemind.tools")
    cap = _Cap()
    logger.addHandler(cap)
    try:
        t0 = time.monotonic()
        res = await tr._destructive_gate("run_bash", {"cmd": "find . -name __pycache__ -exec rm -rf {} +"})
        elapsed = time.monotonic() - t0

        check("headless destructive_gate fails closed immediately (<0.2s)", elapsed < 0.2, f"elapsed={elapsed:.3f}s")
        check("headless destructive_gate returns DECLINED block",
              res is not None and "[DESTRUCTIVE GATE: DECLINED]" in res and "headless" in res.lower(),
              f"res={res!r}")
        check("headless destructive_gate never registers pending approval",
              run_id not in tr._pending_approvals and not rc.is_pause_waiting(run_id))
        check("headless destructive_gate logs WARNING with (headless)",
              any(r.levelno == logging.WARNING and "(headless)" in r.getMessage() for r in records))

        # Non-destructive bash still passes through cleanly in headless mode
        safe_res = await tr._destructive_gate("run_bash", {"cmd": "python -m pytest -q"})
        check("headless allows non-destructive commands", safe_res is None, f"safe_res={safe_res!r}")

        # ask_user in headless mode returns immediately without pausing
        t0 = time.monotonic()
        ask_res = await tr._run_inline_tool("ask_user", {"question": "Which style?"})
        ask_elapsed = time.monotonic() - t0
        check("headless ask_user returns immediately without pausing",
              ask_elapsed < 0.2 and "headless" in ask_res.lower() and not rc.is_pause_waiting(run_id),
              f"elapsed={ask_elapsed:.3f}s res={ask_res!r}")
    finally:
        logger.removeHandler(cap)
        tr._pause_timeout_s.reset(tok_to)
        tr._run_settings_snapshot.reset(tok_snap)
        tr._current_run_id.reset(tok_rid)


async def test_interactive_destructive_gate_abort() -> None:
    print("-- 3. interactive _destructive_gate wakes on abort and cleans pending card --")
    chat_id = "cafebabe5678"
    run_id = "run-interactive-abort-1"
    rc._register_abort(run_id)
    await rc.claim_chat_run(chat_id, run_id)

    tok_rid = tr._current_run_id.set(run_id)
    tok_snap = tr._run_settings_snapshot.set({"headless": False})
    tok_to = tr._pause_timeout_s.set(600)

    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("hivemind.tools")
    cap = _Cap()
    logger.addHandler(cap)
    try:
        async def _abort_soon():
            await asyncio.sleep(0.05)
            await rc.abort_chat_run(chat_id)

        t0 = time.monotonic()
        t_ab = asyncio.create_task(_abort_soon())
        res = await tr._destructive_gate("run_bash", {"cmd": "rm -rf ./build"})
        elapsed = time.monotonic() - t0
        await t_ab

        check("interactive destructive_gate wakes on abort (<1s)", elapsed < 1.0, f"elapsed={elapsed:.3f}s")
        check("interactive destructive_gate denies on abort",
              res is not None and "[DESTRUCTIVE GATE: DECLINED]" in res,
              f"res={res!r}")
        check("pending approval card cleaned up on abort", run_id not in tr._pending_approvals)
        check("abort logged with (aborted) classification",
              any(r.levelno == logging.WARNING and "(aborted)" in r.getMessage() for r in records))
    finally:
        logger.removeHandler(cap)
        tr._pause_timeout_s.reset(tok_to)
        tr._run_settings_snapshot.reset(tok_snap)
        tr._current_run_id.reset(tok_rid)
        rc._unregister_abort(run_id)


async def test_bench_headless_pin_and_warmup() -> None:
    print("-- 4. bench/runner.py headless pin, server.py wiring & warmup ping --")
    task = Task("t1", "fix bug", "pytest")
    body_duo = _hivemind_body(task, Path("."), duo=True, model="qwen3.6:35b-a3b-mtp", ctx_tokens=122880)
    body_dir = _hivemind_body(task, Path("."), duo=False, model="qwen3.6:35b-a3b-mtp", ctx_tokens=122880)
    check("_hivemind_body sets headless=True for duo", body_duo.get("headless") is True)
    check("_hivemind_body sets headless=True for direct", body_dir.get("headless") is True)

    srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("server.py forwards headless body flag to _model_overrides",
          '_model_overrides["headless"]' in srv_src)

    posted: list[tuple[str, dict]] = []

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posted.append((url, json or {}))
            return _FakeResp()

    cfg = Config(tasks_dir=Path("bench/tasks"), out=Path("out.json"), model="qwen3.6:35b-a3b-mtp", llama_url="http://127.0.0.1:8101")
    with patch("bench.runner._active_llama_url", return_value="http://127.0.0.1:8101"), \
         patch("bench.runner.httpx.AsyncClient", _FakeClient):
        await _warmup_llama(cfg, ["hivemind_duo", "aider"])

    check("_warmup_llama sends 1-token chat completion ping",
          len(posted) == 1
          and posted[0][0] == "http://127.0.0.1:8101/v1/chat/completions"
          and posted[0][1].get("max_tokens") == 1
          and posted[0][1].get("model") == "qwen3.6:35b-a3b-mtp",
          f"posted={posted!r}")

    # Skip warmup when remote aider-only
    posted.clear()
    with patch("bench.runner._aider_remote_base", return_value="https://openrouter.ai/api/v1"), \
         patch("bench.runner.httpx.AsyncClient", _FakeClient):
        await _warmup_llama(cfg, ["aider"])
    check("_warmup_llama skips remote aider-only runs", len(posted) == 0)


async def main() -> int:
    await test_wait_for_resume_abort_wake()
    await test_headless_destructive_gate_and_ask_user()
    await test_interactive_destructive_gate_abort()
    await test_bench_headless_pin_and_warmup()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
