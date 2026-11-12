#!/usr/bin/env python3
"""Analyze benchmark result JSONs (Wilson CI + paired McNemar + D1 relative).

Usage:
    python bench/analyze.py --results run1.json run2.json
    python bench/analyze.py --results bench/results/run-*.json

Cell-level pass rates get a 95% Wilson interval. The paired comparison uses
one majority verdict per (task, condition): a task counts as passed when
more than half of its runs passed. Tasks missing in either condition are
skipped. McNemar's exact test is two-sided over the discordant pairs.

Since D1 (2026-11-09) the summary also carries a `relative` block: pass-rate
deltas vs the raw/raw_files baselines, per-category deltas, cached/net token
ratios vs aider and the tool-round counts (bench/relative.py).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.relative import build_relative, category as _rel_category  # noqa: E402
from bench.relative import print_relative  # noqa: E402

Z = 1.959963984540054


def wilson(passes: int, total: int) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if total <= 0:
        return 0.0, 0.0
    p = passes / total
    denom = 1.0 + Z * Z / total
    centre = (p + Z * Z / (2 * total)) / denom
    half = Z * math.sqrt(p * (1 - p) / total + Z * Z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def majority_verdicts(rows: list[dict]) -> dict[str, dict[str, bool]]:
    """condition -> task -> passed in the majority of its runs.

    A tie (e.g. 1 pass / 1 fail) counts as NOT passed; use 3+ runs for a
    meaningful majority.
    """
    counts: dict[str, dict[str, list[bool]]] = {}
    for row in rows:
        cond = str(row.get("condition") or "")
        task = str(row.get("task") or "")
        if cond and task:
            counts.setdefault(cond, {}).setdefault(task, []).append(bool(row.get("ok")))
    out: dict[str, dict[str, bool]] = {}
    for cond, tasks in counts.items():
        out[cond] = {
            task: sum(oks) * 2 > len(oks) for task, oks in tasks.items()
        }
    return out


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


def _category(task: str) -> str:
    """Kept for backward compatibility; the shared mapping lives in
    bench/relative.py (single source for runner and analyzer)."""
    return _rel_category(task)


def summarize(rows: list[dict]) -> dict:
    # DEDUPE (2026-11-08): overlapping result files (resumed run + original)
    # must not double-vote a task. The run number is shifted per file, so the
    # same cell copied into two checkpoints can arrive under two run numbers;
    # its timestamp + measurement fingerprint identifies the duplicate.
    _unique: dict[tuple, dict] = {}
    for _idx, _row in enumerate(rows):
        _ts = str(_row.get("timestamp") or "")
        if _ts:
            _key = ("cell", str(_row.get("condition") or ""),
                    str(_row.get("task") or ""), _ts,
                    _row.get("seconds"), _row.get("tokens"), _row.get("ok"))
        else:
            _key = (str(_row.get("condition") or ""), str(_row.get("task") or ""),
                    _row.get("run", _idx))
        _unique[_key] = _row
    rows = list(_unique.values())
    conditions: dict[str, dict] = {}
    for cond in sorted({str(r.get("condition") or "") for r in rows} - {""}):
        mine = [r for r in rows if r.get("condition") == cond]
        passes = sum(1 for r in mine if r.get("ok"))
        lo, hi = wilson(passes, len(mine))
        conditions[cond] = {
            "cells": len(mine),
            "passes": passes,
            "pass_rate": passes / len(mine) if mine else 0.0,
            "ci95": [round(lo, 4), round(hi, 4)],
            "tokens_total": sum(int(r.get("tokens") or 0) for r in mine),
            "cached_tokens_total": sum(int(r.get("cached_tokens") or 0) for r in mine),
            "net_tokens_total": sum(int(r.get("net_tokens") or r.get("tokens") or 0) for r in mine),
        }
    verdicts = majority_verdicts(rows)
    paired = {}
    names = sorted(verdicts)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted(set(verdicts[a]) & set(verdicts[b]))
            if not common:
                continue
            b_only = sum(1 for t in common if verdicts[a][t] and not verdicts[b][t])
            c_only = sum(1 for t in common if not verdicts[a][t] and verdicts[b][t])
            paired[f"{a} vs {b}"] = {
                "tasks": len(common),
                f"{a}_only": b_only,
                f"{b}_only": c_only,
                "mcnemar_p": round(mcnemar_exact(b_only, c_only), 4),
            }
    categories: dict[str, dict[str, float]] = {}
    for cond, tasks in verdicts.items():
        buckets: dict[str, list[bool]] = {}
        for task, ok in tasks.items():
            buckets.setdefault(_category(task), []).append(ok)
        categories[cond] = {
            name: round(sum(vals) / len(vals), 3) for name, vals in sorted(buckets.items())
        }
    # D1: compact relative block on the same (deduped) rows - deltas vs the
    # raw/raw_files baselines, per-category deltas, aider token ratios, rounds.
    return {"conditions": conditions, "paired": paired, "categories": categories,
            "relative": build_relative(rows)}


def _print(summary: dict) -> None:
    print(f"{'condition':18} {'cells':>5} {'pass':>6} {'95% CI':>16}")
    for cond, row in summary["conditions"].items():
        lo, hi = row["ci95"]
        print(f"{cond:18} {row['cells']:5d} {row['pass_rate']:6.2f} "
              f"[{lo:.2f}, {hi:.2f}]")
    print()
    for pair, row in summary["paired"].items():
        a, b = pair.split(" vs ")
        print(f"paired (majority) {pair}: tasks={row['tasks']} "
              f"{a}_only={row[f'{a}_only']} {b}_only={row[f'{b}_only']} "
              f"McNemar p={row['mcnemar_p']}")
    print()
    for cond, cats in summary["categories"].items():
        print(f"{cond:18} " + "  ".join(f"{k}={v:.2f}" for k, v in cats.items()))
    if summary.get("relative"):
        print_relative(summary["relative"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", nargs="*", default=[])
    parser.add_argument("--by-model", action="store_true",
                        help="differentiate conditions by model tag (e.g. hivemind_duo@35b-a3b-ud)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    if not args.results:
        default_files = sorted((_ROOT / "bench" / "results").glob("*.json"))
        if default_files:
            args.results = [str(f) for f in default_files]
        else:
            print("Keine Ergebnis-Dateien in bench/results/ gefunden. Starte zuerst einen Benchmark (Zelle 5, 6a oder 7b)!")
            return 0
    rows: list[dict] = []
    offsets_per_group: dict[tuple[str, str], int] = {}
    gpus: set[str] = set()
    models: set[str] = set()
    for name in args.results:
        path = Path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return 2
        file_model = data.get("model") or ""
        if file_model:
            models.add(file_model)
        file_gpu = data.get("gpu") or {}
        if isinstance(file_gpu, dict) and file_gpu.get("name") and file_gpu["name"] != "Unknown":
            gpus.add(f"{file_gpu['name']} ({file_gpu.get('vram_total', '').strip()})".strip())
        file_rows = data.get("results") or []
        cur_file_max_runs: dict[tuple[str, str], int] = {}
        for r in file_rows:
            if not r.get("model") and file_model:
                r["model"] = file_model
            c = r.get("condition") or "unknown"
            if args.by_model and r.get("model"):
                m_short = r["model"].split(":")[-1] if ":" in r["model"] else r["model"]
                c = f"{c}@{m_short}"
                r["condition"] = c
            t = r.get("task") or ""
            grp_key = (c, t)
            orig_run = int(r.get("run") or 1)
            off = offsets_per_group.get(grp_key, 0)
            r["run"] = orig_run + off
            cur_file_max_runs[grp_key] = max(cur_file_max_runs.get(grp_key, 0), orig_run)
            rows.append(r)
        for k, max_run in cur_file_max_runs.items():
            offsets_per_group[k] = offsets_per_group.get(k, 0) + max_run
    if not rows:
        print("no result rows", file=sys.stderr)
        return 2
    summary = summarize(rows)
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        if gpus:
            print(f"[Hardware] GPU: {', '.join(sorted(gpus))}")
        if models:
            print(f"[Evaluation] Model(s): {', '.join(sorted(models))}")
        max_run = max((r.get("run", 1) for r in rows), default=1)
        print(f"[Tracking] Total pooled runs: {max_run} run(s) across {len(args.results)} file(s)\n")
        _print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
