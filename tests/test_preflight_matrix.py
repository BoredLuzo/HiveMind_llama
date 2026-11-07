# -*- coding: utf-8 -*-
"""Preflight matrix (audit 2026-10-30).

Model known/unknown  x  GGUF present/missing  x  process count ok/-1.
The fail-closed rules must hold in every cell, and a transient tasklist
hiccup must NOT block a legitimate load (one retry first).

Windows-only paths are exercised through monkeypatching, so the matrix also
runs on a non-Windows box (there is no tasklist there: llama_process_count
uses pgrep and returns -1 on failure - the same fail-closed contract).
"""
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend import llama_preflight as LP  # noqa: E402

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="plain")
check = h.check

# --- GGUF present / missing -------------------------------------------
gg = Path(tempfile.gettempdir()) / "_audit_matrix.gguf"
with open(gg, "wb") as f:
    f.truncate(2 * 1024 * 1024 * 1024)   # sparse 2 GiB
try:
    size_known = LP.gguf_size_gb(str(gg))
    size_unknown_alias = LP.gguf_size_gb("definitely-not-a-model-xyz")
    missing_path = LP.gguf_size_gb(str(gg) + ".missing")
    check("GGUF present -> size > 0", size_known > 1.0, f"({size_known:.2f} GB)")
    check("unknown alias -> size 0", size_unknown_alias == 0.0)
    check("missing path -> size 0", missing_path == 0.0)

    # --- matrix: guard decision -----------------------------------------
    check("known + enough RAM -> allow",
          not LP.ram_guard_blocks(str(gg), 30.0))
    check("known + tight RAM -> block",
          LP.ram_guard_blocks(str(gg), 1.0))
    check("unknown alias + enough RAM -> BLOCK (fail-closed)",
          LP.ram_guard_blocks("definitely-not-a-model-xyz", 30.0))
    check("missing path + enough RAM -> BLOCK (fail-closed)",
          LP.ram_guard_blocks(str(gg) + ".missing", 30.0))
    check("unknown free RAM (-1) -> block",
          LP.ram_guard_blocks(str(gg), -1.0))
    check("unknown free RAM (None) -> block",
          LP.ram_guard_blocks(str(gg), None))
finally:
    gg.unlink(missing_ok=True)

# --- process count: ok / transient / broken ---------------------------
_ok_out = subprocess.CompletedProcess(
    args=[], returncode=0,
    stdout='"llama-server.exe","1234","Console","1","1.000 K"\n', stderr="")

with mock.patch.object(LP.subprocess, "run", return_value=_ok_out):
    n = LP.llama_process_count()
check("process_count ok -> counts the process", n == 1, f"({n})")

# transient: first call raises, second works -> retry saves the load
calls = {"n": 0}


def _flaky(*a, **k):
    calls["n"] += 1
    if calls["n"] == 1:
        raise subprocess.TimeoutExpired(cmd="tasklist", timeout=30)
    return _ok_out


with mock.patch.object(LP.subprocess, "run", side_effect=_flaky):
    n = LP.llama_process_count()
check("transient tasklist failure -> retried, not -1", n == 1, f"({n})")
# 1 call raises (first attempt, first image) + 2 calls of the retry
# (llama-server.exe and llama-bench.exe) = 3
check("retry actually happened (3 subprocess calls)", calls["n"] == 3,
      f"({calls['n']})")

# broken: both calls fail -> -1 (fail-closed)
def _always(*a, **k):
    raise OSError("tasklist broken")


with mock.patch.object(LP.subprocess, "run", side_effect=_always):
    n = LP.llama_process_count()
check("persistent tasklist failure -> -1 (fail-closed)", n == -1, f"({n})")

# and a caller must treat -1 as "not free"
if sys.platform == "nt":
    check("non-Windows path is separate (pgrep)",
          "pgrep" in (Path(LP.__file__).read_text(encoding="utf-8")))
else:
    print("  SKIP pgrep-path note (running on non-Windows)")

# Windows path exists even when we are not on Windows
src = Path(LP.__file__).read_text(encoding="utf-8")
check("both platform paths implemented (os.name check + pgrep fallback)",
      'os.name == "nt"' in src and "pgrep" in src)

sys.exit(h.finish("preflight_matrix"))
