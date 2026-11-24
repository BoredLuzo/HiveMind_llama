# -*- coding: utf-8 -*-
"""Regression checks proving the test suite never emits Windows console beeps.

Run: python tests/test_silent_test_suite.py
Exit 0 = all pass, Exit 1 = failure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402
from tests._silent_choice import (  # noqa: E402
    ensure_silent_choice_dir,
    prepend_silent_choice_to_path,
)

h = Harness(style="summary", pad=True)
check = h.check


def main() -> int:
    # 1. PATH helper behavior (cross-platform)
    updated_path = prepend_silent_choice_to_path(
        r"C:\Windows\System32;C:\Python",
        silent_dir=r"C:\Temp\silent_choice_dir",
    )
    check(
        "prepend_silent_choice_to_path places shim ahead of System32",
        updated_path.split(os.pathsep)[0] == r"C:\Temp\silent_choice_dir",
        f"(got={updated_path!r})",
    )

    if os.name == "nt":
        silent_dir = ensure_silent_choice_dir()
        exe = Path(silent_dir) / "choice.exe" if silent_dir else Path()
        check(
            "silent choice.exe shim compiled",
            bool(silent_dir and exe.is_file() and exe.stat().st_size > 0),
        )

        raw = exe.read_bytes() if exe.is_file() else b""
        for forbidden in (b"Beep", b"MessageBeep", b"PlaySound", b"winmm", b"Console.Beep"):
            check(
                f"silent choice.exe binary has no {forbidden.decode('ascii')} reference",
                forbidden not in raw,
            )

        shim_env = {
            **os.environ,
            "PATH": prepend_silent_choice_to_path(os.environ.get("PATH", ""), silent_dir),
        }

        # Valid match
        r_valid = subprocess.run(
            [str(exe), "/c", "YN", "/n", "/m", "Prompt"],
            input=b"n",
            capture_output=True,
            timeout=10,
        )
        check("silent choice valid match /c YN with 'n' exits 2", r_valid.returncode == 2,
              f"(rc={r_valid.returncode})")

        # Garbage + CRLF ignored cleanly without sound
        r_garbage = subprocess.run(
            [str(exe), "/c", "YN", "/n"],
            input=b"\r\n9\r\nX\r\nY",
            capture_output=True,
            timeout=10,
        )
        check("silent choice skips CR/LF/garbage and matches 'Y' -> rc=1",
              r_garbage.returncode == 1, f"(rc={r_garbage.returncode})")

        # Back-to-back calls via pipe (unbuffered 1-byte consumption)
        r_pipe = subprocess.run(
            ["cmd", "/v:on", "/d", "/c", "choice /c AB /n & echo FIRST=!errorlevel! & choice /c CD /n & echo SECOND=!errorlevel!"],
            input=b"AC",
            env=shim_env,
            capture_output=True,
            timeout=10,
        )
        pipe_out = r_pipe.stdout.decode("utf-8", errors="replace")
        check(
            "two consecutive choice calls over pipe do not steal bytes",
            r_pipe.returncode == 0 and "FIRST=1" in pipe_out and "SECOND=1" in pipe_out,
            f"(rc={r_pipe.returncode}, out={pipe_out!r})",
        )

        # Back-to-back calls via redirected file
        with tempfile.TemporaryDirectory(prefix="hmsilent_") as td:
            ans = Path(td) / "ans.txt"
            ans.write_bytes(b"BD")
            with open(ans, "rb") as fh:
                r_file = subprocess.run(
                    ["cmd", "/v:on", "/d", "/c", "choice /c AB /n & echo FIRST=!errorlevel! & choice /c CD /n & echo SECOND=!errorlevel!"],
                    stdin=fh,
                    env=shim_env,
                    capture_output=True,
                    timeout=10,
                )
        file_out = r_file.stdout.decode("utf-8", errors="replace")
        check(
            "two consecutive choice calls over redirected file do not steal bytes",
            r_file.returncode == 0 and "FIRST=2" in file_out and "SECOND=2" in file_out,
            f"(rc={r_file.returncode}, out={file_out!r})",
        )

        # EOF with /t 30 /d N must return default (2) immediately without 30s sleep
        t0 = time.monotonic()
        r_def = subprocess.run(
            [str(exe), "/c", "YN", "/n", "/t", "30", "/d", "N"],
            input=b"",
            capture_output=True,
            timeout=10,
        )
        elapsed = time.monotonic() - t0
        check(
            "EOF with /t 30 /d N returns default N (rc=2) immediately",
            r_def.returncode == 2 and elapsed < 5.0,
            f"(rc={r_def.returncode}, elapsed={elapsed:.2f}s)",
        )

        # Case-sensitive /cs
        r_cs = subprocess.run(
            [str(exe), "/c", "aA", "/cs", "/n"],
            input=b"A",
            capture_output=True,
            timeout=10,
        )
        check("case-sensitive /cs distinguishes 'a' vs 'A' -> rc=2", r_cs.returncode == 2,
              f"(rc={r_cs.returncode})")

        # test_installer_sim wiring resolves shim ahead of System32
        import tests.test_installer_sim as isim  # noqa: E402

        resolved = shutil.which("choice", path=isim.build_env()["PATH"])
        check(
            "test_installer_sim build_env resolves choice to silent shim dir",
            bool(
                isim._SILENT_CHOICE_DIR
                and resolved
                and os.path.normcase(str(Path(resolved).parent))
                == os.path.normcase(str(Path(isim._SILENT_CHOICE_DIR)))
            ),
            f"(resolved={resolved!r})",
        )
    else:
        check("Windows silent choice runtime checks skipped on POSIX", True)

    # 3. Static wiring checks across test files and runner
    isim_src = (ROOT / "tests" / "test_installer_sim.py").read_text(encoding="utf-8")
    check(
        "case_install_optionals_garbage_voice_pick does not feed CR/LF garbage to choice",
        'stdin="O\\nN\\nN\\n9\\nN\\nX\\n"' not in isim_src,
    )

    rr_src = (ROOT / "tests" / "run_regressions.py").read_text(encoding="utf-8")
    check(
        "run_regressions sets both HIVEMIND_NO_NOTIFY and prepend_silent_choice_to_path",
        '"HIVEMIND_NO_NOTIFY": "1"' in rr_src and "prepend_silent_choice_to_path" in rr_src,
    )

    pw_src = (ROOT / "tests" / "test_ui_playwright_smoke.py").read_text(encoding="utf-8")
    check(
        "test_ui_playwright_smoke launches Chromium with --mute-audio",
        "--mute-audio" in pw_src,
    )

    gitignore_lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    check(
        ".gitignore tracks tests/_silent_choice.py explicitly",
        "!tests/_silent_choice.py" in gitignore_lines,
    )

    return h.finish("silent_test_suite")


if __name__ == "__main__":
    sys.exit(main())
