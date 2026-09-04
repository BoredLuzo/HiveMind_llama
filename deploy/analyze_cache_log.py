# -*- coding: utf-8 -*-
"""A/B-Analyzer fuer die Cache-Telemetrie im hivemind.log.

Extrahiert aus einem/mehreren Log(s):
  [CACHE] prompt=.. cached=.. reuse=..%          (pro Coder-Round)
  [CTX-COMPRESS] done before=.. after=.. mode=.. (Kompressionen, Shrink)
  [CTX-EVICT-EMERGENCY] / [CTX-EVICT-LEGACY]     (Notfall-/Legacy-Evictions)
  [MSGSIG-CHANGE], [CACHE-MISS]

Output: Vergleichstabelle je Log-Datei + (optional) CSV der Einzel-Rounds.

Usage:
  python deploy/analyze_cache_log.py <log> [<log2> ...] [--csv out.csv] [--rounds 12]

Kernfragen, die es beantwortet:
  - Reuse%-Verlauf: kippt reuse nach Kompression/Eviction (Legacy) oder bleibt
    es zwischen Kompressionen hoch (cache-freundlich)?
  - Kumulativer Reprefill (prompt - cached) ueber alle Rounds.
"""
import csv
import re
import statistics
import sys
from pathlib import Path

_RE_CACHE = re.compile(r"\[CACHE\].*?prompt=(\d+)\s+cached=(\d+)\s+reuse=(\d+)%")
_RE_MODEL = re.compile(r"\[CACHE\] round=([^\s]+)")
_RE_COMPRESS_DONE = re.compile(
    r"\[CTX-COMPRESS\] done before=(\d+)\s+after=(\d+).*?mode=(\S+)")
_RE_COMPRESS_TRIG = re.compile(r"\[CTX-COMPRESS\] trigger=(\S+)")
_RE_EVICT_EMERG = re.compile(r"\[CTX-EVICT-EMERGENCY\]")
_RE_EVICT_LEGACY = re.compile(r"\[CTX-EVICT-LEGACY\]")
_RE_MSGSIG = re.compile(r"\[MSGSIG-CHANGE\]")
_RE_CACHE_MISS = re.compile(r"\[CACHE-MISS\]")
_RE_CTX_EVICT_GENERIC = re.compile(r"\[CTX-EVICT\]")  # altes Event (falls vorhanden)


def analyze(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    rounds = []
    for m in _RE_CACHE.finditer(text):
        rounds.append({
            "prompt": int(m.group(1)),
            "cached": int(m.group(2)),
            "reuse": int(m.group(3)),
        })
    compresses = []
    for m in _RE_COMPRESS_DONE.finditer(text):
        compresses.append({"before": int(m.group(1)), "after": int(m.group(2)),
                           "mode": m.group(3)})
    compress_trig = len(_RE_COMPRESS_TRIG.findall(text))
    return {
        "file": str(path),
        "rounds": rounds,
        "n_rounds": len(rounds),
        "compresses": compresses,
        "n_compress_done": len(compresses),
        "n_compress_trig": compress_trig,
        "n_evict_emergency": len(_RE_EVICT_EMERG.findall(text)),
        "n_evict_legacy": len(_RE_EVICT_LEGACY.findall(text)),
        "n_evict_generic": len(_RE_CTX_EVICT_GENERIC.findall(text)),
        "n_msgsig": len(_RE_MSGSIG.findall(text)),
        "n_cache_miss": len(_RE_CACHE_MISS.findall(text)),
    }


def _pct(vals):
    return f"{statistics.mean(vals):.1f}%" if vals else "-"


def fmt_size(n):
    return f"{n:,}"


def print_file_summary(res):
    r = res["rounds"]
    reuse = [x["reuse"] for x in r]
    total_prompt = sum(x["prompt"] for x in r)
    total_cached = sum(x["cached"] for x in r)
    total_reprefill = total_prompt - total_cached
    modes = {}
    for c in res["compresses"]:
        modes[c["mode"]] = modes.get(c["mode"], 0) + 1
    print(f"\n=== {res['file']} ===")
    print(f"  Tool-Rounds mit usage:      {res['n_rounds']}")
    if r:
        print(f"  Reuse  avg/min:             {_pct(reuse)}  /  min {min(reuse)}%")
        # Verlauf in 4 Buckets (Anfang -> Ende)
        step = max(1, len(r) // 4)
        buckets = [r[i:i + step] for i in range(0, len(r), step)][:4]
        traj = "  Reuse-Verlauf (4 Buckets):   " + "  ".join(
            f"[{statistics.mean([x['reuse'] for x in b]):.0f}%]" for b in buckets)
        print(traj)
    print(f"  Kumulativer Reprefill:       {fmt_size(total_reprefill):>12} tok"
          f"  (prompt {fmt_size(total_prompt)}, cached {fmt_size(total_cached)})")
    print(f"  Kompressionen (done):        {res['n_compress_done']}"
          f"  {modes if modes else ''}")
    print(f"  Kompressionen (trigger):     {res['n_compress_trig']}")
    print(f"  Evictions emergency/legacy:  {res['n_evict_emergency']} / {res['n_evict_legacy']}"
          + (f" (+generic {res['n_evict_generic']})" if res["n_evict_generic"] else ""))
    print(f"  MSGSIG-CHANGE:               {res['n_msgsig']}")
    print(f"  CACHE-MISS (chat_run):       {res['n_cache_miss']}")
    # Detailliste der letzten Kompressionen
    for c in res["compresses"][-6:]:
        shrink = (c["before"] - c["after"]) / max(1, c["before"]) * 100
        print(f"    - compress {c['mode']}: {fmt_size(c['before'])} -> "
              f"{fmt_size(c['after'])} tok  ({shrink:.0f}% shrink)")
    return res


def rounds_table_csv(paths, csv_path, limit_rounds):
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "round", "prompt_tokens", "cached_tokens",
                    "reuse_pct", "reprefill_tokens"])
        for p in paths:
            res = analyze(p)
            for i, x in enumerate(res["rounds"], start=1):
                if limit_rounds and i > limit_rounds:
                    break
                w.writerow([res["file"], i, x["prompt"], x["cached"],
                            x["reuse"], x["prompt"] - x["cached"]])
    print(f"\nCSV geschrieben: {csv_path}")


def main(argv):
    args = []
    _skip_next = False
    for a in argv:
        if _skip_next:
            _skip_next = False
            continue
        if a in ("--csv", "--rounds"):
            _skip_next = True
            continue
        if not a.startswith("--"):
            args.append(a)
    csv_path = None
    limit_rounds = 0
    if "--csv" in argv:
        i = argv.index("--csv")
        csv_path = argv[i + 1] if i + 1 < len(argv) else "cache_rounds.csv"
    if "--rounds" in argv:
        i = argv.index("--rounds")
        try:
            limit_rounds = int(argv[i + 1])
        except (IndexError, ValueError):
            limit_rounds = 0
    if not args:
        print(__doc__)
        return 1
    paths = [Path(a) for a in args]
    results = []
    for p in paths:
        if not p.exists():
            print(f"[SKIP] nicht gefunden: {p}")
            continue
        results.append(print_file_summary(analyze(p)))
    if csv_path:
        rounds_table_csv(paths, csv_path, limit_rounds)
    print("\nHinweis: reuse% sinkt nach Kompression/Eviction immer kurz ab "
          "(Rebuild). Relevant ist, ob es DANACH wieder hochlaeuft "
          "(Cache-Recovery) statt dauerhaft niedrig zu bleiben.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
