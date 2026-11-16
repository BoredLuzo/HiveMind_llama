# -*- coding: utf-8 -*-
"""Updater offers the optional components that are not installed yet.

A release can add optional pieces (voice in/out, routing decider, browser
engine, POSIX tools). After an update the user is asked ONCE for the ones
that are missing; a fully equipped install gets a single line and no prompt.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check


UPDATE = (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8", errors="replace")
SCRIPT_PATH = ROOT / "deploy" / "check_optionals.bat"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8", errors="replace") if SCRIPT_PATH.exists() else ""

# ── 1. wiring ───────────────────────────────────────────────────────────────
check("1a the updater calls the optional step", "check_optionals.bat" in UPDATE)
_i = UPDATE.index("check_optionals.bat") if "check_optionals.bat" in UPDATE else -1
check("1b it runs after the dependency refresh",
      _i > 0 and UPDATE.index("Refreshing deps  : requirements.txt") < _i)
check("1c it runs before the watchdog re-arm (the shim self-swap is a shim step)",
      _i > 0 and "arm_watchdog(base)" in UPDATE[_i:])
check("1d the call is guarded (older zips without the file still update)",
      "if co.exists():" in UPDATE)

# ── 2. detection markers ────────────────────────────────────────────────────
for marker, what in (
        ("stt\\bin\\*.exe", "speech-to-text binaries"),
        ("stt\\models\\*", "speech-to-text model"),
        ("models--kyutai--pocket-tts", "text-to-speech cache"),
        ("models\\decider\\gliner25-decide", "routing decider"),
        ('import playwright', "browser engine (package probe)"),
        ("Git\\usr\\bin\\grep.exe", "Git Bash covers POSIX")):
    check(f"2 detects {what}", marker in SCRIPT)

# ── 3. install actions ──────────────────────────────────────────────────────
for action, what in (
        ("setup_voice.bat\"", "voice download"),
        ("setup_voice.bat\" --tts", "TTS pre-warm"),
        ("fetch_decider.py", "decider download"),
        ("playwright install chromium", "browser install"),
        ("winget install --id Microsoft.CoreUtils", "POSIX tools")):
    check(f"3 installs {what}", action in SCRIPT)
check("3f every install is failure-tolerant (no bare exit /b)",
      "exit /b" not in SCRIPT)

# ── 4. the question is asked, and only when something is missing ────────────
check("4a there is a single Y/N question",
      SCRIPT.count("choice /c YN") == 1)
check("4b nothing is installed without the answer",
      SCRIPT.index("choice /c YN") < SCRIPT.index('setup_voice.bat"'))
check("4c an equipped install exits before the question",
      "Everything optional is already installed" in SCRIPT and
      SCRIPT.index("Everything optional is already installed") < SCRIPT.index("choice /c YN"))
check("4d a skipped answer names the manual commands",
      "Add them later with the commands above" in SCRIPT)

# ── 5. dry run really installs nothing ─────────────────────────────────────
if SCRIPT_PATH.exists():
    r = subprocess.run(["cmd", "/c", str(SCRIPT_PATH), "--dry-run"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    check("5a --dry-run exits clean", r.returncode == 0, f"(rc={r.returncode})")
    check("5b --dry-run prints the table", "Optional components" in out)
    check("5c --dry-run reaches no installer",
          "dry run" in out.lower() or "already installed" in out)
else:
    check("5 the script exists", False)

# ── 6. decider CPU torch, HF cache detection, & post-install import check ───
INSTALL_BAT = (ROOT / "install.bat").read_text(encoding="utf-8", errors="replace")
FETCH_DEC = (ROOT / "deploy" / "fetch_decider.py").read_text(encoding="utf-8", errors="replace")
WD_BAT = (ROOT / "watchdog_engine.bat").read_text(encoding="utf-8", errors="replace")

check("6a decider package install runs before fetch_decider.py",
      SCRIPT.index("requirements-decider.txt") < SCRIPT.index("fetch_decider.py"))
check("6b check_optionals uses CPU PyTorch index for decider",
      "download.pytorch.org/whl/cpu" in SCRIPT
      and "unsafe-best-match -r requirements-decider.txt" in SCRIPT)
check("6c check_optionals verifies real torch+gliner2 import AFTER uv",
      SCRIPT.count("import torch, gliner2; from gliner2 import AutoExtractor") >= 3)
check("6d check_optionals retries --reinstall when uv fast-path leaves broken imports",
      "pip install --reinstall" in SCRIPT
      and "routing decider package install failed or remained incomplete" in SCRIPT)
check("6e check_optionals recognizes HF cache from install.bat",
      "models--fastino--GLiNER2.5-Decide*" in SCRIPT)
check("6f install.bat installs gliner2[local] with AutoExtractor check",
      "gliner2[local]" in INSTALL_BAT
      and "import torch, gliner2; from gliner2 import AutoExtractor" in INSTALL_BAT)
check("6g fetch_decider checks torch + AutoExtractor",
      "import torch" in FETCH_DEC and "AutoExtractor" in FETCH_DEC)

# Sandbox check: check_optionals detects broken vs working decider via PYTHONPATH
import os as _os  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402

with _tempfile.TemporaryDirectory(prefix="hvm_opt_") as _td:
    _sim = Path(_td) / "inst"
    (_sim / "deploy").mkdir(parents=True)
    (_sim / "stt" / "bin").mkdir(parents=True)
    (_sim / "stt" / "models").mkdir(parents=True)
    (_sim / "stt" / "bin" / "whisper.exe").write_bytes(b"")
    (_sim / "stt" / "models" / "ggml.bin").write_bytes(b"")
    _shutil.copy2(SCRIPT_PATH, _sim / "deploy" / "check_optionals.bat")

    _fake_pylib = Path(_td) / "pylib"
    _fake_pylib.mkdir()
    (_fake_pylib / "torch.py").write_text("X = 1\n", encoding="utf-8")
    (_fake_pylib / "gliner2.py").write_text("class AutoExtractor:\n    pass\n", encoding="utf-8")
    (_fake_pylib / "playwright.py").write_text("X = 1\n", encoding="utf-8")

    _fake_prof = Path(_td) / "home"
    (_fake_prof / ".cache" / "huggingface" / "hub" / "models--kyutai--pocket-tts").mkdir(parents=True)
    (_fake_prof / ".cache" / "huggingface" / "hub" / "models--fastino--GLiNER2.5-Decide").mkdir(parents=True)

    _env = dict(_os.environ)
    _windir = _os.environ.get("WINDIR", r"C:\Windows")
    _env["PATH"] = _os.pathsep.join([
        str(Path(sys.executable).parent),
        rf"{_windir}\System32",
        rf"{_windir}\System32\WindowsPowerShell\v1.0",
    ])
    _env["PYTHONPATH"] = str(_fake_pylib)
    _env["USERPROFILE"] = str(_fake_prof)
    _env["HF_HOME"] = str(_fake_prof / "hf_home")
    _env.pop("VIRTUAL_ENV", None)

    # Mock git bash path? Even if POSIX shows missing, routing decider must NOT show missing
    _r_ok = subprocess.run(
        ["cmd", "/d", "/c", str(_sim / "deploy" / "check_optionals.bat"), "--dry-run"],
        cwd=str(_sim), env=_env, capture_output=True, text=True, timeout=60)
    _out_ok = (_r_ok.stdout or "") + (_r_ok.stderr or "")
    check("6h HF-cached decider + working torch/gliner2 is NOT flagged missing",
          _r_ok.returncode == 0 and "[missing] routing decider" not in _out_ok,
          f"(rc={_r_ok.returncode})")

    # Now break gliner2 import (same failure mode as Checked 1 package + ImportError)
    (_fake_pylib / "gliner2.py").write_text(
        "raise ImportError('broken transitive dep')\n", encoding="utf-8")
    _r_bad = subprocess.run(
        ["cmd", "/d", "/c", str(_sim / "deploy" / "check_optionals.bat"), "--dry-run"],
        cwd=str(_sim), env=_env, capture_output=True, text=True, timeout=60)
    _out_bad = (_r_bad.stdout or "") + (_r_bad.stderr or "")
    check("6i broken gliner2 import IS flagged missing despite HF cache weights",
          _r_bad.returncode == 0 and "[missing] routing decider" in _out_bad,
          f"(rc={_r_bad.returncode})")

# ── 7. watchdog_engine.bat stop creates %LOCALAPPDATA%\HiveMindGateway ───────
check("7a watchdog_engine ensures WDDIR exists before logging",
      "mkdir \"%WDDIR%\"" in WD_BAT)
with _tempfile.TemporaryDirectory(prefix="hvm_wd_") as _td:
    _wd_dir = Path(_td) / "inst"
    _wd_dir.mkdir()
    _shutil.copy2(ROOT / "watchdog_engine.bat", _wd_dir / "watchdog_engine.bat")
    _local = Path(_td) / "fresh_localappdata"
    _local.mkdir()  # NO HiveMindGateway subdir inside yet
    _env_wd = dict(_os.environ, LOCALAPPDATA=str(_local))
    _r_wd = subprocess.run(
        ["cmd", "/d", "/c", str(_wd_dir / "watchdog_engine.bat"), "stop"],
        cwd=str(_wd_dir), env=_env_wd, capture_output=True, text=True, timeout=60)
    _wd_out = (_r_wd.stdout or "") + (_r_wd.stderr or "")
    _wd_low = _wd_out.lower()
    _log_file = _local / "HiveMindGateway" / "watchdog.log"
    check("7b watchdog stop exits 0 cleanly without path/settings errors on clean machine",
          _r_wd.returncode == 0
          and not _wd_out.strip()
          and "pfad nicht gefunden" not in _wd_low
          and "cannot find the path specified" not in _wd_low,
          f"(rc={_r_wd.returncode}, out={_wd_out.strip()!r})")
    check("7c watchdog stop writes watchdog.log inside created dir",
          _log_file.is_file()
          and "watchdog stop requested" in _log_file.read_text(encoding="utf-8", errors="replace"))

sys.exit(h.finish())
