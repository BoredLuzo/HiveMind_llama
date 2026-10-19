"""write_file hang regression (2026-10-19): a denied folder must fail at once.

tempfile.mkstemp retries PermissionError up to TMP_MAX (2**31-1 on Windows) while
os.access(dir, W_OK) reports writable - that is an endless loop, not a missing
permission. This test pins the bounded helper and the WRITE_FILE_PERMISSION path.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.handlers.file_ops import _mkstemp_bounded  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _deny_dir():
    if os.name == "nt":
        cands = [Path(r"C:\Windows\System32"), Path(r"C:\Program Files")]
    else:
        cands = [Path("/proc"), Path("/sys")]
    for c in cands:
        if c.is_dir():
            return c
    return None


with tempfile.TemporaryDirectory() as td:
    fd, p = _mkstemp_bounded(Path(td), ".tmp")
    os.close(fd)
    check("writable folder creates the temp file", p.exists(), f"({p.name})")
    p.unlink()
    fd, p = _mkstemp_bounded(Path(td), ".txt")
    os.close(fd)
    check("suffix is honoured", p.suffix == ".txt")
    p.unlink()

d = _deny_dir()
if d is None:
    print("  SKIP no permission-denied folder on this platform")
else:
    t0 = time.time()
    err = None
    try:
        _mkstemp_bounded(d, ".tmp")
    except PermissionError as exc:
        err = exc
    except Exception as exc:          # noqa: BLE001 - recorded for the assertion
        err = exc
    dt = time.time() - t0
    check("denied folder raises instead of looping", err is not None,
          f"({type(err).__name__})")
    check("denied folder fails in under a second", dt < 1.0, f"({dt:.3f}s)")

src = (ROOT / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
check("write_file returns WRITE_FILE_PERMISSION", "WRITE_FILE_PERMISSION" in src)
check("the atomic writes use the bounded helper",
      src.count("_mkstemp_bounded(") >= 7, f"({src.count('_mkstemp_bounded(')})")
check("no raw tempfile.mkstemp call is left", "tempfile.mkstemp(" not in src)

print()
print(f"SUMMARY write_hang: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
