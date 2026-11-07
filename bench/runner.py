#!/usr/bin/env python3
"""Minimal local coding benchmark runner (D1, 2026-11-07).

Four conditions, same model and same llama-server:
  raw              one prompt, no tools; the model returns a unified diff
  hivemind_direct  HiveMind /stream, mode=chat, direct tools tier=full
  hivemind_duo     HiveMind /stream, mode=code_duo, agentic
  aider            aider CLI against the same OpenAI-compatible endpoint

Metric: pass rate per condition (check command exit 0), median duration and
tokens. The headline number is the DELTA vs the raw baseline, not an absolute
score - RExBench/WebGen-Bench show low absolute numbers for every agent.

Usage:
    python bench/runner.py --list
    python bench/runner.py --model qwen3.5:4b-mtp --runs 3
    python bench/runner.py --conditions raw,hivemind_direct --runs 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONDITIONS = ("raw", "hivemind_direct", "hivemind_duo", "aider")


@dataclass
class Task:
    id: str
    prompt: str
    check: str
    timeout_s: int = 600
    repo: Path | None = None
    tests: Path | None = None


@dataclass
class Config:
    tasks_dir: Path
    out: Path
    model: str
    llama_url: str = "http://127.0.0.1:8080"
    hivemind_url: str = "http://127.0.0.1:8001"
    conditions: list[str] = field(default_factory=lambda: list(CONDITIONS))
    runs: int = 1
    task_timeout_s: int = 600
    keep_work: bool = False


def load_tasks(tasks_dir: Path) -> list[Task]:
    """Read every <task>/task.json; repo/ is the starting workspace."""
    tasks: list[Task] = []
    for spec in sorted(Path(tasks_dir).glob("*/task.json")):
        try:
            data = json.loads(spec.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid task {spec}: {exc}") from exc
        repo = spec.parent / "repo"
        tests = spec.parent / "tests"
        tasks.append(Task(
            id=str(data.get("id") or spec.parent.name),
            prompt=str(data.get("prompt") or ""),
            check=str(data.get("check") or ""),
            timeout_s=int(data.get("timeout_s") or 600),
            repo=repo if repo.is_dir() else None,
            tests=tests if tests.is_dir() else None,
        ))
    return tasks


def prepare_workspace(task: Task, base: Path) -> Path:
    """Fresh copy of the task fixture; the source repo is never modified."""
    work = Path(base) / task.id
    if task.repo is not None:
        shutil.copytree(task.repo, work)
    else:
        work.mkdir(parents=True)
    return work


def run_check(task: Task, work: Path, timeout_s: int | None = None) -> tuple[bool, str]:
    """Run the task's check command; exit 0 = pass.

    Hidden tests (task/tests/) are copied in only now, after the agent had
    its turn. The agent never sees them and cannot edit them into passing.
    """
    if task.tests is not None:
        shutil.copytree(task.tests, work, dirs_exist_ok=True)
    if not task.check.strip():
        return False, "task.json has no check command"
    timeout = timeout_s or task.timeout_s
    try:
        r = subprocess.run(
            task.check, shell=True, cwd=str(work), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"check timed out after {timeout}s"
    except OSError as exc:
        return False, f"check failed to start: {exc}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out[-2000:]


def _extract_patch(text: str) -> str:
    match = re.search(r"```(?:diff|patch)?\s*(.*?)```", text or "", re.S)
    return (match.group(1) if match else (text or "")).strip()


def apply_patch(patch_text: str, work: Path) -> tuple[bool, str]:
    """Apply a model-produced unified diff with git apply."""
    patch = _extract_patch(patch_text)
    if not patch:
        return False, "no patch in model output"
    patch_file = work / "model.patch"
    patch_file.write_text(patch + "\n", encoding="utf-8")
    try:
        r = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            cwd=str(work), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git apply failed: {exc}"
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[-1000:]


# ── conditions ─────────────────────────────────────────────────────────────
async def _run_raw(task: Task, work: Path, cfg: Config) -> dict:
    prompt = (task.prompt + "\n\nReturn ONLY a unified diff (git apply format). "
              "Do not explain.")
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": "You are a precise coding assistant."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 4096,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
        r = await client.post(cfg.llama_url.rstrip("/") + "/v1/chat/completions",
                              json=payload)
        r.raise_for_status()
        data = r.json()
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    text = str(msg.get("content") or "")
    applied, detail = apply_patch(text, work)
    usage = data.get("usage") or {}
    return {"patch_applied": applied, "detail": detail,
            "tokens": int(usage.get("total_tokens") or 0)}


async def _run_hivemind(task: Task, work: Path, cfg: Config, duo: bool) -> dict:
    body = {
        "q": task.prompt,
        "workspace": str(work),
        "chat_id": uuid.uuid4().hex,
        "mode": "code_duo" if duo else "chat",
    }
    if duo:
        body["duo_agentic_mode"] = True
    else:
        body["direct_tools_enabled"] = True
        body["direct_tools_tier"] = "full"
    done: dict = {}
    error = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0)) as client:
        async with client.stream("POST",
                                 cfg.hivemind_url.rstrip("/") + "/stream",
                                 json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except ValueError:
                    continue
                if ev.get("type") == "done":
                    done = ev
                elif ev.get("type") == "error":
                    error = str(ev.get("content") or ev)[:500]
    tokens = int(done.get("tokens_generated") or 0) + int(done.get("prompt_tokens_total") or 0)
    return {"tokens": tokens, "stop_reason": done.get("stop_reason", ""),
            "detail": error}


def _run_aider(task: Task, work: Path, cfg: Config) -> dict:
    if shutil.which("aider") is None:
        return {"tokens": 0, "detail": "aider is not installed"}
    cmd = [
        "aider", "--yes", "--no-git", "--no-auto-commits", "--no-check-update",
        "--model", f"openai/{cfg.model}",
        "--openai-api-base", cfg.llama_url.rstrip("/") + "/v1",
        "--openai-api-key", "sk-local",
        "--message", task.prompt,
    ]
    env = {**os.environ, "OPENAI_API_KEY": "sk-local",
           "OPENAI_API_BASE": cfg.llama_url.rstrip("/") + "/v1"}
    try:
        r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=task.timeout_s, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"tokens": 0, "detail": f"aider failed: {exc}"}
    detail = ((r.stdout or "") + (r.stderr or "")).strip()[-1000:]
    match = re.search(r"Tokens:\s*([\d.,]+)\s*sent.*?([\d.,]+)\s*received",
                      detail, re.S | re.I)
    tokens = 0
    if match:
        tokens = int(match.group(1).replace(".", "").replace(",", "")) \
            + int(match.group(2).replace(".", "").replace(",", ""))
    return {"tokens": tokens, "detail": detail}


async def _one_run(cond: str, task: Task, cfg: Config, base: Path) -> dict:
    work = prepare_workspace(task, base)
    started = time.time()
    extra: dict = {}
    try:
        if cond == "raw":
            extra = await _run_raw(task, work, cfg)
        elif cond == "hivemind_direct":
            extra = await _run_hivemind(task, work, cfg, duo=False)
        elif cond == "hivemind_duo":
            extra = await _run_hivemind(task, work, cfg, duo=True)
        elif cond == "aider":
            extra = await asyncio.to_thread(_run_aider, task, work, cfg)
        else:
            raise ValueError(f"unknown condition: {cond}")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        extra = {"tokens": 0, "detail": f"{type(exc).__name__}: {exc}"[:800]}
    seconds = round(time.time() - started, 1)
    ok, check_out = run_check(task, work, cfg.task_timeout_s)
    return {
        "task": task.id,
        "condition": cond,
        "ok": bool(ok),
        "seconds": seconds,
        "tokens": int(extra.get("tokens") or 0),
        "stop_reason": extra.get("stop_reason", ""),
        "detail": extra.get("detail", "") or check_out,
    }


def aggregate(rows: list[dict]) -> dict:
    """Per-condition pass rate, median duration, tokens, delta vs raw."""
    summary: dict[str, dict] = {}
    for cond in CONDITIONS:
        mine = [r for r in rows if r["condition"] == cond]
        if not mine:
            continue
        duration = [r["seconds"] for r in mine]
        summary[cond] = {
            "runs": len(mine),
            "pass_rate": round(sum(1 for r in mine if r["ok"]) / len(mine), 4),
            "median_seconds": round(statistics.median(duration), 1),
            "tokens_total": sum(int(r["tokens"] or 0) for r in mine),
        }
    base = summary.get("raw", {}).get("pass_rate")
    for cond, row in summary.items():
        row["delta_vs_raw"] = None if base is None else round(row["pass_rate"] - base, 4)
    return summary


def print_summary(summary: dict) -> None:
    print()
    print(f"{'condition':<20} {'pass':>6} {'delta':>8} {'median s':>9} {'tokens':>9}")
    for cond, row in summary.items():
        delta = row["delta_vs_raw"]
        delta_s = "-" if delta is None else f"{delta:+.2f}"
        print(f"{cond:<20} {row['pass_rate']:>6.2f} {delta_s:>8} "
              f"{row['median_seconds']:>9.1f} {row['tokens_total']:>9d}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=str(Path(__file__).parent / "tasks"))
    parser.add_argument("--out", default="")
    parser.add_argument("--model", default="qwen3.5:4b-mtp")
    parser.add_argument("--llama", default="http://127.0.0.1:8080")
    parser.add_argument("--hivemind", default="http://127.0.0.1:8001")
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--task-timeout", type=int, default=600)
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    tasks = load_tasks(Path(args.tasks))
    if args.list:
        for task in tasks:
            print(f"{task.id:24s} {task.prompt[:70]}")
        return 0 if tasks else 1
    if not tasks:
        print(f"no tasks under {args.tasks}", file=sys.stderr)
        return 1
    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        print(f"unknown condition(s): {unknown}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path(__file__).parent / "results" / f"run-{stamp}.json")
    cfg = Config(
        tasks_dir=Path(args.tasks), out=out, model=args.model,
        llama_url=args.llama, hivemind_url=args.hivemind,
        conditions=conditions, runs=max(1, args.runs),
        task_timeout_s=args.task_timeout, keep_work=args.keep_work,
    )

    rows: list[dict] = []
    work_root = (Path(__file__).parent / ".work" / stamp) if cfg.keep_work else None
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)
    tmp = None if work_root is not None else tempfile.TemporaryDirectory(prefix="hm_bench_")
    base = work_root if work_root is not None else Path(tmp.name)
    try:
        for run_idx in range(cfg.runs):
            for task in tasks:
                for cond in conditions:
                    # Unique workspace per run/condition: a reused task dir
                    # would accumulate the previous agent's edits.
                    cell = base / f"run{run_idx + 1}" / cond
                    row = asyncio.run(_one_run(cond, task, cfg, cell))
                    row["run"] = run_idx + 1
                    rows.append(row)
                    print(f"[run {run_idx + 1}/{cfg.runs}] {task.id:<20} {cond:<16} "
                          f"{'PASS' if row['ok'] else 'FAIL':4s} {row['seconds']:>6.1f}s")
    finally:
        if tmp is not None:
            tmp.cleanup()

    summary = aggregate(rows)
    payload = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": cfg.model,
        "runs": cfg.runs,
        "conditions": conditions,
        "summary": summary,
        "results": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print_summary(summary)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
