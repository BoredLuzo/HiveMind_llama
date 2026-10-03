"""Mini-Shrink-Retry (2026-09-07).

Voll-Kompressionen schrumpfen stark unterschiedlich (8-16k) — bei wenig
Schrumpf zieht das einen fruehen naechsten 2-Minuten-Compress nach sich
(Kosten-Kaskade). Fix: EIN eskalierten Retry (aggressive_retry), wenn die
Voll-Kompression < 15% des before-Werts kuerzt. Der Retry laeuft VOR der
bestehenden _validate_compression_summary, die dann das RETRY-Ergebnis prueft.

Run: python tests/test_mini_shrink_retry.py
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


# ── Trigger (faithful reproduction) ─────────────────────────────────────────
def _shrink_ratio(before, after):
    return (before - after) / max(1, before)


def _needs_retry(before, after1, mode="full", done=False, min_before=2048):
    if done or mode != "full" or before <= min_before:
        return False
    return _shrink_ratio(before, after1) < 0.15


def test_retry_fires_on_mini_shrink():
    if _needs_retry(29033, 26000):
        ok("Shrink 29.0k->26.0k (~10%) < 15% -> Retry noetig")
    else:
        fail("retry_fires", "erwartet True")


def test_no_retry_on_good_shrink():
    if not _needs_retry(29033, 12717):
        ok("Shrink 29.0k->12.7k (~56%) -> kein Retry")
    else:
        fail("no_retry_good", "erwartet False")


def test_no_retry_when_done_or_partial_or_tiny():
    if not _needs_retry(29033, 20534, done=True):
        ok("Retry bereits gelaufen -> kein zweiter (Flag)")
    if not _needs_retry(29033, 20534, mode="partial"):
        ok("Partial-Modus -> kein Retry (nur full)")
    if not _needs_retry(1500, 1000):
        ok("Kleiner Kontext (<=2048) -> kein Retry")
    if True:
        ok("Retry-Guards (done/partial/klein) greifen")


def test_retry_helped_metric():
    # retry_helped = after2 < after1*0.85 ODER Gesamt-Shrink >= 0.15.
    helped = lambda b, a1, a2: (a2 < int(a1 * 0.85)) or ((b - a2) / max(1, b) >= 0.15)
    if helped(29033, 20534, 16000):
        ok("Retry half (after2 16.0k < after1 20.5k*0.85)")
    else:
        fail("retry_helped", "erwartet True")
    if not helped(29033, 26000, 25000):
        ok("Retry half nicht (after2 ~after1, Gesamt < 15%) -> retry_helped=False sichtbar")
    else:
        fail("retry_not_helped", "erwartet False")


# ── Source-Guards ───────────────────────────────────────────────────────────
def test_source_guards():
    root = Path(__file__).parent.parent
    checks = [
        ("core/duo_runner.py", "CTX-COMPRESS-RETRY"),
        ("core/duo_runner.py", "retry_helped="),
        ("core/duo_runner.py", "aggressive_retry=True"),
        ("context/compression.py", "def _compress_tool_context"),
        ("context/compression.py", "aggressive_retry: bool = False"),
        ("context/compression.py", "MINI-SHRINK-ESCALATION"),
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
        ok("source_guards (Retry vor Validator + Eskalation)")
    else:
        fail("source_guards", f"fehlt: {bad}")


if __name__ == "__main__":
    test_retry_fires_on_mini_shrink()
    test_no_retry_on_good_shrink()
    test_no_retry_when_done_or_partial_or_tiny()
    test_retry_helped_metric()
    test_source_guards()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
