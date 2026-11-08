#!/usr/bin/env python3
"""Analyze benchmark result JSONs (Wilson CI + paired McNemar).

Usage:
    python bench/analyze.py --results run1.json run2.json
    python bench/analyze.py --results bench/results/run-*.json

Cell-level pass rates get a 95% Wilson interval. The paired comparison uses
one majority verdict per (task, condition): a task counts as passed when
more than half of its runs passed. Tasks missing in either condition are
skipped. McNemar's exact test is two-sided over the discordant pairs.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

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
    for prefix, name in (("fix_", "bugfix"), ("feature_", "feature"),
                         ("refactor_", "refactor"), ("multi_", "multi-file")):
        if task.startswith(prefix):
            return name
    return "other"


def summarize(rows: list[dict]) -> dict:
    # DEDUPE (2026-11-08): overlapping result files (resumed run + original)
    # must not double-vote a task. Keep the last row per (condition, task, run).
    _unique: dict[tuple, dict] = {}
    for _idx, _row in enumerate(rows):
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
    return {"conditions": conditions, "paired": paired, "categories": categories}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", nargs="+", required=True)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    rows: list[dict] = []
    for name in args.results:
        path = Path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return 2
        rows.extend(data.get("results") or [])
    if not rows:
        print("no result rows", file=sys.stderr)
        return 2
    summary = summarize(rows)
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        _print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
