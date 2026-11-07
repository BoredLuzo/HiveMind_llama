# -*- coding: utf-8 -*-
"""ask_user_auto_proceed tests (W2-3, 2026-11-07).

The UI toggle existed before but only drove the per-card "Auto 30s" timer.
W2-3 wires it into the ENGINE: the ask_user governor timeout now also starts
for non-until-finished runs when ask_user_auto_proceed is on, using a fixed
30 s (AUTO_PROCEED_TIMEOUT_S). Precedence: until_finished keeps its
configured ask_user_timeout_until_finished_seconds. The setting is re-read
at QUESTION time, so a mid-run toggle takes effect without a restart.

Pinned here:
  1. configure_run exposes auto_proceed (bool) for True/False.
  2. Source pin: _handle_ask_user reads ask_user_auto_proceed at question
     time and starts the timeout with 30 s (auto_proceed) — until_finished
     branch keeps precedence in source order.
  3. Functional pin (mocked start_timeout + load_settings): engine starts
     30 s on auto_proceed, configured timeout on until_finished, nothing
     when both off, and the CURRENT value wins over the run-start snapshot.
  4. Unit: start_timeout(run_id, 0, ...) is a no-op; a tiny timeout calls
     infra.run_control.set_user_answer with the auto answer (<1 s).

Run: python tests/test_ask_user_auto_proceed.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def check(name, cond, msg=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {msg}")


# ── 1. configure_run ────────────────────────────────────────────────────────

def test_configure_run_auto_proceed():
    from infra import ask_user_governor as gov
    check("AUTO_PROCEED_TIMEOUT_S == 30", gov.AUTO_PROCEED_TIMEOUT_S == 30,
          str(gov.AUTO_PROCEED_TIMEOUT_S))
    for rid, cfg_over, want in (
        ("aup-cfg-true", {"ask_user_auto_proceed": True}, True),
        ("aup-cfg-false", {"ask_user_auto_proceed": False}, False),
        ("aup-cfg-missing", {}, False),                      # default off
        ("aup-cfg-truthy", {"ask_user_auto_proceed": 1}, True),  # bool cast
    ):
        gov.cleanup_governor(rid)
        gov.configure_run(rid, until_finished=False, settings=cfg_over)
        got = gov.get_run_config(rid)
        check(f"configure_run auto_proceed={want} ({rid})",
              isinstance(got.get("auto_proceed"), bool) and got["auto_proceed"] is want,
              repr(got))
        gov.cleanup_governor(rid)
    check("cleanup removes run config", gov.get_run_config("aup-cfg-true") is None)


# ── 2. source pin: runner asks at question time, uses 30 s ─────────────────

def _handler_source() -> str:
    """Source of _handle_ask_user only (up to the MCP-mode block after it)."""
    src = (ROOT / "tools" / "runner.py").read_text(encoding="utf-8")
    start = src.index("async def _handle_ask_user")
    end = src.index("try:  # MCP mode gate", start)
    return src[start:end]


def test_source_pins():
    src = _handler_source()
    check("handler reads ask_user_auto_proceed", "ask_user_auto_proceed" in src)
    check("handler reads CURRENT settings (load_settings)", "load_settings" in src)
    check("handler uses AUTO_PROCEED_TIMEOUT_S", "AUTO_PROCEED_TIMEOUT_S" in src)
    check("handler imports governor AUTO_PROCEED_TIMEOUT_S",
          "AUTO_PROCEED_TIMEOUT_S as _gov_AUTO_PROCEED_TIMEOUT_S" in src)
    # Precedence in source order: `if until_finished` branch BEFORE the
    # `elif auto_proceed` branch (configured timeout wins over fixed 30 s).
    i_uf = src.index('if _gcfg["until_finished"] and _gcfg["timeout_s"] > 0:')
    i_ap = src.index("elif _auto_proceed_now:")
    check("until_finished branch precedes auto_proceed (precedence)", i_uf < i_ap,
          f"until_finished@{i_uf} auto_proceed@{i_ap}")
    # The until_finished branch uses the configured timeout, auto_proceed
    # the fixed constant.
    check("until_finished branch uses configured timeout_s",
          "_ask_timeout_used = _gcfg[\"timeout_s\"]" in src)
    check("auto_proceed branch uses 30 s constant",
          "_ask_timeout_used = _gov_AUTO_PROCEED_TIMEOUT_S" in src)
    # The emitted event reports the actually used timeout.
    check("event reports actually used timeout",
          '"timeout_seconds": _ask_timeout_used' in src)
    check("pending governor timeout cancelled after resume",
          "_gov_cancel_timeout(run_id)" in src.split("wait_for_resume", 1)[1])


# ── 3. functional pin: engine starts the timeout ────────────────────────────

def test_engine_starts_timeout():
    import infra.ask_user_governor as gov
    import settings as settings_mod
    import tools.runner as tr

    calls: list[tuple[str, float, str]] = []

    async def fake_start_timeout(run_id, timeout_s, auto_answer):
        calls.append((run_id, timeout_s, auto_answer))

    real_start = gov.start_timeout
    real_load = settings_mod.load_settings
    gov.start_timeout = fake_start_timeout

    def _ask(rid):
        """One ask_user call in run context `rid` (pause timeout 0 = fast)."""
        # ContextVars must be set PER CASE: asyncio.run copies the current
        # context, and a setup loop would leave only the last value visible.
        tr._current_run_id.set(rid)
        tr._pause_timeout_s.set(0)   # fast: wait_for_resume times out at once
        return asyncio.run(tr._handle_ask_user({"question": "go?"}, None, None))

    try:
        # Case 1: auto_proceed ON (question-time) -> fixed 30 s.
        gov.cleanup_governor("aup-eng-1")
        gov.configure_run("aup-eng-1", until_finished=False,
                          settings={"ask_user_auto_answer": "auto-answer-text"})
        settings_mod.load_settings = lambda: {"ask_user_auto_proceed": True}
        _ask("aup-eng-1")
        check("engine starts timeout on auto_proceed", len(calls) == 1, repr(calls))
        if calls:
            check("auto_proceed uses 30 s", calls[0][1] == gov.AUTO_PROCEED_TIMEOUT_S,
                  repr(calls[0]))
            check("timeout uses governor auto_answer text",
                  calls[0][2] == "auto-answer-text", repr(calls[0]))

        # Case 2: until_finished wins with its configured timeout (300s default
        # would surprise nobody, use a distinct value to pin the precedence).
        calls.clear()
        gov.cleanup_governor("aup-eng-2")
        gov.configure_run("aup-eng-2", until_finished=True,
                          settings={"ask_user_timeout_until_finished_seconds": 777,
                                    "ask_user_auto_proceed": True})
        _ask("aup-eng-2")
        check("until_finished keeps configured timeout (precedence)",
              calls and calls[0][1] == 777, repr(calls))

        # Case 3: both off -> no governor timeout at all.
        calls.clear()
        gov.cleanup_governor("aup-eng-3")
        gov.configure_run("aup-eng-3", until_finished=False, settings={})
        settings_mod.load_settings = lambda: {}
        _ask("aup-eng-3")
        check("no timeout when both off", not calls, repr(calls))

        # Case 4: CURRENT settings win over the run-start snapshot — the
        # mid-run toggle takes effect without restarting the run.
        calls.clear()
        gov.cleanup_governor("aup-eng-4")
        gov.configure_run("aup-eng-4", until_finished=False,
                          settings={"ask_user_auto_proceed": False})  # snapshot off
        settings_mod.load_settings = lambda: {"ask_user_auto_proceed": True}  # toggled on
        _ask("aup-eng-4")
        check("question-time toggle wins over run-start snapshot",
              calls and calls[0][1] == gov.AUTO_PROCEED_TIMEOUT_S, repr(calls))
    finally:
        gov.start_timeout = real_start
        settings_mod.load_settings = real_load
        for rid in ("aup-eng-1", "aup-eng-2", "aup-eng-3", "aup-eng-4"):
            gov.cleanup_governor(rid)
            tr._pending_approvals.pop(rid, None)


# ── 4. unit: start_timeout no-op / real auto answer ─────────────────────────

def test_start_timeout_unit():
    import infra.run_control as rc
    from infra import ask_user_governor as gov

    answers: list[tuple[str, str]] = []
    real_set = rc.set_user_answer
    rc.set_user_answer = lambda rid, ans: answers.append((rid, ans))
    try:
        # 0 s -> no-op: no task, no answer, no flag.
        async def _zero():
            await gov.start_timeout("aup-zero", 0, "unused")
        asyncio.run(_zero())
        check("start_timeout(0) is a no-op (no task)",
              "aup-zero" not in gov._TIMEOUT_TASKS)
        check("start_timeout(0) sets no answer", not answers, repr(answers))
        check("start_timeout(0) sets no flag",
              not gov.is_timeout_answer_sent("aup-zero"))

        # Tiny timeout -> pause must exist, then set_user_answer fires.
        async def _tiny():
            await rc.initiate_pause("aup-tiny", "q?")
            await gov.start_timeout("aup-tiny", 0.01, "auto-answer")
            await asyncio.sleep(0.08)
        asyncio.run(_tiny())
        check("start_timeout(0.01) auto-answers via set_user_answer",
              ("aup-tiny", "auto-answer") in answers, repr(answers))
        check("timeout answer flag set", gov.is_timeout_answer_sent("aup-tiny"))
    finally:
        rc.set_user_answer = real_set
        for rid in ("aup-zero", "aup-tiny"):
            gov.cleanup_governor(rid)
            rc.cleanup_pause(rid)


if __name__ == "__main__":
    print("== ask_user_auto_proceed (W2-3) ==")
    test_configure_run_auto_proceed()
    test_source_pins()
    test_engine_starts_timeout()
    test_start_timeout_unit()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
