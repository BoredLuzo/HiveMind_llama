# -*- coding: utf-8 -*-
"""Regression suite for Wave 2 (run_bash refinements + P1 tools) fixes.

Covers:
  1. _should_rewrite_posix_drive: leaves python/node route strings, $(sed ...),
     PATH="/c/foo:$PATH", sed '/c/Users/d' intact; TMPDIR & /tmp rewrite work
     end-to-end in Git Bash with MSYS_NO_PATHCONV=1 on Windows.
  2. _bash_blocklisted: blocks rm -rf -- /, rm -rf "/", '.', "~", //, .., C:/,
     leading-space format/diskpart, and start_background post-rewrite block.
  3. _split_shim_args quote concatenation, _is_search_no_match with 2>&1,
     _is_unscoped_pip_install with `uv venv && uv pip install`.
  4. PowerShell fallback quote-aware && (trailing backslash) and 2>&1 wrapper.
  5. _stream_proc preserves unflushed partial line on timeout and bounds proc.wait().
  6. run_python returns RUN_PYTHON_EXEC_ERROR on non-zero exit with stdout.
  7. run_tests rejects CLI-flag injection targets starting with '-'.
  8. child_env.tool_env strips engine venv from front of PATH behind system Python.
  9. read_file truncated range computes accurate continuation start_line & rejects end_line=0.
 10. WorkspaceTransaction preserves exact LF line endings on Windows undo/rollback.
 11. parse_unified_diff strips tab-separated timestamps from diff -u headers.
 12. find_files matches src/**/*.py direct children; search_code checks junctions pre-resolve.
 13. git_commit rejects directories & resolves relative workspace; git_status uses scrubbed_env
     and caps log; push_after_commit redacts / tokens; send_file enforces workspace_lock.
 14. _repair_json_backslashes preserves \\ escapes; normalize_tool_path strips /C:/;
     _inline_check_workspace catches symlink loop RuntimeError cleanly.
 15. linting._pyright_lint_result passes tool_env(workspace) and reaps tree on timeout.

Run: python tests/test_wave2_tools_and_bash.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402
import hive_functions.git_tools as HGT  # noqa: E402
from infra import child_env as CE  # noqa: E402
from tools import background as BG  # noqa: E402
from tools import errors as ER  # noqa: E402
from tools.handlers import code_intel as CI  # noqa: E402
from tools.handlers import exec_tools as ET  # noqa: E402
from tools.handlers import file_ops as FO  # noqa: E402
from tools.handlers import git_tools as TGH  # noqa: E402
from tools.handlers import linting as LINT  # noqa: E402
from tools.handlers import send_file as SFH  # noqa: E402
from tools.handlers import _shared as THS  # noqa: E402
from tools.patch_diff import parse_unified_diff  # noqa: E402
from tools.workspace import WorkspaceTransaction  # noqa: E402
from utils import file as UFILE  # noqa: E402
from utils import tool as UTOOL  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def test_1_posix_drive_and_tmp_in_git_bash() -> None:
    """Fix 1: POSIX drive edge cases + TMPDIR & /tmp compatibility under MSYS_NO_PATHCONV=1."""
    if sys.platform == "win32":
        for raw in (
            """python -c "print('/c/items_nonexistent_xyz')" """,
            """$(sed '/c/Users/d' f.txt)""",
            """PATH="/c/Users:$PATH" echo hi""",
            """sed '/c/Users/d' f.txt""",
        ):
            cmd = raw.strip()
            check(f"1a untouched: {cmd}",
                  ET._rewrite_posix_drive_paths(cmd) == cmd,
                  ET._rewrite_posix_drive_paths(cmd))
        check("1b real /c/Users path still rewritten",
              ET._rewrite_posix_drive_paths("ls /c/Users/demo") == "ls C:/Users/demo")
    if sys.platform == "win32" and ET._windows_bash_path():
        with tempfile.TemporaryDirectory(prefix="hm_w2_tmp_") as td:
            ws = Path(td)
            out = asyncio.run(ET._inline_tool_run_bash({
                "cmd": 'f=$(mktemp); echo hello_mktemp > "$f"; python -c "import sys; print(open(sys.argv[1]).read().strip())" "$f"; rm -f "$f"'
            }, ws, str(ws)))
            check("1c mktemp file readable by native python.exe under MSYS_NO_PATHCONV=1",
                  "[run_bash: exit 0]" in out and "hello_mktemp" in out, out)
            out2 = asyncio.run(ET._inline_tool_run_bash({
                "cmd": 'echo hi_tmp > /tmp/hm_w2_probe.txt && python -c "print(open(\'/tmp/hm_w2_probe.txt\').read().strip())" && rm -f /tmp/hm_w2_probe.txt'
            }, ws, str(ws)))
            check("1d /tmp/<name> works across Git Bash and native python.exe",
                  "[run_bash: exit 0]" in out2 and "hi_tmp" in out2, out2)


def test_2_blocklist_bypasses() -> None:
    """Fix 2: _bash_blocklisted & start_background close quoting/--/leading-space bypasses."""
    for bad in ("rm -rf -- /", 'rm -rf "/"', "rm -rf '.'", 'rm -rf "~"', "rm -rf //", "rm -rf .."):
        check(f"2a blocked: {bad!r}", ET._bash_blocklisted(bad) is not None)
    for safe in ("rm -rf .pytest_cache", "rm -rf ./dist", "rm -rf .venv", "rm -rf /tmp/mytest", "rm -rf build"):
        check(f"2b safe: {safe!r}", ET._bash_blocklisted(safe) is None)
    orig_plat = ET.sys.platform
    ET.sys.platform = "win32"
    try:
        for bad_w in (" format C:", " diskpart", "rm -rf C:/", "rm -rf C:\\"):
            check(f"2c win32 blocked: {bad_w!r}", ET._bash_blocklisted(bad_w) is not None)
    finally:
        ET.sys.platform = orig_plat
    if os.name == "nt" and ET._windows_bash_path():
        res_bg = BG.start_background("rd /s /q /c/users")
        check("2d start_background rejects post-rewrite dangerous command",
              res_bg.get("ok") is False and "blocked" in str(res_bg.get("error", "")).lower(),
              str(res_bg))


def test_3_shims_grep_2amp1_uv_pip() -> None:
    """Fix 3: _split_shim_args quote concat, _is_search_no_match 2>&1, uv venv && uv pip."""
    check("3a _split_shim_args quote concat 1", ET._split_shim_args('"a b".bak') == ["a b.bak"])
    check("3b _split_shim_args quote concat 2", ET._split_shim_args('dir/"sub dir"') == ["dir/sub dir"])
    check("3c _is_search_no_match allows 2>&1",
          ET._is_search_no_match("grep -n foo a.txt 2>&1", 1, "", ""))
    check("3d _is_search_no_match still rejects &&",
          not ET._is_search_no_match("python bad.py && grep -n foo a.txt", 1, "", ""))
    with tempfile.TemporaryDirectory(prefix="hm_w2_uv_") as td:
        check("3e uv venv && uv pip install allowed without activate",
              not ET._is_unscoped_pip_install("uv venv && uv pip install pytest", Path(td)))


def test_4_ps_quoting_and_2amp1() -> None:
    """Fix 4: PowerShell && ignores Windows backslashes and 2>&1 wrapper checks $?."""
    cmd_bs = r'cd "C:\proj\" && dir'
    parts = ET._split_unquoted_and(cmd_bs)
    check("4a PS trailing backslash inside double quotes still splits &&",
          len(parts) == 2 and parts[0].strip() == r'cd "C:\proj\"' and parts[1].strip() == "dir",
          str(parts))
    src = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(encoding="utf-8")
    check("4b PS 2>&1 wrapper preserves cmdlet errors via Continue + ErrorRecord $__ok",
          "$ErrorActionPreference='Continue'" in src and "elseif (-not $__ok)" in src)


def test_5_stream_proc_partial_and_closed_fds_timeout() -> None:
    """Fix 5: _stream_proc flushes unterminated partial line and times out after closed stdout."""
    with tempfile.TemporaryDirectory(prefix="hm_w2_sp_") as td:
        p1, _, rc1, to1 = asyncio.run(ET._stream_proc(
            [sys.executable, "-u", "-c", "import sys, time; sys.stdout.write('PARTIAL_NO_NL'); sys.stdout.flush(); time.sleep(5)"],
            1, td, CE.tool_env(),
        ))
        check("5a partial unterminated stdout preserved on timeout",
              to1 and rc1 == -1 and "PARTIAL_NO_NL" in p1, repr(p1))
        if sys.platform != "win32" or ET._windows_bash_path():
            bash_exe = ET._windows_bash_path() if sys.platform == "win32" else ET._BASH_BIN
            _, _, rc2, to2 = asyncio.run(ET._stream_proc(
                [bash_exe, "-c", "exec >/dev/null 2>&1; sleep 5"],
                1, td, CE.tool_env(),
            ))
            check("5b child closing stdout/stderr before sleeping still times out cleanly",
                  to2 and rc2 == -1, f"rc={rc2} to={to2}")


def test_6_and_7_run_python_and_run_tests_flags() -> None:
    """Fix 6 & 7: run_python non-zero with stdout -> RUN_PYTHON_EXEC_ERROR; run_tests rejects '-' flags."""
    with tempfile.TemporaryDirectory(prefix="hm_w2_py_") as td:
        ws = Path(td)
        res_err = asyncio.run(ET._inline_tool_run_python(
            {"code": "print('BEFORE_CRASH'); raise SystemExit(2)"}, ws, str(ws)))
        check("6a run_python non-zero with stdout emits RUN_PYTHON_EXEC_ERROR",
              ER.tool_call_failed(res_err, "run_python")
              and "RUN_PYTHON_EXEC_ERROR" in res_err
              and "BEFORE_CRASH" in res_err,
              res_err[:180])
        res_ok = asyncio.run(ET._inline_tool_run_python({"code": "print('OK_OUT')"}, ws, str(ws)))
        check("6b run_python exit 0 succeeds",
              not ER.tool_call_failed(res_ok, "run_python") and "OK_OUT" in res_ok)

        orig_runner = THS._run_test_suite
        THS._run_test_suite = mock.AsyncMock()
        try:
            rt_bad = asyncio.run(ET._inline_tool_run_tests({"paths": ["--version"]}, ws, str(ws)))
            check("7a run_tests rejects '--version' target as INVALID_ARGUMENT",
                  "INVALID_ARGUMENT" in rt_bad and THS._run_test_suite.call_count == 0,
                  rt_bad)
        finally:
            THS._run_test_suite = orig_runner


def test_8_child_env_strips_engine_venv() -> None:
    """Fix 8: tool_env removes engine venv from front of cur_path behind system Python."""
    fake_eng = Path(r"C:\HiveMind\.venv\Scripts") if sys.platform == "win32" else Path("/opt/hivemind/.venv/bin")
    fake_sys = Path(r"C:\Python314") if sys.platform == "win32" else Path("/usr/bin")
    fake_cur = os.pathsep.join([str(fake_eng), str(fake_sys)])
    with mock.patch.dict(os.environ, {"PATH": fake_cur}, clear=False), \
            mock.patch.object(CE, "_engine_venv_dir", return_value=fake_eng), \
            mock.patch.object(CE, "_find_system_python", return_value=fake_sys / ("python.exe" if sys.platform == "win32" else "python3")):
        env = CE.tool_env()
    entries = env["PATH"].split(os.pathsep)
    check("8 system Python precedes stripped engine venv",
          entries[0] == str(fake_sys) and entries[-1] == str(fake_eng) and entries.count(str(fake_eng)) == 1,
          str(entries))


def test_9_read_file_truncated_range_and_end_zero() -> None:
    """Fix 9: read_file truncated range reports true last shown line and rejects end_line=0."""
    with tempfile.TemporaryDirectory(prefix="hm_w2_rf_") as td:
        ws = Path(td)
        f = ws / "big.txt"
        f.write_text("\n".join(f"line_{i:03d}_payload_" + ("x" * 60) for i in range(1, 101)) + "\n", encoding="utf-8")
        with mock.patch("core.budgets.active", return_value=SimpleNamespace(read_char_cap=1200, outline_items=50)):
            out = asyncio.run(FO._inline_tool_read_file({"path": "big.txt", "start_line": 1, "end_line": 100}, ws, str(ws)))
        check("9a truncated range does not claim line 100 or skip to 101",
              "continue with start_line=101" not in out and "[TRUNCATED:" in out,
              out.splitlines()[0] + " ... " + out.splitlines()[-1])
        out_zero = asyncio.run(FO._inline_tool_read_file({"path": "big.txt", "start_line": 1, "end_line": 0}, ws, str(ws)))
        check("9b end_line=0 returns INVALID_ARGUMENT",
              "INVALID_ARGUMENT" in out_zero, out_zero)
        file_ops_src = (ROOT / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
        check("9c fallback hint uses start_line=1, end_line=50",
              "line_range=" not in file_ops_src and "start_line=1, end_line=50" in file_ops_src)


def test_10_workspace_transaction_preserves_lf() -> None:
    """Fix 10: WorkspaceTransaction preserves LF bytes on Windows."""
    with tempfile.TemporaryDirectory(prefix="hm_w2_wt_") as td:
        p = Path(td) / "script.sh"
        raw_lf = b"#!/bin/sh\necho hi\n"
        p.write_bytes(raw_lf)
        tx = WorkspaceTransaction()
        tx.begin()
        tx.capture_before(p)
        p.write_bytes(b"#!/bin/sh\necho changed\r\n")
        ok, _ = tx.undo_path(p)
        check("10a undo_path restores exact LF bytes",
              ok and p.read_bytes() == raw_lf, repr(p.read_bytes()))
        tx.capture_before(p)
        p.write_bytes(b"dirty\r\n")
        tx.rollback()
        check("10b rollback restores exact LF bytes",
              p.read_bytes() == raw_lf, repr(p.read_bytes()))


def test_11_patch_diff_tab_timestamp() -> None:
    """Fix 11: parse_unified_diff handles diff -u tab-separated timestamps."""
    diff_u = (
        "--- a/foo.py\t2026-11-26 12:00:00.000000000 +0100\n"
        "+++ b/foo.py\t2026-11-26 12:05:00.000000000 +0100\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    fps = parse_unified_diff(diff_u)
    check("11a diff -u with tab timestamp parses cleanly",
          len(fps) == 1 and fps[0].old_path == "foo.py" and fps[0].new_path == "foo.py",
          str(fps))
    diff_new = (
        "--- /dev/null\t1970-01-01 01:00:00 +0000\n"
        "+++ b/new.py\t2026-11-26 12:05:00 +0000\n"
        "@@ -0,0 +1,1 @@\n"
        "+hi\n"
    )
    fps2 = parse_unified_diff(diff_new)
    check("11b /dev/null with tab timestamp creates file",
          len(fps2) == 1 and fps2[0].old_path is None and fps2[0].new_path == "new.py",
          str(fps2))


def test_12_find_files_and_search_code() -> None:
    """Fix 12: find_files src/**/*.py finds direct children; search_code checks junctions pre-resolve."""
    with tempfile.TemporaryDirectory(prefix="hm_w2_ci_") as td:
        ws = Path(td)
        (ws / "src").mkdir()
        (ws / "src" / "direct.py").write_text("x = 1\n", encoding="utf-8")
        (ws / "src" / "sub").mkdir()
        (ws / "src" / "sub" / "nested.py").write_text("y = 2\n", encoding="utf-8")
        out_ff = asyncio.run(CI._inline_tool_find_files({"pattern": "src/**/*.py"}, ws, str(ws)))
        check("12a find_files src/**/*.py includes direct and nested children",
              "direct.py" in out_ff and "nested.py" in out_ff, out_ff)

        (ws / "junc").mkdir()
        (ws / "junc" / "secret_hit.txt").write_text("NEEDLE_JUNC\n", encoding="utf-8")
        with mock.patch.object(UFILE, "_is_junction", side_effect=lambda p: Path(p).name == "junc"):
            out_sc = asyncio.run(CI._inline_tool_search_code({"pattern": "NEEDLE_JUNC", "path": "junc/secret_hit.txt"}, ws, str(ws)))
        check("12b search_code rejects junction before resolve",
              "PATH_JUNCTION_DENIED" in out_sc, out_sc)


def test_13_git_and_send_file_hardening() -> None:
    """Fix 13: git_commit dir reject & env scrub, log cap, token redaction, send_file lock."""
    with tempfile.TemporaryDirectory(prefix="hm_w2_gs_") as td:
        ws = Path(td)
        (ws / "config").mkdir()
        (ws / "config" / ".env").write_text("SECRET=1\n", encoding="utf-8")
        with mock.patch.object(THS, "_GIT_TOOLS_AVAILABLE", True), \
                mock.patch.object(THS, "exec_git_commit", new=mock.AsyncMock(return_value="OK")):
            res_dir = asyncio.run(TGH._inline_tool_git_commit({"message": "m", "paths": ["config"]}, ws, str(ws)))
            check("13a git_commit rejects directory path", "INVALID_ARGUMENT" in res_dir, res_dir)

        called_argvs: list[tuple] = []

        class _FakeGitProc:
            returncode = 0

            async def communicate(self):
                return b"abc init\n", b""

        async def _fake_sub(*argv, **kw):
            called_argvs.append((list(argv), kw.get("env") or {}))
            return _FakeGitProc()

        with mock.patch.dict(os.environ, {"HIVEMIND_INTERNAL_TOKEN": "topsec"}, clear=False), \
                mock.patch.object(THS, "_GIT_TOOLS_AVAILABLE", True), \
                mock.patch.object(TGH.asyncio, "create_subprocess_exec", side_effect=_fake_sub):
            asyncio.run(TGH._inline_tool_git_status({"cmd": "log"}, ws, str(ws)))
        check("13b git_status uses scrubbed env and caps log",
              bool(called_argvs)
              and "-n" in called_argvs[0][0]
              and "HIVEMIND_INTERNAL_TOKEN" not in called_argvs[0][1],
              str(called_argvs))

        with mock.patch.object(HGT, "_git_auto_push_enabled", return_value=True), \
                mock.patch.object(HGT, "auto_commit_block_reason", return_value=""), \
                mock.patch.object(HGT, "_load_settings", return_value={
                    "git_repo_url": "https://example.com/r.git",
                    "git_username": "u",
                    "git_token": "tok/with/slash",
                }), \
                mock.patch.object(HGT, "_git_run", new=mock.AsyncMock(
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr="fatal: tok%2Fwith%2Fslash bad")
                )):
            push_err = asyncio.run(HGT.push_after_commit(str(ws)))
        check("13c push_after_commit redacts slash-escaped token",
              "tok%2Fwith%2Fslash" not in push_err and "***" in push_err, push_err)

        (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
        sf_out = asyncio.run(SFH._inline_tool_send_file({"path": ".env"}, ws, str(ws)))
        check("13d send_file enforces _inline_check_workspace on secret path",
              "SECRET_PATH_DENIED" in sf_out or "SEND_FILE_BLOCKED" in sf_out, sf_out)


def test_14_utils_and_linting() -> None:
    """Fix 14 & 15: _repair_json_backslashes, normalize_tool_path /C:/, symlink loop, linting env/reap."""
    raw = r'{"path": "C:\\Users\\demo\\a.py", "content": "C:\work"}'
    repaired = UTOOL._repair_json_backslashes(raw)
    parsed = json.loads(repaired)
    check("14a _repair_json_backslashes keeps valid \\\\ and fixes lone \\w",
          parsed["path"] == r"C:\Users\demo\a.py" and parsed["content"] == r"C:\work",
          repr(repaired))

    with mock.patch.object(UFILE.os, "name", "nt"):
        nt_p = UFILE.normalize_tool_path("/C:/ws/a.py")
    check("14b normalize_tool_path strips leading MSYS / on win32",
          nt_p == "c:/ws/a.py" or nt_p.startswith("c:/"), repr(nt_p))

    with tempfile.TemporaryDirectory(prefix="hm_w2_loop_") as td:
        ws = Path(td)
        probe = ws / "loop_target"
        with mock.patch.object(Path, "resolve", side_effect=RuntimeError("Symlink loop")):
            err_loop = UFILE._inline_check_workspace(probe, str(ws), "read_file")
        check("14c _inline_check_workspace catches symlink loop RuntimeError",
              isinstance(err_loop, str) and "PATH_OUTSIDE_WORKSPACE" in err_loop, repr(err_loop))

        captured_envs: list[dict] = []
        killed: list[object] = []

        async def _fake_timeout_exec(*argv, **kw):
            captured_envs.append(kw.get("env") or {})
            return SimpleNamespace(
                communicate=mock.AsyncMock(side_effect=asyncio.TimeoutError()),
                pid=99999,
            )

        async def _fake_kill_reap(proc):
            killed.append(proc)

        with mock.patch.dict(os.environ, {"HIVEMIND_INTERNAL_TOKEN": "sec"}, clear=False), \
                mock.patch.object(LINT.asyncio, "create_subprocess_exec", side_effect=_fake_timeout_exec), \
                mock.patch.object(LINT, "_lint_kill_reap", side_effect=_fake_kill_reap):
            out_pyr = asyncio.run(LINT._pyright_lint_result(["pyright"], ws / "a.py", ws))
        check("15 linting._pyright_lint_result uses tool_env and kill_tree_and_reap on timeout",
              "timed out" in out_pyr
              and len(killed) == 1
              and bool(captured_envs)
              and "HIVEMIND_INTERNAL_TOKEN" not in captured_envs[0],
              f"killed={len(killed)} out={out_pyr!r}")


def main() -> int:
    test_1_posix_drive_and_tmp_in_git_bash()
    test_2_blocklist_bypasses()
    test_3_shims_grep_2amp1_uv_pip()
    test_4_ps_quoting_and_2amp1()
    test_5_stream_proc_partial_and_closed_fds_timeout()
    test_6_and_7_run_python_and_run_tests_flags()
    test_8_child_env_strips_engine_venv()
    test_9_read_file_truncated_range_and_end_zero()
    test_10_workspace_transaction_preserves_lf()
    test_11_patch_diff_tab_timestamp()
    test_12_find_files_and_search_code()
    test_13_git_and_send_file_hardening()
    test_14_utils_and_linting()
    return h.finish("wave2_tools_and_bash")


if __name__ == "__main__":
    raise SystemExit(main())
