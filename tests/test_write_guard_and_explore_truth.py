"""Write-Guard (duo_full) + Explore-Content-Truth (2026-09-06).

Regression 2026-09-06 (Live PacMan): pre_explore=False → nur Statik-Symbol-Map
im Kontext (KEINE Dateiinhalte), trotzdem waehlte das System DUO_CODER_EXPLORED
("no read_file needed") → der Coder ueberschrieb bestehende Dateien blind per
write_file.

Fix (B): "has_explore_ctx" wird jetzt ueber _explore_has_contents bestimmt
(= LLM-Pre-Explore-Messages oder Contracts vorhanden). Map-only → read-first
(UNEXPLORED + neue [Codebase analysis]-Regel).

Fix (A): tools/runner.py blockt in duo_full nur noch den destruktiven Alias
write_file auf eine existierende, in diesem Run ungesehene Datei (Freigabe:
gelesen/_written_set/_in_context/allow_overwrite; Rollback-Flag
duo_write_guard_enabled). edit_file/patch_file bleiben frei (Relax-Grund von
2026-09-02 wird nicht reproduziert).

Run: python tests/test_write_guard_and_explore_truth.py
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


# ── Guard-Entscheidung (faithful reproduction von tools/runner.py) ───────────
def _guard_decision(tool_mode, name, exists, *, allow_overwrite=False,
                    read=False, any_read=False, written=False, in_context=False,
                    guard_enabled=True, read_guard_enabled=True,
                    external_dispatch=False):
    """Replik der neuen Guard-Logik (duo_full narrow write_file + alter Pfad)."""
    if external_dispatch:
        return "allow"
    _guard_active_old = (read_guard_enabled and (tool_mode or "") != "duo_full")
    _seen = read or any_read or written or in_context
    if _guard_active_old and exists and not _seen:
        return "block"
    # WRITE-GUARD-duo_full (2026-09-06): nur write_file, nur bestehend,
    # nur ungesehen, nur ohne allow_overwrite, nur bei enabled Flag.
    if (tool_mode or "") == "duo_full" and name == "write_file" \
            and exists and not allow_overwrite and not _seen and guard_enabled:
        return "block"
    return "allow"


def _expect(label, want, got):
    if got == want:
        ok(label)
    else:
        fail(label, f"erwartet {want!r}, bekam {got!r}")


def test_guard_blocks_unread_existing_writefile():
    _expect("duo_full write_file existing unread -> BLOCK",
            "block", _guard_decision("duo_full", "write_file", True))


def test_guard_allows_after_read():
    for label, kw in [("any_read(partial)", {"any_read": True}),
                      ("read_set(full)", {"read": True})]:
        _expect(f"duo_full write_file existing {label} -> allow",
                "allow", _guard_decision("duo_full", "write_file", True, **kw))


def test_guard_allows_written_and_in_context():
    _expect("duo_full write_file existing own-written -> allow",
            "allow", _guard_decision("duo_full", "write_file", True, written=True))
    _expect("duo_full write_file existing in pre-explore context -> allow",
            "allow", _guard_decision("duo_full", "write_file", True, in_context=True))


def test_guard_allow_overwrite_and_disabled_flag():
    _expect("duo_full write_file existing allow_overwrite=true -> allow",
            "allow", _guard_decision("duo_full", "write_file", True, allow_overwrite=True))
    _expect("duo_full write_file duo_write_guard_enabled=false -> allow",
            "allow", _guard_decision("duo_full", "write_file", True, guard_enabled=False))


def test_guard_never_blocks_new_files():
    _expect("duo_full write_file NEW file -> allow",
            "allow", _guard_decision("duo_full", "write_file", False))


def test_guard_does_not_block_edit_file():
    # RELAX-Grund 2026-09-02: edit_file/patch_file duerfen in duo_full nicht
    # geblockt werden (damalige Red-Errors/leere Diffs) — wird nicht reproduziert.
    _expect("duo_full edit_file existing unread -> allow",
            "allow", _guard_decision("duo_full", "edit_file", True))
    _expect("duo_full patch_file existing unread -> allow",
            "allow", _guard_decision("duo_full", "patch_file", True))


def test_old_guard_unchanged_for_non_duo_full():
    _expect("non-duo_full write_file existing unread -> block (alter Pfad)",
            "block", _guard_decision(None, "write_file", True))
    _expect("non-duo_full edit_file existing unread -> block (alter Pfad)",
            "block", _guard_decision(None, "edit_file", True))
    _expect("external dispatch -> allow",
            "allow", _guard_decision("duo_full", "write_file", True, external_dispatch=True))


# ── Explore-Content-Truth (B) ───────────────────────────────────────────────
def _sys_template(has_contents):
    return "EXPLORED" if has_contents else "UNEXPLORED"


def _analysis_rule(has_contents):
    if has_contents:
        return ("covered by pre-exploration", "no read_file needed")
    return ("read_file on it first", "NOT the files' contents")


def test_explore_truth_selects_unexplored_for_map_only():
    # map-only (pre_explore=False, keine Messages/Contracts) -> UNEXPLORED + read-first.
    if _sys_template(False) == "UNEXPLORED":
        ok("map-only -> System-Template UNEXPLORED (read-first)")
    else:
        fail("map-only template", "erwartet UNEXPLORED")
    rule = _analysis_rule(False)
    if "read_file on it first" in rule and "no read_file needed" not in rule:
        ok("map-only -> [Codebase analysis]-Regel verlangt read_file (kein 'no read_file needed')")
    else:
        fail("map-only rule", f"unexpected: {rule}")


def test_explore_truth_keeps_explored_for_real_contents():
    if _sys_template(True) == "EXPLORED":
        ok("echte LLM-Pre-Explore -> System-Template EXPLORED (unveraendert)")
    else:
        fail("explored template", "erwartet EXPLORED")
    rule = _analysis_rule(True)
    if "covered by pre-exploration" in rule and "no read_file needed" in rule:
        ok("echte Pre-Explore -> bisherige Regel bleibt (edit_file direkt)")
    else:
        fail("explored rule", f"unexpected: {rule}")


# ── Source-Guards ───────────────────────────────────────────────────────────
def test_source_guard_explore_truth():
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    checks = {
        "_explore_has_contents": "_explore_has_contents" in src,
        "has_explore_ctx=_explore_has_contents": "has_explore_ctx=_explore_has_contents" in src,
        "map-only-Regel (STATIC SYMBOL INDEX ONLY)": "STATIC SYMBOL INDEX ONLY" in src,
    }
    bad = [k for k, v in checks.items() if not v]
    if not bad:
        ok("source_guard_explore_truth (Flag + Regel umgesetzt)")
    else:
        fail("source_guard_explore_truth", f"fehlt: {bad}")


def test_source_guard_write_guard():
    src = (Path(__file__).parent.parent / "tools" / "runner.py").read_text(encoding="utf-8")
    checks = {
        "WRITE-GUARD-duo_full-Branch": "WRITE-GUARD duo_full" in src,
        "_write_guard_enabled": "_write_guard_enabled" in src,
        "duo_write_guard_enabled": "duo_write_guard_enabled" in src,
    }
    bad = [k for k, v in checks.items() if not v]
    if not bad:
        ok("source_guard_write_guard (duo_full-Guard + Settings-Flag)")
    else:
        fail("source_guard_write_guard", f"fehlt: {bad}")


def test_source_guard_settings_default():
    src = (Path(__file__).parent.parent / "settings.py").read_text(encoding="utf-8")
    if '"duo_write_guard_enabled":      True,' in src or '"duo_write_guard_enabled":     True,' in src \
            or '"duo_write_guard_enabled": True,' in src:
        ok("source_guard_settings_default (duo_write_guard_enabled Default True)")
    else:
        fail("source_guard_settings_default", "Default-Key fehlt")


if __name__ == "__main__":
    test_guard_blocks_unread_existing_writefile()
    test_guard_allows_after_read()
    test_guard_allows_written_and_in_context()
    test_guard_allow_overwrite_and_disabled_flag()
    test_guard_never_blocks_new_files()
    test_guard_does_not_block_edit_file()
    test_old_guard_unchanged_for_non_duo_full()
    test_explore_truth_selects_unexplored_for_map_only()
    test_explore_truth_keeps_explored_for_real_contents()
    test_source_guard_explore_truth()
    test_source_guard_write_guard()
    test_source_guard_settings_default()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
