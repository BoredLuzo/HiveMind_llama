#!/usr/bin/env python3
"""External-harness adapter (D8, 2026-11-09): run the bench task suite
through Claude Code and/or Codex CLI when they are installed.

NON-INVASIVE BY DESIGN
  - bench/runner.py conditions (raw/raw_files/hivemind_*/aider) are NOT
    touched; this script only REUSES the runner's task loading, workspace
    preparation, churn metrics and the hidden-test check.
  - Result rows carry the same fields as runner rows (task/condition/ok/
    seconds/tokens/...), so bench/analyze.py reads external results unchanged.
  - Results land in bench/results/external/ - never mixed into a runner file.

PREREQUISITES (hard, checked, never auto-installed):
  claude  Claude Code CLI on PATH (or --claude-bin / HIVEMIND_CLAUDE_BIN)
          plus a working login session or ANTHROPIC_API_KEY.
  codex   Codex CLI on PATH (or --codex-bin / HIVEMIND_CODEX_BIN)
          plus a working login session or OPENAI_API_KEY.

Binaries or credentials absent -> the harness is skipped cleanly; nothing
fails loudly, and a run with zero available harnesses exits 3 without
writing a result file.

The external harnesses run their OWN default models on their OWN servers -
record them, but report them separately from the same-model comparison
(see docs/benchmarks/ROADMAP.md Phase 5). No results are claimed from this
scaffold alone; a claim needs actual runs.

Usage:
    python bench/external/run_external.py --list
    python bench/external/run_external.py --harness claude --runs 3
    python bench/external/run_external.py --harness claude,codex --only fix_add
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner import (  # noqa: E402
    Config, Task, _payload, aggregate, diff_stats, load_tasks, prepare_workspace,
    print_summary, run_check,
)
from bench.relative import build_relative  # noqa: E402

HARNESSES = ("claude", "codex")


# ── output parsers ─────────────────────────────────────────────────────────
def _first_json_object(text: str) -> dict:
    """First parseable JSON object in (possibly noisy) CLI output."""
    decoder = json.JSONDecoder()
    start = (text or "").find("{")
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return {}


def _walk_dicts(obj) -> "list[dict]":
    out: list[dict] = []
    if isinstance(obj, dict):
        out.append(obj)
        for value in obj.values():
            out.extend(_walk_dicts(value))
    elif isinstance(obj, list):
        for value in obj:
            out.extend(_walk_dicts(value))
    return out


def _tail(text: str, limit: int = 500) -> str:
    return (text or "").strip()[-limit:]


def parse_claude(stdout: str, stderr: str) -> dict:
    """Parse `claude -p --output-format json`.

    usage: input_tokens (uncached), cache_creation_input_tokens,
    cache_read_input_tokens, output_tokens. `tokens` mirrors the runner's
    semantics (full throughput incl. cached prompt), `net_tokens` the
    uncached+completion part. cost_usd only when the CLI reports it.
    """
    data = _first_json_object(stdout)
    usage = data.get("usage") or {}
    uncached_in = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cached = (int(usage.get("cache_read_input_tokens") or 0)
              + int(usage.get("cache_creation_input_tokens") or 0))
    cost = data.get("total_cost_usd")
    detail = str(data.get("result") or "")
    if data.get("is_error"):
        detail = f"harness reported is_error: {detail}"
    parsed = {
        "tokens": uncached_in + completion + cached,
        "cached_tokens": cached,
        "net_tokens": uncached_in + completion,
        "rounds": int(data.get("num_turns") or 0),
        "cost_usd": round(float(cost), 6) if isinstance(cost, (int, float)) else None,
        "duration_reported_s": round(int(data.get("duration_ms") or 0) / 1000, 1),
        "model_detected": "",
        "detail": detail.strip() or _tail(stdout + stderr),
    }
    return parsed


def parse_codex(stdout: str, stderr: str) -> dict:
    """Parse `codex exec --json` JSONL.

    Event shapes differ between Codex versions, so this is deliberately
    best-effort: keep the LAST `total_token_usage` dict seen anywhere in the
    stream and the last reported model name. Zero fallbacks, never a crash.
    """
    usage: dict = {}
    model = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for node in _walk_dicts(event):
            candidate = node.get("total_token_usage")
            if isinstance(candidate, dict):
                usage = candidate
            if isinstance(node.get("model"), str) and node["model"]:
                model = node["model"]
    uncached_in = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_output_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (uncached_in + completion + cached))
    return {
        "tokens": total,
        "cached_tokens": cached,
        "net_tokens": max(0, total - cached),
        "rounds": 0,
        "cost_usd": None,
        "duration_reported_s": 0.0,
        "model_detected": model,
        "detail": _tail(stdout + stderr),
    }


# ── command builders (cwd = the fresh task workspace) ─────────────────────
def build_claude_command(bin_path: str, prompt: str, model: str,
                         work: Path, max_turns: int) -> list[str]:
    cmd = [
        bin_path, "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(max_turns),
        # Benchmark cells must not stop on a permission prompt.
        "--permission-mode", "bypassPermissions",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def build_codex_command(bin_path: str, prompt: str, model: str,
                        work: Path, max_turns: int) -> list[str]:
    cmd = [
        bin_path, "exec", "--json",
        "--skip-git-repo-check",
        "--sandbox", "workspace-write",
        "-C", str(work),
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


@dataclass
class HarnessSpec:
    harness: str
    condition: str
    bin_name: str
    env_bin: str
    build_command: Callable[..., list[str]]
    parse: Callable[[str, str], dict]
    prerequisites: str


HARNESS_SPECS: dict[str, HarnessSpec] = {
    "claude": HarnessSpec(
        harness="claude",
        condition="claude_code",
        bin_name="claude",
        env_bin="HIVEMIND_CLAUDE_BIN",
        build_command=build_claude_command,
        parse=parse_claude,
        prerequisites="Claude Code CLI on PATH + login session or ANTHROPIC_API_KEY",
    ),
    "codex": HarnessSpec(
        harness="codex",
        condition="codex",
        bin_name="codex",
        env_bin="HIVEMIND_CODEX_BIN",
        build_command=build_codex_command,
        parse=parse_codex,
        prerequisites="Codex CLI on PATH + login session or OPENAI_API_KEY",
    ),
}


def resolve_bin(harness: str, explicit: str = "") -> str:
    """Explicit arg > env override > PATH. Empty string = not available."""
    spec = HARNESS_SPECS[harness]
    candidate = (explicit or os.environ.get(spec.env_bin) or "").strip()
    if candidate:
        return candidate if Path(candidate).is_file() else ""
    return shutil.which(spec.bin_name) or ""


def _invoke(cmd: list[str], work: Path, timeout_s: int) -> tuple[int, str, str]:
    """Run the harness CLI; split out so tests can stub it."""
    r = subprocess.run(
        cmd, cwd=str(work), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout_s,
    )
    return r.returncode, r.stdout or "", r.stderr or ""


def plan_cell(harness: str, task: Task, bin_path: str, model: str,
              max_turns: int) -> dict:
    """What WOULD run - no execution (used by --list / --dry-run)."""
    spec = HARNESS_SPECS[harness]
    cmd = spec.build_command(bin_path, task.prompt, model, Path(task.id), max_turns)
    preview = " ".join(str(part) for part in cmd)
    if len(preview) > 160:
        preview = preview[:157] + "..."
    return {
        "harness": harness,
        "condition": spec.condition,
        "binary": bin_path or "NOT FOUND",
        "model": model or "default",
        "command_preview": preview,
    }


def execute_cell(harness: str, task: Task, bin_path: str, model: str,
                 work: Path, *, max_turns: int, timeout_s: int) -> dict:
    """One (task, harness) cell: agent phase + the shared hidden-test check."""
    spec = HARNESS_SPECS[harness]
    cmd = spec.build_command(bin_path, task.prompt, model, work, max_turns)
    started = time.time()
    code, out, err = -1, "", ""
    try:
        code, out, err = _invoke(cmd, work, timeout_s)
    except subprocess.TimeoutExpired:
        err = f"harness timed out after {timeout_s}s"
    except OSError as exc:
        err = f"harness failed to start: {exc}"
    seconds = round(time.time() - started, 1)
    parsed = spec.parse(out, err)
    # Churn before hidden tests are installed (agent output only), exactly
    # like bench/runner.py.
    churn = diff_stats(task.repo, work)
    ok, check_out = run_check(task, work)
    return {
        "task": task.id,
        "condition": spec.condition,
        "ok": bool(ok),
        "seconds": seconds,
        "tokens": int(parsed["tokens"]),
        "cached_tokens": int(parsed["cached_tokens"]),
        "net_tokens": int(parsed["net_tokens"]),
        "rounds": int(parsed["rounds"]),
        "stop_reason": "",
        "detail": parsed["detail"],
        "check_output": check_out[-2000:],
        "harness": harness,
        "model": model or parsed.get("model_detected") or "default",
        "command": " ".join(str(part) for part in cmd),
        "exit_code": code,
        "cost_usd": parsed.get("cost_usd"),
        "duration_reported_s": parsed.get("duration_reported_s", 0.0),
        **churn,
    }


def _persist_external(out: Path, rows: list[dict], cfg: Config,
                      conditions: list[str], started_at: str,
                      models: dict) -> None:
    """Same shape/atomicity as runner._persist, plus external metadata."""
    payload = _payload(rows, cfg, conditions, started_at)
    payload["kind"] = "external"
    payload["models"] = models
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(out)


def _print_plan(tasks: list[Task], harnesses: list[str], bins: dict,
                args) -> None:
    print(f"tasks ({len(tasks)}) under {args.tasks}:")
    for task in tasks:
        print(f"  {task.id:24s} {task.prompt[:70]}")
    print("\nharnesses (dry run - nothing is executed):")
    for harness in harnesses:
        spec = HARNESS_SPECS[harness]
        bin_path = bins[harness]
        status = bin_path if bin_path else "NOT FOUND"
        model = _model_for(args, harness)
        print(f"  {harness}  (condition {spec.condition})  binary: {status}")
        if not bin_path:
            print(f"    prerequisite: {spec.prerequisites}")
            continue
        plan = plan_cell(harness, tasks[0], bin_path, model, args.max_turns)
        print(f"    model: {plan['model']}")
        print(f"    would run: {plan['command_preview']}")
    print("\nprerequisites are NOT installed automatically; missing binaries "
          "are skipped.\nNo results are claimed by a dry run.")


def _model_for(args, harness: str) -> str:
    return (args.claude_model if harness == "claude" else args.codex_model).strip()


def _parse_harnesses(raw: str) -> list[str]:
    requested = [h.strip() for h in raw.split(",") if h.strip()]
    unknown = [h for h in requested if h not in HARNESS_SPECS]
    if unknown:
        raise SystemExit(f"unknown harness(es): {unknown} "
                         f"(known: {', '.join(HARNESSES)})")
    seen: list[str] = []
    for harness in requested:
        if harness not in seen:
            seen.append(harness)
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=str(Path(__file__).parent.parent / "tasks"))
    parser.add_argument("--out", default="")
    parser.add_argument("--harness", default=",".join(HARNESSES),
                        help="comma list from: " + ", ".join(HARNESSES))
    parser.add_argument("--claude-bin", default="",
                        help="path to the claude CLI; default PATH or HIVEMIND_CLAUDE_BIN")
    parser.add_argument("--codex-bin", default="",
                        help="path to the codex CLI; default PATH or HIVEMIND_CODEX_BIN")
    parser.add_argument("--claude-model", default="",
                        help="pass --model to claude; default = the CLI's own default")
    parser.add_argument("--codex-model", default="",
                        help="pass --model to codex; default = the CLI's own default")
    parser.add_argument("--max-turns", type=int, default=30,
                        help="claude --max-turns cap per task (default 30)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--cell-timeout", type=int, default=1800,
                        help="wall-clock cap per agent cell in s (default 1800)")
    parser.add_argument("--only", default="", help="comma-separated task ids")
    parser.add_argument("--list", action="store_true",
                        help="dry run: tasks, binaries and the commands that would run")
    args = parser.parse_args(argv)

    tasks = load_tasks(Path(args.tasks))
    if args.only:
        keep = {t.strip() for t in args.only.split(",") if t.strip()}
        tasks = [t for t in tasks if t.id in keep]
    harnesses = _parse_harnesses(args.harness)
    bins = {harness: resolve_bin(
        harness, args.claude_bin if harness == "claude" else args.codex_bin)
        for harness in harnesses}

    if args.list:
        if not tasks:
            print(f"no tasks under {args.tasks}", file=sys.stderr)
            return 1
        _print_plan(tasks, harnesses, bins, args)
        return 0
    if not tasks:
        print(f"no tasks under {args.tasks}", file=sys.stderr)
        return 1

    # Skip cleanly when a binary is absent; never half-run a harness.
    missing = [h for h in harnesses if not bins[h]]
    for harness in missing:
        spec = HARNESS_SPECS[harness]
        print(f"[skip] {spec.condition}: binary '{spec.bin_name}' not found "
              f"({spec.prerequisites})")
    active = [h for h in harnesses if bins[h]]
    if not active:
        print("no external harness available - nothing to run "
              "(install at least one; see bench/external/README.md)",
              file=sys.stderr)
        return 3

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path(__file__).parent.parent / "results"
               / "external" / f"run-{stamp}.json")
    models = {h: (_model_for(args, h) or "default") for h in active}
    cfg = Config(tasks_dir=Path(args.tasks), out=out, model="external",
                 runs=max(1, args.runs), cell_timeout_s=max(1, args.cell_timeout))
    conditions = [HARNESS_SPECS[h].condition for h in active]
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    rows: list[dict] = []
    work_root = Path(tempfile.mkdtemp(prefix="hm_bench_ext_"))
    try:
        for run_idx in range(cfg.runs):
            for task in tasks:
                for harness in active:
                    cell = work_root / f"run{run_idx + 1}" / harness / task.id
                    work = prepare_workspace(task, cell)
                    timeout = min(task.timeout_s, cfg.cell_timeout_s)
                    row = execute_cell(
                        harness, task, bins[harness], _model_for(args, harness),
                        work, max_turns=args.max_turns, timeout_s=timeout)
                    row["run"] = run_idx + 1
                    rows.append(row)
                    _persist_external(out, rows, cfg, conditions, started_at, models)
                    print(f"[run {run_idx + 1}/{cfg.runs}] {task.id:<20} "
                          f"{row['condition']:<16} "
                          f"{'PASS' if row['ok'] else 'FAIL':4s} "
                          f"{row['seconds']:>6.1f}s")
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    summary = aggregate(rows)
    print_summary(summary, build_relative(rows))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
