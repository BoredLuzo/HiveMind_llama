# -*- coding: utf-8 -*-
"""The install.bat settings writer must not destroy user data on bad input.

install.bat asks for the VRAM budget and the server port and hands both to a
`python -c` one-liner that writes settings.json. Two live defects lived there:

1. A typo at the VRAM prompt ("eight" instead of 8.0) OVERWROTE the stored
   value with the default 8.0 - silent data loss on a repair re-run.
2. A port like 0 or 99999 passed the old `isdigit()` gate (both are digits)
   and was written as-is; the first `start_hivemind.bat` then died with an
   obscure bind error.

The fix: garbage input keeps the previous value, input is stripped, and the
port must be 1-65535. This suite extracts the REAL one-liner from install.bat
and executes it against a stubbed settings module - the shipped code, not a
copy - and asserts the guard on the batch side as well.

Run: python tests/test_installer_settings_writer.py
"""
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


BAT = (ROOT / "install.bat").read_text(encoding="ascii", errors="replace")

# ── locate and extract the writer one-liner ────────────────────────────────
_line = next((l for l in BAT.splitlines() if "from settings import" in l), "")
check("writer one-liner found in install.bat", _line != "")
_m = re.search(r'-c "(.*)"\s*$', _line)
check("one-liner has a -c argument", _m is not None)
CODE = _m.group(1) if _m else ""

check("port range is enforced (1-65535)", "1 <= int(_p) <= 65535" in CODE)
check("VRAM input is stripped", ".strip().replace(',', '.')" in CODE)
check("port input is stripped",
      "os.environ.get('HM_PORT', '8001').strip()" in CODE)


def run_writer(hm_port, hm_vram, initial):
    """Exec the extracted one-liner with HM_* env vars and a stub module."""
    stub = types.ModuleType("settings")
    state = dict(initial)

    stub.load_settings = lambda: dict(state)

    def _save(s):
        state.clear()
        state.update(s)

    stub.save_settings = _save
    saved_settings = sys.modules.get("settings")
    saved_env = {k: os.environ.get(k) for k in ("HM_PORT", "HM_VRAM",
                                                "HM_BACKEND")}
    sys.modules["settings"] = stub
    try:
        os.environ["HM_PORT"] = str(hm_port)
        os.environ["HM_VRAM"] = str(hm_vram)
        os.environ["HM_BACKEND"] = "vulkan"
        g = {"__builtins__": __builtins__}
        exec(CODE, g)  # noqa: S102 - repo's own shipped code
    finally:
        if saved_settings is None:
            sys.modules.pop("settings", None)
        else:
            sys.modules["settings"] = saved_settings
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return state


# ── valid input behaves exactly as before ──────────────────────────────────
s = run_writer("8001", "8.0", {})
check("fresh install: defaults land", s.get("server_port") == 8001
      and s.get("vram_budget_gb") == 8.0)
s = run_writer("8090", "6,5", {"server_port": 8001, "vram_budget_gb": 8.0})
check("valid custom input accepted", s.get("server_port") == 8090
      and s.get("vram_budget_gb") == 6.5)
s = run_writer("8090", " 6 ", {})
check("surrounding spaces are stripped", s.get("vram_budget_gb") == 6.0)
check("gpu_backend written", run_writer("8001", "8.0",
                                        {}).get("gpu_backend") == "vulkan")
s = run_writer("8001", "8.0", {"workspace": "C:/w"})
check("existing workspace preserved", s.get("workspace") == "C:/w")
s = run_writer("8001", "8.0", {})
check("workspace default added empty", s.get("workspace") == "")

# ── garbage input keeps the previous value (was: default overwrite) ────────
s = run_writer("abc", "8.0", {"server_port": 8090})
check("non-numeric port keeps previous value", s.get("server_port") == 8090)
s = run_writer("0", "8.0", {"server_port": 8090})
check("port 0 rejected, previous kept", s.get("server_port") == 8090)
s = run_writer("99999", "8.0", {"server_port": 8090})
check("port 99999 rejected, previous kept", s.get("server_port") == 8090)
s = run_writer("65536", "8.0", {"server_port": 8001})
check("port 65536 rejected", s.get("server_port") == 8001)
s = run_writer("65535", "8.0", {"server_port": 8001})
check("port 65535 accepted (upper bound)", s.get("server_port") == 65535)
s = run_writer("8001", "eight", {"vram_budget_gb": 12.5})
check("garbage VRAM keeps previous value", s.get("vram_budget_gb") == 12.5)
s = run_writer("8001", "eight", {})
check("garbage VRAM with nothing stored falls back to 8.0",
      s.get("vram_budget_gb") == 8.0)
s = run_writer("8001", "-4", {"vram_budget_gb": 12.5})
check("negative VRAM rejected, previous kept", s.get("vram_budget_gb") == 12.5)
s = run_writer("8001", "12.5extra", {"vram_budget_gb": 8.0})
check("leading-number garbage still parses its numeric head",
      s.get("vram_budget_gb") == 12.5)

# ── hostile pre-existing settings.json values must not crash the writer ────
s = run_writer("abc", "abc", {"server_port": "not-a-number",
                              "vram_budget_gb": "gone"})
check("string garbage in settings.json is repaired, not crashed on",
      s.get("server_port") == 8001 and s.get("vram_budget_gb") == 8.0)

# ── batch-side guards ──────────────────────────────────────────────────────
check("voice pick normalizes unknown input to skip",
      'if not "%VOICE_PICK%"=="1" if not "%VOICE_PICK%"=="2" '
      'if not "%VOICE_PICK%"=="3" if not "%VOICE_PICK%"=="4" '
      'set "VOICE_PICK=4"' in BAT)
check("no stale (step 1/8) text left", "(step 1/8)" not in BAT)
check("optionals path says step 1/9", "(step 1/9)" in BAT)

print()
print(f"SUMMARY installer_settings_writer: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
