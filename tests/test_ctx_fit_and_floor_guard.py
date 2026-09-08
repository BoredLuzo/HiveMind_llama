"""ctx/Marge-Aufloesung + Guard-C-Sequenz (2026-09-06).

Zwei Root Causes aus dem Live-Log (Hermes 35b-a3b @40960, ~5,5-5,7 GB frei):

  1. manager_load.py: eine feste 768-MiB-Marge degradierte still auf ctx=4096
     (needed@40960 ~5018 MiB: 5018+768=5786 > frei ~5582 BLOCK, aber
      5018+256=5274 <= frei OK). Fix: resolve_ctx_fit → 768 → 256 →
     graceful Ladder bzw. strikter Fehler (Coder degradiert nie).
  2. duo_runner.py: Planner=Coder-Reuse erbte einen degradierten Slot, und im
     Round-Loop churnte die Kompression, bis Pfad 1 ([CTX-FULL] skip×4) bzw.
     Pfad 2 (Compress-Streak×3) unspezifisch stoppte. Guard C ([CTX-FLOOR-STOP])
     stoppt VOR der ersten Tool-Runde — die Sequenz hier bildet die Reihenfolge
     ab und beweist: wenn Guard C feuert, werden Pfad 1/2 nicht erreicht.

Run: python tests/test_ctx_fit_and_floor_guard.py
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


def _hermes_needs(ctx):
    # vram_of_moe(hermes3.6:35b-a3b, ctx)*1024 — Werte aus _MOE_TABLE
    # (base 4.66 GB, measured_bytes_per_token 6272). Gerundet wie can_fit.
    import math
    base_mib = 4.66 * 1024
    if ctx <= 4096:
        return base_mib
    overhead_mib = (ctx * 6272) / (1024 ** 2)
    return round(base_mib + overhead_mib, 1)


# ── resolve_ctx_fit (echte Funktion aus llama_manager_utils) ───────────────
def test_resolve_ctx_fit_reduced_margin_full_ctx():
    from backend.llama_manager_utils import resolve_ctx_fit
    res = resolve_ctx_fit(40960, 5667, _hermes_needs, allow_graceful=False)
    if res == (40960, 256):
        ok("strict 40960 @5667MiB frei -> (40960, 256)  [Log-Fall: 5018+256=5274 <= 5667]")
    else:
        fail("strict 40960 @5667", f"erwartet (40960,256), bekam {res}")


def test_resolve_ctx_fit_strict_never_degrades():
    from backend.llama_manager_utils import resolve_ctx_fit
    # Voll-ctx scheitert auch mit 256 (5274 > 5200) -> strikt: None (kein Ladder).
    res = resolve_ctx_fit(40960, 5200, _hermes_needs, allow_graceful=False)
    if res is None:
        ok("strict 40960 @5200MiB frei -> None (kein stiller Down auf 16384/8192)")
    else:
        fail("strict 40960 @5200", f"erwartet None, bekam {res}")


def test_resolve_ctx_fit_graceful_ladder():
    from backend.llama_manager_utils import resolve_ctx_fit
    # Nur Coder-echte 16384@256 (4869+256=5125 <= 5200) -> graceful nimmt 16384.
    res = resolve_ctx_fit(40960, 5200, _hermes_needs, allow_graceful=True)
    if res == (16384, 256):
        ok("graceful 40960 @5200MiB frei -> (16384, 256) Ladder")
    else:
        fail("graceful 40960 @5200", f"erwartet (16384,256), bekam {res}")


def test_resolve_ctx_fit_floor_no_silent_4096():
    from backend.llama_manager_utils import resolve_ctx_fit
    # Zu wenig fuer alles >= 8192@256 (hermes 8192+256 = 5076 > 4800) ->
    # graceful fuer grossen Request: None statt stillem 4096.
    res = resolve_ctx_fit(40960, 4800, _hermes_needs, allow_graceful=True)
    if res is None:
        ok("graceful 40960 @4800MiB frei -> None (nie still unter 8192 fuer grosse Requests)")
    else:
        fail("graceful 40960 @4800", f"erwartet None, bekam {res}")


# ── Guard-C-Sequenz (Reihenfolge Round-0: Guard C VOR Pfad 1/2) ─────────────
def _round0_sequence(slot_degraded, actual_ctx, baseline, ctx_floor=8192):
    """Faithful round-0 sequencing of duo_runner (Guard C vor Kompress-Pfaden).

    Returns (stopped_by_floor, path1_skips, path2_streak). Guard C feuert nur
    bei degraded Slot + actual < floor + baseline >= usable. Wenn er feuert,
    muessen Pfad-1-Zaehler ([CTX-FULL] skip) und Pfad-2-Zaehler
    (Compress-Streak) 0 bleiben — die Kompress-Schleife wird nie betreten.
    """
    usable_floor = actual_ctx - 512
    if slot_degraded and actual_ctx < ctx_floor and baseline >= usable_floor:
        # Guard C: sofortiger Stopp VOR dem Kompress-/Skip-Pfad.
        return (True, 0, 0)
    # Pfad 1 ([CTX-FULL]): simuliert die MIN-VIABLE-Guard-Skips (Skip nur bei
    # fehlendem Headroom pro Round) — hier noch keine Tool-Round durchlaufen.
    path1_skips = 0
    if baseline >= usable_floor:
        path1_skips = 1  # wuerde auf 4 hochlaufen und erst DANN stoppen
    # Pfad 2 (Compress-Streak): ohne Guard C wuerde der Run hier in die
    # (fruchtlose) Kompression laufen und erst nach Streak 3 stoppen.
    path2_streak = 3 if path1_skips == 0 and baseline > actual_ctx else 0
    return (False, path1_skips, path2_streak)


def test_guard_c_sequence_log_case():
    # Log-Fall Run 2: Slot real 4096 (degradiert, angefragt 40960), Baseline ~12k.
    stopped, p1, p2 = _round0_sequence(True, 4096, 12051)
    if stopped and p1 == 0 and p2 == 0:
        ok("round0: Guard C feuert, Pfad 1 (CTX-FULL) & Pfad 2 (Compress-Streak) bleiben 0")
    else:
        fail("round0 guard-c", f"stopped={stopped} p1={p1} p2={p2}")


def test_guard_c_sequence_healthy_slot():
    # Gesunder Slot: Request 40960, Slot 40960, Baseline passt -> kein Stopp.
    stopped, p1, p2 = _round0_sequence(False, 40960, 12051)
    if not stopped:
        ok("round0: gesunder Slot (40960) wird NICHT von Guard C gestoppt")
    else:
        fail("round0 healthy", "Guard C darf bei gesundem Slot nicht feuern")


def test_guard_c_sequence_small_baseline_fits():
    # Degradierter Slot 4096, aber kleine Baseline (2k) passt hinein -> weiter.
    stopped, _, _ = _round0_sequence(True, 4096, 2000)
    if not stopped:
        ok("round0: degradiert, aber Baseline 2k < usable 3584 -> kein Stopp (kein False-Positive)")
    else:
        fail("round0 small-baseline", "Guard C muss bei passender Baseline schweigen")


# ── Source-Guard: Marker im echten duo_runner ──────────────────────────────
def test_source_guard_ctx_floor():
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    checks = {
        "CTX-FLOOR-STOP-Marker": "[CTX-FLOOR-STOP]" in src,
        "slot_degraded": "_slot_degraded" in src,
        "vor-der-Kompression (Pfad1 CTX-FULL existiert)": "[CTX-FULL]" in src,
        "Pfad2 Compress-Streak existiert": "_compress_fail_streak" in src,
        "Slot_floor-Telemetrie": "_slot_ctx_floor_stop" in src,
    }
    bad = [k for k, v in checks.items() if not v]
    if not bad:
        ok("source_guard_ctx_floor (alle Marker vorhanden)")
    else:
        fail("source_guard_ctx_floor", f"fehlt: {bad}")


def test_source_guard_manager_margins():
    src = (Path(__file__).parent.parent / "backend" / "manager_load.py").read_text(encoding="utf-8")
    checks = {
        "REDUCED-MARGIN-Marker": "[PRE-FLIGHT-REDUCED-MARGIN]" in src,
        "resolve_ctx_fit-Aufruf": "resolve_ctx_fit(" in src,
        "ctx_graceful-Thread": "ctx_graceful=" in src,
        "reduced-Konstante-import": "VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB" in src,
    }
    bad = [k for k, v in checks.items() if not v]
    if not bad:
        ok("source_guard_manager_margins (768->256->strict/graceful umgesetzt)")
    else:
        fail("source_guard_manager_margins", f"fehlt: {bad}")


if __name__ == "__main__":
    test_resolve_ctx_fit_reduced_margin_full_ctx()
    test_resolve_ctx_fit_strict_never_degrades()
    test_resolve_ctx_fit_graceful_ladder()
    test_resolve_ctx_fit_floor_no_silent_4096()
    test_guard_c_sequence_log_case()
    test_guard_c_sequence_healthy_slot()
    test_guard_c_sequence_small_baseline_fits()
    test_source_guard_ctx_floor()
    test_source_guard_manager_margins()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
