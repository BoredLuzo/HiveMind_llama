#!/usr/bin/env python3
"""Relative-gain metrics for the benchmark reports (D1, 2026-11-09).

The headline number is never an absolute pass rate - RExBench/WebGen-Bench
show low absolute numbers for every agent. What matters is the DELTA against
the no-tool baselines (`raw`, `raw_files`), per task category, plus the token
cost of that delta (HiveMind vs aider, the same-model reference) and how many
tool rounds it took.

This module is stdlib-only and shared by bench/runner.py and bench/analyze.py
so both reports compute the same numbers from the same rows.
"""
from __future__ import annotations

import statistics

# Baselines the deltas are taken against, in reporting order. `raw` is the
# strict single-shot diff control, `raw_files` the easier full-file variant.
BASELINES = ("raw", "raw_files")
# Token ratios are reported against the same-model CLI harness (aider runs on
# the same llama-server, so its token spend is the honest reference point).
REFERENCE_CONDITION = "aider"

CATEGORY_PREFIXES = (("fix_", "bugfix"), ("feature_", "feature"),
                     ("refactor_", "refactor"), ("multi_", "multi-file"),
                     ("hard_", "hard"), ("polyglot_", "polyglot"),
                     ("deepswe_", "deepswe"))


def category(task: str) -> str:
    """Task id prefix -> category (bugfix/feature/refactor/multi-file/other)."""
    name = str(task or "")
    for prefix, cat in CATEGORY_PREFIXES:
        if name.startswith(prefix):
            return cat
    return "other"


def _ratio(value: float, base) -> float | None:
    """value/base rounded, or None when the base is missing or zero."""
    if base is None:
        return None
    try:
        base_f = float(base)
    except (TypeError, ValueError):
        return None
    if base_f == 0:
        return None
    return round(float(value) / base_f, 4)


def _condition_order(rows: list[dict]) -> list[str]:
    """Conditions in order of first appearance (stable, no import of runner)."""
    seen: list[str] = []
    for row in rows:
        cond = str(row.get("condition") or "")
        if cond and cond not in seen:
            seen.append(cond)
    return seen


def condition_stats(rows: list[dict], cond: str) -> dict:
    """Cell-level stats for one condition, categories included.

    Missing/zero tokens leave `cached_share` as None - a condition that spent
    no tokens has no share to report. Same for the ratios built from this.
    """
    mine = [r for r in rows if str(r.get("condition") or "") == cond]
    if not mine:
        return {}
    tokens = sum(int(r.get("tokens") or 0) for r in mine)
    cached = sum(int(r.get("cached_tokens") or 0) for r in mine)
    net = sum(
        int(r["net_tokens"] if r.get("net_tokens") is not None else (r.get("tokens") or 0))
        for r in mine
    )
    rounds = [int(r.get("rounds") or 0) for r in mine]
    cats: dict[str, list[int]] = {}
    for row in mine:
        cats.setdefault(category(row.get("task")), []).append(1 if row.get("ok") else 0)
    return {
        "runs": len(mine),
        "pass_rate": round(sum(1 for r in mine if r.get("ok")) / len(mine), 4),
        "tokens_total": tokens,
        "cached_tokens_total": cached,
        "net_tokens_total": net,
        "cached_share": round(cached / tokens, 4) if tokens else None,
        "rounds_total": sum(rounds),
        "rounds_median": round(statistics.median(rounds), 1) if rounds else 0.0,
        "categories": {name: round(sum(v) / len(v), 4)
                       for name, v in sorted(cats.items())},
    }


def build_relative(rows: list[dict]) -> dict:
    """Compact `relative` block for the JSON summary.

    Per condition: pass-rate deltas vs every baseline present (None when that
    baseline is missing from the rows), per-category rates and deltas,
    cached/net token ratios vs the aider reference (None when aider is absent
    or spent zero tokens) and the tool-round counts.
    """
    order = _condition_order(rows)
    stats = {cond: condition_stats(rows, cond) for cond in order}
    out: dict = {
        "baseline": BASELINES[0],
        "baselines_present": [b for b in BASELINES if any(c == b or c.startswith(b + "@") for c in stats)],
        "reference": REFERENCE_CONDITION,
        "conditions": {},
    }
    for cond in order:
        mine = stats[cond]
        sfx = f"@{cond.split('@', 1)[1]}" if "@" in cond else ""
        ref = stats.get(REFERENCE_CONDITION + sfx, {})
        entry: dict = {"pass_rate": mine["pass_rate"]}
        for base in BASELINES:
            base_stats = stats.get(base + sfx)
            entry[f"delta_vs_{base}"] = (
                None if base_stats is None
                else round(mine["pass_rate"] - base_stats["pass_rate"], 4))
        cats: dict = {}
        for name, rate in mine["categories"].items():
            cat_entry: dict = {"pass_rate": rate}
            for base in BASELINES:
                base_rate = stats.get(base + sfx, {}).get("categories", {}).get(name)
                cat_entry[f"delta_vs_{base}"] = (
                    None if base_rate is None else round(rate - base_rate, 4))
            cats[name] = cat_entry
        entry["categories"] = cats
        entry["tokens"] = {
            "total": mine["tokens_total"],
            "net_total": mine["net_tokens_total"],
            "cached_total": mine["cached_tokens_total"],
            "cached_share": mine["cached_share"],
            "net_vs_aider_ratio": _ratio(mine["net_tokens_total"],
                                         ref.get("net_tokens_total")),
            "total_vs_aider_ratio": _ratio(mine["tokens_total"],
                                           ref.get("tokens_total")),
        }
        entry["rounds"] = {"total": mine["rounds_total"],
                           "median": mine["rounds_median"]}
        out["conditions"][cond] = entry
    return out


def _fmt_delta(value) -> str:
    return "-" if value is None else f"{value:+.2f}"


def _fmt_ratio(value) -> str:
    return "-" if value is None else f"{value:.2f}x"


def print_relative(relative: dict) -> None:
    """Readable block for runner.py / analyze.py console output."""
    print()
    print("relative (pass-rate deltas; token ratios vs "
          f"{relative.get('reference', REFERENCE_CONDITION)})")
    print(f"{'condition':<20} {'pass':>6} {'d_raw':>7} {'d_raw_files':>11} "
          f"{'net/aider':>10} {'cached':>7} {'rounds':>6}")
    for cond, entry in relative.get("conditions", {}).items():
        tokens = entry["tokens"]
        cached = tokens["cached_share"]
        print(f"{cond:<20} {entry['pass_rate']:>6.2f} "
              f"{_fmt_delta(entry.get('delta_vs_raw')):>7} "
              f"{_fmt_delta(entry.get('delta_vs_raw_files')):>11} "
              f"{_fmt_ratio(tokens['net_vs_aider_ratio']):>10} "
              f"{'-' if cached is None else f'{cached * 100:.0f}%':>7} "
              f"{entry['rounds']['median']:>6.1f}")
        for cat, cat_entry in entry.get("categories", {}).items():
            deltas = "  ".join(
                f"d_vs_{base}={_fmt_delta(cat_entry.get(f'delta_vs_{base}'))}"
                for base in BASELINES if f"delta_vs_{base}" in cat_entry)
            print(f"  {cat:<18} pass={cat_entry['pass_rate']:.2f}  {deltas}")
