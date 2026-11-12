#!/usr/bin/env python3
"""Minimal local coding benchmark runner (D1, 2026-11-07).

Default conditions, same model and same llama-server:
  hivemind_direct  HiveMind /stream, mode=chat, direct tools tier=full
  hivemind_duo     HiveMind /stream, mode=code_duo, agentic
  aider            aider CLI against the same OpenAI-compatible endpoint

Metric: pass rate per condition (check command exit 0), median duration and
tokens. Aider is the same-model reference; token ratios against it are the
honest cost side of the comparison.

The no-tool conditions (`raw`, `raw_files`) stay available for diagnostics
but are opt-in. `raw` only sees the instruction and has to guess a diff, so a
0/3 row says nothing about HiveMind or Aider; do not report it as a result.

Since D1 (2026-11-09) the report also carries per-category deltas,
cached/net token ratios vs aider and tool rounds, both as summary fields and
in a compact `relative` block (bench/relative.py). External harnesses
(Claude Code/Codex, D8) live in bench/external/ and do not change any
condition here.

Usage:
    python bench/runner.py --list
    python bench/runner.py --model qwen3.5:4b-mtp --runs 3
    python bench/runner.py --conditions hivemind_duo,aider --runs 1
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import shutil
import sqlite3
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.relative import build_relative, print_relative  # noqa: E402

# raw        = one prompt, unified diff (diagnostic only, opt-in)
# raw_files  = one prompt, full corrected files (diagnostic only, opt-in:
#              small models often fail on diff syntax, not on the fix itself)
# The score-bearing comparison is HiveMind vs aider on the same server.
CONDITIONS = ("raw", "raw_files", "hivemind_direct", "hivemind_duo", "hivemind_duo_full", "hivemind_duo_lean", "aider", "hermes", "pi")
DEFAULT_CONDITIONS = ("hivemind_direct", "hivemind_duo", "aider")


@dataclass
class Task:
    id: str
    prompt: str
    check: str
    timeout_s: int = 600
    repo: Path | None = None
    tests: Path | None = None


def detect_gpu_info() -> dict:
    info = {"name": "Unknown", "vram_total": "", "driver": ""}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, check=True, timeout=5
        )
        lines = [line.strip() for line in r.stdout.strip().splitlines() if line.strip()]
        if lines:
            parts = [p.strip() for p in lines[0].split(",")]
            info["name"] = parts[0]
            if len(parts) > 1:
                info["vram_total"] = parts[1]
            if len(parts) > 2:
                info["driver"] = parts[2]
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        pass
    return info


@dataclass
class Config:
    tasks_dir: Path
    out: Path
    model: str
    llama_url: str = "http://127.0.0.1:8080"
    hivemind_url: str = "http://127.0.0.1:8001"
    conditions: list[str] = field(default_factory=lambda: list(DEFAULT_CONDITIONS))
    runs: int = 1
    run_offset: int = 0
    gpu_info: dict = field(default_factory=dict)
    # None = use each task's timeout_s; a CLI value overrides the corpus.
    task_timeout_s: int | None = None
    # Hard wall-clock cap per cell (agent phase). A heartbeating but wedged
    # until_finished run must not hold the whole run for hours.
    cell_timeout_s: int = 1800
    keep_work: bool = False
    # Optional path to aider (a separate venv is fine; not on PATH).
    aider_bin: str = ""
    # Optional path to hermes agent CLI.
    hermes_bin: str = ""
    # Optional path to pi coding agent CLI.
    pi_bin: str = ""
    # Coder-alone mode for the duo condition (planner off, same model).
    duo_planner: bool = True
    # Context window tokens (pinned per model/run, default 32768, 131072 for deepswe).
    ctx_tokens: int = 32768
    # Optional mirror directory (e.g. Google Drive) synced after every cell.
    sync_dir: Path | None = None
    # Resume an existing --out / --sync-dir checkpoint instead of rerunning done cells.
    resume: bool = False

def _file_map(root: Path | None) -> dict[str, str]:
    """Relative path -> text for a workspace (skip caches/binary-sized files)."""
    out: dict[str, str] = {}
    if root is None or not Path(root).is_dir():
        return out
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if path.name == "model.patch" or any(
                part.startswith(".aider") for part in rel.parts):
            # Aider writes .aider.chat.history.md / .aider.input.history and
            # .aider.tags.cache.*/ into the workspace; those are harness
            # artifacts, not code churn.
            continue
        if any(part in ("__pycache__", ".git", ".pytest_cache", ".ruff_cache",
                        ".mypy_cache", ".venv", "node_modules", "htmlcov")
               for part in rel.parts):
            continue
        if path.name in (".coverage",):
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            out[rel.as_posix()] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def diff_stats(repo: Path | None, work: Path) -> dict:
    """Code-churn metrics: changed files/lines, new and deleted files.

    Quality is more than a green test: a full-file rewrite that passes is not
    the same as a surgical edit. Recorded per cell so the next run can compare
    HiveMind and Aider on churn as well as pass rate.
    """
    before = _file_map(repo)
    after = _file_map(work)
    changed: list[str] = []
    new_files: list[str] = []
    deleted: list[str] = []
    added = removed = 0
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel, "")
        new = after.get(rel, "")
        if rel not in before:
            new_files.append(rel)
        if rel not in after:
            deleted.append(rel)
        if old == new:
            continue
        changed.append(rel)
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                         lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return {
        "files_changed": len(changed),
        "changed_files": changed[:100],
        "lines_added": added,
        "lines_removed": removed,
        "new_files": new_files[:100],
        "deleted_files": deleted[:100],
    }


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
    env = dict(os.environ)
    py_bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = f"{py_bin_dir}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{work}:{env.get('PYTHONPATH', '')}"
    try:
        r = subprocess.run(
            task.check, shell=True, cwd=str(work), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
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


def _agent_timeout_s(task: Task, cfg: Config) -> float:
    """Single source of truth for per-harness agent execution timeout."""
    base = float(cfg.task_timeout_s or task.timeout_s or 300)
    if cfg.cell_timeout_s:
        return min(base, float(cfg.cell_timeout_s))
    return base


async def _abort_hivemind(base_url: str, chat_id: str) -> None:
    """Best-effort abort so a timed-out HiveMind run stops using llama-server."""
    if not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            await client.post(
                base_url.rstrip("/") + "/abort",
                params={"chat_id": chat_id, "silent": "true"},
            )
    except (httpx.HTTPError, OSError):
        pass


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
    _agent_to = _agent_timeout_s(task, cfg)
    async with httpx.AsyncClient(timeout=httpx.Timeout(_agent_to, connect=10.0)) as client:
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
                   *, duo_planner: bool = True, duo_tier: str = "core",
                   ctx_tokens: int = 32768) -> dict:
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
        # TIER/VERIFICATION PINS (2026-11-08 audit): the install may carry
        # duo_coder_tool_tier=full and test-feedback toggles; pin the core
        # tier and disable the auto-test feedback loops so hidden tests never
        # enter the agent workspace.
        body["duo_coder_tool_tier"] = str(duo_tier or "core")
        body["duo_test_feedback"] = False
        body["duo_test_feedback_chunk"] = False
        body["duo_test_feedback_final"] = False
        # Context: default 32k, configurable via --num-ctx (e.g. 128k for deepswe)
        body["duo_coder_ctx_agentic"] = int(ctx_tokens)
        body["duo_coder_ctx_normal"] = int(ctx_tokens)
        # Agentic duo resolves coder/critic/planner from the agent cards; the
        # server patches both the card and the flat key for these overrides.
        body["duo_coder_model"] = model
        body["duo_critic_model"] = model
        body["duo_planner_model"] = model
    else:
        body["direct_tools_enabled"] = True
        body["direct_tools_tier"] = "full"
    return body


async def _run_hivemind(task: Task, work: Path, cfg: Config, duo: bool,
                        *, duo_tier: str = "core") -> dict:
    body = _hivemind_body(task, work, duo, cfg.model,
                          duo_planner=cfg.duo_planner, duo_tier=duo_tier,
                          ctx_tokens=cfg.ctx_tokens)
    done: dict = {}
    error = ""
    usage_prompt = usage_completion = usage_cached = usage_rounds = 0
    _agent_to = _agent_timeout_s(task, cfg)
    _chat_id = str(body.get("chat_id") or "")

    async def _stream_run() -> None:
        nonlocal done, error, usage_prompt, usage_completion, usage_cached, usage_rounds
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(_agent_to + 15.0, connect=10.0)) as client:
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
                        usage_cached += int(ev.get("cached_tokens") or 0)
                        usage_rounds += 1
                    elif ev.get("type") == "error":
                        error = str(ev.get("content") or ev)[:500]

    try:
        await asyncio.wait_for(_stream_run(), timeout=_agent_to)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        await _abort_hivemind(cfg.hivemind_url, _chat_id)
        done = {"stop_reason": "timeout"}
        error = f"hivemind timed out after {int(_agent_to)}s"

    _usage_total = usage_prompt + usage_completion
    tokens = _usage_total or (
        int(done.get("tokens_generated") or 0)
        + int(done.get("prompt_tokens_total") or 0))
    _uncached_prompt = max(0, usage_prompt - usage_cached)
    _net_tokens = (_uncached_prompt + usage_completion) if _usage_total else tokens
    return {"tokens": tokens, "cached_tokens": usage_cached,
            "net_tokens": _net_tokens, "rounds": usage_rounds,
            "stop_reason": done.get("stop_reason", ""),
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


def _active_llama_url(default_url: str) -> str:
    # MAX_SLOTS=3 (BASE_PORT 8101 -> 8103): the third slot must be probed too.
    for url in (default_url, "http://127.0.0.1:8101",
                "http://127.0.0.1:8102", "http://127.0.0.1:8103"):
        try:
            if httpx.get(url.rstrip("/") + "/health", timeout=1.5).status_code == 200:
                return url.rstrip("/")
        except httpx.HTTPError:
            continue
    return default_url.rstrip("/")


def _aider_remote_base() -> str:
    """HIVEMIND_AIDER_API_BASE (e.g. https://openrouter.ai/api/v1) or ''."""
    return (os.environ.get("HIVEMIND_AIDER_API_BASE") or "").strip().rstrip("/")


def _run_aider(task: Task, work: Path, cfg: Config) -> dict:
    aider_bin = _resolve_aider_bin(cfg)
    if not aider_bin:
        return {"tokens": 0,
                "detail": "aider is not installed (set --aider-bin / HIVEMIND_AIDER_BIN)"}
    # Optional remote OpenAI-compatible endpoint (e.g. a hosted SOTA model for a
    # separately reported reference run). Default = local llama-server, key
    # "sk-local". The key is read from the environment only, never logged.
    remote_base = _aider_remote_base()
    api_base = remote_base or (_active_llama_url(cfg.llama_url) + "/v1")
    api_key = (os.environ.get("HIVEMIND_AIDER_API_KEY") or "").strip() or "sk-local"
    # Aider has no --max-chat-tokens; unknown local models need an explicit
    # context window through its metadata file, and chat summarization is
    # controlled by --max-chat-history-tokens.
    _meta_dir = Path(tempfile.mkdtemp(prefix="hm_aider_meta_"))
    _meta_file = _meta_dir / "model.metadata.json"
    _meta_file.write_text(json.dumps({
        f"openai/{cfg.model}": {
            "max_input_tokens": int(cfg.ctx_tokens),
            "max_tokens": int(cfg.ctx_tokens),
            "max_output_tokens": 8192,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        }
    }, indent=2), encoding="utf-8")
    cmd = [
        aider_bin, "--yes-always", "--no-git", "--no-auto-commits",
        "--no-check-update",
        # Aider otherwise offers/opens its model-warning docs page in the
        # browser for every unknown local model — one tab per benchmark cell.
        "--no-show-model-warnings",
        "--edit-format", "diff",
        "--model", f"openai/{cfg.model}",
        "--openai-api-base", api_base,
        "--model-metadata-file", str(_meta_file),
        "--max-chat-history-tokens", str(cfg.ctx_tokens),
    ]
    cmd.extend(["--message", task.prompt, *_aider_files(work)])
    env = {**os.environ, "OPENAI_API_KEY": api_key,
           "OPENAI_API_BASE": api_base}
    try:
        r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_agent_timeout_s(task, cfg), env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"tokens": 0, "stop_reason": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "", "detail": f"aider failed: {exc}"}
    finally:
        shutil.rmtree(_meta_dir, ignore_errors=True)
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


def _resolve_hermes_bin(cfg: Config) -> str:
    """Explicit path/env wins; otherwise PATH. Empty = not available."""
    candidate = (cfg.hermes_bin or os.environ.get("HIVEMIND_HERMES_BIN") or "").strip()
    if candidate:
        return candidate if Path(candidate).is_file() else ""
    return shutil.which("hermes") or ""


def _read_hermes_state_usage(hermes_home: Path) -> dict:
    """Extract cumulative token & round metrics from Hermes SQLite state.db."""
    db_path = hermes_home / "state.db"
    if not db_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if not cols:
                return {}

            def _sum_col(name: str) -> str:
                return f"COALESCE(SUM({name}), 0)" if name in cols else "0"

            sql = (
                f"SELECT {_sum_col('input_tokens')}, "
                f"{_sum_col('output_tokens')}, "
                f"{_sum_col('cache_read_tokens')}, "
                f"{_sum_col('cache_write_tokens')}, "
                f"{_sum_col('api_call_count')} "
                "FROM sessions"
            )
            row = conn.execute(sql).fetchone()
            if not row:
                return {}
            inp, out, cache_read, cache_write, api_calls = (int(x or 0) for x in row)
            gross = inp + out + cache_read + cache_write
            net = inp + out + cache_write
            return {
                "tokens": gross,
                "cached_tokens": cache_read,
                "net_tokens": net,
                "rounds": api_calls,
            }
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {}


def _run_hermes(task: Task, work: Path, cfg: Config) -> dict:
    hermes_bin = _resolve_hermes_bin(cfg)
    if not hermes_bin:
        return {"tokens": 0,
                "detail": "hermes is not installed (set --hermes-bin / HIVEMIND_HERMES_BIN)"}
    remote_base = _aider_remote_base()
    api_base = remote_base or (_active_llama_url(cfg.llama_url) + "/v1")
    api_key = (os.environ.get("HIVEMIND_HERMES_API_KEY") or os.environ.get("HIVEMIND_AIDER_API_KEY") or "").strip() or "sk-local"
    # Hermes reads its endpoint from {HERMES_HOME}/config.yaml. The CLI has no
    # --base-url/--api-key flags, and --safe-mode would ignore this config.
    # Keep the home outside the task workspace so it never pollutes the patch.
    hermes_home = Path(tempfile.mkdtemp(prefix="hm_hermes_"))
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        f"  default: {cfg.model}\n"
        f"  base_url: {api_base}\n"
        "  api_mode: chat_completions\n"
        f"  api_key: {api_key}\n"
        f"  context_length: {cfg.ctx_tokens}\n",
        encoding="utf-8",
    )
    cmd = [
        hermes_bin, "chat",
        "-Q", "--yolo",
        "--ignore-rules",
        "--provider", "custom",
        "--model", cfg.model,
        "-q", task.prompt,
    ]
    env = {**os.environ, "HERMES_HOME": str(hermes_home),
           "OPENAI_API_KEY": api_key,
           "OPENAI_BASE_URL": api_base, "OPENAI_API_BASE": api_base}
    usage: dict = {}
    _agent_to = _agent_timeout_s(task, cfg)
    try:
        r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_agent_to, env=env)
        usage = _read_hermes_state_usage(hermes_home)
    except (OSError, subprocess.SubprocessError) as exc:
        usage = _read_hermes_state_usage(hermes_home)
        return {
            "tokens": int(usage.get("tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
            "net_tokens": int(usage.get("net_tokens") or 0),
            "rounds": int(usage.get("rounds") or 0),
            "stop_reason": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "",
            "detail": f"hermes failed: {exc}",
        }
    finally:
        shutil.rmtree(hermes_home, ignore_errors=True)
    detail = ((r.stdout or "") + (r.stderr or "")).strip()[-1000:]
    return {
        "tokens": int(usage.get("tokens") or 0),
        "cached_tokens": int(usage.get("cached_tokens") or 0),
        "net_tokens": int(usage.get("net_tokens") or 0),
        "rounds": int(usage.get("rounds") or 0),
        "detail": detail,
    }


def _resolve_pi_bin(cfg: Config) -> str:
    """Explicit path/env wins; otherwise PATH and standard Pi user bin dirs."""
    candidate = (cfg.pi_bin or os.environ.get("HIVEMIND_PI_BIN") or "").strip()
    if candidate:
        return candidate if Path(candidate).is_file() else ""
    which_pi = shutil.which("pi")
    if which_pi:
        return which_pi
    home = Path.home()
    for p in (
        home / ".pi" / "agent" / "bin" / "pi",
        home / ".local" / "node" / "bin" / "pi",
        home / ".local" / "bin" / "pi",
        Path("/usr/local/bin/pi"),
    ):
        if p.is_file():
            return str(p)
    return ""


def _parse_pi_json_output(stdout: str) -> dict:
    """Extract token usage, rounds, and text/error detail from Pi --mode json JSONL."""
    total_tokens = 0
    cached_tokens = 0
    net_tokens = 0
    rounds = 0
    last_texts: list[str] = []
    errors: list[str] = []
    parsed_any = False

    def _acc_usage(u: dict) -> None:
        nonlocal total_tokens, cached_tokens, net_tokens, rounds
        inp = int(u.get("input") or 0)
        out = int(u.get("output") or 0)
        c_read = int(u.get("cacheRead") or 0)
        c_write = int(u.get("cacheWrite") or 0)
        tot = int(u.get("totalTokens") or 0)
        gross_turn = tot if tot > 0 else (inp + out + c_read + c_write)
        net_turn = max(0, gross_turn - c_read)
        total_tokens += gross_turn
        cached_tokens += c_read
        net_tokens += net_turn
        if gross_turn > 0 or out > 0:
            rounds += 1

    def _collect_msg(msg: dict) -> None:
        usage = msg.get("usage")
        if isinstance(usage, dict):
            _acc_usage(usage)
        err = msg.get("errorMessage")
        if err:
            errors.append(str(err))
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    last_texts.append(str(block["text"]))
        elif isinstance(content, str) and content:
            last_texts.append(content)

    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        parsed_any = True
        etype = ev.get("type")
        if etype == "message_end":
            msg = ev.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                _collect_msg(msg)
        elif etype == "agent_end" and rounds == 0:
            for msg in (ev.get("messages") or []):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    _collect_msg(msg)

    detail_parts = []
    if errors:
        detail_parts.append("ERROR: " + "; ".join(errors))
    if last_texts:
        detail_parts.append("\n".join(last_texts[-2:]))
    return {
        "parsed": parsed_any,
        "tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "net_tokens": net_tokens,
        "rounds": rounds,
        "detail": "\n".join(detail_parts).strip()[-1000:],
    }


def _run_pi(task: Task, work: Path, cfg: Config) -> dict:
    pi_bin = _resolve_pi_bin(cfg)
    if not pi_bin:
        return {"tokens": 0,
                "detail": "pi is not installed (set --pi-bin / HIVEMIND_PI_BIN)"}
    remote_base = _aider_remote_base()
    api_base = remote_base or (_active_llama_url(cfg.llama_url) + "/v1")
    api_key = (os.environ.get("HIVEMIND_PI_API_KEY") or os.environ.get("HIVEMIND_AIDER_API_KEY") or "").strip() or "sk-local"

    # Configure custom local endpoint via Pi agent config dir. Keep it
    # outside the workspace: Pi writes sessions/config there and those files
    # must not pollute churn metrics or a later patch.
    pi_conf_dir = Path(tempfile.mkdtemp(prefix="hm_pi_"))
    models_spec = json.dumps({
        "providers": {
            "local": {
                "baseUrl": api_base,
                "api": "openai-completions",
                "apiKey": api_key,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [
                    {
                        "id": cfg.model,
                        "name": f"Local {cfg.model}",
                        "reasoning": False,
                        "input": ["text"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": cfg.ctx_tokens,
                        "maxTokens": 4096,
                    }
                ],
            }
        }
    }, indent=2)
    (pi_conf_dir / "models.json").write_text(models_spec, encoding="utf-8")
    (pi_conf_dir / "agent").mkdir(parents=True, exist_ok=True)
    (pi_conf_dir / "agent" / "models.json").write_text(models_spec, encoding="utf-8")

    base_cmd = [
        pi_bin,
        "--no-session",
        "--provider", "local",
        "--model", cfg.model,
    ]
    cmd = [*base_cmd, "--mode", "json", "-p", task.prompt]
    home = Path.home()
    extra_path = os.pathsep.join(
        str(p) for p in (
            home / ".local" / "node" / "bin",
            home / ".pi" / "agent" / "bin",
            home / ".local" / "bin",
        ) if p.is_dir()
    )
    cur_path = os.environ.get("PATH", "")
    env = {
        **os.environ,
        "PATH": f"{extra_path}{os.pathsep}{cur_path}" if extra_path else cur_path,
        "PI_CODING_AGENT_DIR": str(pi_conf_dir),
        "OPENAI_API_KEY": api_key,
        "OPENAI_BASE_URL": api_base,
    }
    _agent_to = _agent_timeout_s(task, cfg)
    try:
        r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_agent_to, env=env)
        _err = (r.stderr or "").lower()
        # Only a rejected --mode flag may trigger the no-JSON retry. A plain
        # "model" error (bad model id, missing model) used to match the old
        # substring check and reran the whole task without metrics.
        if r.returncode != 0 and "--mode" in _err and any(
                marker in _err for marker in
                ("unknown", "unrecognized", "no such option", "invalid option")):
            r = subprocess.run([*base_cmd, "-p", task.prompt],
                               cwd=str(work), capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=_agent_to, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raw_out = getattr(exc, "stdout", None) or ""
        if isinstance(raw_out, bytes):
            raw_out = raw_out.decode("utf-8", errors="replace")
        partial = _parse_pi_json_output(raw_out)
        return {
            "tokens": int(partial.get("tokens") or 0),
            "cached_tokens": int(partial.get("cached_tokens") or 0),
            "net_tokens": int(partial.get("net_tokens") or 0),
            "rounds": int(partial.get("rounds") or 0),
            "stop_reason": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "",
            "detail": f"pi failed: {exc}",
        }
    finally:
        shutil.rmtree(pi_conf_dir, ignore_errors=True)
    parsed = _parse_pi_json_output(r.stdout or "")
    raw_detail = ((r.stdout or "") + (r.stderr or "")).strip()[-1000:]
    detail = parsed["detail"] or raw_detail
    if r.returncode != 0 and (r.stderr or "").strip():
        detail = (detail + "\nstderr: " + (r.stderr or "").strip())[-1000:]
    return {
        "tokens": int(parsed.get("tokens") or 0),
        "cached_tokens": int(parsed.get("cached_tokens") or 0),
        "net_tokens": int(parsed.get("net_tokens") or 0),
        "rounds": int(parsed.get("rounds") or 0),
        "detail": detail,
    }


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
            extra = await _run_hivemind(task, work, cfg, duo=True, duo_tier="core")
        elif cond == "hivemind_duo_full":
            extra = await _run_hivemind(task, work, cfg, duo=True, duo_tier="full")
        elif cond == "hivemind_duo_lean":
            extra = await _run_hivemind(task, work, cfg, duo=True, duo_tier="lean")
        elif cond == "aider":
            extra = await asyncio.to_thread(_run_aider, task, work, cfg)
        elif cond == "hermes":
            extra = await asyncio.to_thread(_run_hermes, task, work, cfg)
        elif cond == "pi":
            extra = await asyncio.to_thread(_run_pi, task, work, cfg)
        else:
            raise ValueError(f"unknown condition: {cond}")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        extra = {"tokens": 0, "detail": f"{type(exc).__name__}: {exc}"[:800]}
    seconds = round(time.time() - started, 1)
    # CHURN before hidden tests are installed (agent output only).
    _churn = diff_stats(task.repo, work)
    ok, check_out = run_check(task, work, cfg.task_timeout_s)
    tok_total = int(extra.get("tokens") or 0)
    tok_sec = round(tok_total / seconds, 1) if seconds > 0 and tok_total > 0 else 0.0
    return {
        "task": task.id,
        "condition": cond,
        "model": cfg.model,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": bool(ok),
        "seconds": seconds,
        "tokens": tok_total,
        "cached_tokens": int(extra.get("cached_tokens") or 0),
        "net_tokens": int(extra.get("net_tokens") or tok_total),
        "tok_per_sec": tok_sec,
        "rounds": int(extra.get("rounds") or 0),
        "stop_reason": extra.get("stop_reason", ""),
        "detail": extra.get("detail", "") or check_out,
        "check_output": check_out[-2000:],
        **_churn,
    }


def aggregate(rows: list[dict]) -> dict:
    """Per-condition pass rate, median duration, tokens and token ratios.

    The `delta_vs_raw`/`delta_vs_raw_files` fields are filled only when the
    matching diagnostic baseline was explicitly run; they are None otherwise.
    D1 (2026-11-09): the cached-token share, the tool-round counts and - in the
    JSON's separate `relative` block - per-category deltas and aider token
    ratios. All fields are additive; existing keys keep their meaning.
    """
    summary: dict[str, dict] = {}
    conds: list[str] = []
    for row in rows:
        cond = str(row.get("condition") or "")
        # External-harness rows (bench/external, D8) use condition names
        # outside CONDITIONS; report them instead of dropping them silently.
        if cond and cond not in conds:
            conds.append(cond)
    for cond in conds:
        mine = [r for r in rows if r.get("condition") == cond]
        if not mine:
            continue
        duration = [r["seconds"] for r in mine]
        rounds = [int(r.get("rounds") or 0) for r in mine]
        tok_sec_list = [r["tok_per_sec"] for r in mine if r.get("tok_per_sec")]
        tokens_total = sum(int(r["tokens"] or 0) for r in mine)
        cached_total = sum(int(r.get("cached_tokens") or 0) for r in mine)
        summary[cond] = {
            "runs": len(mine),
            "pass_rate": round(sum(1 for r in mine if r["ok"]) / len(mine), 4),
            "median_seconds": round(statistics.median(duration), 1),
            "median_tok_per_sec": round(statistics.median(tok_sec_list), 1) if tok_sec_list else 0.0,
            "tokens_total": tokens_total,
            "cached_tokens_total": cached_total,
            "net_tokens_total": sum(int(r.get("net_tokens") or r.get("tokens") or 0) for r in mine),
            "cached_share": round(cached_total / tokens_total, 4) if tokens_total else None,
            "rounds_total": sum(rounds),
            "rounds_median": round(statistics.median(rounds), 1) if rounds else 0.0,
        }
    base = summary.get("raw", {}).get("pass_rate")
    base_files = summary.get("raw_files", {}).get("pass_rate")
    for cond, row in summary.items():
        row["delta_vs_raw"] = None if base is None else round(row["pass_rate"] - base, 4)
        row["delta_vs_raw_files"] = (None if base_files is None
                                     else round(row["pass_rate"] - base_files, 4))
    return summary


def print_summary(summary: dict, relative: dict | None = None) -> None:
    print()
    print(f"{'condition':<20} {'pass':>6} {'delta':>8} {'median s':>9} {'tok/s':>7} {'tokens':>9} "
          f"{'net tok':>9} {'rounds':>6}")
    for cond, row in summary.items():
        delta = row["delta_vs_raw"]
        delta_s = "-" if delta is None else f"{delta:+.2f}"
        tps = row.get("median_tok_per_sec", 0.0)
        print(f"{cond:<20} {row['pass_rate']:>6.2f} {delta_s:>8} "
              f"{row['median_seconds']:>9.1f} {tps:>7.1f} {row['tokens_total']:>9d} "
              f"{row['net_tokens_total']:>9d} {row['rounds_median']:>6.1f}")
    if relative:
        print_relative(relative)


def _payload(rows: list[dict], cfg: Config, conditions: list[str],
             started_at: str) -> dict:
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "started_at": started_at,
        "updated_at": now_iso,
        "model": cfg.model,
        "num_ctx": getattr(cfg, "ctx_tokens", 32768),
        "tasks_dir": str(cfg.tasks_dir.as_posix()),
        "runs": cfg.runs,
        "run_offset": getattr(cfg, "run_offset", 0),
        "gpu": getattr(cfg, "gpu_info", {}),
        "conditions": conditions,
        "summary": aggregate(rows),
        # D1: compact relative block (deltas vs raw/raw_files, category
        # deltas, aider token ratios, rounds). Additive - the older keys are
        # unchanged, so previous tooling keeps reading the file.
        "relative": build_relative(rows),
        "results": rows,
    }


def _sync_to_dir(src: Path, sync_dir: Path | None) -> None:
    """Best-effort mirror of checkpoint JSON to a persistent dir (e.g. Drive)."""
    if not sync_dir or not src.is_file():
        return
    try:
        sync_dir.mkdir(parents=True, exist_ok=True)
        dst = sync_dir / src.name
        if dst.resolve() == src.resolve():
            return
        tmp_dst = sync_dir / f".{src.name}.tmp"
        shutil.copy2(src, tmp_dst)
        try:
            tmp_dst.replace(dst)
        except OSError:
            shutil.copy2(src, dst)
            tmp_dst.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[sync WARN] could not mirror {src.name} to {sync_dir}: {exc}",
              file=sys.stderr)


def _persist(out: Path, rows: list[dict], cfg: Config,
             conditions: list[str], started_at: str) -> None:
    """Atomic write after every cell (+ optional mirror sync)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_payload(rows, cfg, conditions, started_at), indent=1),
                   encoding="utf-8")
    tmp.replace(out)
    _sync_to_dir(out, getattr(cfg, "sync_dir", None))


def _read_checkpoint_rows(path: Path) -> tuple[dict, list[dict]]:
    if not path.is_file():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data, list(data["results"])
    except (OSError, ValueError, TypeError):
        pass
    return {}, []


def _load_resume_state(
    out: Path,
    sync_dir: Path | None,
    model: str,
    ctx_tokens: int,
) -> tuple[list[dict], set[tuple[int, str, str]], str]:
    """Pull newer/larger checkpoint from sync_dir -> out and return done keys."""
    local_data, local_rows = _read_checkpoint_rows(out)
    if sync_dir:
        remote = sync_dir / out.name
        remote_data, remote_rows = _read_checkpoint_rows(remote)
        if remote_rows and len(remote_rows) > len(local_rows):
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(remote, out)
                local_data, local_rows = remote_data, remote_rows
                print(f"[sync] restored {len(local_rows)} row(s) from {remote} -> {out}")
            except OSError as exc:
                print(f"[sync WARN] could not restore {remote}: {exc}", file=sys.stderr)

    if not local_rows:
        return [], set(), ""
    saved_model = str(local_data.get("model") or "")
    saved_ctx = local_data.get("num_ctx")
    if saved_model and saved_model != model:
        return [], set(), ""
    if saved_ctx is not None and int(saved_ctx) != int(ctx_tokens):
        return [], set(), ""

    done_keys: set[tuple[int, str, str]] = set()
    kept_rows: list[dict] = []
    for r in local_rows:
        if not isinstance(r, dict):
            continue
        if r.get("model") and str(r["model"]) != model:
            continue
        kept_rows.append(r)
        run_no = int(r.get("run") or 1)
        task_id = str(r.get("task") or "")
        cond = str(r.get("condition") or "")
        if task_id and cond:
            done_keys.add((run_no, task_id, cond))
    started_at = str(local_data.get("started_at") or "")
    return kept_rows, done_keys, started_at


async def _preflight(cfg: Config, conditions: list[str]) -> str:
    """Fail fast when a required endpoint is dead (empty string = OK)."""
    # Missing CLIs must abort BEFORE the first cell: otherwise every task
    # writes a real FAIL row into the resume checkpoint instead of naming
    # the missing binary once.
    for _cond, _resolve, _label in (
            ("aider", _resolve_aider_bin, "aider"),
            ("hermes", _resolve_hermes_bin, "hermes"),
            ("pi", _resolve_pi_bin, "pi")):
        if _cond in conditions and not _resolve(cfg):
            return f"{_label} CLI not found (set --{_label}-bin / HIVEMIND_{_label.upper()}_BIN)"
    # Condition ORDER must not change the preflight: an external harness calls
    # llama-server directly even when a HiveMind condition is also present.
    need_llama = any(c in ("raw", "raw_files", "aider", "hermes", "pi")
                     for c in conditions)
    if need_llama and _aider_remote_base() and set(conditions) == {"aider"}:
        need_llama = False  # aider-only run against a remote endpoint
    need_hive = any(c.startswith("hivemind_") for c in conditions)
    async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0)) as client:
        if need_llama:
            try:
                r = await client.get(_active_llama_url(cfg.llama_url) + "/v1/models")
                r.raise_for_status()
            except httpx.HTTPError as exc:
                return f"llama-server at {cfg.llama_url}: {exc}"
        if need_hive:
            try:
                r = await client.get(cfg.hivemind_url.rstrip("/") + "/health")
                r.raise_for_status()
            except httpx.HTTPError as exc:
                return f"HiveMind at {cfg.hivemind_url}: {exc}"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=str(Path(__file__).parent / "tasks"))
    parser.add_argument("--out", default="")
    parser.add_argument("--model", default="qwen3.5:4b-mtp")
    parser.add_argument("--llama", default="http://127.0.0.1:8080")
    parser.add_argument("--hivemind", default="http://127.0.0.1:8001")
    parser.add_argument("--conditions", default=",".join(DEFAULT_CONDITIONS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--num-ctx", type=int, default=32768,
                        help="context window token size (default: 32768, deepswe: 131072)")
    parser.add_argument("--task-timeout", type=int, default=None,
                        help="override every task's timeout_s (default: per task)")
    parser.add_argument("--cell-timeout", type=int, default=1800,
                        help="hard wall-clock cap per agent cell (default 1800s)")
    parser.add_argument("--aider-bin", default="",
                        help="path to aider(.exe); default PATH or "
                             "HIVEMIND_AIDER_BIN")
    parser.add_argument("--hermes-bin", default="",
                        help="path to hermes(.exe); default PATH or "
                             "HIVEMIND_HERMES_BIN")
    parser.add_argument("--pi-bin", default="",
                        help="path to pi(.exe); default PATH or "
                             "HIVEMIND_PI_BIN")
    parser.add_argument("--no-duo-planner", action="store_true",
                        help="coder-alone mode: duo runs with planner off")
    parser.add_argument("--run-offset", type=int,
                        default=int(os.environ.get("HM_RUN_OFFSET", 0)),
                        help="offset added to the run index (e.g. 1 if this is run 2, default: 0)")
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="", help="comma-separated task ids to run")
    parser.add_argument("--sync-dir", default=os.environ.get("HM_SYNC_DIR", ""),
                        help="optional directory (e.g. Google Drive) to mirror results after every cell")
    parser.add_argument("--resume", action="store_true",
                        default=(os.environ.get("HM_RESUME", "0").strip() == "1"),
                        help="resume from existing --out / --sync-dir checkpoint, skipping done cells")
    args = parser.parse_args()

    tasks = load_tasks(Path(args.tasks))
    if args.only:
        _only_ids = {t.strip() for t in args.only.split(",") if t.strip()}
        tasks = [t for t in tasks if t.id in _only_ids]
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

    gpu_info = detect_gpu_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path(__file__).parent / "results" / f"run-{stamp}.json")
    sync_dir = Path(args.sync_dir) if str(args.sync_dir or "").strip() else None
    cfg = Config(
        tasks_dir=Path(args.tasks), out=out, model=args.model,
        llama_url=args.llama, hivemind_url=args.hivemind,
        conditions=conditions, runs=max(1, args.runs),
        run_offset=max(0, args.run_offset),
        gpu_info=gpu_info,
        task_timeout_s=args.task_timeout, keep_work=args.keep_work,
        cell_timeout_s=max(1, args.cell_timeout),
        aider_bin=args.aider_bin,
        hermes_bin=args.hermes_bin,
        pi_bin=args.pi_bin,
        duo_planner=not args.no_duo_planner,
        ctx_tokens=max(1024, args.num_ctx),
        sync_dir=sync_dir,
        resume=bool(args.resume),
    )

    rows: list[dict] = []
    done_keys: set[tuple[int, str, str]] = set()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if cfg.resume:
        rows, done_keys, saved_start = _load_resume_state(
            out, sync_dir, cfg.model, cfg.ctx_tokens)
        if saved_start:
            started_at = saved_start

    total_runs = cfg.runs + cfg.run_offset
    planned = [
        (run_idx + 1 + cfg.run_offset, task, cond)
        for run_idx in range(cfg.runs)
        for task in tasks
        for cond in conditions
    ]
    pending = [(r, t, c) for (r, t, c) in planned if (r, t.id, c) not in done_keys]

    gpu_s = f"{gpu_info.get('name', 'CPU')} ({gpu_info.get('vram_total', '')})"
    print(f"\n[bench] starting benchmark: {len(tasks)} tasks, {cfg.runs} run(s), run_offset={cfg.run_offset}")
    print(f"[bench] model: {cfg.model} (ctx {cfg.ctx_tokens}) | GPU: {gpu_s}")
    print(f"[bench] conditions: {', '.join(conditions)}")
    if done_keys:
        print(f"[bench] resume: {len(planned) - len(pending)}/{len(planned)} cells already completed, {len(pending)} remaining")
    if sync_dir:
        print(f"[bench] drive sync: {sync_dir / out.name}")
    print()

    if not pending:
        print("[bench] all planned cells already completed in checkpoint — skipping execution.")
        _persist(out, rows, cfg, conditions, started_at)
        summary = aggregate(rows)
        print_summary(summary, build_relative(rows))
        print(f"\nwrote {out}")
        return 0

    _pf = asyncio.run(_preflight(cfg, conditions))
    if _pf:
        print(f"preflight failed: {_pf}", file=sys.stderr)
        return 3

    work_root = (Path(__file__).parent / ".work" / stamp) if cfg.keep_work else None
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)
    tmp: str | None = None
    if work_root is not None:
        base = work_root
    else:
        tmp = tempfile.mkdtemp(prefix="hm_bench_")
        base = Path(tmp)
    try:
        for run_idx in range(cfg.runs):
            actual_run = run_idx + 1 + cfg.run_offset
            for task in tasks:
                for cond in conditions:
                    if (actual_run, task.id, cond) in done_keys:
                        print(f"[SKIP]   [run {actual_run}/{total_runs}] {task.id:<22} {cond:<16} (checkpoint)")
                        continue
                    # Unique workspace per run/condition: a reused task dir
                    # would accumulate the previous agent's edits.
                    cell = base / f"run{actual_run}" / cond
                    # The check command runs AFTER the agent timeout and needs
                    # real headroom (DeepSWE verifiers can run for minutes);
                    # a flat +60 s aborted long checks.
                    _check_budget = max(120.0, min(float(task.timeout_s or 300), 1800.0))
                    _cell_cap = max(float(cfg.cell_timeout_s),
                                    _agent_timeout_s(task, cfg) + _check_budget + 30.0)
                    try:
                        row = asyncio.run(asyncio.wait_for(
                            _one_run(cond, task, cfg, cell),
                            timeout=_cell_cap))
                    except asyncio.TimeoutError:
                        row = {
                            "task": task.id, "condition": cond, "model": cfg.model,
                            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "ok": False,
                            "seconds": round(_cell_cap, 1), "tokens": 0,
                            "cached_tokens": 0, "net_tokens": 0, "tok_per_sec": 0.0,
                            "rounds": 0,
                            "stop_reason": "cell_timeout",
                            "detail": f"cell timeout after {int(_cell_cap)}s",
                            "check_output": "",
                            **diff_stats(task.repo, cell / task.id),
                        }
                    row["run"] = actual_run
                    rows.append(row)
                    done_keys.add((actual_run, task.id, cond))
                    _persist(out, rows, cfg, conditions, started_at)
                    now_str = datetime.now().strftime("%H:%M:%S")
                    tps = row.get("tok_per_sec", 0.0)
                    tps_str = f"({tps:>5.1f} tok/s)" if tps > 0 else " " * 13
                    print(f"[{now_str}] [run {actual_run}/{total_runs}] {task.id:<22} {cond:<16} "
                          f"{'PASS' if row['ok'] else 'FAIL':4s} {row['seconds']:>6.1f}s {tps_str}")
    finally:
        if tmp is not None:
            # Windows: aider/servers can still hold a handle for a moment;
            # cleanup must never kill the run after the results are written.
            shutil.rmtree(tmp, ignore_errors=True)

    summary = aggregate(rows)
    print_summary(summary, build_relative(rows))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
