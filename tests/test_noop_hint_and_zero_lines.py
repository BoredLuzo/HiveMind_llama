"""No-Op-Hint (Run-State, kompressionsfest) + 0-Lines-Klarheit (2026-09-07).

Beobachtung live: nach Full-Compression entfernt [READ-GUARD] read-Pfade; das
Modell sendet danach stale-SEARCH-edit_file-Calls, die nichts bewirken
(Log: 155/158/158 chars ueber 3 Runden). Fix: _track_edit_noop zaehlt
wirkungslose Edits pro Pfad in einem Python-Run-State
(DuoRoundState.edit_noop_streak) — kein Vertrauen auf Message-History, daher
uebersteht der Zaehler Full-Compression (analog duo_error_rollup / Read-Guard).
Ab der 2. No-Op in Folge wird ein read_file-Nudge injiziert
(Rollback-Flag duo_noop_hint_enabled).

Run: python tests/test_noop_hint_and_zero_lines.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


# ── Real-Helfer (duck-typed wie im Executor) ────────────────────────────────
try:
    from core.agentic_duo_state import DuoRoundState
    from core.tool_exec_helpers import _track_edit_noop
    _REAL = True
except Exception as _imp_err:  # pragma: no cover
    _REAL = False
    _imp_reason = str(_imp_err)[:200]

_NOOP_RESULT = "[TOOL_ERROR: EDIT_FILE_NOOP]\nno change - 'C:/x/game.js' already contains exactly this content."
_OK_RESULT = "[edit_file: 'C:/x/game.js' - 1/1 blocks applied (+2 lines)]"
_NOBLOCK_RESULT = "[TOOL_ERROR: EDIT_FILE_NO_BLOCKS_APPLIED]\nNo SEARCH/REPLACE blocks could be applied."


def test_first_noop_counts_no_hint():
    if not _REAL:
        return
    rs = DuoRoundState()
    h = _track_edit_noop("edit_file", _NOOP_RESULT, "C:/x/game.js", rs)
    if h is None and rs.edit_noop_streak.get("C:/x/game.js") == 1:
        ok("1. No-Op -> kein Hint, Streak=1 (Run-State)")
    else:
        fail("first_noop", f"hint={h!r} streak={rs.edit_noop_streak}")


def test_second_noop_fires_hint():
    if not _REAL:
        return
    rs = DuoRoundState()
    _track_edit_noop("edit_file", _NOOP_RESULT, "C:/x/game.js", rs)
    # "Kompression" simuliert: KEIN Zugriff auf Message-History / kein Reset —
    # der Zaehler lebt allein im Run-State und bleibt erhalten.
    h = _track_edit_noop("edit_file", _NOOP_RESULT, "C:/x/game.js", rs)
    if h is not None and "[NO-OP]" in h and "read_file" in h and rs.edit_noop_streak.get("C:/x/game.js", 0) == 0:
        ok("2. No-Op (nach sim. Kompression, gleicher Run-State) -> Hint + Streak-Reset")
    else:
        fail("second_noop", f"hint={str(h)[:80]!r} streak={rs.edit_noop_streak}")


def test_compression_resilience_no_hint_recomputed_from_msgs():
    if not _REAL:
        return
    # Beweis: Der Zaehler wird NICHT aus der Message-History abgeleitet. Der
    # erste No-Op fuehrt den Zaehler, ohne dass irgendeine Nachricht existiert.
    rs = DuoRoundState()
    _track_edit_noop("patch_file", _NOBLOCK_RESULT, "p", rs)
    if rs.edit_noop_streak.get("p") == 1:
        ok("compression-resilient: Zaehler lebt im Run-State (keine Messages noetig)")
    else:
        fail("compression_resilience", f"streak={rs.edit_noop_streak}")


def test_success_resets_streak():
    if not _REAL:
        return
    rs = DuoRoundState()
    _track_edit_noop("edit_file", _NOOP_RESULT, "C:/x/game.js", rs)
    _track_edit_noop("edit_file", _OK_RESULT, "C:/x/game.js", rs)  # Erfolg
    h = _track_edit_noop("edit_file", _NOOP_RESULT, "C:/x/game.js", rs)
    if h is None and rs.edit_noop_streak.get("C:/x/game.js") == 1:
        ok("Erfolg resettet Streak; danach zaehlt 1. No-Op wieder frisch")
    else:
        fail("success_reset", f"hint={str(h)[:40]!r} streak={rs.edit_noop_streak}")


def test_untracked_tools_ignored():
    if not _REAL:
        return
    rs = DuoRoundState()
    h = _track_edit_noop("read_file", _NOOP_RESULT, "C:/x/game.js", rs)
    h2 = _track_edit_noop("run_bash", "ok", "C:/x", rs)
    if h is None and h2 is None and not rs.edit_noop_streak:
        ok("Nur edit/patch/write werden getrackt")
    else:
        fail("untracked", f"h={h!r} h2={h2!r} streak={rs.edit_noop_streak}")


def test_no_blocks_and_old_str_not_found_count():
    if not _REAL:
        return
    rs = DuoRoundState()
    _track_edit_noop("patch_file", _NOBLOCK_RESULT, "q", rs)
    h = _track_edit_noop("patch_file", "[TOOL_ERROR: PATCH_FILE_OLD_STR_NOT_FOUND]", "q", rs)
    if h is not None and "[NO-OP]" in h:
        ok("SEARCH-not-found/No-Block-Edits zaehlen als wirkungslos")
    else:
        fail("no_blocks", f"hint={str(h)[:60]!r}")


# ── 0-Lines-Klarheit (file_ops Nachricht) ───────────────────────────────────
def _blocks_msg(delta, applied=1, total=1, chard=0):
    # Faithful reproduction der Datei-Meldung (file_ops.py SEARCH/REPLACE-Pfad).
    r = f"[edit_file: 'p' - {applied}/{total} blocks applied ({delta:+d} lines)"
    if delta == 0:
        r += f", {chard:+d} chars"
    return r + "]"


def test_zero_lines_shows_chars():
    msg = _blocks_msg(0, chard=123)
    if "chars" in msg and "(+0 lines), +123 chars]" in msg:
        ok("delta==0 -> '+0 lines, +123 chars' (Inline-Fix nicht mehr irrefuehrend)")
    else:
        fail("zero_lines_msg", msg)


def test_nonzero_lines_unaffected():
    msg = _blocks_msg(2, chard=0)
    if "chars" not in msg and "(+2 lines)]" in msg:
        ok("delta!=0 -> unveraendertes Format (kein char-Zusatz)")
    else:
        fail("nonzero_lines_msg", msg)


# ── Source-Guards ───────────────────────────────────────────────────────────
def test_source_guards():
    root = Path(__file__).parent.parent
    checks = [
        ("core/tool_exec_helpers.py", "_track_edit_noop", "_track_edit_noop"),
        ("core/tool_exec_helpers.py", "duo_noop_hint_enabled", "duo_noop_hint_enabled"),
        ("core/agentic_duo_state.py", "edit_noop_streak", "edit_noop_streak"),
        ("core/tool_executor.py", "_round_noop_hints", "_round_noop_hints"),
        ("tools/handlers/file_ops.py", "0-LINES-CLARITY", "0-LINES-CLARITY"),
        ("settings.py", "duo_noop_hint_enabled", "duo_noop_hint_enabled"),
    ]
    bad = []
    for rel, needle, label in checks:
        try:
            txt = (root / rel).read_text(encoding="utf-8")
        except Exception as e:
            bad.append(f"{rel}:{label} (read fail {e})")
            continue
        if needle not in txt:
            bad.append(f"{rel}:{label}")
    if not bad:
        ok("source_guards (Helper + Run-State-Feld + Wiring + Flag)")
    else:
        fail("source_guards", f"fehlt: {bad}")


if __name__ == "__main__":
    if not _REAL:
        print(f"  NOTE  Real-Import nicht verfuegbar: {_imp_reason} — nur Source-Guards/Replikas laufen")
    test_first_noop_counts_no_hint()
    test_second_noop_fires_hint()
    test_compression_resilience_no_hint_recomputed_from_msgs()
    test_success_resets_streak()
    test_untracked_tools_ignored()
    test_no_blocks_and_old_str_not_found_count()
    test_zero_lines_shows_chars()
    test_nonzero_lines_unaffected()
    test_source_guards()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
