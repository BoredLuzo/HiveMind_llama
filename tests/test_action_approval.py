# -*- coding: utf-8 -*-
"""Action-approval gate tests (2026-09-18).

Feature: optional pause-and-ask before state-changing tool calls
(duo_action_approval_enabled). The user answers
  1 = approve once        -> this call proceeds, nothing remembered
  2 = approve this exact call -> remembered for the CURRENT CHAT only
      (in-memory; writes per file, commands per exact arguments) — a new
      chat asks again
  3 / anything unclear    -> deny: tool returns ACTION_APPROVAL_DENIED
  0|<note>                -> "input only": the tool does not run, the note
      goes back to the model as the call's outcome

Autonomous/throttled runs and runs without a run_id bypass the gate.

Run: python tests/test_action_approval.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def test_answer_parsing():
    from tools.runner import _parse_approval_answer as p
    check("'1' -> once", p("1") == "once")
    check("'once' -> once", p("  once") == "once")
    check("'Approve once please' -> once", p("Approve once please") == "once")
    check("'2' -> repo", p("2") == "repo")
    check("'repo' -> repo", p("repo") == "repo")
    check("'always in this repo' -> repo", p("always in this repo") == "repo")
    check("'3' -> deny", p("3") == "deny")
    check("'no' -> deny", p("no") == "deny")
    check("'' -> deny (fail-safe)", p("") == "deny")
    check("unclear -> deny (fail-safe)", p("what do you want?") == "deny")


def test_memory_scope():
    """Approvals are exact-match AND chat-scoped, in-memory only: a new chat
    (new _current_run_id scope) asks again; nothing persists to disk."""
    import tools.runner as tr
    tr._approval_memory.clear()
    tr._approval_once_paths.clear()

    ws = Path(tempfile.mkdtemp(prefix="hvm_appr_"))
    cmd = {"command": "python -m pytest -q"}
    tr._current_run_id.set("chat-A")
    check("fresh chat: no approval", not tr._repo_approval_granted(ws, "run_bash", cmd))
    tr._remember_repo_approval(ws, "run_bash", cmd)
    # EXACT-MATCH: the same 1:1 command is remembered, anything else asks again
    check("same chat, same command: granted", tr._repo_approval_granted(ws, "run_bash", cmd))
    check("same chat, different command: NOT granted",
          not tr._repo_approval_granted(ws, "run_bash", {"command": "python -m pytest -q -x"}))
    check("same chat, different tool: NOT granted", not tr._repo_approval_granted(ws, "run_python", cmd))
    ws_b = Path(tempfile.mkdtemp(prefix="hvm_appr_b_"))
    check("other workspace: NOT granted", not tr._repo_approval_granted(ws_b, "run_bash", cmd))
    # writes remember per FILE
    tr._remember_repo_approval(ws, "edit_file", {"path": "src/a.js", "old_text": "x", "new_text": "y"})
    check("same chat: same file granted (any write tool)",
          tr._repo_approval_granted(ws, "write_file", {"path": "src/a.js", "content": "z"}))
    check("same chat: other file NOT granted",
          not tr._repo_approval_granted(ws, "edit_file", {"path": "src/b.js", "old_text": "x", "new_text": "y"}))
    # NEW CHAT: the memory does not cross the chat boundary
    tr._current_run_id.set("chat-B")
    check("NEW chat: asks again", not tr._repo_approval_granted(ws, "run_bash", cmd))
    check("NEW chat: writes ask again",
          not tr._repo_approval_granted(ws, "edit_file", {"path": "src/a.js", "old_text": "x", "new_text": "y"}))


def test_gate_flow():
    try:
        from core import state as _st
        _ = _st.settings
    except ImportError as e:
        print(f"  SKIP  gate flow ({type(e).__name__}: {e})")
        return

    import tools.runner as tr
    import infra.run_control as rc
    from core import state as st

    tmp = Path(tempfile.mkdtemp(prefix="hvm_appr_gate_"))
    ws = tmp / "ws"
    ws.mkdir()
    tr._approval_memory.clear()
    tr._approval_once_paths.clear()
    st.settings["duo_action_approval_enabled"] = True
    tr._ask_user_gate.set("open")
    tr._current_run_id.set("test-run-1")
    tr._tool_loop_emit.set(None)

    paused = []

    async def fake_pause(run_id, question):
        paused.append((run_id, question))
        # mirror the real initiate_pause so the gate's abort-aware wait
        # finds a pause event
        rc._pause_events[run_id] = asyncio.Event()
        # while paused, the pending info must be visible to the recovery
        # endpoint (page reload during the pause loses the SSE event)
        info = tr._pending_approval_info(run_id)
        assert info and info.get("tool") in question, f"pending info missing: {info}"

    async def fake_resume(run_id, timeout_s=600):
        return "2"   # approve for this repo

    orig_pause, orig_resume = rc.initiate_pause, rc.wait_for_resume
    rc.initiate_pause, rc.wait_for_resume = fake_pause, fake_resume
    try:
        # disabled -> no gate at all
        st.settings["duo_action_approval_enabled"] = False
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "ls"}, ws))
        check("disabled: proceeds without pause", out is None and not paused)

        # enabled, non-listed tool -> no gate
        st.settings["duo_action_approval_enabled"] = True
        out = asyncio.run(tr._check_action_approval("read_file", {"path": "x"}, ws))
        check("non-listed tool: proceeds", out is None and not paused)

        # '2' -> proceeds AND remembers EXACTLY this command
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "rm -rf /tmp/x"}, ws))
        check("'2' -> proceeds", out is None, str(out)[:120])
        check("pause happened once", len(paused) == 1 and "run_bash" in paused[0][1])
        check("pending info popped after decision",
              tr._pending_approval_info("test-run-1") is None)
        check("'2' remembered for the exact command",
              tr._repo_approval_granted(ws, "run_bash", {"command": "rm -rf /tmp/x"}))

        # exact-match: SAME command proceeds without a new pause, a
        # DIFFERENT command asks again
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "rm -rf /tmp/x"}, ws))
        check("same command: proceeds without new pause",
              out is None and len(paused) == 1)
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "echo hi"}, ws))
        check("different command: asks again", out is None and len(paused) == 2)

        # deny path
        async def fake_resume_deny(run_id, timeout_s=600):
            return "3"
        ws2 = tmp / "ws2"
        ws2.mkdir()
        rc.wait_for_resume = fake_resume_deny
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "ls"}, ws2))
        check("'3' -> denied error", out is not None and out[0] == "DENY"
              and "ACTION_APPROVAL_DENIED" in str(out[1]))
        check("deny: nothing remembered",
              not tr._repo_approval_granted(ws2, "run_bash", {"command": "ls"}))

        # unclear answer -> deny (fail-safe)
        async def fake_resume_unclear(run_id, timeout_s=600):
            return "hmm what?"
        rc.wait_for_resume = fake_resume_unclear
        out = asyncio.run(tr._check_action_approval("run_python", {"code": "1+1"}, ws2))
        check("unclear -> denied", out is not None and out[0] == "DENY")

        # note pass-through: approve WITH guidance -> ("NOTE", text)
        async def fake_resume_note(run_id, timeout_s=600):
            return "1|please use fetch instead of curl"
        rc.wait_for_resume = fake_resume_note
        out = asyncio.run(tr._check_action_approval("run_python", {"code": "req"}, ws2))
        check("note: approve with guidance -> NOTE",
              out is not None and out[0] == "NOTE" and out[1] == "please use fetch instead of curl",
              str(out))

        # input-only: "0|..." sends the message WITHOUT approving or denying —
        # the tool does not run, the note goes back as the call's outcome
        async def fake_resume_input(run_id, timeout_s=600):
            return "0|mach es leichter"
        rc.wait_for_resume = fake_resume_input
        out = asyncio.run(tr._check_action_approval("run_python", {"code": "x"}, ws2))
        check("input-only -> INPUT_ONLY with note",
              out is not None and out[0] == "INPUT_ONLY" and out[1] == "mach es leichter", str(out))
        check("input-only parser: '0' prefix", tr._parse_approval_answer("0|hi") == "input")
        check("parser: 'no' still deny, not eaten by input",
              tr._parse_approval_answer("no") == "deny", tr._parse_approval_answer("no"))
        async def fake_resume_deny_note(run_id, timeout_s=600):
            return "3|too dangerous"
        rc.wait_for_resume = fake_resume_deny_note
        out = asyncio.run(tr._check_action_approval("run_python", {"code": "x"}, ws2))
        check("note: deny with reason -> DENY + User message",
              out is not None and out[0] == "DENY" and "User message: too dangerous" in str(out[1]),
              str(out)[:140])

        # 'once' for a write covers the SAME PATH (auto-split continuations,
        # follow-up edits) — no second ask. Different path still asks.
        wsp = tmp / "wsp"
        wsp.mkdir()
        async def fake_resume_once(run_id, timeout_s=600):
            return "1"
        rc.wait_for_resume = fake_resume_once
        _nb = len(paused)
        wf = {"path": "src/game.js", "content": "..."}
        out = asyncio.run(tr._check_action_approval("write_file", dict(wf), wsp))
        check("write once: first ask proceeds", out is None and len(paused) == _nb + 1)
        out = asyncio.run(tr._check_action_approval("write_file_append",
                                                    {"path": "src/game.js", "content": "more"}, wsp))
        check("write once: same path auto-approved (no new pause)",
              out is None and len(paused) == _nb + 1)
        out = asyncio.run(tr._check_action_approval("edit_file",
                                                    {"path": "src/other.js", "old_text": "a", "new_text": "b"}, wsp))
        check("write once: DIFFERENT path asks again",
              out is None and len(paused) == _nb + 2)

        # Gate-mode independence (2026-09-18 live bug): until_finished duo
        # runs set the ask_user gate to throttled_autonomous — that is the
        # user's NORMAL interactive mode, approvals must still fire there.
        rc.wait_for_resume = fake_resume   # answers "2" (approve workspace)
        tr._ask_user_gate.set("throttled_autonomous")
        ws3 = tmp / "ws3"
        ws3.mkdir()
        _n_before2 = len(paused)
        out = asyncio.run(tr._check_action_approval("run_python", {"code": "1+1"}, ws3))
        check("throttled mode still pauses (no silent bypass)",
              out is None and len(paused) == _n_before2 + 1,
              f"out={str(out)[:80]} paused={len(paused)}")

        # Abort during pause (browser closed): the gate must wake on the
        # run's abort event instead of sleeping for 3600s while a
        # disconnected run keeps firing toasts.
        async def fake_never_resume(run_id, timeout_s=600):
            await asyncio.sleep(30)
            return "1"
        rc.wait_for_resume = fake_never_resume
        ws4 = tmp / "ws4"
        ws4.mkdir()
        rc._run_abort_registry.setdefault("test-run-1", asyncio.Event())
        rc._run_abort_registry["test-run-1"].clear()
        _nb3 = len(paused)

        async def abort_scenario():
            gate_task = asyncio.ensure_future(
                tr._check_action_approval("run_bash", {"command": "long"}, ws4))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(paused) > _nb3:
                    break
            await asyncio.sleep(0.05)   # let the gate reach its wait
            rc._run_abort_registry["test-run-1"].set()
            return await asyncio.wait_for(gate_task, timeout=5)

        out = asyncio.run(abort_scenario())
        check("abort during pause -> immediate deny (no hour-long sleep)",
              out is not None and out[0] == "DENY" and "aborted" in str(out[1]), str(out)[:120])
    finally:
        rc.initiate_pause, rc.wait_for_resume = orig_pause, orig_resume
        st.settings["duo_action_approval_enabled"] = False
        tr._ask_user_gate.set("open")
        tr._current_run_id.set(None)


def test_gate_scope():
    from tools.runner import _APPROVAL_TOOLS as T
    check("code/cmd tools gated",
          {"run_bash", "run_python", "install_package", "start_background"} <= T, str(T))
    check("file writes gated (2 files slipped through once)",
          {"write_file", "edit_file", "write_file_append"} <= T, str(T))
    check("git_commit gated", "git_commit" in T, str(T))
    check("read-only tools NOT gated",
          not T & {"read_file", "search_code", "list_dir", "find_files", "web_search"}, str(T))


if __name__ == "__main__":
    test_answer_parsing()
    test_memory_scope()
    test_gate_scope()
    test_gate_flow()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
