# -*- coding: utf-8 -*-
"""Regression suite: workspace .venv enforcement + HIVEMIND.md project rules.

Covers:
  1. infra/child_env.py 3-tier Python resolution (workspace .venv > real system
     Python > engine venv fallback), WindowsApps stub filtering, and engine
     VIRTUAL_ENV scrubbing.
  2. tools/handlers/exec_tools.py strict workspace .venv guard for pip install
     in run_bash (PIP_NO_WORKSPACE_VENV).
  3. hive_functions/project_rules.py discovery priority (HIVEMIND.md >
     .hivemind/HIVEMIND.md > AGENTS.md > fallback CLAUDE.md), containment,
     budget truncation, and wiring in duo_runner + direct_runner.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _make_dummy_py(ws: Path, folder: str = ".venv") -> Path:
    p = (
        ws / folder / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else ws / folder / "bin" / "python"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    return p


def test_python_resolution_and_tool_env() -> None:
    from infra import child_env as ce

    with tempfile.TemporaryDirectory(prefix="hm_pyres_") as td:
        tmp = Path(td)
        ws = tmp / "proj"
        ws.mkdir()
        fake_sys_dir = tmp / "sys_py"
        fake_sys_dir.mkdir()
        fake_sys_py = fake_sys_dir / ("python.exe" if sys.platform == "win32" else "python3")
        fake_sys_py.write_text("", encoding="utf-8")

        fake_eng_dir = tmp / "eng_venv" / ("Scripts" if sys.platform == "win32" else "bin")
        fake_eng_dir.mkdir(parents=True)
        fake_eng_py = fake_eng_dir / ("python.exe" if sys.platform == "win32" else "python")
        fake_eng_py.write_text("", encoding="utf-8")

        # 1a: WindowsApps stub is ignored on Windows
        if sys.platform == "win32":
            stub = Path(r"C:\Users\demo\AppData\Local\Microsoft\WindowsApps\python.exe")
            check("1a WindowsApps stub flagged", ce._is_windows_store_python(stub))

        # 1b: No workspace venv + real system Python -> system Python wins, engine appended
        with mock.patch.object(ce.sys, "executable", str(fake_eng_py)), \
                mock.patch.dict(os.environ, {
                    "PATH": str(fake_sys_dir),
                    "VIRTUAL_ENV": str(fake_eng_dir.parent),
                }, clear=False):
            resolved = ce.resolve_workspace_python(ws)
            check("1b1 resolve_workspace_python picks real system Python over engine venv",
                  resolved is not None and resolved.resolve() == fake_sys_py.resolve(),
                  f"got={resolved}")
            env = ce.tool_env(workspace=ws)
            path_parts = env["PATH"].split(os.pathsep)
            check("1b2 tool_env puts system Python ahead of engine venv fallback",
                  path_parts.index(str(fake_sys_dir)) < path_parts.index(str(fake_eng_dir)),
                  f"parts={path_parts}")
            check("1b3 tool_env scrubs HiveMind's own VIRTUAL_ENV",
                  "VIRTUAL_ENV" not in env, f"venv={env.get('VIRTUAL_ENV')}")

        # 1c: No workspace venv + NO system Python -> engine venv prepended as fallback
        empty_dir = tmp / "empty_path"
        empty_dir.mkdir()
        with mock.patch.object(ce.sys, "executable", str(fake_eng_py)), \
                mock.patch.dict(os.environ, {"PATH": str(empty_dir)}, clear=False):
            resolved_fb = ce.resolve_workspace_python(ws)
            check("1c1 resolve_workspace_python falls back to engine venv when no system Python",
                  resolved_fb is not None and resolved_fb.resolve() == fake_eng_py.resolve(),
                  f"got={resolved_fb}")
            env_fb = ce.tool_env(workspace=ws)
            check("1c2 tool_env prepends engine venv when PATH has no system Python",
                  env_fb["PATH"].split(os.pathsep)[0] == str(fake_eng_dir),
                  f"first={env_fb['PATH'].split(os.pathsep)[0]}")

        # 1d: Workspace .venv wins over both system Python and engine venv
        ws_py = _make_dummy_py(ws, ".venv")
        with mock.patch.object(ce.sys, "executable", str(fake_eng_py)), \
                mock.patch.dict(os.environ, {"PATH": str(fake_sys_dir)}, clear=False):
            resolved_ws = ce.resolve_workspace_python(ws)
            check("1d1 workspace .venv beats system Python and engine venv",
                  resolved_ws is not None and resolved_ws.resolve() == ws_py.resolve(),
                  f"got={resolved_ws}")
            env_ws = ce.tool_env(workspace=ws)
            check("1d2 tool_env prepends workspace .venv and sets VIRTUAL_ENV",
                  env_ws["PATH"].split(os.pathsep)[0] == str(ws_py.parent)
                  and env_ws.get("VIRTUAL_ENV") == str(ws_py.parent.parent),
                  f"first={env_ws['PATH'].split(os.pathsep)[0]} venv={env_ws.get('VIRTUAL_ENV')}")


def test_run_bash_pip_venv_guard() -> None:
    from tools.handlers.exec_tools import _is_unscoped_pip_install, _inline_tool_run_bash

    with tempfile.TemporaryDirectory(prefix="hm_pipguard_") as td:
        ws = Path(td)
        # 2a: Blocked commands without workspace .venv
        for cmd in (
            "pip install pygame-ce",
            "pip3 install requests",
            "python -m pip install flask",
            "python3 -m pip install -r requirements.txt",
            "uv pip install pytest",
            "cd sub && pip install numpy",
            "python -m venv .venv && pip install pygame-ce",
        ):
            check(f"2a blocks unscoped pip: {cmd!r}",
                  _is_unscoped_pip_install(cmd, ws) is True)

        # 2b: Allowed commands without workspace .venv
        for cmd in (
            "pip list",
            "pip --version",
            "python -m pip show pytest",
            'echo "pip install pygame"',
            "python -m venv .venv && .venv/Scripts/python -m pip install pygame-ce",
            "uv pip install --python .venv pygame-ce",
        ):
            check(f"2b allows safe cmd: {cmd!r}",
                  _is_unscoped_pip_install(cmd, ws) is False)

        # 2c: End-to-end run_bash returns PIP_NO_WORKSPACE_VENV
        res = asyncio.run(_inline_tool_run_bash({"cmd": "pip install pygame-ce"}, ws, str(ws)))
        check("2c run_bash refuses pip install with PIP_NO_WORKSPACE_VENV",
              "[TOOL_ERROR:PIP_NO_WORKSPACE_VENV]" in res and "python -m venv .venv" in res,
              f"res={res[:180]!r}")

        # 2d: Once workspace .venv exists, pip install is allowed
        _make_dummy_py(ws, ".venv")
        check("2d pip install allowed once workspace .venv exists",
              _is_unscoped_pip_install("pip install pygame-ce", ws) is False)


def test_project_rules_loader() -> None:
    from hive_functions.project_rules import format_project_rules, load_project_rules

    with tempfile.TemporaryDirectory(prefix="hm_rules_") as td:
        tmp = Path(td)
        # Parent rule must NEVER leak into child workspace
        (tmp / "HIVEMIND.md").write_text("PARENT_SECRET_RULE", encoding="utf-8")
        ws = tmp / "repo"
        ws.mkdir()

        # 3a: Empty workspace returns empty
        blk0, names0 = format_project_rules(ws)
        check("3a empty workspace ignores parent HIVEMIND.md",
              blk0 == "" and names0 == [], f"names={names0}")

        # 3b: CLAUDE.md fallback works when primary files absent
        (ws / "CLAUDE.md").write_text("Use pytest -q.", encoding="utf-8")
        blk_c, names_c = format_project_rules(ws)
        check("3b CLAUDE.md loaded as fallback",
              names_c == ["CLAUDE.md"] and "Use pytest -q." in blk_c,
              f"names={names_c}")

        # 3c: HIVEMIND.md + AGENTS.md take priority over CLAUDE.md
        (ws / "HIVEMIND.md").write_text("Always use .venv.", encoding="utf-8")
        (ws / "AGENTS.md").write_text("English comments only.", encoding="utf-8")
        blk_p, names_p = format_project_rules(ws)
        check("3c HIVEMIND.md + AGENTS.md supersede CLAUDE.md in order",
              names_p == ["HIVEMIND.md", "AGENTS.md"]
              and "Always use .venv." in blk_p
              and "English comments only." in blk_p
              and "Use pytest -q." not in blk_p,
              f"names={names_p}")

        # 3d: Budget truncation stays within max_chars and marks truncation
        (ws / "HIVEMIND.md").write_text("\n".join(f"Rule line {i}: keep code clean" for i in range(400)), encoding="utf-8")
        txt_cap, _ = load_project_rules(ws, max_chars=400)
        check("3d truncation caps cleanly with marker",
              len(txt_cap) <= 400 and "truncated" in txt_cap,
              f"len={len(txt_cap)}")

        # 3e: Symlink escape outside workspace is refused (when OS allows symlinks)
        outside = tmp / "secret.md"
        outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
        ws_sym = tmp / "sym_repo"
        ws_sym.mkdir()
        try:
            os.symlink(outside, ws_sym / "HIVEMIND.md")
            sym_ok = True
        except OSError:
            sym_ok = False
        if sym_ok:
            blk_s, names_s = format_project_rules(ws_sym)
            check("3e symlink outside workspace is refused",
                  blk_s == "" and names_s == [] and "OUTSIDE_SECRET" not in blk_s)

    # 3f: Wiring in duo_runner.py and direct_runner.py
    duo_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    direct_src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
    check("3f1 duo_runner wires format_project_rules for coder and planner",
          "from hive_functions.project_rules import format_project_rules" in duo_src
          and "_project_rules_block=_project_rules_planner" in duo_src)
    check("3f2 duo_runner gates project rules to first prompt in chat only",
          "if _ws_str and _is_first_chat_turn:" in duo_src
          and 'not bool(getattr(ctx, "pipeline_sess_msgs", None))' in duo_src)
    check("3f3 direct_runner wires format_project_rules on first turn only without mutating sys_p",
          "from hive_functions.project_rules import format_project_rules" in direct_src
          and "if _direct_ws and _direct_first_turn:" in direct_src
          and 'direct_input = _direct_rules + "\\n\\n" + direct_input' in direct_src)


def main() -> int:
    test_python_resolution_and_tool_env()
    test_run_bash_pip_venv_guard()
    test_project_rules_loader()
    return h.finish("project_rules_and_venv_guard")


if __name__ == "__main__":
    raise SystemExit(main())
