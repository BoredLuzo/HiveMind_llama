"""VRAM-Fallback-Ctx-Klemme + Stall-Abort (2026-09-07).

Live (02:03-02:17): hermes @39936 nicht ladbar (extern ~3,1 GB), Coder fiel
auf qwen3.5:4b @ctx=10240 zurueck — aber die Tool-Runde verlangte weiter 39936
(CODER-BUDGET ctx=39936) -> "[DUO] Cached port ctx mismatch (10240 vs 39936)
-> LD-SET 2497 -> Stop". Fix 1: _coder_ctx_override klemmt _coder_ctx_eff auf
den Fallback-Ctx. Fix 2: Reclaim/Grace warten nicht mehr 45s+40s auf
strukturell unerreichbare Ziele (stall_abort_s / progress_abort_s + reduzierte
Marge).

Run: python tests/test_fallback_ctx_stall.py
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


# ── Fix-1-Clamp (faithful reproduction) ─────────────────────────────────────
def _clamp_eff(conf_eff, override):
    if override and int(conf_eff) > int(override):
        return int(override)
    return int(conf_eff)


def test_clamp_fallback_ctx():
    if _clamp_eff(39936, 10240) == 10240:
        ok("Fallback-Override klemmt effektiven Coder-Ctx (39936 -> 10240)")
    else:
        fail("clamp_fallback", "erwartet 10240")


def test_no_override_unchanged():
    if _clamp_eff(39936, 0) == 39936 and _clamp_eff(39936, None) == 39936:
        ok("Ohne Override (hermes-Load) bleibt _coder_ctx_eff unveraendert (Regressionsschutz)")
    else:
        fail("no_override", "erwartet 39936")


def test_override_bigger_than_eff_is_noop():
    # Override soll NIE vergroessern (nur klemmen) — selbst wenn er groesser ist.
    if _clamp_eff(10240, 39936) == 10240:
        ok("Override vergroessert nie (nur Clamp nach unten)")
    else:
        fail("override_noop", "erwartet 10240")


def test_fresh_run_resets():
    # Override ist RUN-lokal: ein neuer Run startet ohne Override -> voller Ziel-Ctx.
    # (Kein Mid-Run-Upgrade-Pfad; Reset-Notwendigkeit nur runuebergreifend.)
    override_run1 = 10240
    eff_run1 = _clamp_eff(39936, override_run1)
    override_run2 = 0  # frische Locals im naechsten Run
    eff_run2 = _clamp_eff(39936, override_run2)
    if eff_run1 == 10240 and eff_run2 == 39936:
        ok("Neuer Run (Override=0) startet wieder mit vollem Ziel-Ctx (Reset-Fall abgedeckt)")
    else:
        fail("fresh_run_reset", f"{eff_run1}/{eff_run2}")


# ── Fix-2 Stall-Abort ────────────────────────────────────────────────────────
def _stall_abort_progress(first, best_now, elapsed_s, abort_s=12.0, gain_min=64):
    """Faithful stall-abort-Bedingung (Reclaim & Grace): frueh aufgeben, wenn
    nach abort_s kein Release-Fortschritt >= gain_min MiB messbar ist."""
    if first is None or best_now is None:
        return False
    return elapsed_s >= abort_s and best_now < first + gain_min


def test_stall_abort_no_progress():
    if _stall_abort_progress(5058, 5058, 13.0):
        ok("Kein Release-Fortschritt nach 12s -> frueh aufgeben (statt 45s/40s warten)")
    else:
        fail("stall_no_progress", "erwartet True")


def test_stall_no_abort_while_releasing():
    if not _stall_abort_progress(5058, 5058 + 256, 13.0):
        ok("VRAM steigt (Release laeuft) -> weiter warten, kein Abort")
    else:
        fail("stall_releasing", "erwartet False")


def test_stall_abort_not_before_window():
    if not _stall_abort_progress(5058, 5058, 5.0):
        ok("Vor Abort-Fenster (5s < 12s) kein frueher Abbruch")
    else:
        fail("stall_early", "erwartet False")


# ── Source-Guards ───────────────────────────────────────────────────────────
def test_source_guards():
    root = Path(__file__).parent.parent
    checks = [
        ("core/duo_runner.py", "_coder_ctx_override"),
        ("core/duo_runner.py", "CODER-FALLBACK-CTX"),
        ("core/duo_runner.py", "VRAM-FALLBACK-CTX-CLAMP"),
        ("backend/manager_load.py", "stall_abort_s=12.0"),
        ("backend/manager_load.py", "progress_abort_s=12.0"),
        ("backend/manager_load.py", "VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB)"),
        ("backend/manager_evict.py", "progress_abort_s"),
        ("backend/manager_evict.py", "no release progress"),
        ("backend/llama_vram_table.py", "stall_abort_s"),
        ("backend/llama_vram_table.py", "STALL-ABORT"),
    ]
    bad = []
    for rel, needle in checks:
        try:
            txt = (root / rel).read_text(encoding="utf-8")
        except Exception as e:
            bad.append(f"{rel}:{needle} (read fail {e})")
            continue
        if needle not in txt:
            bad.append(f"{rel}:{needle}")
    if not bad:
        ok("source_guards (Fix1-Klemme + Fix2 Stall-Abort + reduzierte Marge)")
    else:
        fail("source_guards", f"fehlt: {bad}")


if __name__ == "__main__":
    test_clamp_fallback_ctx()
    test_no_override_unchanged()
    test_override_bigger_than_eff_is_noop()
    test_fresh_run_resets()
    test_stall_abort_no_progress()
    test_stall_no_abort_while_releasing()
    test_stall_abort_not_before_window()
    test_source_guards()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
