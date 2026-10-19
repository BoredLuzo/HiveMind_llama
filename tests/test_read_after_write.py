"""read_file nach write_file (2026-10-15, Beobachtung: write 15.6 KB ->
read 3.9 KB las das Modell als "nicht vollstaendig geschrieben").

Echter Pfad: die REALEN Handler _inline_tool_write_file /
_inline_tool_read_file (tools/handlers/file_ops.py) + der echte
Executor-Cap _cap_tool_result (core/tool_executor.py) — keine Cap-Funktion
isoliert. Kleiner Kontext (n_ctx 4096) => kleiner read_char_cap.
Erwartet: Datei komplett auf Disk; read_file kappt NUR mit klarer
Truncation-Meldung, die Zeilen- UND Zeichenzahl der vollen Datei traegt,
und der Executor-Cap darf diese Meldung nicht wieder abschneiden."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ENC-FIX: Labels enthalten non-ASCII — robust ohne PYTHONUTF8.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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


FULL = "".join(f"Zeile {i:04d} aaaa bbbb cccc dddd eeee\n" for i in range(570))
assert len(FULL) >= 16000, len(FULL)


def main():
    from core import budgets as B
    from core.tool_executor import _cap_tool_result
    from tools.handlers import _inline_tool_read_file, _inline_tool_write_file

    ws = Path(tempfile.mkdtemp(prefix="hvm_readwrite_"))
    b = B.resolve(n_ctx=4096, cpt=3.5, max_out=512, settings={})
    B.set_active(b)
    cap = b.read_char_cap
    check("cap < file size (kleiner ctx kappt by design)", 0 < cap < len(FULL),
          f" cap={cap} file={len(FULL)}")

    async def _run():
        # DETERMINISTIC (2026-10-19): the write used to race the two reads inside
        # asyncio.gather, so the reads sometimes hit a file that was not on disk
        # yet and got FILE_READ_FAILED - a flaky suite, not a product bug (the
        # executor only prefetches read-only tools in parallel, write_file is
        # not one of them). This test is about the cap, so await the write first.
        wr = await _inline_tool_write_file({"path": "big.py", "content": FULL},
                                           ws, str(ws))
        rd_full, rd_range = await asyncio.gather(
            _inline_tool_read_file({"path": "big.py"}, ws, str(ws)),
            _inline_tool_read_file({"path": "big.py", "start_line": 1,
                                    "end_line": 99999}, ws, str(ws)))
        return wr, rd_full, rd_range

    wr, rd_full, rd_range = asyncio.run(_run())

    disk = (ws / "big.py").read_text(encoding="utf-8")
    check("write_file: komplette 16k-Datei auf Disk", disk == FULL,
          f" disk={len(disk)}")
    check("write_file meldet Erfolg", "write_file:" in wr and "created" in wr)

    # Voll-Lesung ueber dem Cap: Abbruch mit klarer "Datei ist komplett"-Meldung
    check("full read ueber Cap -> RANGE-Hinweis mit voller Groesse",
          "FILE_TOO_LARGE_NEED_RANGE" in rd_full and str(len(FULL)) in rd_full
          and "COMPLETE" in rd_full)

    # Range-Lesung: kappt, aber Meldung muss volle Zeilen- UND Zeichenzahl tragen
    n_lines_full = FULL.count("\n")
    check("range read: Gesamtzahl der Zeilen im Header",
          f"/ {n_lines_full}]" in rd_range, f" ({n_lines_full})")
    check("range read: TRUNCATED-Note vorhanden", "TRUNCATED" in rd_range)
    check("range read: Note nennt Zeichenzahl der vollen Datei",
          str(len(FULL)) in rd_range.split("TRUNCATED")[-1][:400])
    check("range read: Note nennt konkrete Fortsetzungszeile",
          "start_line" in rd_range.split("TRUNCATED")[-1][:400])

    # Executor-Cap (echter Pfad): darf die Note nicht abschneiden
    capped = _cap_tool_result(rd_range, tool_name="read_file")
    check("executor cap laesst Truncation-Note intakt",
          "TRUNCATED" in capped and str(len(FULL)) in capped[-500:])
    check("executor cap-Fall: kein zirkulaerer read_file-Hinweis",
          "Use read_file to retrieve full content" not in capped)

    print()
    print(f"SUMMARY read_after_write: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
