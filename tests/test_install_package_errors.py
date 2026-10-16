"""install_package must report ITS OWN failure and give a usable next step.

Live 2026-10-16 (owner screenshot): the chat showed
  install_package · pip: pygame
  [TOOL_ERROR:RUN_BASH_NONZERO] run_bash: run_bash exited with code 1 (shell: bash)
i.e. an install_package call failing with a run_bash label - it dispatches
through the run_bash handler and returned that text verbatim. The model then
needed three more rounds (manual pip with --only-binary, python --version,
pip install --upgrade pip setuptools wheel) to work out that pygame has no
wheel for Python 3.14.

Second half: the source-build hint existed but only triggered on
"Building wheel for"/"Preparing metadata"/"config_win.py". The real output said
"Installing build dependencies" + "Getting requirements to build wheel ...
status 'error'" + FileNotFoundError, so no hint appeared.

This test fakes the run_bash dispatch (no network, no pip) and pins:
  (a) the relabel: INSTALL_FAILED + "install_package: 'pip install X'", no
      run_bash wording, meta tool rewritten
  (b) the broadened source-build detection (each live-observed marker)
  (c) the actionable three-step hint
  (d) success and unrelated failures stay untouched

Run: python tests/test_install_package_errors.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import tools.runner as _runner                                    # noqa: E402
from tools.handlers.exec_tools import _inline_tool_install_package  # noqa: E402

_FAKE = {"out": ""}


async def _fake_run_inline_tool(name, args, workspace_lock=None, **kw):
    _FAKE["name"] = name
    _FAKE["args"] = args
    return _FAKE["out"]


_runner._run_inline_tool = _fake_run_inline_tool


def _call(packages="pygame", manager="pip", out=""):
    _FAKE["out"] = out
    return asyncio.run(_inline_tool_install_package(
        {"manager": manager, "packages": packages}, _WS, None))


# a throwaway workspace with a fake venv, so the pip guard passes for every call
_WS = Path(tempfile.mkdtemp(prefix="hvm_install_"))
(_WS / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
(_WS / ".venv" / "Scripts" / "python.exe").write_bytes(b"")


_LIVE_ERR = (
    "[TOOL_ERROR:RUN_BASH_NONZERO] run_bash: run_bash exited with code 1 (shell: bash).\n"
    "Collecting pygame\n"
    "  Installing build dependencies: started\n"
    "  Getting requirements to build wheel: finished with status 'error'\n"
    "  error: subprocess-exited-with-error\n"
    "  FileNotFoundError: [WinError 2] Das System kann die angegebene Datei nicht finden\n"
    "\n[TOOL_ERROR_META] {\"cmd\": \"x\", \"exit_code\": 1, \"tool\": \"run_bash\"}\n"
)

# a venv so the pip guard passes
_res = _call(out=_LIVE_ERR)

# ── (a) label ───────────────────────────────────────────────────────────────
check("failure is labelled INSTALL_FAILED", "[TOOL_ERROR:INSTALL_FAILED]" in _res)
check("the run_bash tag is gone", "RUN_BASH_NONZERO" not in _res.split("[HINT]")[0])
check("no 'run_bash: run_bash exited' wording",
      "run_bash: run_bash exited" not in _res and "run_bash exited" not in _res)
check("it names the real call", "install_package: 'pip install pygame' failed" in _res)
check("the meta names install_package", '"tool": "install_package"' in _res)
check("the pip output survives", "Collecting pygame" in _res)

# ── (b) broadened source-build detection ────────────────────────────────────
for _marker in ("Building wheel for", "Preparing metadata", "config_win.py",
                "Getting requirements to build wheel", "Installing build dependencies",
                "metadata-generation-failed", "subprocess-exited-with-error",
                "Microsoft Visual C++", "No module named 'setuptools'"):
    _r = _call(out=f"[TOOL_ERROR:RUN_BASH_NONZERO] run_bash exited with code 1 (shell: bash).\n{_marker} x\n")
    check(f"hint appears for '{_marker[:34]}'", "[HINT]" in _r)

# ── (c) the hint is actionable ──────────────────────────────────────────────
check("hint forbids a blind retry", "Do NOT retry the same install" in _res)
check("hint gives a runnable only-binary probe",
      "--only-binary=:all: pygame" in _res and "run_bash(cmd=" in _res)
check("hint uses the workspace venv interpreter", "python.exe" in _res or "bin/python" in _res)
check("hint names the wheel variant", "pygame-ce" in _res)
check("hint names the venv/Python option", "-m venv .venv" in _res)
# the suggested flag must NOT be smuggled through install_package: the
# validator blocks anything starting with '-' (flag injection), which is a
# deliberate guard - so the hint routes the probe through run_bash instead.
from tools.handlers.exec_tools import _validate_install_packages  # noqa: E402
_ok_flag, _flag_msg = _validate_install_packages("--only-binary=:all: pygame")
check("the validator still blocks flag injection", _ok_flag is False,
      f" ({_flag_msg})")
check("the hint does not tell the model to pass the flag to install_package",
      "install_package(manager='pip', packages='--only-binary" not in _res)

# ── (d) untouched paths ─────────────────────────────────────────────────────
_ok = _call(out="[run_bash: exit 0]\nSuccessfully installed pygame-2.6.1\n")
check("success is returned unchanged",
      _ok == "[run_bash: exit 0]\nSuccessfully installed pygame-2.6.1\n")
_other = _call(out="[TOOL_ERROR:RUN_BASH_NONZERO] run_bash exited with code 2 (shell: bash).\nboom\n")
check("unrelated failure is relabelled but gets no source-build hint",
      "[TOOL_ERROR:INSTALL_FAILED]" in _other and "[HINT]" not in _other)
_empty_ws = Path(tempfile.mkdtemp(prefix="hvm_install_empty_"))
_guard = asyncio.run(_inline_tool_install_package(
    {"manager": "pip", "packages": "pygame"}, _empty_ws, None))
check("the venv guard still answers first",
      "PIP_NO_WORKSPACE_VENV" in _guard or "pip refused" in _guard)
shutil.rmtree(_empty_ws, ignore_errors=True)
shutil.rmtree(_WS, ignore_errors=True)

# ── (e) the same hint on the run_bash path ──────────────────────────────────
# install_package is tier 99 and drops out at small windows; at exactly those
# sizes run_bash is the only path left, so the pip guidance must live there too.
from tools.handlers.exec_tools import (_pip_packages_from_cmd,   # noqa: E402
                                       _pip_source_build_hint)
check("plain pip install", _pip_packages_from_cmd("pip install pygame") == "pygame")
check("venv interpreter + flags",
      _pip_packages_from_cmd('".venv/Scripts/python.exe" -m pip install --prefer-binary pygame 2>&1')
      == "pygame")
check("chained command with a pipe",
      _pip_packages_from_cmd("cd /c/x && python -m pip install pygame-ce | tail -5") == "pygame-ce")
check("requirements-file installs get no package hint",
      _pip_packages_from_cmd("python -m pip install -r requirements.txt") == "")
check("non-pip commands are ignored",
      _pip_packages_from_cmd("npm install left-pad") == ""
      and _pip_packages_from_cmd("ls -la") == "")
_h = _pip_source_build_hint("pygame", "C:/ws/.venv/Scripts/python.exe")
check("hint carries the interpreter and package",
      "C:/ws/.venv/Scripts/python.exe" in _h and "--only-binary=:all: pygame" in _h)
_src_exec = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(encoding="utf-8", errors="replace")
check("run_bash checks the shared marker list",
      "any(_p in _err for _p in _PIP_SOURCE_BUILD_MARKERS)" in _src_exec)
check("run_bash appends the hint for pip failures",
      "_err += _pip_source_build_hint(_pkgs)" in _src_exec)
check("install_package uses the same helper",
      "_res += _pip_source_build_hint(packages" in _src_exec)
check("one marker list, not two", _src_exec.count("_PIP_SOURCE_BUILD_MARKERS = (") == 1)

print()
print(f"SUMMARY install_package_errors: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
