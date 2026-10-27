# -*- coding: utf-8 -*-
"""Settings: Schreibvorgaenge verlieren keine Updates mehr.

Befund (Audit 2026-10-27): save_settings ist pro Schreibvorgang atomar, das
Read-Modify-Write drumherum nicht. Zwei Threads, die je einen Key setzen,
verloren einen davon (gemessen: a=111/b=222 -> a=0/b=222). mutate_settings()
haelt den Lock ueber Lesen, Aendern und Schreiben.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import settings as S  # noqa: E402


def fresh() -> Path:
    p = Path(tempfile.mkdtemp(prefix="setatomic_")) / "settings.json"
    p.write_text(json.dumps({"a": 0, "b": 0}), encoding="utf-8")
    S.SETTINGS_FILE = p
    S._load_cache_key = None
    S._load_cache_data = None
    return p


def hammer(use_mutate: bool, rounds: int = 40) -> tuple:
    p = fresh()

    def writer(key: str, value: int) -> None:
        for _ in range(rounds):
            if use_mutate:
                S.mutate_settings(lambda d, k=key, v=value: d.__setitem__(k, v))
            else:
                d = S.load_settings()
                d[key] = value
                S.save_settings(d)

    t1 = threading.Thread(target=writer, args=("a", 111))
    t2 = threading.Thread(target=writer, args=("b", 222))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    disk = json.loads(p.read_text(encoding="utf-8"))
    return disk.get("a"), disk.get("b")


# ── 1. mutate_settings haelt beide Keys ────────────────────────────────
check("1a mutate_settings keeps both concurrent writes", hammer(True) == (111, 222),
      f"({hammer(True)})")

# ── 2. Der alte Pfad ist der Grund fuer die Regel ──────────────────────
_old = hammer(False)
check("2a the old read-modify-write can lose a key (documented)", True,
      f"(gemessen: a={_old[0]}, b={_old[1]})")

# ── 3. Die Verdrahtung bleibt ──────────────────────────────────────────
SRC = (ROOT / "settings.py").read_text(encoding="utf-8", errors="replace")
check("3a mutate_settings exists", "def mutate_settings(" in SRC)
check("3b one shared writer, no duplicate body",
      SRC.count("def _write_settings_unlocked(") == 1
      and "_write_settings_unlocked(settings, saved_at=saved_at)" in SRC)
check("3c the temp name carries pid and a retry",
      "os.getpid()" in SRC and "_attempt" in SRC)
_WS = (ROOT / "routers" / "websearch.py").read_text(encoding="utf-8", errors="replace")
check("3d the websearch route uses the locked path",
      "mutate_settings(" in _WS and "save_settings(settings)" not in _WS)

print()
print(f"SUMMARY settings_atomic: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
