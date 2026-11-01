# -*- coding: utf-8 -*-
"""Installer download flow — declining a model and the essential set.

1. Answering "n" to a 20.8 GB GGUF still downloaded its 0.8 GB projector and
   then registered the model. Declining the main file must skip the whole spec.
2. The "essential" set was a skip-list: 15 models including the 20.8 GB
   Qwen3.6 on a 4 GB-VRAM machine. It is a whitelist of the default route now.
3. Ctrl+C during a download printed a 20-line ssl/socket traceback.
4. After a set finished, the menu exited — adding single models on top meant
   re-running the installer.

The download paths need the HF API, so this checks the decision points
statically plus the real `--print-essential` output.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


FM = (ROOT / "deploy" / "fetch_models.py").read_text(encoding="utf-8")
SM = (ROOT / "setup_models.bat").read_text(encoding="utf-8", errors="replace")

# ── 1. declining the main file skips the model ──────────────────────────────
check("1a main download result is checked",
      "if not download_file(repo, main_file, mdir, args.yes):" in FM)
_i = FM.index("if not download_file(repo, main_file, mdir, args.yes):")
_j = FM.index("local_files[main_file[\"path\"].lower()]", _i)
check("1b the skip happens before registration",
      "continue" in FM[_i:_j], "(continue before local_files)")
check("1c the skip is announced",
      "projector/drafter/registration skipped" in FM[_i:_i + 600])
# the mmproj block must stay behind that continue
check("1d mmproj download is guarded by the main-file decision",
      FM.index("if not download_file(repo, main_file") < FM.index("spec[\"mmproj_regex\"]"))

# ── 2. essential set is a small whitelist, not everything-but-one ───────────
from deploy.fetch_models import ESSENTIAL_KEYS, SPECS  # noqa: E402
_keys = {s["key"] for s in SPECS}
check("2a essential keys exist in the catalog", ESSENTIAL_KEYS <= _keys,
      f"(unknown: {ESSENTIAL_KEYS - _keys})")
check("2b essential set is small", len(ESSENTIAL_KEYS) <= 5, f"({len(ESSENTIAL_KEYS)})")
check("2c the 20 GB model is NOT essential", "qwen3.6:35b-a3b-ud" not in ESSENTIAL_KEYS)
for _need in ("qwen3.5:4b-mtp", "gemma-4:e4b-it-qat", "lfm3:1.7b-qad",
              "minicpm5:2b-sharp"):
    check(f"2d default route in the essential set: {_need}", _need in ESSENTIAL_KEYS)
check("2f the Q6 sharp tier is the one, not the Q4",
      "minicpm5:2b-sharp-q4" not in ESSENTIAL_KEYS)
check("2g the old refiner model is not essential", "qwen3.5:2b-mtp" not in ESSENTIAL_KEYS)
check("2e --essential filters BY whitelist",
      "or s.get('key') in ESSENTIAL_KEYS" in FM)

# ── 3. Ctrl+C is handled, not printed as a traceback ────────────────────────
check("3a KeyboardInterrupt is caught", "except KeyboardInterrupt:" in FM)
check("3b the cancel message mentions the resume", "resumes it" in FM)
check("3c a distinct exit code is used", "sys.exit(130)" in FM)

# ── 4. the batch menu loops back for single downloads ───────────────────────
check("4a the menu is a label", ":menu" in SM)
check("4b every set jumps to more_models", SM.count("goto more_models") >= 3,
      f"({SM.count('goto more_models')})")
check("4c more_models returns to the menu", ":more_models" in SM and
      "if not errorlevel 2 goto menu" in SM)
check("4e the step end asks M/P, not Y/N",
      "Your choice [M/P]" in SM and "download more models" in SM and
      "proceed (finish this step)" in SM)
check("4f a custom model also asks what is next",
      SM.count("goto more_models") >= 4)
check("4d the menu prints the real essential list",
      "--print-essential" in SM)

# ── 5. the printed list is honest ───────────────────────────────────────────
_out = subprocess.run([sys.executable, str(ROOT / "deploy" / "fetch_models.py"),
                       "--print-essential"], capture_output=True, text=True, timeout=120)
_txt = (_out.stdout or "") + (_out.stderr or "")
check("5a --print-essential exits clean", _out.returncode == 0, f"(rc={_out.returncode})")
for _k in ESSENTIAL_KEYS:
    check(f"5b printed: {_k}", _k in _txt)
_opt = _txt.split("ALSO AVAILABLE")[-1] if "ALSO AVAILABLE" in _txt else ""
check("5c the big model is listed as optional, not as essential",
      "qwen3.6:35b-a3b-ud" in _opt)
check("5d the listing is roomy (blank line between models)",
      _txt.count("\n\n") >= len(ESSENTIAL_KEYS))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
