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
# raw        = one prompt, unified diff (the strict baseline)
# raw_files  = one prompt, full corrected files (optional, separately reported:
#              small models often fail on diff syntax, not on the fix itself)
CONDITIONS = ("raw", "raw_files", "hivemind_direct", "hivemind_duo", "aider")
DEFAULT_CONDITIONS = ("raw", "hivemind_direct", "hivemind_duo", "aider")


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
    conditions: list[str] = field(default_factory=lambda: list(DEFAULT_CONDITIONS))
    runs: int = 1
    # None = use each task's timeout_s; a CLI value overrides the corpus.
    task_timeout_s: int | None = None
    keep_work: bool = False
    # Optional path to aider (a separate venv is fine; not on PATH).
    aider_bin: str = ""
    # Coder-alone mode for the duo condition (planner off, same model).
    duo_planner: bool = True


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


def install_hidden_tests(task: Task, work: Path) -> None:
    """Copy tests/ over the workspace, replacing files AND directories.

    copytree(dirs_exist_ok=True) silently copies a file INTO an existing
    directory of the same name (Windows: dst/test.py/test.py), so an agent
    could shadow a hidden test by creating that directory. Replace the
    destination explicitly before every file.
    """
    if task.tests is None:
        return
    for src in task.tests.rglob("*"):
        dst = work / src.relative_to(task.tests)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if dst.is_dir():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def run_check(task: Task, work: Path, timeout_s: int | None = None) -> tuple[bool, str]:
    """Run the task's check command; exit 0 = pass.

    Hidden tests (task/tests/) are copied in only now, after the agent had
    its turn. The agent never sees them and cannot edit them into passing.
    """
    if task.tests is not None:
        try:
            install_hidden_tests(task, work)
        except OSError as exc:
            return False, f"hidden test install failed: {exc}"
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


_FILE_BLOCK_RE = re.compile(
    r"###\s*FILE:\s*(?P<path>[^\n]+)\n```[^\n]*\n(?P<content>.*?)\n```", re.S)
_DIFF_PROMPT = ("Return ONLY a unified diff (git apply format). Do not explain.")
_FILES_PROMPT = (
    "Return the complete corrected content of every file you change. Use "
    "exactly this format for each file, no diff and no explanation:\n\n"
    "### FILE: relative/path.py\n```python\n<complete file content>\n```")


def _parse_file_blocks(text: str) -> dict[str, str]:
    """Parse '### FILE: path' + fenced block pairs from model output."""
    out: dict[str, str] = {}
    for match in _FILE_BLOCK_RE.finditer(text or ""):
        name = match.group("path").strip().strip("`").strip()
        if name:
            out[name] = match.group("content")
    return out


def _safe_rel_path(name: str) -> Path | None:
    """Workspace-relative path or None (absolute / .. / drive paths)."""
    clean = str(name or "").strip().replace("\\", "/")
    if not clean or clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
        return None
    path = Path(clean)
    if path.is_absolute():
        return None
    if any(part == ".." for part in path.parts):
        return None
    return path


def apply_full_files(text: str, work: Path) -> tuple[bool, str]:
    """Write the model's full-file blocks; reject paths outside the workspace."""
    blocks = _parse_file_blocks(text)
    if not blocks:
        return False, "no '### FILE:' block in model output"
    written: list[str] = []
    for name, content in blocks.items():
        rel = _safe_rel_path(name)
        if rel is None:
            return False, f"unsafe path in model output: {name!r}"
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not content.endswith("\n"):
            content += "\n"
        dst.write_text(content, encoding="utf-8")
        written.append(rel.as_posix())
    return True, "wrote " + ", ".join(written)


# ── conditions ─────────────────────────────────────────────────────────────
async def _run_raw(task: Task, work: Path, cfg: Config, *,
                   full_files: bool = False) -> dict:
    prompt = task.prompt + "\n\n" + (_FILES_PROMPT if full_files else _DIFF_PROMPT)
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
    if full_files:
        applied, detail = apply_full_files(text, work)
    else:
        applied, detail = apply_patch(text, work)
    usage = data.get("usage") or {}
    return {"patch_applied": applied, "detail": detail,
            "tokens": int(usage.get("total_tokens") or 0)}


def _hivemind_body(task: Task, work: Path, duo: bool, model: str,
                   *, duo_planner: bool = True) -> dict:
    """Request body for the two HiveMind conditions.

    MODE-FIX (deep audit 2026-11-07): "chat" is NOT a mode the engine knows
    (recognized: auto|automap|simple|pipeline|agent|code_duo); it fell into
    the judge else-branch and could route a "direct" run into pipeline/duo,
    silently invalidating the D1 baseline. "simple" is the real direct route.

    MODEL-FAIRNESS: HiveMind normally reads agents.<role>.model from settings;
    without these overrides the conditions would silently run different models
    than `raw`/aider. The server accepts direct/duo model keys per request.
    """
    body = {
        "q": task.prompt,
        "workspace": str(work),
        "chat_id": uuid.uuid4().hex,
        "mode": "code_duo" if duo else "simple",
        "direct_model": model,
        # SETTINGS-FAIRNESS: pin the agentic config to the shipped defaults,
        # otherwise an install with until_finished=False/planner off measures
        # a different product than a fresh install (2026-11-08 run showed a
        # duo coder that answered once without reading a file).
        "until_finished": True,
    }
    if duo:
        body["duo_agentic_mode"] = True
        # The engine reads the BODY key "duo_planner" (the settings key is
        # duo_planner_enabled; _body_or_settings maps them).
        body["duo_planner"] = bool(duo_planner)
        body["duo_chunking"] = False
        # TOOL-ROUNDS (2026-11-08 discovery): an install with
        # duo_tool_rounds=0 runs the coder WITHOUT tools. Pin the shipped
        # default (50); otherwise "duo" measures a text-only coder.
        body["duo_tool_rounds"] = 50
        # Agentic duo resolves coder/critic/planner from the agent cards; the
        # server patches both the card and the flat key for these overrides.
        body["duo_coder_model"] = model
        body["duo_critic_model"] = model
        body["duo_planner_model"] = model
    else:
        body["direct_tools_enabled"] = True
        body["direct_tools_tier"] = "full"
    return body


async def _run_hivemind(task: Task, work: Path, cfg: Config, duo: bool) -> dict:
    body = _hivemind_body(task, work, duo, cfg.model,
                          duo_planner=cfg.duo_planner)
    done: dict = {}
    error = ""
    usage_prompt = usage_completion = 0
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
                elif ev.get("type") == "usage_meta":
                    # TOKEN-METRIC (2026-11-08): tool loops report their usage
                    # per phase; the done event only carries the outer ledger.
                    usage_prompt += int(ev.get("prompt_tokens") or 0)
                    usage_completion += int(ev.get("completion_tokens") or 0)
                elif ev.get("type") == "error":
                    error = str(ev.get("content") or ev)[:500]
    _usage_total = usage_prompt + usage_completion
    tokens = _usage_total or (
        int(done.get("tokens_generated") or 0)
        + int(done.get("prompt_tokens_total") or 0))
    return {"tokens": tokens, "stop_reason": done.get("stop_reason", ""),
            "detail": error}


def _parse_token_count(number: str, suffix: str = "") -> int:
    """'1.2' + 'k' -> 1200; '1,234' -> 1234; junk -> 0."""
    try:
        value = float(str(number).replace(",", ""))
    except ValueError:
        return 0
    return int(value * {"k": 1000, "m": 1_000_000}.get((suffix or "").lower(), 1))


def _aider_files(work: Path, limit: int = 40) -> list[str]:
    """Workspace files to put in aider's chat (relative, sorted, bounded)."""
    files: list[str] = []
    for path in sorted(work.rglob("*")):
        if len(files) >= limit:
            break
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.name.startswith("."):
            continue
        files.append(path.relative_to(work).as_posix())
    return files


def _resolve_aider_bin(cfg: Config) -> str:
    """Explicit path/env wins; otherwise PATH. Empty = not available."""
    candidate = (cfg.aider_bin or os.environ.get("HIVEMIND_AIDER_BIN") or "").strip()
    if candidate:
        return candidate if Path(candidate).is_file() else ""
    return shutil.which("aider") or ""


def _run_aider(task: Task, work: Path, cfg: Config) -> dict:
    aider_bin = _resolve_aider_bin(cfg)
    if not aider_bin:
        return {"tokens": 0,
                "detail": "aider is not installed (set --aider-bin / HIVEMIND_AIDER_BIN)"}
    cmd = [
        aider_bin, "--yes-always", "--no-git", "--no-auto-commits",
        "--no-check-update",
        "--model", f"openai/{cfg.model}",
        "--openai-api-base", cfg.llama_url.rstrip("/") + "/v1",
        "--openai-api-key", "sk-local",
        "--message", task.prompt,
        *_aider_files(work),
    ]
    env = {**os.environ, "OPENAI_API_KEY": "sk-local",
           "OPENAI_API_BASE": cfg.llama_url.rstrip("/") + "/v1"}
    try:
        r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=cfg.task_timeout_s or task.timeout_s, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"tokens": 0, "detail": f"aider failed: {exc}"}
    detail = ((r.stdout or "") + (r.stderr or "")).strip()[-1000:]
    # "Tokens: 1.2k sent, 345 received" (k/m suffixes are common).
    match = re.search(
        r"Tokens:\s*([\d.,]+)\s*([kKmM]?)\s*sent.*?"
        r"([\d.,]+)\s*([kKmM]?)\s*received",
        detail, re.S | re.I)
    tokens = 0
    if match:
        tokens = (_parse_token_count(match.group(1), match.group(2))
                  + _parse_token_count(match.group(3), match.group(4)))
    return {"tokens": tokens, "detail": detail}


async def _one_run(cond: str, task: Task, cfg: Config, base: Path) -> dict:
    work = prepare_workspace(task, base)
    started = time.time()
    extra: dict = {}
    try:
        if cond == "raw":
            extra = await _run_raw(task, work, cfg)
        elif cond == "raw_files":
            extra = await _run_raw(task, work, cfg, full_files=True)
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


def _payload(rows: list[dict], cfg: Config, conditions: list[str],
             started_at: str) -> dict:
    return {
        "started_at": started_at,
        "model": cfg.model,
        "runs": cfg.runs,
        "conditions": conditions,
        "summary": aggregate(rows),
        "results": rows,
    }


def _persist(out: Path, rows: list[dict], cfg: Config,
             conditions: list[str], started_at: str) -> None:
    """Atomic write after every cell: a crash keeps the completed rows."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_payload(rows, cfg, conditions, started_at), indent=1),
                   encoding="utf-8")
    tmp.replace(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=str(Path(__file__).parent / "tasks"))
    parser.add_argument("--out", default="")
    parser.add_argument("--model", default="qwen3.5:4b-mtp")
    parser.add_argument("--llama", default="http://127.0.0.1:8080")
    parser.add_argument("--hivemind", default="http://127.0.0.1:8001")
    parser.add_argument("--conditions", default=",".join(DEFAULT_CONDITIONS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--task-timeout", type=int, default=None,
                        help="override every task's timeout_s (default: per task)")
    parser.add_argument("--aider-bin", default="",
                        help="path to aider(.exe); default PATH or "
                             "HIVEMIND_AIDER_BIN")
    parser.add_argument("--no-duo-planner", action="store_true",
                        help="coder-alone mode: duo runs with planner off")
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
        aider_bin=args.aider_bin,
        duo_planner=not args.no_duo_planner,
    )

    rows: list[dict] = []
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    work_root = (Path(__file__).parent / ".work" / stamp) if cfg.keep_work else None
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)
    tmp = None if work_root is not None else tempfile.mkdtemp(prefix="hm_bench_")
    base = work_root if work_root is not None else Path(tmp)
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
                    _persist(out, rows, cfg, conditions, started_at)
                    print(f"[run {run_idx + 1}/{cfg.runs}] {task.id:<20} {cond:<16} "
                          f"{'PASS' if row['ok'] else 'FAIL':4s} {row['seconds']:>6.1f}s")
    finally:
        if tmp is not None:
            # Windows: aider/servers can still hold a handle for a moment;
            # cleanup must never kill the run after the results are written.
            shutil.rmtree(tmp, ignore_errors=True)

    summary = aggregate(rows)
    print_summary(summary)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
