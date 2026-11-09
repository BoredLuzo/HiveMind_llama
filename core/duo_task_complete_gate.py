"""Task-complete verify gate (C3, 2026-11-07 maintainability sprint).

The `task_complete_called` block of the duo tool round, moved VERBATIM
behind an async-generator seam. `ps.action == "break"` ends the tool-round
loop (the former inline `break`); all other fields sync in `finally`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import py_compile
import re
import shutil
import subprocess
import time

from hive_functions.test_runner import (
    run_tests as _run_test_suite, NO_TESTS_RE,
)
from core.tool_executor import ToolExecResult
from core.duo.preflight import (
    completion_verdict, needs_test_verification, _await_with_hb,
)

logger = logging.getLogger("hivemind.duo")


def _claim_abs_path(fp: str, workspace: str) -> str:
    """Absolute path for a task_complete file claim.

    Windows models sometimes emit "/C:/dir/file" - os.path.isabs accepts it,
    os.path.exists does not. Strip the extra leading slash on Windows.
    """
    _p = str(fp or "")
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", _p):
        _p = _p[1:]
    return _p if os.path.isabs(_p) else os.path.join(workspace, _p)


def _deterministic_syntax_verify(workspace: str, written_files: list | set | None) -> str:
    """SYNC helper (run via asyncio.to_thread): check syntax of written files.

    Returns an empty string when all checkable files pass, or the error text.
    """
    errs: list[str] = []
    node_bin = shutil.which("node")
    tsc_bin = shutil.which("tsc")
    tsc_ran = False
    for raw_fp in sorted(written_files or []):
        fp = _claim_abs_path(str(raw_fp), workspace)
        if not os.path.isfile(fp):
            continue
        low = fp.lower()
        try:
            if low.endswith(".py"):
                py_compile.compile(fp, doraise=True)
            elif low.endswith(".json"):
                with open(fp, "r", encoding="utf-8") as fh:
                    json.load(fh)
            elif low.endswith((".js", ".mjs", ".cjs")) and node_bin:
                r = subprocess.run([node_bin, "--check", fp],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    errs.append(f"{raw_fp}: {(r.stderr or r.stdout).strip()[:300]}")
            elif low.endswith((".ts", ".tsx")) and tsc_bin and not tsc_ran:
                if os.path.isfile(os.path.join(workspace, "tsconfig.json")):
                    tsc_ran = True
                    r = subprocess.run([tsc_bin, "--noEmit"], cwd=workspace,
                                       capture_output=True, text=True, timeout=30)
                    if r.returncode != 0:
                        errs.append(f"tsc --noEmit: {(r.stderr or r.stdout).strip()[:300]}")
        except (OSError, ValueError, py_compile.PyCompileError, subprocess.SubprocessError) as exc:
            errs.append(f"{raw_fp}: {exc}")
    return "\n".join(errs)


# VERIFY-GATE PROMPT: shared by the veto fallback when the harness cannot run
# the suite at all.
_VERIFY_REQUIRED_PROMPT = (
    "[VERIFY REQUIRED] You called task_complete but the last file write "
    "has not been followed by a successful test run.\n"
    "Do this NOW, in this order:\n"
    "1. Call run_tests. If it reports no suite, write a minimal smoke test "
    "that exercises the created files and run it via run_bash instead "
    "(e.g. python selftest.py).\n"
    "2. Ensure it exits with code 0.\n"
    "3. Then call task_complete again with status "
    "{completed, blockers, build_status}."
)


@dataclasses.dataclass
class _DuoTaskCompleteGateState:
    """In/out locals of the verify gate."""
    exec_result: ToolExecResult = dataclasses.field(default_factory=ToolExecResult)
    written_files: list = dataclasses.field(default_factory=list)
    dtool_msgs: list = dataclasses.field(default_factory=list)
    ws_str: str = ""
    verify_mutation_serial: int = 0
    state: dict = dataclasses.field(default_factory=dict)
    verify_warned: bool = False
    verify_last_ok_serial: int = 0
    verify_gate_blocks: int = 0
    completion_unverified: bool = False
    duo_deadline_at: float = 0.0
    grace_round_active: bool = False
    action: str = "proceed"   # "break" ends the tool-round loop


async def _duo_task_complete_gate(ctx, ps: _DuoTaskCompleteGateState):
    _exec_result = ps.exec_result
    _written_files = ps.written_files
    _dtool_msgs = ps.dtool_msgs
    _ws_str = ps.ws_str
    _verify_mutation_serial = ps.verify_mutation_serial
    state = ps.state
    _verify_warned = ps.verify_warned
    _verify_last_ok_serial = ps.verify_last_ok_serial
    _verify_gate_blocks = ps.verify_gate_blocks
    _completion_unverified = ps.completion_unverified
    _duo_deadline_at = ps.duo_deadline_at
    _grace_round_active = ps.grace_round_active
    try:
        # Verify-Gate (Agentic-Mode): ensure run_bash ran after last write
        if ctx.duo_config.agentic_mode:
            _verdict = completion_verdict(
                mutation_serial=_verify_mutation_serial,
                last_ok_serial=_verify_last_ok_serial,
                has_written_files=bool(_written_files),
                vetoes=_verify_gate_blocks if _verify_warned else 0,
            )
            if _verdict == "unverified":
                _verify_gate_blocks += 1
                _completion_unverified = True
                logger.warning(
                    "[VERIFY-GATE] task_complete accepted after %d vetoes "
                    "WITHOUT a green test run — result marked unverified",
                    _verify_gate_blocks,
                )
            elif (_verdict == "veto"
                  and not needs_test_verification(_written_files)):
                # Static-only change (HTML/CSS/MD/...): a
                # repo-wide test run would only surface
                # unrelated pre-existing failures and
                # derail the completion into an edit/test
                # loop. No test suite applies here.
                logger.info(
                    "[VERIFY-GATE] no testable code changed "
                    "(%d file(s)) - completion accepted without tests",
                    len(_written_files or []))
            elif _verdict == "veto":
                _exec_result.task_complete_called = False
                _verify_gate_blocks += 1
                _verify_warned = True
                # AUTO-VERIFY (2026-10-23): the harness runs
                # the verify suite itself on veto — green →
                # verified, red → failures into context,
                # scoped via the edit→test mapping ([] → full).
                _av_paths: list[str] | None = None
                try:
                    from hive_functions.testmap import map_edits_to_tests as _av_map
                    _av_paths = _av_map(_ws_str, list(_written_files or [])) or None
                except Exception as _av_map_err:
                    logger.debug(
                        "[VERIFY-GATE] veto test mapping failed: %s", _av_map_err)
                # AUDIT FIX (F3, 2026-10-24): the inline
                # await used to block this generator for
                # up to 180 s with no heartbeat yield and
                # no deadline grace — the run could die as
                # duo_timed_out right after a GREEN verify.
                try:
                    _av_res = await _await_with_hb(
                        lambda: _run_test_suite(
                            workspace=_ws_str, timeout=90,
                            chat_id=ctx.chat_id or "",
                            paths=_av_paths,
                        ),
                        timeout=200.0,
                        emit_fn=ctx.emit,
                        label="Auto-Verify: running the test suite",
                        interval=10.0,
                    )
                except Exception as _av_exc:
                    _av_res = None
                    logger.warning(
                        "[VERIFY-GATE] auto-verify run_tests failed: %s: %s",
                        type(_av_exc).__name__, str(_av_exc)[:150],
                    )
                if _av_res is not None and _av_res.is_clean():
                    _verify_last_ok_serial = _verify_mutation_serial
                    # re-arm the gate: the next write cycle
                    # gets its own veto+auto-verify chance
                    # (vetoes>=1 would downgrade it straight
                    # to "unverified")
                    _verify_gate_blocks = 0
                    _verify_warned = False
                    _exec_result.task_complete_called = True
                    logger.info(
                        "[VERIFY-GATE] auto-verify green — completion accepted as verified"
                    )
                    _dtool_msgs.append({"role": "user", "content": (
                        "[AUTO-VERIFY] The harness ran the test suite itself "
                        "after your task_complete: ALL TESTS GREEN. "
                        "Your completion is accepted as verified."
                    )})
                    # AUDIT FIX (F1): bare `await ctx.emit` never
                    # reached the SSE stream (emit only serializes)
                    # — the status events must be yielded.
                    try:
                        yield await ctx.emit({"type": "status", "content": (
                            "✅ Auto-Verify: tests green — completion verified"
                        )})
                    except Exception as _av_emit_err:
                        logger.debug(
                            "[VERIFY-GATE] auto-verify green emit failed: %s",
                            _av_emit_err)
                    # AUDIT FIX (F3b): the verify run (up to 200 s)
                    # consumed run budget — grant a fresh grace so
                    # a green verify cannot be followed by
                    # duo_timed_out (same shape as the wedge handoff).
                    _av_grace = time.monotonic() + 300.0
                    if _av_grace > _duo_deadline_at:
                        _gref = state.setdefault(
                            "_grace_extensions_ref", [0])
                        if int(_gref[0]) < 1:
                            _gref[0] = int(_gref[0]) + 1
                            _duo_deadline_at = _av_grace
                        else:
                            logger.warning(
                                "[DEADLINE-GRACE] auto-verify "
                                "succeeded, but the one allowed "
                                "extension is spent - deadline stays")
                elif _av_res is not None and (
                        not _av_res.command
                        or NO_TESTS_RE.search(str(_av_res.raw_output or ""))):
                    # AUDIT FIX (F2): "nothing ran" is broader than
                    # command=="" — a workspace WITH a manifest but
                    # zero tests also answers exit 5 / "no tests ran"
                    # with a non-empty command. Run deterministic syntax check
                    # instead of forcing an extra LLM round.
                    logger.info(
                        "[VERIFY-GATE] auto-verify: no tests ran in workspace — running deterministic syntax check")
                    _syn_err = await asyncio.to_thread(
                        _deterministic_syntax_verify, _ws_str, _written_files)
                    if not _syn_err:
                        _verify_last_ok_serial = _verify_mutation_serial
                        _verify_gate_blocks = 0
                        _verify_warned = False
                        _exec_result.task_complete_called = True
                        state["verify_status"] = "syntax_verified"
                        _dtool_msgs.append({"role": "user", "content": (
                            "[AUTO-VERIFY] No test suite found in workspace; deterministic syntax check "
                            "passed (status=syntax_verified). Ad-hoc smoke check is optional; completion accepted."
                        )})
                        try:
                            yield await ctx.emit({"type": "status", "content": (
                                "✅ Auto-Verify: syntax_verified (no test suite in workspace)"
                            )})
                        except (OSError, RuntimeError, TypeError, ValueError) as _av_emit_err:
                            logger.debug("[VERIFY-GATE] syntax-verify green emit failed: %s", _av_emit_err)
                    else:
                        _dtool_msgs.append({"role": "user", "content": (
                            "[VERIFY REQUIRED] Deterministic syntax check failed on your written files:\n"
                            + _syn_err[:900]
                            + "\nFix the syntax error above and call task_complete again."
                        )})
                        try:
                            yield await ctx.emit({"type": "status", "content": (
                                "❌ Auto-Verify: syntax error in written files — completion blocked"
                            )})
                        except (OSError, RuntimeError, TypeError, ValueError) as _av_emit_err:
                            logger.debug("[VERIFY-GATE] syntax-verify red emit failed: %s", _av_emit_err)
                elif _av_res is not None:
                    _av_note = (f"{_av_res.failure_count} test failure(s)"
                                if _av_res.failure_count
                                else "the test run did not pass")
                    _dtool_msgs.append({"role": "user", "content": (
                        "[VERIFY REQUIRED] You called task_complete but the harness "
                        "re-ran the tests itself and they FAILED:\n"
                        + str(_av_res.inject_msg)[:900] + "\n"
                        "Fix the failures above, run_tests again, then call "
                        "task_complete. Do NOT call task_complete while tests are red."
                    )})
                    try:
                        yield await ctx.emit({"type": "status", "content": (
                            f"❌ Auto-Verify: {_av_note} — completion blocked"
                        )})
                    except Exception as _av_emit_err:
                        logger.debug(
                            "[VERIFY-GATE] auto-verify red emit failed: %s",
                            _av_emit_err)
                else:
                    # Harness could not run the suite — fall
                    # back to instructing the model.
                    # vague "run your tests" — baseline FAIL repo_peon r1
                    # run_tests → no-suite fallback run_bash → task_complete.
                    _dtool_msgs.append({"role": "user", "content": (
                        _VERIFY_REQUIRED_PROMPT
                    )})
        # Fall-through: re-check after Verify injection
        if not _exec_result.task_complete_called:
            pass  # Verify blocked the completion — loop continues
        else:
            # TC-LOOP-FIX: an accepted task_complete ends the
            # tool loop immediately (observed live Run 10: coder called
            # task_complete 4x+ in a row, chunk path never reached).
            # Verify that files claimed in task_complete actually exist on disk
            _missing = []
            for _fp in sorted(_written_files or []):
                # CLAIM-PATH (2026-11-03, live): a model
                # wrote "/C:/.../big.py"; os.path.isabs
                # says True and exists() says False, so a
                # real file was reported missing.
                _fp_n = _claim_abs_path(_fp, _ws_str)
                if not os.path.exists(_fp_n):
                    _missing.append(_fp)
            if _missing:
                logger.warning(
                    "[TASK_COMPLETE VERIFY] %d file(s) claimed but missing: %s",
                    len(_missing), _missing[:10],
                )
            if _completion_unverified:
                # make the unverified completion visible instead of
                # letting it read like a verified one
                try:
                    yield await ctx.emit({"type": "status", "content": (
                        "⚠ Completed without a green test run after the "
                        "last write — treat this result as unverified."
                    )})
                except Exception as _uv_err:
                    logger.debug(
                        "[VERIFY-GATE] unverified status emit failed: %s", _uv_err
                    )
                logger.warning(
                    "[VERIFY-GATE] run ended unverified "
                    "(written_files=%d, last_ok_serial=%d, mutation_serial=%d)",
                    len(_written_files or []), _verify_last_ok_serial,
                    _verify_mutation_serial,
                )
            _grace_round_active = False
            ps.action = "break"
            return
    finally:
        ps.verify_warned = _verify_warned
        ps.verify_last_ok_serial = _verify_last_ok_serial
        ps.verify_gate_blocks = _verify_gate_blocks
        ps.completion_unverified = _completion_unverified
        ps.duo_deadline_at = _duo_deadline_at
        ps.grace_round_active = _grace_round_active
