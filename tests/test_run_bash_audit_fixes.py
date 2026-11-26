# -*- coding: utf-8 -*-
"""Regression suite for the 12 run_bash deep-audit fixes.

Covers:
  1. POSIX drive rewrite only touches real drive mounts, leaving sed/grep/awk regexes intact.
  2. _stream_proc preserves head + tail + omission banner when output exceeds line_cap.
  3. RUN_BASH_NONZERO truncation preserves [stderr] alongside stdout tail when >3400 chars.
  4. Windows mkdir/touch/rm shims support single quotes, flags (-c, -v, -f), globs, and lock guards.
  5. _bash_blocklisted avoids substring false positives (.pytest_cache, grep "format ") while blocking real danger.
  6. tool_env() sets MSYS_NO_PATHCONV=1 and MSYS2_ARG_CONV_EXCL=* on win32 so POSIX-like args stay intact.
  7. Timeout responses include partial stdout/stderr before timeout.
  8. start_background runs Git Bash on Windows when _run_bash_shell_mode() == "bash".
  9. search_code preserves $, `, ", ' across argv backends without stripping.
 10. run_bash preserves leading indentation on line 1 of stdout.
 11. Standalone grep/rg exit 1 with empty stdout/stderr returns clean no-match info (not RUN_BASH_NONZERO).
 12. PowerShell fallback quote-aware && and 2>&1 helpers ignore quoted operators.

Run: python tests/test_run_bash_audit_fixes.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402
from infra.child_env import tool_env  # noqa: E402
from tools import background as bg  # noqa: E402
from tools.handlers import code_intel as ci  # noqa: E402
from tools.handlers import exec_tools as et  # noqa: E402
from utils.tool import run_bash_failed  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def test_1_drive_path_rewrite_preserves_sed_grep_awk() -> None:
    """Fix #1: sed/grep/awk regexes must never be corrupted into X:/."""
    untouched = (
        "sed -n '/a/,/b/p' f.js",
        "sed '/x/d' f.js",
        "sed -n '/c/p' f.js",
        "sed 's/ /x/g' f.js",
        "sed 's/(a)/b/g' f.js",
        "sed '/c/s/old/new/g' f.js",
        "sed 's/foo /c/gp' f.js",
        "grep '/v/' f.js",
        "grep -E '(/a/|/b/)' f.js",
        "grep '/c/nonexistent_seg_xyz' f.js",
        "awk '/p/ {print}' f.js",
        "awk '/c/foo/ {print}' f.js",
    )
    for cmd in untouched:
        got = et._rewrite_posix_drive_paths(cmd, existing_drives={"c", "d"})
        if cmd in ("sed '/c/s/old/new/g' f.js", "grep '/c/nonexistent_seg_xyz' f.js", "awk '/c/foo/ {print}' f.js") and sys.platform == "win32":
            # Also verify live filesystem drive-existence check (existing_drives=None)
            got = et._rewrite_posix_drive_paths(cmd)
        check(f"1a untouched: {cmd}", got == cmd, f"got={got!r}")

    rewritten = et._rewrite_posix_drive_paths("sed -n '1,5p' /c/Users/demo/f.js", existing_drives={"c"})
    check("1b real drive mount still rewritten",
          rewritten == "sed -n '1,5p' C:/Users/demo/f.js",
          f"got={rewritten!r}")

    if sys.platform == "win32" and et._windows_bash_path():
        with tempfile.TemporaryDirectory(prefix="hm_rb_sed_") as td:
            ws = Path(td)
            f = ws / "sample.txt"
            f.write_text("alpha\n/a/ match\n/b/ end\nomega\n", encoding="utf-8")
            out = asyncio.run(et._inline_tool_run_bash(
                {"cmd": "sed -n '/a/,/b/p' sample.txt"}, ws, str(ws)))
            check("1c end-to-end sed range in run_bash succeeds",
                  "[run_bash: exit 0]" in out and "/a/ match" in out and "/b/ end" in out,
                  f"out={out!r}")


def test_2_stream_proc_head_tail_banner() -> None:
    """Fix #2: >line_cap lines preserve head + omission banner + tail."""
    with tempfile.TemporaryDirectory(prefix="hm_rb_stream_") as td:
        script = "for i in range(1, 451): print(f'LINE_{i:03d}')"
        out, err, rc, timed_out = asyncio.run(
            et._stream_proc([sys.executable, "-c", script], 15, td, tool_env(), line_cap=300)
        )
        lines = out.splitlines()
        check("2a exit 0 and not timed out", rc == 0 and not timed_out and not err, f"rc={rc}")
        check("2b preserves head LINE_001..LINE_060",
              lines[0] == "LINE_001" and lines[59] == "LINE_060",
              f"head={lines[:2]}..{lines[58:61]}")
        check("2c inserts omitted-lines banner at boundary",
              len(lines) == 301
              and lines[60] == "[... 150 line(s) omitted of 450 total (line cap 300) — narrow output with head/tail/sed -n/grep ...]",
              f"len={len(lines)} banner={lines[60] if len(lines) > 60 else None!r}")
        check("2d preserves tail LINE_211..LINE_450",
              lines[61] == "LINE_211" and lines[-1] == "LINE_450",
              f"tail_bounds={lines[61]!r}..{lines[-1]!r}")


def test_3_nonzero_preserves_stderr_over_3400() -> None:
    """Fix #3: RUN_BASH_NONZERO keeps [stderr] error visible even with large stdout."""
    with tempfile.TemporaryDirectory(prefix="hm_rb_stderr_") as td:
        ws = Path(td)
        cmd = (
            'python -c "import sys; '
            'sys.stderr.write(\'CRITICAL_STDERR_CAUSE\\n\'); '
            '[print(f\'STDOUT_PADDING_LINE_{i:03d}_\' + \'x\'*40) for i in range(120)]; '
            'sys.exit(2)"'
        )
        out = asyncio.run(et._inline_tool_run_bash({"cmd": cmd}, ws, str(ws)))
        check("3a RUN_BASH_NONZERO raised", "RUN_BASH_NONZERO" in out)
        check("3b [stderr] and CRITICAL_STDERR_CAUSE preserved within 4000 chars",
              "[stderr]" in out[:4000]
              and "CRITICAL_STDERR_CAUSE" in out[:4000]
              and "STDOUT_PADDING_LINE_119_" in out[:4000],
              f"len={len(out)} snippet={out[:300]!r}")


def test_4_windows_shims_quotes_flags_globs() -> None:
    """Fix #4: Windows mkdir/touch/rm shims support single quotes, flags, globs, and guards."""
    with tempfile.TemporaryDirectory(prefix="hm_rb_shim_") as td:
        ws = Path(td)
        orig_plat = et.sys.platform
        et.sys.platform = "win32"
        try:
            r_mk = asyncio.run(et._inline_tool_run_bash(
                {"cmd": "mkdir -p -v 'my dir'"}, ws, str(ws)))
            check("4a mkdir handles -v flag and single-quoted space dir",
                  "[exit 0" in r_mk and (ws / "my dir").is_dir() and not (ws / "-v").exists(),
                  f"res={r_mk!r}")

            r_tc_no = asyncio.run(et._inline_tool_run_bash(
                {"cmd": "touch -c 'ghost file.txt'"}, ws, str(ws)))
            check("4b touch -c does not create missing file or file named -c",
                  "[exit 0" in r_tc_no
                  and not (ws / "-c").exists()
                  and not (ws / "ghost file.txt").exists(),
                  f"res={r_tc_no!r}")

            r_tc = asyncio.run(et._inline_tool_run_bash(
                {"cmd": "touch 'my dir/my file.txt'"}, ws, str(ws)))
            check("4c touch creates single-quoted path with spaces",
                  "[exit 0" in r_tc and (ws / "my dir" / "my file.txt").is_file(),
                  f"res={r_tc!r}")

            (ws / "a.bak").write_text("1", encoding="utf-8")
            (ws / "b.bak").write_text("2", encoding="utf-8")
            (ws / "keep.txt").write_text("3", encoding="utf-8")

            r_glob = asyncio.run(et._inline_tool_run_bash({"cmd": "rm *.bak"}, ws, str(ws)))
            check("4d rm *.bak expands glob and leaves keep.txt",
                  "[exit 0 - removed:" in r_glob
                  and not (ws / "a.bak").exists()
                  and not (ws / "b.bak").exists()
                  and (ws / "keep.txt").exists(),
                  f"res={r_glob!r}")

            r_miss = asyncio.run(et._inline_tool_run_bash({"cmd": "rm *.nope"}, ws, str(ws)))
            check("4e rm *.nope without -f reports [rm error]",
                  "[rm error]" in r_miss and "removed:" not in r_miss,
                  f"res={r_miss!r}")

            r_miss_f = asyncio.run(et._inline_tool_run_bash({"cmd": "rm -f *.nope"}, ws, str(ws)))
            check("4f rm -f *.nope does not lie removed",
                  "[exit 0 - nothing to remove]" in r_miss_f and "removed:" not in r_miss_f,
                  f"res={r_miss_f!r}")

            r_rm_sq = asyncio.run(et._inline_tool_run_bash(
                {"cmd": "rm 'my dir/my file.txt'"}, ws, str(ws)))
            check("4g rm handles single-quoted path with spaces",
                  "[exit 0 - removed:" in r_rm_sq and not (ws / "my dir" / "my file.txt").exists(),
                  f"res={r_rm_sq!r}")

            r_esc = asyncio.run(et._inline_tool_run_bash(
                {"cmd": "rm ../*.txt"}, ws, str(ws)))
            check("4h rm wildcard outside workspace lock is refused",
                  "rm error" in r_esc,
                  f"res={r_esc!r}")
        finally:
            et.sys.platform = orig_plat


def test_5_blocklist_boundaries() -> None:
    """Fix #5: no false positives on .pytest_cache / 'format ', real danger blocked."""
    safe_cmds = (
        "rm -rf .pytest_cache",
        "rm -rf ./dist",
        "rm -rf .venv",
        "rm -rf /tmp/mytest",
        "chmod 777 /tmp/a",
        'grep "format " a.py',
        "sed 's/format /fmt /' a.txt",
        'git log --format "%h" -n 5',
    )
    for cmd in safe_cmds:
        check(f"5a safe allowed: {cmd}", et._bash_blocklisted(cmd) is None)

    dangerous_common = (
        "rm -rf /",
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf ~/",
        "rm -rf .",
        "rm -rf ./",
        "rm -rfv .",
        "chmod 777 /",
    )
    for cmd in dangerous_common:
        check(f"5b dangerous blocked: {cmd}", et._bash_blocklisted(cmd) is not None)

    orig_plat = et.sys.platform
    et.sys.platform = "win32"
    try:
        for cmd in ("format C:", "format D: /q", "echo x && format E:", "diskpart", "echo x; diskpart.exe"):
            check(f"5c win32 dangerous blocked: {cmd}", et._bash_blocklisted(cmd) is not None)
    finally:
        et.sys.platform = orig_plat


def test_6_msys_no_pathconv() -> None:
    """Fix #6: tool_env sets MSYS_NO_PATHCONV=1 on win32 so /api/v1/users stays verbatim."""
    env = tool_env()
    if sys.platform == "win32":
        check("6a tool_env sets MSYS_NO_PATHCONV=1", env.get("MSYS_NO_PATHCONV") == "1")
        check("6b tool_env sets MSYS2_ARG_CONV_EXCL=*", env.get("MSYS2_ARG_CONV_EXCL") == "*")
        if et._windows_bash_path():
            with tempfile.TemporaryDirectory(prefix="hm_rb_msys_") as td:
                ws = Path(td)
                out = asyncio.run(et._inline_tool_run_bash(
                    {"cmd": 'python -c "import sys; print(sys.argv[1])" "/api/v1/users"'},
                    ws, str(ws)))
                check("6c Git Bash preserves /api/v1/users arg for python.exe",
                      "[run_bash: exit 0]" in out
                      and "/api/v1/users" in out
                      and "Program Files/Git" not in out,
                      f"out={out!r}")
    else:
        check("6a POSIX host check ok", isinstance(env, dict))


def test_7_timeout_preserves_partial_output() -> None:
    """Fix #7: RUN_BASH_TIMEOUT and RUN_PYTHON_TIMEOUT preserve partial output."""
    async def _fake_timeout_stream(cmd, timeout_s, cwd, env, line_cap=300):
        return "PARTIAL_STDOUT_LINE", "PARTIAL_STDERR_LINE", -1, True

    with tempfile.TemporaryDirectory(prefix="hm_rb_to_") as td:
        ws = Path(td)
        with mock.patch.object(et, "_stream_proc", side_effect=_fake_timeout_stream):
            out = asyncio.run(et._inline_tool_run_bash({"cmd": "echo slow"}, ws, str(ws)))
        check("7a run_bash timeout includes partial stdout and stderr",
              "RUN_BASH_TIMEOUT" in out
              and "[partial output before timeout]" in out
              and "PARTIAL_STDOUT_LINE" in out
              and "PARTIAL_STDERR_LINE" in out,
              f"out={out!r}")

        with mock.patch.object(et, "_stream_proc", side_effect=_fake_timeout_stream):
            py_out = asyncio.run(et._inline_tool_run_python({"code": "print(1)"}, ws, str(ws)))
        check("7b run_python timeout includes partial stdout and stderr",
              "RUN_PYTHON_TIMEOUT" in py_out
              and "[partial output before timeout]" in py_out
              and "PARTIAL_STDOUT_LINE" in py_out
              and "PARTIAL_STDERR_LINE" in py_out,
              f"py_out={py_out!r}")


def test_8_start_background_uses_git_bash_on_windows() -> None:
    """Fix #8: start_background uses Git Bash on Windows when _run_bash_shell_mode() == 'bash'."""
    if sys.platform != "win32" or not et._windows_bash_path():
        check("8a non-win32 / no Git Bash skip ok", True)
        return
    with tempfile.TemporaryDirectory(prefix="hm_rb_bg_") as td:
        ws = Path(td)
        res = bg.start_background(
            'MYVAR=hello_bg_bash python -c "import os; print(os.environ.get(\'MYVAR\'))"',
            cwd=ws,
        )
        check("8a start_background ok", bool(res.get("ok")), f"res={res!r}")
        handle = res.get("handle", "")
        if handle:
            try:
                deadline = time.time() + 5.0
                out = ""
                while time.time() < deadline:
                    out = bg.get_background_output(handle)
                    if "hello_bg_bash" in out:
                        break
                    time.sleep(0.05)
                check("8b POSIX env assignment works in start_background on Windows",
                      "hello_bg_bash" in out, f"out={out!r}")
            finally:
                bg.stop_background(handle)


def test_9_search_code_preserves_special_chars() -> None:
    """Fix #9: search_code preserves single quotes, backticks, double quotes, and $var."""
    with tempfile.TemporaryDirectory(prefix="hm_rb_sc_") as td:
        ws = Path(td)
        (ws / "data.txt").write_text(
            "hit_sq = 'single_needle'\n"
            "hit_bt = `tick_needle`\n"
            'hit_dq = "double_needle"\n'
            "hit_dl = $dollar_needle\n",
            encoding="utf-8",
        )
        for pat in ("'single_needle'", "`tick_needle`", '"double_needle"', r"\$dollar_needle"):
            out = asyncio.run(ci._inline_tool_search_code({"pattern": pat, "path": "data.txt"}, ws, str(ws)))
            needle = "$dollar_needle" if pat.startswith(r"\$") else pat
            check(f"9a live search_code finds {pat}",
                  f"[search_code: {pat}]" in out and needle in out,
                  f"out={out!r}")

        captured_posix: list[list[str]] = []

        class _FakeExec:
            returncode = 0
            stdout = None

            async def communicate(self):
                return b"data.txt:4:hit_dl = $dollar_needle\n", b""

        async def _fake_sub(*argv, **kw):
            captured_posix.append(list(argv))
            return _FakeExec()

        with mock.patch.object(ci.sys, "platform", "linux"), \
                mock.patch.object(ci.shutil, "which", return_value=None), \
                mock.patch.object(ci.asyncio, "create_subprocess_exec", side_effect=_fake_sub):
            asyncio.run(ci._inline_tool_search_code({"pattern": "$dollar_needle", "path": "data.txt"}, ws, str(ws)))
        check("9b POSIX grep receives raw $ pattern via argv",
              bool(captured_posix) and "$dollar_needle" in captured_posix[0],
              f"argv={captured_posix}")


def test_10_preserve_leading_indent_on_line1() -> None:
    """Fix #10: run_bash keeps leading indentation on line 1."""
    with tempfile.TemporaryDirectory(prefix="hm_rb_ind_") as td:
        ws = Path(td)
        out = asyncio.run(et._inline_tool_run_bash(
            {"cmd": 'python -c "print(\'    const x = 1;\\n    const y = 2;\')"'},
            ws, str(ws),
        ))
        check("10 preserves leading 4 spaces on first output line",
              "[run_bash: exit 0]\n    const x = 1;" in out,
              f"out={out!r}")


def test_11_grep_no_match_not_error() -> None:
    """Fix #11: standalone grep/rg exit 1 with empty stdout+stderr is not RUN_BASH_NONZERO."""
    with tempfile.TemporaryDirectory(prefix="hm_rb_grep_") as td:
        ws = Path(td)
        (ws / "sample.txt").write_text("hello world\n", encoding="utf-8")

        out_none = asyncio.run(et._inline_tool_run_bash(
            {"cmd": "grep -n nonexistent_token sample.txt"}, ws, str(ws)))
        check("11a grep exit 1 returns clean no-match message",
              "[run_bash: exit 1 — no matches found]" in out_none
              and "RUN_BASH_NONZERO" not in out_none
              and not run_bash_failed(out_none),
              f"out={out_none!r}")

        out_err = asyncio.run(et._inline_tool_run_bash(
            {"cmd": "grep -n nonexistent_token does_not_exist.txt"}, ws, str(ws)))
        check("11b grep missing file (exit 2 + stderr) stays RUN_BASH_NONZERO",
              "RUN_BASH_NONZERO" in out_err and run_bash_failed(out_err),
              f"out={out_err!r}")

        out_compound = asyncio.run(et._inline_tool_run_bash(
            {"cmd": 'python -c "import sys; sys.exit(1)" && grep -n hello sample.txt'},
            ws, str(ws)))
        check("11c compound command ending in grep still reports RUN_BASH_NONZERO on prior failure",
              "RUN_BASH_NONZERO" in out_compound and run_bash_failed(out_compound),
              f"out={out_compound!r}")


def test_12_quote_aware_ps_helpers() -> None:
    """Fix #12: _split_unquoted_and and _has_unquoted_token ignore quoted strings."""
    q_and = 'node -e "if (a && b) console.log(1)"'
    check("12a quoted && not split", et._split_unquoted_and(q_and) == [q_and])
    check("12b unquoted && split cleanly",
          [p.strip() for p in et._split_unquoted_and('echo "a && b" && echo ok')]
          == ['echo "a && b"', "echo ok"])
    check("12c quoted 2>&1 ignored",
          not et._has_unquoted_token('echo "use 2>&1"', "2>&1"))
    check("12d unquoted 2>&1 detected",
          et._has_unquoted_token('echo "ok" 2>&1', "2>&1"))


def main() -> int:
    test_1_drive_path_rewrite_preserves_sed_grep_awk()
    test_2_stream_proc_head_tail_banner()
    test_3_nonzero_preserves_stderr_over_3400()
    test_4_windows_shims_quotes_flags_globs()
    test_5_blocklist_boundaries()
    test_6_msys_no_pathconv()
    test_7_timeout_preserves_partial_output()
    test_8_start_background_uses_git_bash_on_windows()
    test_9_search_code_preserves_special_chars()
    test_10_preserve_leading_indent_on_line1()
    test_11_grep_no_match_not_error()
    test_12_quote_aware_ps_helpers()
    return h.finish("run_bash_audit_fixes")


if __name__ == "__main__":
    raise SystemExit(main())
