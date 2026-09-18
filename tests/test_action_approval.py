# -*- coding: utf-8 -*-
"""Action-approval gate tests (2026-09-18).

Feature: optional pause-and-ask before state-changing tool calls
(duo_action_approval_enabled). The user answers
  1 = approve once        -> this call proceeds, nothing remembered
  2 = approve this tool in this workspace -> persisted in
      tool_approvals.json (next to settings.json), future calls of the
      same tool in the same workspace skip the gate
  3 / anything unclear    -> deny: tool returns ACTION_APPROVAL_DENIED

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


def test_persistence_and_scope():
    import tools.runner as tr
    tmp = Path(tempfile.mkdtemp(prefix="hvm_appr_"))
    tr._action_approvals_cache = None
    tr._approvals_file = lambda: tmp / "tool_approvals.json"

    ws = tmp / "projA"
    ws.mkdir()
    check("fresh: no approval", not tr._repo_approval_granted(ws, "run_bash"))
    tr._remember_repo_approval(ws, "run_bash")
    check("after remember: granted", tr._repo_approval_granted(ws, "run_bash"))
    check("other tool: NOT granted", not tr._repo_approval_granted(ws, "run_python"))
    ws_b = tmp / "projB"
    ws_b.mkdir()
    check("other workspace: NOT granted", not tr._repo_approval_granted(ws_b, "run_bash"))
    # file really persisted
    import json
    on_disk = json.loads((tmp / "tool_approvals.json").read_text(encoding="utf-8"))
    check("persisted to disk", "run_bash" in on_disk.get(str(ws.resolve()), {}), str(on_disk)[:120])


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
    tr._action_approvals_cache = None
    tr._approvals_file = lambda: tmp / "tool_approvals.json"
    st.settings["duo_action_approval_enabled"] = True
    tr._ask_user_gate.set("open")
    tr._current_run_id.set("test-run-1")
    tr._tool_loop_emit.set(None)

    paused = []

    async def fake_pause(run_id, question):
        paused.append((run_id, question))
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

        # '2' -> proceeds AND remembers; pending info cleaned up
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "rm -rf /tmp/x"}, ws))
        check("'2' -> proceeds", out is None, str(out)[:120])
        check("pause happened once", len(paused) == 1 and "run_bash" in paused[0][1])
        check("pending info popped after decision",
              tr._pending_approval_info("test-run-1") is None)
        check("'2' remembered for workspace", tr._repo_approval_granted(ws, "run_bash"))

        # second call: approved via repo memory -> NO pause
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "echo hi"}, ws))
        check("repo-approved: proceeds without new pause", out is None and len(paused) == 1)

        # deny path
        async def fake_resume_deny(run_id, timeout_s=600):
            return "3"
        ws2 = tmp / "ws2"
        ws2.mkdir()
        rc.wait_for_resume = fake_resume_deny
        out = asyncio.run(tr._check_action_approval("run_bash", {"command": "ls"}, ws2))
        check("'3' -> denied error", out is not None and "ACTION_APPROVAL_DENIED" in str(out))
        check("deny: nothing remembered", not tr._repo_approval_granted(ws2, "run_bash"))

        # unclear answer -> deny (fail-safe)
        async def fake_resume_unclear(run_id, timeout_s=600):
            return "hmm what?"
        rc.wait_for_resume = fake_resume_unclear
        out = asyncio.run(tr._check_action_approval("run_python", {"code": "1+1"}, ws2))
        check("unclear -> denied", out is not None and "ACTION_APPROVAL_DENIED" in str(out))

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
    test_persistence_and_scope()
    test_gate_scope()
    test_gate_flow()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
