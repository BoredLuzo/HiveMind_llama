"""Eval: difflib autojunk in tools/patch_2_fuzzy_edit.py (A2, 2026-10-15).

autojunk (default True) markiert in Sequenzen >= 200 Elementen alle
Elemente als Junk, die in >1% der Sequenz vorkommen. Bei einem SEARCH-Block
aus vielen identischen Zeilen ist im Small-Block-Pfad b = Kandidatenfenster
(als String, >= 200 Zeichen) — jedes haeufige Zeichen wird Junk, der Match
kollabiert. Diese Suite prueft: 15 identische Zeilen in uniqueer Umgebung
muessen exakt matchen. Kein LLM noetig.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.patch_2_fuzzy_edit import fuzzy_replace, best_match_location, _norm

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


LINE = "data = append_slice(data, index)"
RUN = 15  # < 20 -> small-block path (char-level SequenceMatcher, b >= 200 chars)

content_lines = [f"head_unique_{i:03d} = {i}" for i in range(40)] \
    + [LINE] * RUN \
    + [f"tail_unique_{i:03d} = {i}" for i in range(40)]
content = "\n".join(content_lines) + "\n"
old_str = "\n".join([LINE] * RUN)
new_str = "replaced_marker = True"

# Fehlmatch-Probe: der exakte Block MUSS am Run-Anfang (Zeile 40) matchen.
loc = best_match_location(content, old_str)
check("T1 best_match_location finds run at line 40",
      loc is not None and loc[0] == 40 and loc[1] >= 0.99,
      f" got {loc}")

result = fuzzy_replace(content, old_str, new_str)
check("T2 fuzzy_replace applies the edit", result is not None)
if result is not None:
    check("T3 replacement landed once",
          result.count("replaced_marker") == 1 and LINE not in result)
    check("T4 unique context preserved", "head_unique_000" in result and "tail_unique_039" in result)
else:
    # Diagnose: derselbe Match mit ausgeschaltetem autojunk
    import difflib
    win = "\n".join(content_lines[40:40 + RUN])
    r_on = difflib.SequenceMatcher(None, old_str, win).ratio()
    r_off = difflib.SequenceMatcher(None, old_str, win, autojunk=False).ratio()
    check("T5 same window scores ~1.0 with autojunk=False (root cause proof)",
          r_off >= 0.99, f" autojunk_on={r_on:.3f} autojunk_off={r_off:.3f}")

# ── Der entscheidende autojunk-Schaden: Anker/Ratio auf Wiederholungsblöcken ─
# 19 identische Zeilen (< 20 -> Small-Block-Pfad, b = Fenstertext >= 200
# Zeichen -> autojunk purgt JEDES haeufige Zeichen aus b2j). Misst vor dem
# Fix (autojunk=True): wahres Fenster ratio 0.067 statt 0.948, und der
# FAILED-Anchor von best_match_location zeigte auf Zeile 54 (ratio 0.31)
# statt in den Block (40..58) — die "+/-5 echte Zeilen"-Diagnostik log.
DRIFT_RUN = 19
run_lines = [LINE] * DRIFT_RUN
block_lines = [f"head_unique_{i:03d} = {i}" for i in range(40)] \
    + run_lines \
    + [f"tail_unique_{i:03d} = {i}" for i in range(40)]
block_content = "\n".join(block_lines) + "\n"
drifted = list(run_lines)
drifted[1] = "data = apend_slice(data, index)"  # Modell zitiert Zeile 2 falsch
old_drift = "\n".join(drifted)

loc_drift = best_match_location(block_content, old_drift)
check("T6 anchor on drifted run lands inside the block with honest ratio",
      loc_drift is not None and 40 <= loc_drift[0] <= 40 + DRIFT_RUN - 1 and loc_drift[1] >= 0.85,
      f" got {loc_drift}")

# Reiner Wiederholungsblock mit Drift ist GENUIN ambig (jedes verschobene
# Fenster in den identischen Zeilen passt fast gleich gut, gemessen 0.958
# bei Start+1) — die Ambiguitaetsregel (2026-09-17) lehnt bewusst ab; das
# bleibt auch mit autojunk=False so und ist NICHT autojunk-geschuldet.
res_drift = fuzzy_replace(block_content, old_drift, new_str)
check("T7 ambiguous pure-repetition edit still declined by design",
      res_drift is None)

print(f"SUMMARY fuzzy_autojunk: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
